import importlib.util
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import db, registry, state, tokens


def local_day(ts_utc: str) -> str:
    return (datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
            .astimezone().strftime("%Y-%m-%d"))


def cum(i=0, o=0, cr=0, cw=0, th=0):
    return {"in": i, "out": o, "cr": cr, "cw": cw, "th": th}


class FoldTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
            " VALUES (1,'acme-mini','x',datetime('now'),datetime('now'))")

    def tearDown(self):
        self.conn.close()

    def event(self, eid, tokens_cum, *, event="turn_done", agent="claude-code",
              session="s1", ts="2026-09-02T10:00:00Z", frontend="cli", model="model-a"):
        detail = {"summary": "done", "frontend": frontend, "model": model, "collection_mode": "hooks"}
        if tokens_cum is not None:
            detail["tokens"] = {"v": 1, "cum": tokens_cum}
        return {"agent": agent, "session_id": session, "event_id": eid, "event": event,
                "ts": ts, "project": "acme-web", "branch": "main", "detail": detail}

    def daily(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM token_daily ORDER BY model")]

    def test_first_fold_inserts_full_cumulative_as_delta(self):
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(100, 50, 1000, 200, 30)}))
        rows = self.daily()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["input"], r["output"], r["cache_read"], r["cache_write"], r["thinking"]),
                         (100, 50, 1000, 200, 30))
        self.assertEqual((r["day"], r["project"], r["model"], r["frontend"], r["turns"], r["source"]),
                         (local_day("2026-09-02T10:00:00Z"), "acme-web", "model-a", "cli", 1, "events"))

    def test_second_fold_adds_only_delta(self):
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(100, 50)}))
        state.ingest(self.conn, 1, self.event("e2", {"model-a": cum(250, 80)}))
        r = self.daily()[0]
        self.assertEqual((r["input"], r["output"], r["turns"]), (250, 80, 2))
        stored = json.loads(self.conn.execute("SELECT tokens_cum FROM sessions").fetchone()["tokens_cum"])
        self.assertEqual(stored["model-a"]["in"], 250)

    def test_identical_cumulative_resend_adds_nothing(self):
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(100, 50)}))
        state.ingest(self.conn, 1, self.event("e2", {"model-a": cum(100, 50)}))
        r = self.daily()[0]
        self.assertEqual((r["input"], r["output"]), (100, 50))
        self.assertEqual(r["turns"], 2)  # 턴은 실제로 2번 끝났다

    def test_counter_reset_clamps_to_zero_and_moves_baseline(self):
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(100, 50)}))
        state.ingest(self.conn, 1, self.event("e2", {"model-a": cum(40, 10)}))
        r = self.daily()[0]
        self.assertEqual((r["input"], r["output"]), (100, 50))  # 역행분은 버림
        stored = json.loads(self.conn.execute("SELECT tokens_cum FROM sessions").fetchone()["tokens_cum"])
        self.assertEqual(stored["model-a"]["in"], 40)  # 기준점은 새 값으로
        state.ingest(self.conn, 1, self.event("e3", {"model-a": cum(60, 15)}))
        self.assertEqual(self.daily()[0]["input"], 120)  # 40→60 델타 20 가산

    def test_claude_per_model_split(self):
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(100, 50), "model-b": cum(10, 5)}))
        state.ingest(self.conn, 1, self.event("e2", {"model-a": cum(150, 70), "model-b": cum(10, 5)}))
        rows = self.daily()
        self.assertEqual([(r["model"], r["input"], r["output"]) for r in rows],
                         [("model-a", 150, 70), ("model-b", 10, 5)])
        self.assertEqual(sum(r["turns"] for r in rows), 2)  # 턴 수는 이벤트당 1회만

    def test_codex_session_baseline_survives_model_switch(self):
        state.ingest(self.conn, 1, self.event(
            "e1", {"gpt-a": cum(100, 50)}, agent="codex-cli", model="gpt-a"))
        # 모델 전환 — 세션 전역 카운터는 계속 증가할 뿐
        state.ingest(self.conn, 1, self.event(
            "e2", {"gpt-b": cum(180, 90)}, agent="codex-cli", model="gpt-b"))
        rows = self.daily()
        self.assertEqual([(r["model"], r["input"], r["output"]) for r in rows],
                         [("gpt-a", 100, 50), ("gpt-b", 80, 40)])  # 이중 계상 없음
        stored = json.loads(self.conn.execute("SELECT tokens_cum FROM sessions").fetchone()["tokens_cum"])
        self.assertEqual(set(stored), {"_session"})

    def test_session_end_catches_up_without_counting_turn(self):
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(100, 50)}))
        state.ingest(self.conn, 1, self.event(
            "e2", {"model-a": cum(130, 60)}, event="session_end", ts="2026-09-02T11:00:00Z"))
        r = self.daily()[0]
        self.assertEqual((r["input"], r["output"], r["turns"]), (130, 60, 1))

    def test_event_without_tokens_is_untouched(self):
        state.ingest(self.conn, 1, self.event("e1", None))
        self.assertEqual(self.daily(), [])
        self.assertIsNone(self.conn.execute("SELECT tokens_cum FROM sessions").fetchone()["tokens_cum"])

    def test_malformed_tokens_never_break_ingest(self):
        ev = self.event("e1", None)
        ev["detail"]["tokens"] = {"v": 1, "cum": {"model-a": "garbage", "": cum(1)}}
        self.assertEqual(state.ingest(self.conn, 1, ev), "inserted")
        self.assertEqual(self.daily(), [])

    def test_auto_frontend_is_tagged(self):
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(10, 5)}, frontend="auto"))
        self.assertEqual(self.daily()[0]["frontend"], "auto")

    def hourly(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM token_hourly ORDER BY hour, model")]

    def test_one_write_path_feeds_hourly_and_daily(self):
        """fold는 로컬 시 키 하나로 시간·일 원장에 같은 값을 쓴다 — 일 키는 시 키의 앞 10자."""
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(100, 50, 1000, 200, 30)}))
        (h,), (d,) = self.hourly(), self.daily()
        self.assertEqual(h["hour"], tokens.local_hour("2026-09-02T10:00:00Z"))
        self.assertEqual(d["day"], h["hour"][:10])
        cols = ("device_id", "agent", "project", "model", "frontend", "source",
                "input", "output", "cache_read", "cache_write", "thinking", "turns")
        self.assertEqual({k: h[k] for k in cols}, {k: d[k] for k in cols})

    def test_hourly_delta_lands_in_event_hour_after_baseline_warmup(self):
        """기준점은 이전 시간의 이벤트로 데워진다 — 둘째 시간에는 누적이 아니라 델타만 들어간다."""
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(100, 50)}, ts="2026-09-02T10:10:00Z"))
        state.ingest(self.conn, 1, self.event("e2", {"model-a": cum(250, 80)}, ts="2026-09-02T12:10:00Z"))
        self.assertEqual([(r["hour"], r["input"], r["output"]) for r in self.hourly()],
                         [(tokens.local_hour("2026-09-02T10:10:00Z"), 100, 50),
                          (tokens.local_hour("2026-09-02T12:10:00Z"), 150, 30)])

    def test_hourly_turn_counted_once_on_first_model_even_with_zero_delta(self):
        state.ingest(self.conn, 1, self.event("e1", {"model-a": cum(100, 50), "model-b": cum(10, 5)}))
        state.ingest(self.conn, 1, self.event(
            "e2", {"model-a": cum(100, 50), "model-b": cum(10, 5)}, ts="2026-09-02T12:00:00Z"))   # 델타 0 — 턴만
        rows = self.hourly()
        self.assertEqual(sum(r["turns"] for r in rows), 2)
        first, second = rows[:2], rows[2:]
        self.assertEqual(sorted(r["turns"] for r in first), [0, 1])   # 모델 둘이어도 이벤트당 1회
        self.assertEqual([(r["input"], r["turns"]) for r in second], [(0, 1)])

    def test_local_hour_parsing_rules(self):
        ok = tokens.local_hour("2026-09-02T10:20:30.123Z", "garbage")
        self.assertEqual(len(ok), 13)
        self.assertEqual(tokens.local_hour("not a time", "2026-09-02T10:20:30Z"), ok)   # ts_device 불량 → ts_hub
        self.assertEqual(len(tokens.local_hour("", "")), 13)   # 둘 다 못 쓰면 지금
        self.assertEqual(tokens.local_hour("2026-09-02T10:20:30"), ok)   # tz 없으면 UTC로 간주


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        for did, name in ((1, "acme-mini"), (2, "acme-laptop")):
            self.conn.execute(
                "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
                " VALUES (?,?,'x',datetime('now'),datetime('now'))", (did, name))
        rows = [
            # day(오늘 상대), device, agent, project, model, frontend, in, out, cr, cw, th, turns
            ("0", 1, "claude-code", "acme-web", "model-a", "cli", 100, 50, 1000, 200, 30, 3),
            ("0", 2, "claude-code", "acme-api", "model-a", "cli", 40, 20, 400, 80, 10, 1),
            ("-1", 1, "codex-cli", "acme-web", "gpt-a", "auto", 10, 5, 100, 0, 2, 1),
        ]
        for off, did, agent, proj, model, fe, i, o, cr, cw, th, t in rows:
            self.conn.execute(
                "INSERT INTO token_daily (day, device_id, agent, project, model, frontend, source,"
                " input, output, cache_read, cache_write, thinking, turns)"
                " VALUES (date('now','localtime',? || ' days'),?,?,?,?,?,'events',?,?,?,?,?,?)",
                (off, did, agent, proj, model, fe, i, o, cr, cw, th, t))
        self.reg = registry.Registry(
            project_map={"acme-web": {"service": "Acme", "strength": "strong"},
                         "acme-api": {"service": "Acme", "strength": "strong"}})

    def tearDown(self):
        self.conn.close()

    def test_totals_and_breakdowns(self):
        s = tokens.summary(self.conn, self.reg, days=30)
        self.assertEqual(s["total"]["input"], 150)
        self.assertEqual(s["total"]["turns"], 5)
        self.assertEqual(len(s["days"]), 2)
        self.assertEqual(s["by_service"][0], {"service": "Acme", "input": 150, "output": 75,
                                              "cache_read": 1500, "cache_write": 280, "thinking": 42, "turns": 5})
        self.assertEqual([r["device"] for r in s["by_device"]], ["acme-mini", "acme-laptop"])
        self.assertEqual([r["agent"] for r in s["by_agent"]], ["claude-code", "codex-cli"])

    def test_filters(self):
        self.assertEqual(tokens.summary(self.conn, self.reg, agent="codex-cli")["total"]["input"], 10)
        self.assertEqual(tokens.summary(self.conn, self.reg, device="acme-laptop")["total"]["input"], 40)
        self.assertEqual(tokens.summary(self.conn, self.reg, device="acme-laptop,acme-mini")["total"]["input"], 150)
        self.assertEqual(tokens.summary(self.conn, self.reg, human=True)["total"]["input"], 140)
        self.assertEqual(tokens.summary(self.conn, self.reg, service="Acme")["total"]["input"], 150)
        self.assertEqual(tokens.summary(self.conn, self.reg, model="gpt-a")["total"]["input"], 10)

    def test_series_by_agent_daily_ledger(self):
        s = tokens.summary(self.conn, self.reg, days=30)
        self.assertIsNone(s["hours"])
        sba = s["series_by_agent"]
        self.assertEqual(set(sba), {"claude-code", "codex-cli"})
        self.assertIsNone(sba["claude-code"]["hours"])
        # 데이터 있는 날만 — claude는 오늘 2행 합쳐 1일, codex는 어제 1일
        self.assertEqual([(r["input"], r["turns"]) for r in sba["claude-code"]["days"]], [(140, 4)])
        self.assertEqual([(r["input"], r["turns"]) for r in sba["codex-cli"]["days"]], [(10, 1)])
        self.assertNotEqual(sba["claude-code"]["days"][0]["day"], sba["codex-cli"]["days"][0]["day"])
        # 불변식: 에이전트별 합 == 전체 days
        merged: dict = {}
        for ser in sba.values():
            for r in ser["days"]:
                acc = merged.setdefault(r["day"], dict.fromkeys(r, 0))
                acc["day"] = r["day"]
                for k, v in r.items():
                    if k != "day":
                        acc[k] += v
        self.assertEqual([merged[d["day"]] for d in s["days"]], s["days"])

    def test_series_by_agent_follows_filters(self):
        sba = tokens.summary(self.conn, self.reg, days=30, agent="codex-cli")["series_by_agent"]
        self.assertEqual(set(sba), {"codex-cli"})
        sba = tokens.summary(self.conn, self.reg, days=30, human=True)["series_by_agent"]
        self.assertEqual(set(sba), {"claude-code"})   # codex 행은 auto라 빠지고 키도 없다

    def test_junk_project_names_are_bucketed(self):
        for proj in ("bdfb4439984f4cab80a1ee3b7af456d8", ""):
            self.conn.execute(
                "INSERT INTO token_daily (day, device_id, agent, project, model, frontend, source,"
                " input, output, cache_read, cache_write, thinking, turns)"
                " VALUES (date('now','localtime'),1,'claude-code',?,'model-a','auto','backfill',5,5,0,0,0,0)",
                (proj,))
        s = tokens.summary(self.conn, self.reg, days=30)
        projects = [r["project"] for r in s["by_project"]]
        self.assertIn("(임시)", projects)
        self.assertIn("", projects)   # 미상은 빈 값 그대로 — 표시는 클라이언트 몫
        self.assertNotIn("bdfb4439984f4cab80a1ee3b7af456d8", projects)
        self.assertIn("(임시)", [r["service"] for r in s["by_service"]])

    def test_by_session_from_tokens_cum(self):
        self.conn.execute(
            "INSERT INTO sessions (device_id, agent, session_id, project, model, frontend, tokens_cum,"
            " started_at, last_seen_hub) VALUES (1,'claude-code','s1','acme-web','model-a','cli',?,"
            " datetime('now'), datetime('now'))",
            (json.dumps({"model-a": cum(100, 50, 1000, 200, 30), "model-b": cum(10, 5)}),))
        s = tokens.summary(self.conn, self.reg, days=30)
        self.assertEqual(len(s["by_session"]), 1)
        top = s["by_session"][0]
        self.assertEqual((top["device"], top["service"], top["input"], top["output"]),
                         ("acme-mini", "Acme", 110, 55))


class WindowTests(unittest.TestCase):
    """짧은 범위 롤링 창 — 시간 원장(token_hourly) 조회. 세션 이벤트는 fold(ingest)로, 워커 호출은 원장 쓰기 경로
    (tokens.add_ledger — llm._account_tokens가 쓰는 함수)로 쌓고 window()는 읽기만 한다.
    시간 버킷과 기기/프로젝트/모델/에이전트별 내역·합계가 같은 창에서 나와야 한다."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        for did, name in ((1, "acme-mini"), (2, "acme-hub")):   # 2 = 허브 기기(워커 호출이 귀속되는 곳)
            self.conn.execute(
                "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
                " VALUES (?,?,'x',datetime('now'),datetime('now'))", (did, name))
        self.reg = registry.Registry()
        base = datetime.now().astimezone().replace(minute=30, second=0, microsecond=0)
        self.h1 = base - timedelta(hours=2)
        self.h2 = base - timedelta(hours=1)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def iso(dt):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def ingest(self, eid, ts, cum_in, cum_out, frontend="cli", agent="claude-code", session="s1"):
        state.ingest(self.conn, 1, {
            "agent": agent, "session_id": session, "event_id": eid, "event": "turn_done",
            "ts": self.iso(ts), "project": "acme-web", "branch": "main",
            "detail": {"summary": "x", "frontend": frontend, "model": "model-a",
                       "tokens": {"v": 1, "cum": {"model-a": cum(cum_in, cum_out)}}}})

    def worker_run(self, ts, provider="claude"):
        """허브 워커 호출 한 건 — 쓰기 경로 그대로(항상 auto·워커 cwd 프로젝트·허브 기기)."""
        agent = "codex-cli" if provider == "codex" else "claude-code"
        tokens.add_ledger(self.conn, ts.strftime("%Y-%m-%dT%H"), 2, agent, tokens.CFG.llm_cwd.name,
                          "haiku", "auto", "events", cum(7, 3))

    def test_hourly_buckets_sessions_and_workers(self):
        self.ingest("e1", self.h1, 100, 50)
        self.ingest("e2", self.h2, 250, 80)          # 델타 150/30 → h2 시간대
        self.worker_run(self.h2)
        rows = tokens.window(self.conn, self.reg, hours_back=24)["hours"]
        self.assertEqual(len(rows), 24)
        by = {r["ts"]: r for r in rows}
        self.assertEqual(by[self.h1.strftime("%Y-%m-%dT%H")]["input"], 100)
        self.assertEqual(by[self.h2.strftime("%Y-%m-%dT%H")]["input"], 157)   # 세션 150 + 워커 7
        self.assertEqual(sum(r["output"] for r in rows), 83)

    def test_hourly_human_filter_drops_workers(self):
        self.ingest("e1", self.h2, 100, 50)
        self.worker_run(self.h2)
        rows = tokens.window(self.conn, self.reg, hours_back=24, human=True)["hours"]
        self.assertEqual(sum(r["input"] for r in rows), 100)   # 워커 제외, 세션만

    def test_window_breakdowns_share_the_rolling_window(self):
        self.ingest("e1", self.h1, 100, 50)
        self.ingest("e2", self.h2, 250, 80)
        self.worker_run(self.h2)
        w = tokens.window(self.conn, self.reg, hours_back=24)
        self.assertEqual((w["total"]["input"], w["total"]["output"], w["total"]["turns"]),
                         (257, 83, 2))
        by_dev = {r["device"]: r for r in w["by_device"]}
        self.assertEqual((by_dev["acme-mini"]["input"], by_dev["acme-mini"]["turns"]), (250, 2))
        self.assertEqual(by_dev["acme-hub"]["input"], 7)   # 워커→허브 기기
        by_model = {r["model"]: r for r in w["by_model"]}
        self.assertEqual((by_model["model-a"]["input"], by_model["haiku"]["input"]), (250, 7))
        by_proj = {r["project"]: r for r in w["by_project"]}
        self.assertEqual(by_proj["acme-web"]["input"], 250)
        self.assertEqual(by_proj[tokens.CFG.llm_cwd.name]["input"], 7)
        self.assertEqual(w["by_agent"], [{"agent": "claude-code", "input": 257, "output": 83,
                                          "cache_read": 0, "cache_write": 0, "thinking": 0, "turns": 2}])

    def test_window_counts_turn_even_when_delta_is_zero(self):
        self.ingest("e1", self.h1, 100, 50)
        self.ingest("e2", self.h2, 100, 50)          # 동일 누적 재전송 — 턴은 실제로 끝났다
        w = tokens.window(self.conn, self.reg, hours_back=24)
        self.assertEqual((w["total"]["input"], w["total"]["turns"]), (100, 2))

    def test_series_by_agent_splits_sessions_and_workers(self):
        self.ingest("e1", self.h1, 100, 50)
        self.ingest("e2", self.h2, 250, 80)
        self.ingest("c1", self.h2, 40, 20, agent="codex-cli", session="s2")
        self.worker_run(self.h2)                       # provider claude → claude-code
        self.worker_run(self.h1, provider="codex")     # provider codex → codex-cli
        s = tokens.summary(self.conn, self.reg, days=1)
        sba = s["series_by_agent"]
        self.assertEqual(set(sba), {"claude-code", "codex-cli"})
        hour = lambda dt: dt.strftime("%Y-%m-%dT%H")
        for ag, ser in sba.items():
            self.assertEqual([r["ts"] for r in ser["hours"]], [r["ts"] for r in s["hours"]], ag)
        by = {ag: {r["ts"]: r for r in ser["hours"]} for ag, ser in sba.items()}
        self.assertEqual(by["claude-code"][hour(self.h1)]["input"], 100)
        self.assertEqual(by["claude-code"][hour(self.h2)]["input"], 157)   # 세션 150 + 워커 7
        self.assertEqual(by["codex-cli"][hour(self.h1)]["input"], 7)       # 워커만
        self.assertEqual(by["codex-cli"][hour(self.h2)]["input"], 40)
        # 불변식: 에이전트별 합 == 전체 hours (turns 포함), days도 마찬가지
        cols = ("input", "output", "cache_read", "cache_write", "thinking", "turns")
        for i, row in enumerate(s["hours"]):
            for c in cols:
                self.assertEqual(sum(ser["hours"][i][c] for ser in sba.values()), row[c], (row["ts"], c))
        for i, row in enumerate(s["days"]):
            for c in cols:
                self.assertEqual(sum(ser["days"][i][c] for ser in sba.values()), row[c], (row["day"], c))
            self.assertEqual([ser["days"][i]["day"] for ser in sba.values()], [row["day"]] * len(sba))

    def test_series_by_agent_filters(self):
        self.ingest("e1", self.h2, 100, 50)
        self.worker_run(self.h2, provider="codex")     # codex-cli는 워커분만 있다
        sba = tokens.window(self.conn, self.reg, hours_back=24, agent="claude-code")["series_by_agent"]
        self.assertEqual(set(sba), {"claude-code"})
        self.assertEqual(sum(r["input"] for r in sba["claude-code"]["hours"]), 100)
        sba = tokens.window(self.conn, self.reg, hours_back=24, human=True)["series_by_agent"]
        self.assertEqual(set(sba), {"claude-code"})    # 워커 제외 — 워커만 있던 codex-cli는 키 자체가 없다
        self.assertEqual(tokens.window(self.conn, self.reg, hours_back=24, agent="codex-cli", human=True)
                         ["series_by_agent"], {})

    def test_summary_short_range_is_rolling_not_calendar(self):
        """≤7일 필터의 내역·합계는 일 원장(달력 날짜)이 아니라 시간 원장 롤링 창에서 나온다."""
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
            " VALUES (3,'acme-laptop','x',datetime('now'),datetime('now'))")
        # 일 원장에만 있는 오늘 행(시간 원장 없음 — 전사본 백필) — 짧은 범위 내역엔 안 나와야 한다
        self.conn.execute(
            "INSERT INTO token_daily (day, device_id, agent, project, model, frontend, source,"
            " input, output, cache_read, cache_write, thinking, turns)"
            " VALUES (date('now','localtime'),3,'claude-code','acme-api','model-a','cli',"
            " 'backfill',9999,9999,0,0,0,9)")
        self.ingest("e1", self.h1, 100, 50)
        s = tokens.summary(self.conn, self.reg, days=1)
        self.assertIsNotNone(s["hours"])
        self.assertEqual([r["device"] for r in s["by_device"]], ["acme-mini"])
        self.assertEqual(s["total"]["input"], 100)
        # 긴 범위는 일 원장 그대로 — 원장 전용 행도 보인다
        s30 = tokens.summary(self.conn, self.reg, days=30)
        self.assertIsNone(s30["hours"])
        self.assertIn("acme-laptop", [r["device"] for r in s30["by_device"]])

    def test_window_only_reads_the_ledger(self):
        """window는 이벤트·llm_runs를 재생하지 않는다 — 원장에 없으면 이벤트가 있어도 안 보인다."""
        self.conn.execute(
            "INSERT INTO events (device_id, agent, session_id, event_id, event, ts_device, ts_hub, project, payload)"
            " VALUES (1,'claude-code','s9','x1','turn_done',?,?,'acme-web',?)",
            (self.iso(self.h2), self.iso(self.h2), json.dumps({"tokens": {"cum": {"m": cum(5, 5)}}})))
        self.conn.execute(
            "INSERT INTO llm_runs (site, provider, model, started_at, usage) VALUES ('summary','claude','haiku',?,?)",
            (self.iso(self.h2), json.dumps({"haiku": cum(7, 3)})))
        w = tokens.window(self.conn, self.reg, hours_back=24)
        self.assertEqual((w["total"]["input"], w["total"]["turns"], w["series_by_agent"]), (0, 0, {}))
        self.assertEqual(len(w["hours"]), 24)

    def test_window_bounds_and_zero_fill(self):
        """창 밖(옛 시간·미래 시계 오차)은 버리고 창 안 빈 시간대는 0, days는 시간 버킷의 날짜별 합이다."""
        far = self.h1 - timedelta(hours=40)
        for dt, n in ((far, 1000), (self.h2, 5), (self.h2 + timedelta(hours=5), 2000)):
            tokens.add_ledger(self.conn, dt.strftime("%Y-%m-%dT%H"), 1, "claude-code", "acme-web", "model-a",
                              "cli", "events", cum(n, 1), turns=1)
        w = tokens.window(self.conn, self.reg, hours_back=24)
        self.assertEqual(w["total"]["input"], 5)
        self.assertEqual(sorted(r["input"] for r in w["hours"])[-1], 5)
        self.assertEqual(sum(r["input"] for r in w["hours"]), 5)
        self.assertEqual(sum(r["input"] for r in w["days"]), 5)
        self.assertEqual(sum(r["turns"] for r in w["days"]), 1)
        self.assertEqual(w["days"][0]["day"], w["hours"][0]["ts"][:10])


class AutomationVerdictTests(unittest.TestCase):
    """자동화 판정의 단일 기준 — 세션(sessions.frontend). 시간 원장(1·7일 창)과 일 원장(긴 범위)은 같은 fold가
    쓰므로 같은 판정을 담는다."""

    APP_PROMPT = "This is an app-generated request for a suggested next user message. The user did not write this message."

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
            " VALUES (1,'acme-mini','x',datetime('now'),datetime('now'))")
        self.reg = registry.Registry()
        base = datetime.now().astimezone().replace(minute=30, second=0, microsecond=0)
        self.ts = (base - timedelta(hours=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.n = 0

    def tearDown(self):
        self.conn.close()

    def send(self, session, event, *, agent="claude-code", project="acme-web", frontend="cli",
             model="model-a", tokens_cum=None, **detail):
        self.n += 1
        d = {"summary": "x", "frontend": frontend, "model": model, **detail}
        if tokens_cum is not None:
            d["tokens"] = {"v": 1, "cum": {model: tokens_cum}}
        state.ingest(self.conn, 1, {"agent": agent, "session_id": session, "event_id": f"e{self.n}",
                                    "event": event, "ts": self.ts, "project": project, "branch": "main", "detail": d})

    def totals(self, **kw):
        """(1일 창 입력, 30일 원장 입력) — 같은 필터로 두 경로를 모두 본다."""
        return (tokens.summary(self.conn, self.reg, days=1, **kw)["total"]["input"],
                tokens.summary(self.conn, self.reg, days=30, **kw)["total"]["input"])

    def frontends(self):
        return {r["session_id"]: r["frontend"] for r in self.conn.execute("SELECT session_id, frontend FROM sessions")}

    def test_codex_app_generated_session_agrees_across_ledgers(self):
        self.send("human", "turn_done", tokens_cum=cum(1000, 5))
        self.send("gen", "prompt", agent="codex-cli", frontend="unknown", prompt=self.APP_PROMPT)
        self.send("gen", "turn_done", agent="codex-cli", frontend="unknown", tokens_cum=cum(100, 5))
        self.assertEqual(self.frontends(), {"human": "cli", "gen": "auto"})
        self.assertEqual(self.totals(human=True), (1000, 1000))     # 두 경로 모두 자동화 제외
        self.assertEqual(self.totals(human=False), (1100, 1100))    # 자동화 포함이면 둘 다 전부

    def test_turn_done_before_app_prompt_still_agrees(self):
        """역순: turn_done이 앱 자동 생성 prompt보다 먼저 와도 1일 창과 30일 원장의 human 합이 같다
        (한 번 계산한 값을 두 단위로 조회하므로). 그 이벤트는 도착 시점의 세션 판정(unknown)으로 기록된다 —
        경계 사례로 일부러 남긴 동작."""
        self.send("human", "turn_done", tokens_cum=cum(1000, 5))
        self.send("gen", "turn_done", agent="codex-cli", frontend="unknown", tokens_cum=cum(100, 5))
        self.send("gen", "prompt", agent="codex-cli", frontend="unknown", prompt=self.APP_PROMPT)
        self.send("gen", "turn_done", agent="codex-cli", frontend="unknown", tokens_cum=cum(130, 6))
        self.assertEqual(self.frontends()["gen"], "auto")
        w, l = self.totals(human=True)
        self.assertEqual(w, l)
        self.assertEqual(w, 1100)   # 첫 100은 판정 전 도착분(unknown), 이후 30만 auto
        self.assertEqual(self.totals(human=False), (1130, 1130))

    def test_worker_named_project_is_an_ordinary_session(self):
        """워커 디렉터리 이름(summarizer·llm-cwd)이라고 세션을 auto로 만들지 않는다 — 같은 이름의 사용자
        프로젝트가 자동화로 숨지 않는다. 워커 자동화는 payload(부모 프로세스 감지)·MADISON_SUPPRESS가 맡는다."""
        self.send("mine", "prompt", project="summarizer", frontend="cli")
        self.send("mine", "turn_done", project="summarizer", frontend="cli", tokens_cum=cum(10, 5))
        self.send("mine2", "turn_done", project="llm-cwd", frontend="app", tokens_cum=cum(20, 5))
        self.assertEqual(self.frontends(), {"mine": "cli", "mine2": "app"})
        self.assertEqual(self.totals(human=True), (30, 30))

    def test_payload_auto_session_is_auto_in_both(self):
        self.send("hl", "turn_done", frontend="auto", tokens_cum=cum(10, 5))
        self.assertEqual(self.totals(human=True), (0, 0))
        self.assertEqual(self.totals(), (10, 10))

    def test_unknown_never_overwrites_a_known_frontend(self):
        self.send("s", "prompt", frontend="app")
        self.send("s", "turn_done", frontend="unknown", tokens_cum=cum(10, 5))
        self.assertEqual(self.frontends(), {"s": "app"})
        self.assertEqual([r[0] for r in self.conn.execute("SELECT frontend FROM token_hourly")], ["app"])

    def test_missing_event_project_falls_back_to_session_project(self):
        self.send("s1", "prompt", project="acme-web")
        self.send("s1", "turn_done", project="", tokens_cum=cum(100, 50))
        self.send("s2", "prompt", project="acme-api", frontend="cli")
        self.send("s2", "turn_done", project="", tokens_cum=cum(10, 5))   # project 없이 와도 세션 project로
        by = lambda s: {r["project"]: r["input"] for r in s["by_project"]}
        win = tokens.summary(self.conn, self.reg, days=1)
        led = tokens.summary(self.conn, self.reg, days=30)
        self.assertEqual(by(win), by(led))
        self.assertEqual(by(win), {"acme-web": 100, "acme-api": 10})
        self.assertEqual(self.totals(human=True), (110, 110))

    def test_scale_sort_counts_cache_in_both_paths(self):
        # big-cache(입력 1 + 캐시 읽기 5000)가 big-io(입력+출력 300)보다 앞서야 한다
        self.send("a", "turn_done", model="big-io", tokens_cum=cum(200, 100))
        self.send("b", "turn_done", model="big-cache", agent="codex-cli", tokens_cum=cum(1, 1, cr=5000))
        for days in (1, 30):    # 1=시간 원장 경로, 30=일 원장 경로
            s = tokens.summary(self.conn, self.reg, days=days)
            self.assertEqual([r["model"] for r in s["by_model"]], ["big-cache", "big-io"], days)
            self.assertEqual([r["agent"] for r in s["by_agent"]], ["codex-cli", "claude-code"], days)

    def test_scale_key_definition(self):
        a = {"input": 1, "output": 1, "cache_read": 5000, "cache_write": 0}
        b = {"input": 200, "output": 100, "cache_read": 0, "cache_write": 0}
        self.assertEqual([r["input"] for r in sorted([b, a], key=tokens.scale_key)], [1, 200])


class SharedFilterTests(unittest.TestCase):
    """window(시간 원장)와 _daily_summary(일 원장)는 같은 WHERE 빌더(tokens._where)를 쓴다 —
    같은 행을 두 원장에 넣고 필터별로 내역·합계가 일치해야 한다."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        for did, name in ((1, "acme-mini"), (2, "acme-laptop")):
            self.conn.execute(
                "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
                " VALUES (?,?,'x',datetime('now'),datetime('now'))", (did, name))
        self.reg = registry.Registry(
            project_map={"acme-web": {"service": "Acme", "strength": "strong"},
                         "acme-api": {"service": "Acme", "strength": "strong"}})
        self.hour = datetime.now().astimezone().strftime("%Y-%m-%dT%H")
        for did, agent, proj, model, fe, n in (
                (1, "claude-code", "acme-web", "model-a", "cli", 100), (2, "claude-code", "acme-api", "model-a", "app", 40),
                (1, "codex-cli", "acme-web", "gpt-a", "auto", 10), (2, "codex-cli", "other", "gpt-b", "", 3),
                (1, "claude-code", "", "model-b", "auto", 7)):
            tokens.add_ledger(self.conn, self.hour, did, agent, proj, model, fe, "events",
                              cum(n, n // 2, n * 3, n // 5, n // 10), turns=1)

    def tearDown(self):
        self.conn.close()

    FILTERS = ({}, {"agent": "codex-cli"}, {"device": "acme-laptop"}, {"device": "acme-laptop,acme-mini"},
               {"device": ","}, {"model": "gpt-a"}, {"human": True}, {"service": "Acme"}, {"service": "other"},
               {"service": "Acme", "human": True, "agent": "claude-code"}, {"model": "nope"})

    def test_same_filters_same_results_in_both_ledgers(self):
        keys = ("total", "by_device", "by_project", "by_service", "by_model", "by_agent")
        for f in self.FILTERS:
            win = tokens.window(self.conn, self.reg, hours_back=24, **f)
            day = tokens._daily_summary(self.conn, self.reg, days=1, **{
                "agent": "", "device": "", "service": "", "model": "", "human": False, **f})
            for k in keys:
                self.assertEqual(win[k], day[k], (f, k))
            self.assertEqual({a: sum(r["input"] for r in s["days"]) for a, s in win["series_by_agent"].items()},
                             {a: sum(r["input"] for r in s["days"]) for a, s in day["series_by_agent"].items()}, f)
            self.assertEqual(sum(r["input"] for r in win["hours"]), win["total"]["input"], f)

    def test_filters_actually_filter(self):
        total = lambda **f: tokens.window(self.conn, self.reg, hours_back=24, **f)["total"]["input"]
        self.assertEqual(total(), 160)
        self.assertEqual(total(human=True), 143)           # auto 두 행(10, 7) 제외
        self.assertEqual(total(device="acme-laptop"), 43)
        self.assertEqual(total(service="Acme"), 150)
        self.assertEqual(total(agent="codex-cli", model="gpt-a"), 10)

    def test_both_queries_go_through_the_one_where_builder(self):
        with mock.patch.object(tokens, "_where", wraps=tokens._where) as spy:
            tokens.window(self.conn, self.reg, hours_back=24, agent="claude-code")
            self.assertEqual(spy.call_count, 1)
            tokens._daily_summary(self.conn, self.reg, days=1, agent="claude-code", device="", service="",
                                  model="", human=False)
            self.assertEqual(spy.call_count, 2)
        self.assertEqual(spy.call_args_list[0].args[1], spy.call_args_list[1].args[1])   # 같은 테이블 별칭 규칙


class HourlyBackfillTests(unittest.TestCase):
    """token_hourly가 새로 만들어질 때만 도는 일회성 채우기(db.migrate → tokens.backfill_hourly)."""

    def setUp(self):
        # 파일 DB(WAL) — 동시 기동(두 커넥션) 테스트가 같은 DB를 열 수 있어야 한다
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = str(Path(self.dir) / "t.sqlite")
        self.conn = self.connect()
        self.conn.executescript(db.SCHEMA)
        for did, name in ((1, "acme-mini"), (2, "acme-hub")):
            self.conn.execute(
                "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
                " VALUES (?,?,'x',datetime('now'),datetime('now'))", (did, name))
        envfile = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        envfile.write("MADISON_URL=http://x\nMADISON_DEVICE=acme-hub\n")
        envfile.close()
        self.envpath = Path(envfile.name)
        patcher = mock.patch.object(tokens, "DEVICE_ENV", self.envpath)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.envpath.unlink)
        tokens._hub_dev.update(id=None, at=0.0)
        self.addCleanup(tokens._hub_dev.update, id=None, at=0.0)
        self.n = 0

    def tearDown(self):
        self.conn.close()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def send(self, session, ts, tokens_cum, *, event="turn_done", agent="claude-code", model="model-a",
             frontend="cli", project="acme-web"):
        self.n += 1
        d = {"summary": "x", "frontend": frontend, "model": model}
        if tokens_cum is not None:
            d["tokens"] = {"v": 1, "cum": tokens_cum}
        state.ingest(self.conn, 1, {"agent": agent, "session_id": session, "event_id": f"e{self.n}",
                                    "event": event, "ts": ts, "project": project, "branch": "main", "detail": d})

    def worker(self, ts, usage, provider="claude"):
        """워커 호출 한 건: llm_runs 행 + 쓰기 경로 가산(실시간이었다면 같은 시각에 일어났을 일)."""
        self.conn.execute("INSERT INTO llm_runs (site, provider, model, started_at, usage) VALUES ('summary',?,'haiku',?,?)",
                          (provider, ts, json.dumps(usage)))
        tokens.add_ledger(self.conn, tokens.local_hour(ts), 2, "codex-cli" if provider == "codex" else "claude-code",
                          tokens.CFG.llm_cwd.name, "haiku", "auto", "events", cum(*usage["haiku"].values()))

    def seed(self):
        # Claude: 모델 둘·여러 시간·날짜 경계 넘김, 델타 0 턴, session_end 캐치업
        self.send("c1", "2026-09-02T10:10:00Z", {"model-a": cum(100, 50, 1000, 200, 30), "model-b": cum(10, 5)})
        self.send("c1", "2026-09-02T10:50:00Z", {"model-a": cum(160, 70, 1500, 200, 30), "model-b": cum(10, 5)})
        self.send("c1", "2026-09-02T14:00:00Z", {"model-a": cum(160, 70, 1500, 200, 30), "model-b": cum(10, 5)})
        self.send("c1", "2026-09-02T16:30:00Z", {"model-a": cum(300, 90, 1500, 200, 30), "model-b": cum(10, 5)})
        self.send("c1", "2026-09-03T15:30:00Z", {"model-a": cum(330, 95, 1500, 200, 30)}, event="session_end")
        # Codex: 모델 전환, 세션 전역 카운터, 앱 자동 생성이 아닌 auto 세션
        self.send("x1", "2026-09-02T09:00:00Z", {"gpt-a": cum(100, 50)}, agent="codex-cli", model="gpt-a")
        self.send("x1", "2026-09-02T20:00:00Z", {"gpt-b": cum(180, 90)}, agent="codex-cli", model="gpt-b")
        self.send("a1", "2026-09-02T11:00:00Z", {"model-a": cum(5, 5)}, frontend="auto", project="")
        # 허브 워커: 이벤트 없음 — llm_runs 만
        self.worker("2026-09-02T10:20:00Z", {"haiku": cum(7, 3)})
        self.worker("2026-09-02T10:40:00Z", {"haiku": cum(7, 3)}, provider="codex")
        self.worker("2026-09-03T15:10:00Z", {"haiku": cum(2, 1, 20)})
        self.conn.execute("INSERT INTO llm_runs (site, provider, model, started_at) VALUES ('summary','claude','x',?)",
                          ("2026-09-02T10:20:00Z",))   # usage 없는 행은 건너뛴다
        self.conn.commit()

    def hourly(self):
        return [tuple(r) for r in self.conn.execute("SELECT * FROM token_hourly ORDER BY 1,2,3,4,5,6,7")]

    def daily_of(self, table, key):
        return [tuple(r) for r in self.conn.execute(
            f"SELECT substr({key},1,10), device_id, agent, project, model, frontend, source, SUM(input), SUM(output),"
            f" SUM(cache_read), SUM(cache_write), SUM(thinking), SUM(turns) FROM {table} WHERE source='events'"
            " GROUP BY 1,2,3,4,5,6,7 ORDER BY 1,2,3,4,5,6,7")]

    def test_fill_rebuilds_the_hourly_ledger_and_second_migrate_changes_nothing(self):
        self.seed()
        live = self.hourly()                       # 실시간 fold가 쌓았을 시간 원장 = 기대값
        self.assertTrue(live)
        self.conn.execute("DROP TABLE token_hourly")   # 시간 원장이 없던 시절의 DB
        db.migrate(self.conn)
        filled = self.hourly()
        self.assertEqual(self.daily_of("token_hourly", "hour"), self.daily_of("token_daily", "day"))
        self.assertEqual(filled, live)             # 키·값·턴 모두 같다
        db.migrate(self.conn)                      # 두 번째 — 테이블이 이미 있으니 채우지 않는다
        db.migrate(self.conn)
        self.assertEqual(self.hourly(), filled)
        self.assertEqual(self.daily_of("token_daily", "day"), self.daily_of("token_hourly", "hour"))

    def test_fill_failure_rolls_back_table_creation_and_retries_next_time(self):
        self.seed()
        self.conn.execute("DROP TABLE token_hourly")
        self.conn.commit()
        with mock.patch.object(tokens, "backfill_hourly", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                db.migrate(self.conn)
        self.assertFalse(db._table_exists(self.conn, "token_hourly"))   # 생성도 롤백 — 부분 데이터 없음
        db.migrate(self.conn)                                           # 다음 기동에서 재시도
        self.assertTrue(self.hourly())

    def test_fill_skips_workers_when_hub_device_unknown(self):
        self.seed()
        self.conn.execute("DROP TABLE token_hourly")
        self.envpath.write_text("MADISON_DEVICE=\n")
        tokens._hub_dev.update(id=None, at=0.0)
        db.migrate(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM token_hourly WHERE frontend='auto' AND project=?",
            (tokens.CFG.llm_cwd.name,)).fetchone()[0], 0)
        self.assertTrue(self.conn.execute("SELECT COUNT(*) FROM token_hourly WHERE agent='claude-code'").fetchone()[0])

    # ── 동시 기동(TOCTOU) ─────────────────────────────────

    def _drop_hourly_and_expect(self):
        self.seed()
        expected = self.hourly()
        self.assertTrue(expected)
        self.conn.execute("DROP TABLE token_hourly")
        self.conn.commit()
        return expected

    def test_two_connections_migrating_one_after_another_fill_once(self):
        expected = self._drop_hourly_and_expect()
        a, b = self.connect(), self.connect()
        self.addCleanup(a.close); self.addCleanup(b.close)
        db.migrate(a)
        db.migrate(b)
        self.assertEqual(self.hourly(), expected)

    def test_second_migrate_waits_for_the_write_lock_then_does_not_fill(self):
        """한 쪽이 IMMEDIATE(생성+채우기 중)를 잡은 동안 다른 쪽 migrate는 기다렸다가, 커밋된 테이블을 보고 건너뛴다."""
        expected = self._drop_hourly_and_expect()
        entered, release = threading.Event(), threading.Event()
        calls = []
        real_fill = tokens.backfill_hourly

        def blocking_fill(c):
            calls.append(threading.current_thread().name)
            if len(calls) == 1:           # 첫 호출(A)만 락을 쥔 채 멈춘다
                entered.set()
                self.assertTrue(release.wait(20))
            return real_fill(c)

        errors = []

        def run(name):
            c = self.connect()
            try:
                db.migrate(c)
            except BaseException as e:    # noqa: BLE001 — 스레드 안 실패를 메인에서 단언
                errors.append((name, e))
            finally:
                c.close()

        with mock.patch.object(tokens, "backfill_hourly", blocking_fill):
            ta = threading.Thread(target=run, args=("A",), name="A")
            ta.start()
            self.assertTrue(entered.wait(20))
            # MIGRATIONS의 DELETE·DROP은 쓰기 문이라 B를 우연히 먼저 막는다 — 그 우연에 기대지 않게 비우고 B를 띄운다
            # (A는 이미 지나갔다). 존재 검사가 락 밖에 있으면 B는 '테이블 없음'을 보고 A 뒤에 또 채운다.
            with mock.patch.object(db, "MIGRATIONS", ()):
                tb = threading.Thread(target=run, args=("B",), name="B")
                tb.start()
                time.sleep(0.5)
                self.assertTrue(tb.is_alive(), "B는 A의 쓰기 락이 풀릴 때까지 기다려야 한다")
                release.set()
                ta.join(20); tb.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(calls, ["A"])                  # B는 채우지 않았다
        self.assertEqual(self.hourly(), expected)       # 이중 계상 없음

    def test_migrate_does_not_leave_the_write_lock_behind(self):
        self._drop_hourly_and_expect()
        db.migrate(self.conn)
        other = self.connect()
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")                # 락이 남아 있으면 timeout 후 실패
        other.rollback()

    def test_hour_index_is_dropped_but_primary_key_serves_the_range(self):
        self.conn.execute("CREATE INDEX idx_token_hourly_hour ON token_hourly(hour)")   # 예전 DB
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_token_daily_day ON token_daily(day)")
        db.migrate(self.conn)
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertNotIn("idx_token_hourly_hour", names)
        self.assertIn("idx_token_daily_day", names)     # 일 원장의 기존 인덱스는 그대로
        plan = " ".join(str(tuple(r)) for r in self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM token_hourly WHERE hour BETWEEN 'a' AND 'b'"))
        self.assertIn("autoindex", plan)                # PK 인덱스가 범위를 맡는다
        fresh = sqlite3.connect(":memory:")
        db.migrate(fresh)
        self.assertNotIn("idx_token_hourly_hour", {r[0] for r in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")})
        fresh.close()

    # ── 행 단위 격리 ──────────────────────────────────────

    def _flaky_upsert(self, bad):
        real = tokens._upsert

        def upsert(c, table, key, stamp, device_id, agent, project, model, frontend, source, counts, turns):
            if bad(project, model):
                raise sqlite3.OperationalError("simulated bad row")
            return real(c, table, key, stamp, device_id, agent, project, model, frontend, source, counts, turns)
        return upsert

    def test_corrupt_session_is_skipped_and_the_rest_is_filled(self):
        self.send("good", "2026-09-02T10:10:00Z", {"model-a": cum(100, 50)})
        self.send("bad", "2026-09-02T10:20:00Z", {"model-a": cum(7, 7)}, project="bad-proj")
        self.send("bad", "2026-09-02T11:20:00Z", {"model-a": cum(9, 9)}, project="bad-proj")
        self.send("good2", "2026-09-02T12:10:00Z", {"model-b": cum(5, 5)})
        self.conn.commit()
        want = [r for r in self.hourly() if r[3] != "bad-proj"]
        self.assertTrue(want)
        self.conn.execute("DROP TABLE token_hourly")
        self.conn.commit()
        with mock.patch.object(tokens, "_upsert", self._flaky_upsert(lambda p, m: p == "bad-proj")):
            with self.assertLogs("madison.tokens", "WARNING") as logs:
                db.migrate(self.conn)           # 기동은 막히지 않는다
        self.assertEqual(self.hourly(), want)   # 손상 세션은 부분 행도 없이 통째로 빠진다
        self.assertTrue(any("세션 1건" in m for m in logs.output), logs.output)

    def test_corrupt_worker_run_is_skipped_and_the_rest_is_filled(self):
        self.worker("2026-09-02T10:20:00Z", {"haiku": cum(7, 3)})
        self.conn.execute("INSERT INTO llm_runs (site, provider, model, started_at, usage)"
                          " VALUES ('summary','claude','x',?,?)",
                          ("2026-09-02T11:20:00Z", json.dumps({"boom": cum(9, 9), "haiku": cum(1, 1)})))
        self.worker("2026-09-02T12:20:00Z", {"haiku": cum(2, 2)})
        self.conn.commit()
        self.conn.execute("DROP TABLE token_hourly")
        self.conn.commit()
        with mock.patch.object(tokens, "_upsert", self._flaky_upsert(lambda p, m: m == "boom")):
            with self.assertLogs("madison.tokens", "WARNING") as logs:
                db.migrate(self.conn)
        hours = {r[0]: r for r in self.hourly()}
        self.assertEqual(len(hours), 2)         # 11시 호출은 haiku 가산도 되돌려져 부분 행이 없다
        self.assertTrue(any("워커 호출 1건" in m for m in logs.output), logs.output)

    def test_whole_fill_failure_still_aborts_startup(self):
        """단위 격리는 단위 실패만 삼킨다 — 전체 실패(락·디스크 등)는 롤백 후 기동 실패 그대로."""
        self.seed()
        self.conn.execute("DROP TABLE token_hourly")
        self.conn.commit()
        with mock.patch.object(tokens, "hub_device_id", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                db.migrate(self.conn)
        self.assertFalse(db._table_exists(self.conn, "token_hourly"))

    def test_out_of_range_counter_is_a_bad_field_not_a_failure(self):
        """2^63 이상 정수는 불량 필드(0) — 한 줄 때문에 채우기가 죽지 않고 나머지 필드는 계상된다."""
        self.conn.execute(
            "INSERT INTO events (device_id, agent, session_id, event_id, event, ts_device, ts_hub, project, payload)"
            " VALUES (1,'claude-code','s1','e1','turn_done','2026-10-20T01:00:00Z','2026-10-20T01:00:01Z','p',?)",
            (json.dumps({"tokens": {"v": 1, "cum": {"m": {"in": 2 ** 63, "out": 1, "cr": 0, "cw": 0, "th": 0}}}}),))
        self.conn.execute("DROP TABLE token_hourly")
        self.conn.commit()
        db.migrate(self.conn)
        row = self.conn.execute("SELECT input, output, turns FROM token_hourly").fetchone()
        self.assertEqual(tuple(row), (0, 1, 1))

    def test_fresh_database_migrates_to_empty_ledgers(self):
        fresh = sqlite3.connect(":memory:")
        fresh.row_factory = sqlite3.Row
        db.migrate(fresh)
        self.assertEqual(fresh.execute("SELECT COUNT(*) FROM token_hourly").fetchone()[0], 0)
        db.migrate(fresh)
        fresh.close()


class AtomicFoldTests(unittest.TestCase):
    """fold 전체(모든 add_ledger + tokens_cum 갱신)는 한 SAVEPOINT — 중간 실패가 부분 상태를 남기지 않는다."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
            " VALUES (1,'acme-mini','x',datetime('now'),datetime('now'))")

    def tearDown(self):
        self.conn.close()

    def ev(self, eid, models):
        return {"agent": "claude-code", "session_id": "s", "event_id": eid, "event": "turn_done",
                "ts": "2026-10-29T01:00:00Z", "project": "p",
                "detail": {"frontend": "cli", "model": "m-a", "tokens": {"v": 1, "cum": models}}}

    def totals(self, table):
        return [(r["model"], r["input"], r["turns"]) for r in
                self.conn.execute(f"SELECT model, input, turns FROM {table} ORDER BY model")]

    def stored(self):
        return json.loads(self.conn.execute("SELECT tokens_cum FROM sessions").fetchone()[0])

    def flaky(self, fail_when):
        real = tokens._upsert

        def upsert(c, table, key, stamp, device_id, agent, project, model, frontend, source, counts, turns):
            if fail_when(table, model):
                raise sqlite3.OperationalError("simulated")
            return real(c, table, key, stamp, device_id, agent, project, model, frontend, source, counts, turns)
        return upsert

    def test_failure_on_second_model_leaves_nothing_and_next_turn_counts_exactly(self):
        state.ingest(self.conn, 1, self.ev("e1", {"m-a": cum(100), "m-b": cum(10)}))
        daily1, hourly1, cum1 = self.totals("token_daily"), self.totals("token_hourly"), self.stored()
        with mock.patch.object(tokens, "_upsert", self.flaky(lambda t, m: t == "token_hourly" and m == "m-b")):
            with self.assertLogs("madison.state", "ERROR"):       # ingest가 잡아 로그
                state.ingest(self.conn, 1, self.ev("e2", {"m-a": cum(150), "m-b": cum(15)}))
        self.assertEqual(self.totals("token_daily"), daily1)       # 첫 모델의 일 원장 가산도 되돌려졌다
        self.assertEqual(self.totals("token_hourly"), hourly1)
        self.assertEqual(self.stored(), cum1)                       # 기준점도 그대로
        state.ingest(self.conn, 1, self.ev("e3", {"m-a": cum(150), "m-b": cum(15)}))
        self.assertEqual([t[:2] for t in self.totals("token_daily")], [("m-a", 150), ("m-b", 15)])
        self.assertEqual(self.totals("token_daily"), self.totals("token_hourly"))
        self.assertEqual(self.stored()["m-a"]["in"], 150)

    def test_failure_propagates_from_fold_and_keeps_the_outer_transaction(self):
        state.ingest(self.conn, 1, self.ev("e1", {"m-a": cum(100)}))
        self.conn.execute("INSERT INTO settings (key, value) VALUES ('k','v')")   # 바깥 트랜잭션의 쓰기
        with mock.patch.object(tokens, "_upsert", self.flaky(lambda t, m: True)):
            with self.assertRaises(sqlite3.OperationalError):
                tokens.fold(self.conn, 1, "claude-code", "s", project="p", model="m-a", frontend="cli",
                            ts_device="2026-10-29T01:00:00Z", ts_hub="2026-10-29T01:00:00Z",
                            tokens={"cum": {"m-a": cum(200)}}, count_turn=True)
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0], "v")
        self.assertEqual(self.totals("token_daily"), [("m-a", 100, 1)])

    def test_counts_cap_marks_out_of_range_fields_as_bad(self):
        c = tokens._counts({"in": 10 ** 15, "out": 10 ** 15 + 1, "cr": 2 ** 63, "cw": float("inf"), "th": float("nan")})
        self.assertEqual(c, {"in": 10 ** 15, "out": 0, "cr": 0, "cw": 0, "th": 0})
        self.assertEqual(tokens._counts({"in": True, "out": -1, "cr": "5"}),
                         {"in": 0, "out": 0, "cr": 0, "cw": 0, "th": 0})

    def test_overflow_field_never_breaks_the_fold(self):
        state.ingest(self.conn, 1, self.ev("e1", {"m-a": {"in": 2 ** 63, "out": 4, "cr": 0, "cw": 0, "th": 0}}))
        row = self.conn.execute("SELECT input, output FROM token_daily").fetchone()
        self.assertEqual(tuple(row), (0, 4))
        self.assertEqual(self.totals("token_daily"), self.totals("token_hourly"))


def _load_backfill_script():
    spec = importlib.util.spec_from_file_location(
        "backfill_tokens_script", Path(__file__).resolve().parents[1] / "scripts" / "backfill_tokens.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BackfillScriptFrontendTests(unittest.TestCase):
    """백필 스크립트의 자동화 판정 경로는 resolve_frontend 하나 — 세션 판정 우선, 없을 때만 이름 규칙."""

    def setUp(self):
        self.mod = _load_backfill_script()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        for sid, agent, fe in (("c-auto", "claude-code", "auto"), ("c-cli", "claude-code", "cli"),
                               ("x-cli", "codex-cli", "cli"), ("x-unk", "codex-cli", "unknown")):
            self.conn.execute("INSERT INTO sessions (device_id, agent, session_id, frontend) VALUES (1,?,?,?)",
                              (agent, sid, fe))

    def tearDown(self):
        self.conn.close()

    def test_session_verdict_wins_over_name_rule_in_both_directions(self):
        r = self.mod.resolve_frontend
        self.assertEqual(r(self.conn, 1, "claude-code", "c-auto", "acme-web", ""), "auto")       # 이름은 평범, 세션이 auto
        self.assertEqual(r(self.conn, 1, "claude-code", "c-cli", "llm-cwd", ""), "cli")          # 이름은 워커, 세션이 cli
        self.assertEqual(r(self.conn, 1, "codex-cli", "x-cli", "summarizer", "auto"), "cli")

    def test_name_rule_only_without_a_session_verdict(self):
        r = self.mod.resolve_frontend
        self.assertEqual(r(self.conn, 1, "claude-code", "nope", "llm-cwd", ""), "auto")          # 세션 기록 없음
        self.assertEqual(r(self.conn, 1, "claude-code", "nope", "acme-web", "app"), "app")       # 수집기 값 보존
        self.assertEqual(r(self.conn, 1, "codex-cli", "x-unk", "summarizer", ""), "auto")        # 판정 'unknown'은 판정이 아님
        self.assertEqual(r(self.conn, 1, "claude-code", "", "llm-cwd", ""), "auto")              # id 없는 경로(--from-json)
        self.assertEqual(r(self.conn, 1, "claude-code", "", "acme-web", "cli"), "cli")
        self.assertEqual(r(self.conn, 2, "claude-code", "c-auto", "acme-web", ""), "")           # 다른 기기의 세션은 아님

    def test_codex_scan_carries_session_id_and_joins_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "rollout-x.jsonl"
            lines = [{"timestamp": "2026-09-02T10:00:00Z", "type": "session_meta",
                      "payload": {"id": "x-cli", "cwd": "", "originator": "codex_exec"}},
                     {"timestamp": "2026-09-02T10:00:01Z", "type": "event_msg",
                      "payload": {"type": "token_count", "info": {"total_token_usage": {
                          "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}}}}]
            f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
            agg = self.mod.scan_codex(Path(tmp), "9999-12-31")
            (key,) = agg
            self.assertEqual(key[3:], ("x-cli", "auto"))                     # 수집기 값은 auto(originator)
            out = self.mod.remap_frontend(self.conn, 1, "codex-cli", agg)
            (okey,) = out
            self.assertEqual(okey[3], "cli")                                  # 허브 세션 판정이 이긴다


class TxTests(unittest.TestCase):
    def test_tx_does_not_call_conn_again_when_conn_itself_fails(self):
        with mock.patch.object(db, "conn", side_effect=OSError("cannot open")) as m:
            with self.assertRaises(OSError):
                with db.tx():
                    self.fail("본문은 실행되지 않는다")
        self.assertEqual(m.call_count, 1)

    def test_tx_rolls_back_the_connection_it_used(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t (x)")
        c.commit()
        with mock.patch.object(db, "conn", return_value=c) as m:
            with self.assertRaises(RuntimeError):
                with db.tx() as cc:
                    cc.execute("INSERT INTO t VALUES (1)")
                    raise RuntimeError("boom")
        self.assertEqual(m.call_count, 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
        c.close()


if __name__ == "__main__":
    unittest.main()

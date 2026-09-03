import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

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
    """짧은 범위 롤링 창 — 세션 이벤트 델타 + 워커(llm_runs.usage)의 재계산.
    시간 버킷과 기기/프로젝트/모델/에이전트별 내역·합계가 같은 창에서 나와야 한다."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
            " VALUES (1,'acme-mini','x',datetime('now'),datetime('now'))")
        self.reg = registry.Registry()
        base = datetime.now().astimezone().replace(minute=30, second=0, microsecond=0)
        self.h1 = base - timedelta(hours=2)
        self.h2 = base - timedelta(hours=1)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def iso(dt):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def ingest(self, eid, ts, cum_in, cum_out, frontend="cli"):
        state.ingest(self.conn, 1, {
            "agent": "claude-code", "session_id": "s1", "event_id": eid, "event": "turn_done",
            "ts": self.iso(ts), "project": "acme-web", "branch": "main",
            "detail": {"summary": "x", "frontend": frontend, "model": "model-a",
                       "tokens": {"v": 1, "cum": {"model-a": cum(cum_in, cum_out)}}}})

    def worker_run(self, ts):
        self.conn.execute(
            "INSERT INTO llm_runs (site, provider, model, started_at, usage)"
            " VALUES ('summary','claude','haiku',?,?)",
            (self.iso(ts), json.dumps({"haiku": cum(7, 3)})))

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
        self.assertEqual(sum(r["input"] for n, r in by_dev.items() if n != "acme-mini"), 7)  # 워커→허브 기기
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

    def test_summary_short_range_is_rolling_not_calendar(self):
        """≤7일 필터의 내역·합계는 일 원장(달력 날짜)이 아니라 이벤트 롤링 창에서 나온다."""
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
            " VALUES (2,'acme-laptop','x',datetime('now'),datetime('now'))")
        # 일 원장에만 있는 오늘 행(이벤트 없음 — 백필 등) — 짧은 범위 내역엔 안 나와야 한다
        self.conn.execute(
            "INSERT INTO token_daily (day, device_id, agent, project, model, frontend, source,"
            " input, output, cache_read, cache_write, thinking, turns)"
            " VALUES (date('now','localtime'),2,'claude-code','acme-api','model-a','cli',"
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


if __name__ == "__main__":
    unittest.main()

import json
import sqlite3
import unittest

from server import db, state
from server.config import CFG


class StateCollectionModeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at) VALUES (1,'device-a','x',datetime('now'),datetime('now'))"
        )

    def tearDown(self):
        self.conn.close()

    def event(self, name, eid, ts, detail=None):
        return {
            "agent": "codex-cli",
            "session_id": "session-1",
            "event_id": eid,
            "event": name,
            "ts": ts,
            "project": "madison",
            "branch": "main",
            "detail": detail or {},
        }

    def test_hooked_codex_follows_full_turn_lifecycle(self):
        base = {"frontend": "app", "model": "gpt-test", "effort": "high", "collection_mode": "hooks"}
        events = [
            self.event("session_start", "e1", "2026-08-14T00:00:01Z", {**base, "source": "startup"}),
            self.event("prompt", "e2", "2026-08-14T00:00:02Z", {**base, "prompt": "Fix tracking"}),
            self.event("permission_request", "e3", "2026-08-14T00:00:03Z", {**base, "message": "Needs approval"}),
            self.event("tool_start", "e4", "2026-08-14T00:00:04Z", {**base, "tool": "Bash"}),
            self.event("turn_done", "e5", "2026-08-14T00:00:05Z", {**base, "summary": "Done"}),
        ]
        for event in events:
            self.assertEqual(state.ingest(self.conn, 1, event), "inserted")
        self.assertEqual(state.ingest(self.conn, 1, events[-1]), "duplicate")

        row = self.conn.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual(row["state"], "awaiting_input")
        self.assertEqual(row["turns"], 1)
        self.assertEqual(row["frontend"], "app")
        self.assertEqual(row["collection_mode"], "hooks")
        self.assertEqual(row["model"], "gpt-test")
        self.assertEqual(row["effort"], "high")

        assembled = state.assemble(self.conn)
        session = assembled["sessions"][0]
        self.assertFalse(session["partial"])
        self.assertEqual(session["collection_mode"], "hooks")

    def test_codex_app_generated_sessions_become_auto_and_unknown_never_overrides(self):
        # Codex 앱의 백그라운드 보조 세션: 수집기는 frontend=unknown으로 보내지만 프롬프트 서명으로 auto로 분류하고,
        # 이후 이벤트의 unknown이 그 값을 되돌리지 않는다
        unk = {"frontend": "unknown", "model": "gpt-test", "collection_mode": "hooks"}
        self.assertEqual(state.ingest(self.conn, 1, self.event("session_start", "a1", "2026-10-16T00:00:01Z", {**unk, "source": "fork"})), "inserted")
        self.assertEqual(self.conn.execute("SELECT frontend FROM sessions").fetchone()[0], "unknown")
        prompt = "This is an app-generated request for a suggested next user message. The user did not write this message."
        state.ingest(self.conn, 1, self.event("prompt", "a2", "2026-10-16T00:00:02Z", {**unk, "prompt": prompt}))
        self.assertEqual(self.conn.execute("SELECT frontend FROM sessions").fetchone()[0], "auto")
        state.ingest(self.conn, 1, self.event("turn_done", "a3", "2026-10-16T00:00:03Z", {**unk, "summary": "x"}))
        self.assertEqual(self.conn.execute("SELECT frontend FROM sessions").fetchone()[0], "auto")

        # 프로젝트 추천 생성 프롬프트도 같은 분류
        ev = self.event("prompt", "b2", "2026-10-16T00:01:02Z", {**unk, "prompt": "# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for what this user can do with Codex in this local project: /x"})
        ev["session_id"] = "session-2"
        state.ingest(self.conn, 1, ev)
        self.assertEqual(self.conn.execute("SELECT frontend FROM sessions WHERE session_id='session-2'").fetchone()[0], "auto")

        # 사람이 쓴 프롬프트는 그대로 unknown, 뒤에 실제 값(cli)이 오면 그 값으로 바뀐다
        ev = self.event("prompt", "c2", "2026-10-16T00:02:02Z", {**unk, "prompt": "Fix the tests"}); ev["session_id"] = "session-3"
        state.ingest(self.conn, 1, ev)
        self.assertEqual(self.conn.execute("SELECT frontend FROM sessions WHERE session_id='session-3'").fetchone()[0], "unknown")
        ev = self.event("tool_start", "c3", "2026-10-16T00:02:03Z", {**unk, "frontend": "cli", "tool": "shell"}); ev["session_id"] = "session-3"
        state.ingest(self.conn, 1, ev)
        self.assertEqual(self.conn.execute("SELECT frontend FROM sessions WHERE session_id='session-3'").fetchone()[0], "cli")
        ev = self.event("heartbeat", "c4", "2026-10-16T00:02:04Z", unk); ev["session_id"] = "session-3"
        state.ingest(self.conn, 1, ev)
        self.assertEqual(self.conn.execute("SELECT frontend FROM sessions WHERE session_id='session-3'").fetchone()[0], "cli")

    def test_session_name_sticks_and_is_exposed(self):
        # 세션 이름은 어떤 이벤트에 실려 와도 최신값으로 반영되고, 안 실린 이벤트가 지우지 않는다
        base = {"frontend": "cli", "collection_mode": "hooks"}
        state.ingest(self.conn, 1, self.event("session_start", "n1", "2026-10-01T00:00:01Z", {**base}))
        state.ingest(self.conn, 1, self.event("prompt", "n2", "2026-10-01T00:00:02Z",
                                               {**base, "prompt": "x", "session_name": "회의록 파이프라인"}))
        row = self.conn.execute("SELECT session_name FROM sessions").fetchone()
        self.assertEqual(row["session_name"], "회의록 파이프라인")
        # 이름 없이 오는 heartbeat·turn_done은 유지, 빈 문자열도 무시
        state.ingest(self.conn, 1, self.event("turn_done", "n3", "2026-10-01T00:00:03Z", {**base, "summary": "ok"}))
        state.ingest(self.conn, 1, self.event("heartbeat", "n4", "2026-10-01T00:00:04Z", {**base, "session_name": ""}))
        row = self.conn.execute("SELECT session_name FROM sessions").fetchone()
        self.assertEqual(row["session_name"], "회의록 파이프라인")
        # /rename으로 바뀐 이름은 다음 신호에 반영, 120자 상한
        state.ingest(self.conn, 1, self.event("prompt", "n5", "2026-10-01T00:00:05Z",
                                               {**base, "prompt": "y", "session_name": "새 이름" + "가" * 200}))
        row = self.conn.execute("SELECT session_name FROM sessions").fetchone()
        self.assertEqual(len(row["session_name"]), 120)
        self.assertTrue(row["session_name"].startswith("새 이름"))
        self.assertEqual(state.assemble(self.conn)["sessions"][0]["session_name"], row["session_name"])

    def test_synthetic_model_never_overwrites_real_model(self):
        # Claude Code 전사본의 <synthetic>(인증 만료·API 오류 메시지) 표식이 session_end에 실려 와도
        # 마지막 실제 모델을 지킨다. effort는 빈 값이라 원래부터 덮이지 않는다.
        base = {"frontend": "cli", "collection_mode": "hooks", "agent": "claude-code"}
        real = {**self.event("turn_done", "e1", "2026-09-14T00:00:01Z",
                             {"model": "claude-fable-5", "effort": "xhigh", "summary": "Done"}), **base}
        synthetic = {**self.event("session_end", "e2", "2026-09-16T00:00:02Z",
                                  {"model": "<synthetic>", "effort": "", "reason": "prompt_input_exit"}), **base}
        state.ingest(self.conn, 1, real)
        state.ingest(self.conn, 1, synthetic)
        row = self.conn.execute("SELECT model, effort FROM sessions WHERE session_id='session-1'").fetchone()
        self.assertEqual(row["model"], "claude-fable-5")
        self.assertEqual(row["effort"], "xhigh")

        # 처음부터 <synthetic>뿐인 세션(인증 만료로 응답이 전혀 없던 자동화 실행)은 모델을 비워 둔다
        first = {**self.event("session_end", "e3", "2026-09-16T00:00:03Z",
                              {"model": "<synthetic>", "reason": "other"}), **base, "session_id": "session-2"}
        state.ingest(self.conn, 1, first)
        row = self.conn.execute("SELECT model FROM sessions WHERE session_id='session-2'").fetchone()
        self.assertIsNone(row["model"])

    def test_old_codex_event_remains_marked_as_partial(self):
        state.ingest(
            self.conn,
            1,
            self.event("prompt", "old-1", "2026-08-14T00:00:01Z", {"prompt": "Old watcher"}),
        )
        session = state.assemble(self.conn)["sessions"][0]
        self.assertTrue(session["partial"])
        self.assertIsNone(session["collection_mode"])



class UnconfirmedOverlayTests(unittest.TestCase):
    """'신호 없음' 오버레이 (§4.1) — working은 TTL, 대기 상태는 기기 오프라인, 헤드리스는 종료 전까지 상태 무관 TTL."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        # 기기는 온라인(방금 신호) — 대기 상태의 기기 오프라인 규칙이 끼어들지 않게
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at) VALUES"
            " (1,'device-a','x',datetime('now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'))")
        stale = f"-{CFG.ttl_stale_min + 5} minutes"
        for sid, fe, st, rel in [
            ("cli-working-stale", "cli", "working", stale),
            ("cli-await-stale", "cli", "awaiting_input", stale),
            ("auto-await-stale", "auto", "awaiting_input", stale),
            ("auto-await-fresh", "auto", "awaiting_input", "-1 minutes"),
        ]:
            self.conn.execute(
                "INSERT INTO sessions (device_id, agent, session_id, frontend, state, turns, last_prompt,"
                " last_seen_hub, state_since, started_at) VALUES (1, 'claude-code', ?, ?, ?, 1, 'p',"
                " strftime('%Y-%m-%dT%H:%M:%SZ','now',?), strftime('%Y-%m-%dT%H:%M:%SZ','now',?),"
                " strftime('%Y-%m-%dT%H:%M:%SZ','now',?))",
                (sid, fe, st, rel, rel, rel))
        # 종료된 헤드리스 세션은 TTL과 무관 — 24시간 안이라 현황에 아직 보이는 동안에도 '신호 없음'이 아니다
        self.conn.execute(
            "INSERT INTO sessions (device_id, agent, session_id, frontend, state, end_reason, turns, last_prompt,"
            " last_seen_hub, state_since, started_at, ended_at) VALUES (1, 'claude-code', 'auto-ended-stale', 'auto',"
            " 'ended', 'other', 1, 'p', strftime('%Y-%m-%dT%H:%M:%SZ','now',?), strftime('%Y-%m-%dT%H:%M:%SZ','now',?),"
            " strftime('%Y-%m-%dT%H:%M:%SZ','now',?), strftime('%Y-%m-%dT%H:%M:%SZ','now',?))",
            (stale, stale, stale, stale))

    def tearDown(self):
        self.conn.close()

    def test_headless_sessions_go_stale_by_ttl_regardless_of_state(self):
        # 2026-10-14 사례: session_end를 잃은 claude -p 세션이 awaiting_input에 남았는데, 기기가 온라인이라
        # 대기 규칙으로는 영영 '신호 없음'이 되지 않아 대시보드의 종료 체크가 나오지 않았다
        flags = {s["session_id"]: s["unconfirmed"] for s in state.assemble(self.conn)["sessions"]}
        self.assertTrue(flags["auto-await-stale"])
        self.assertFalse(flags["auto-await-fresh"])
        self.assertFalse(flags["auto-ended-stale"])
        # 대화형 규칙은 그대로 — working만 TTL, 대기 상태는 기기 온라인이면 정상
        self.assertTrue(flags["cli-working-stale"])
        self.assertFalse(flags["cli-await-stale"])

    def test_rule_matrix(self):
        ttl = CFG.ttl_stale_min
        self.assertTrue(state.is_unconfirmed("awaiting_input", "auto", ttl + 1, True, ttl))
        self.assertFalse(state.is_unconfirmed("awaiting_input", "auto", ttl, True, ttl))      # 경계: 초과만
        self.assertTrue(state.is_unconfirmed("needs_approval", "auto", ttl + 1, True, ttl))
        self.assertFalse(state.is_unconfirmed("ended", "auto", ttl + 1, True, ttl))
        self.assertFalse(state.is_unconfirmed("awaiting_input", "cli", ttl + 1, True, ttl))
        self.assertTrue(state.is_unconfirmed("awaiting_input", "cli", 1, False, ttl))         # 기기 오프라인
        self.assertTrue(state.is_unconfirmed("working", None, ttl + 1, True, ttl))            # 프런트엔드 미기록(구형 수집)


if __name__ == "__main__":
    unittest.main()

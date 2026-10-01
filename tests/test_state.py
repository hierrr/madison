import json
import sqlite3
import unittest

from server import db, state


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


if __name__ == "__main__":
    unittest.main()

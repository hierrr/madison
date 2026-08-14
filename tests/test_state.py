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
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at) VALUES (1,'imac3','x',datetime('now'),datetime('now'))"
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

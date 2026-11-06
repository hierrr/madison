import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import Request, HTTPException

from server import app, auth, db, reconciliation, state, tokens


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        for dev in (1, 2):
            self.conn.execute("INSERT INTO devices(id,name,token_hash,created_at) VALUES (?,?,?,?)",
                              (dev, f"device-{dev}", auth.token_hash(f"token-{dev}"), "2026-10-01T00:00:00Z"))
        self.patcher = patch.object(db, "_conn", self.conn)
        self.patcher.start()
        self.send("initial", 100)

    def tearDown(self):
        self.patcher.stop()
        self.conn.close()

    def send(self, eid, count, *, agent="claude-code"):
        return state.ingest(self.conn, 1, {"agent": agent, "session_id": "s1", "event": "turn_done",
                            "event_id": eid, "ts": "2026-10-01T03:00:00Z", "project": "acme",
                            "detail": {"frontend": "cli", "model": "model-a", "tokens": {"cum": {"model-a": {"in": count}}}}})

    def payload(self, count=150, **fields):
        return {"agent": "claude-code", "session_id": "s1", "run_id": "audit-1", "source": "transcript",
                "complete": True, "samples": [{"ts": "2026-10-02T03:00:00Z", "model": "model-a", "cum": {"in": count}}], **fields}

    def apply(self, body=None, device=1):
        return reconciliation.reconcile(self.conn, device, body or self.payload())

    def total(self):
        return self.conn.execute("SELECT SUM(input) FROM token_daily").fetchone()[0]

    def request(self, body=None, device=1):
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        return Request({"type": "http", "method": "POST", "path": "/api/collector/reconcile",
                        "headers": [(b"authorization", f"Bearer token-{device}".encode())],
                        "client": ("127.0.0.1", 1234), "server": ("localhost", 8787), "scheme": "http"}, receive)

    def test_correction_keeps_state_and_turns_and_original_time(self):
        before = dict(self.conn.execute("SELECT * FROM sessions").fetchone())
        result = self.apply()
        after = dict(self.conn.execute("SELECT * FROM sessions").fetchone())
        self.assertEqual(result["added_total"], 50)
        for key in before:
            if key != "tokens_cum":
                self.assertEqual(before[key], after[key], key)
        self.assertEqual(self.total(), 150)
        self.assertEqual(self.conn.execute("SELECT input FROM token_hourly WHERE hour=?",
                                          (tokens.local_hour("2026-10-02T03:00:00Z"),)).fetchone()[0], 50)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_retry_and_new_audit_do_not_add_again(self):
        self.apply()
        self.apply()
        result = self.apply(self.payload(run_id="audit-2"))
        self.assertEqual(result["status"], "matched")
        self.assertEqual(self.total(), 150)

    def test_delayed_old_collector_cannot_regress_corrected_baseline(self):
        self.apply()
        self.send("late", 110)
        self.send("same", 150)
        self.assertEqual(self.total(), 150)
        self.send("next", 170)
        self.send("late-again", 155)
        self.send("same-again", 170)
        self.assertEqual(self.total(), 170)

    def test_codex_global_baseline_and_model_switch(self):
        self.send("codex", 100, agent="codex-cli")
        body = self.payload(agent="codex-cli")
        body["samples"][0]["model"] = "model-b"
        self.apply(body)
        self.send("codex-late", 110, agent="codex-cli")
        self.send("codex-next", 150, agent="codex-cli")
        self.assertEqual(self.total(), 250)
        row = self.conn.execute("SELECT model,input FROM token_daily WHERE agent='codex-cli' AND model='model-b'").fetchone()
        self.assertEqual(tuple(row), ("model-b", 50))

    def test_multi_hour_only_missing_tail_is_added(self):
        body = self.payload(samples=[
            {"ts": "2026-10-01T00:00:00Z", "model": "model-a", "cum": {"in": 90}},
            {"ts": "2026-10-01T04:00:00Z", "model": "model-a", "cum": {"in": 120}},
            {"ts": "2026-10-02T04:00:00Z", "model": "model-a", "cum": {"in": 150}}])
        self.apply(body)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT input FROM token_hourly ORDER BY hour")], [100, 20, 30])

    def test_regressed_and_incomplete_snapshots_are_not_applied(self):
        for body in [self.payload(80), self.payload(complete=False, issue="active_deferred"),
                     self.payload(source="last_session_cache"), self.payload(session_id="unknown")]:
            self.assertNotIn(self.apply(body)["status"], ("matched", "corrected"))
        self.assertEqual(self.total(), 100)

    def test_counter_reset_in_snapshot_is_reported(self):
        body = self.payload(samples=[
            {"ts": "2026-10-01T04:00:00Z", "model": "model-a", "cum": {"in": 160}},
            {"ts": "2026-10-02T04:00:00Z", "model": "model-a", "cum": {"in": 150}}])
        self.assertEqual(self.apply(body)["status"], "counter_conflict")
        self.assertEqual(self.total(), 100)

    def test_other_device_cannot_correct_this_session(self):
        self.assertEqual(self.apply(device=2)["status"], "untracked")
        self.assertEqual(self.total(), 100)

    def test_atomic_ledger_failure(self):
        with patch.object(tokens, "_upsert", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual(self.total(), 100)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM token_reconcile_floors").fetchone()[0], 0)

    def test_old_sessions_before_tracking_are_not_added(self):
        self.conn.execute("INSERT INTO sessions(device_id,agent,session_id,project) VALUES (1,'claude-code','old','acme')")
        body = self.payload(session_id="old")
        body["samples"][0]["ts"] = "2026-08-01T01:00:00Z"
        self.assertEqual(self.apply(body)["status"], "before_tracking")
        self.assertEqual(self.total(), 100)

    def test_session_with_no_prior_token_report_is_recovered(self):
        self.conn.execute("INSERT INTO sessions(device_id,agent,session_id,project,state,frontend,turns)"
                          " VALUES (1,'claude-code','lost-session','acme','ended','auto',0)")
        result = self.apply(self.payload(session_id="lost-session"))
        self.assertEqual(result["added_total"], 150)
        row = self.conn.execute("SELECT state,turns FROM sessions WHERE session_id='lost-session'").fetchone()
        self.assertEqual(tuple(row), ("ended", 0))

    def test_empty_snapshot_cannot_claim_no_usage_for_existing_tokens(self):
        self.assertEqual(self.apply(self.payload(samples=[]))["status"], "counter_conflict")
        self.assertEqual(self.total(), 100)

    async def test_api_validation_and_device_status_scope(self):
        body = self.payload()
        body["samples"][0]["cum"]["in"] = -1
        with self.assertRaises(HTTPException) as error:
            await app.reconcile_usage(self.request(body))
        self.assertEqual(error.exception.status_code, 400)
        status = await app.collector_status(self.request())
        self.assertEqual([r["name"] for r in status["devices"]], ["device-1"])
        self.assertIsNone(status["devices"][0]["version"])

    async def test_delayed_install_and_audit_results(self):
        version = reconciliation.release()["version"]
        await app.post_collector_status(self.request({"action": "installed", "version": version}))
        await app.post_collector_status(self.request({"action": "audit_started", "version": version, "run_id": "audit-1"}))
        await app.reconcile_usage(self.request(self.payload()))
        await app.post_collector_status(self.request({"action": "audit_finished", "version": version, "run_id": "audit-1", "inventory": {"claude_stats_cache": False}}))
        status = reconciliation.status(self.conn)
        self.assertNotIn("summary", status["devices"][0])
        self.assertEqual(status["devices"][0]["audit_state"], "finished")
        results = await app.collector_results(self.request())
        self.assertEqual(results["run_id"], "audit-1")
        self.assertEqual([(r["status"], r["added_total"]) for r in results["sessions"]], [("corrected", 50)])
        self.assertIsNone(status["devices"][1]["version"])

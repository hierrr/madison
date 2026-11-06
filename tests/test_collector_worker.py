import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collector_worker", ROOT / "collector/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "madison"
        self.directory.mkdir()
        (self.directory / "env").write_text("MADISON_URL=http://127.0.0.1:9\nMADISON_TOKEN=test\n")
        (self.directory / "version.json").write_text((ROOT / "collector/version.json").read_text())
        self.worker = worker.Worker(self.directory, user_root=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def transcript(self, values=(10, 20), *, sid="s1"):
        path = self.root / ".claude/projects/acme" / (sid + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps({"type": "assistant", "timestamp": f"2026-10-01T0{i}:00:00Z",
                        "message": {"model": "model-a", "usage": {"input_tokens": n, "output_tokens": 2}}}) + "\n"
                                for i, n in enumerate(values)))
        os.utime(path, (time.time() - 3600, time.time() - 3600))
        return path

    def enqueue(self, path=None):
        with patch.object(worker, "frontend", return_value="cli"):
            self.worker.enqueue("claude-code", {"session_id": "s1", "cwd": str(self.root),
                                "transcript_path": str(path or self.root / "missing"), "reason": "prompt_input_exit",
                                "prompt": "private content must not be saved"})
        return next(self.worker.queue.glob("*.json"))

    def test_enqueue_is_durable_without_reading_transcript_or_network(self):
        with patch.object(self.worker, "request", side_effect=AssertionError("network")):
            path = self.enqueue()
        job = json.loads(path.read_text())
        self.assertNotIn("prompt", job["hook"])
        self.assertEqual(job["hook"]["session_id"], "s1")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_snapshot_retains_original_hours_and_cumulative_values(self):
        snap = worker.snapshot(self.transcript(), "claude-code", "s1", "audit")
        self.assertTrue(snap["complete"])
        self.assertEqual([s["cum"]["in"] for s in snap["samples"]], [10, 30])
        self.assertEqual(snap["samples"][0]["ts"], "2026-10-01T00:00:00+00:00")

    def test_malformed_and_recent_sources_are_not_complete(self):
        path = self.transcript()
        with path.open("a") as stream:
            stream.write('{"partial":')
        self.assertFalse(worker.snapshot(path, "claude-code", "s1", "audit")["complete"])
        self.assertEqual(worker.snapshot(path, "claude-code", "s1", "audit", quiet_seconds=600)["issue"], "active_deferred")

    def test_codex_counter_and_model_are_preserved(self):
        path = self.root / "codex.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in [
            {"type": "turn_context", "payload": {"model": "model-b"}},
            {"timestamp": "2026-10-01T00:00:00Z", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20, "reasoning_output_tokens": 10}}}},
            {"timestamp": "2026-10-01T01:00:00Z", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 120, "cached_input_tokens": 70, "output_tokens": 25, "reasoning_output_tokens": 11}}}}
        ]))
        snap = worker.snapshot(path, "codex-cli", "s1", "audit")
        self.assertEqual(snap["samples"][-1]["cum"], {"in": 50, "out": 25, "cr": 70, "cw": 0, "th": 11})
        self.assertEqual(snap["samples"][-1]["model"], "model-b")

    def test_failed_end_is_retained_then_retried_with_same_id(self):
        path = self.enqueue(self.transcript())
        with patch.object(self.worker, "request", side_effect=OSError("offline")):
            self.worker.run()
        self.assertTrue(path.exists())
        eid = json.loads(path.read_text())["id"]
        calls = []
        def request(route, body=None):
            calls.append((route, body))
            return {"status": "corrected"} if route.endswith("reconcile") else {}
        with patch.object(self.worker, "request", side_effect=request):
            self.worker.run()
        self.assertFalse(path.exists())
        self.assertEqual(calls[0][0], "/api/events")
        self.assertEqual(calls[0][1]["event_id"], "shutdown-" + eid)
        self.assertNotIn("tokens", calls[0][1]["detail"])
        self.assertEqual(calls[1][1]["samples"][-1]["cum"]["in"], 30)

    def test_reconcile_failure_does_not_repeat_successful_end(self):
        path = self.enqueue(self.transcript())
        def fail_reconcile(route, body=None):
            if route.endswith("reconcile"):
                raise OSError("offline")
            return {}
        with patch.object(self.worker, "request", side_effect=fail_reconcile):
            self.worker.run()
        self.assertTrue(json.loads(path.read_text())["sent"])
        with patch.object(self.worker, "request", return_value={"status": "matched"}) as request:
            self.worker.run()
        self.assertEqual([c.args[0] for c in request.call_args_list], ["/api/collector/reconcile"])
        self.assertFalse(path.exists())

    def test_install_audits_once_and_force_can_repeat(self):
        self.transcript()
        calls = []
        def request(route, body=None):
            calls.append((route, body))
            if route.endswith("sessions"):
                return {"sessions": [{"agent": "claude-code", "session_id": "s1"}]}
            return {"status": "matched"}
        with patch.object(self.worker, "request", side_effect=request):
            self.worker.install()
            self.worker.run()
            self.worker.install()
            self.worker.run()
            self.assertEqual(sum(route.endswith("reconcile") for route, _ in calls), 1)
            self.worker.schedule_audit(force=True)
            self.worker.run()
        self.assertEqual(sum(route.endswith("reconcile") for route, _ in calls), 2)
        self.assertTrue((self.directory / "audit-done.json").exists())
        self.assertEqual(list(self.worker.queue.glob("*.json")), [])

    def test_audit_resumes_only_unfinished_sessions(self):
        self.transcript(sid="s1")
        self.transcript(sid="s2")
        uploaded = []
        def request(route, body=None):
            if route.endswith("sessions"):
                return {"sessions": [{"agent": "claude-code", "session_id": s} for s in ("s1", "s2")]}
            if route.endswith("reconcile"):
                if body["session_id"] == "s2" and uploaded == ["s1"]:
                    uploaded.append("failed")
                    raise OSError("offline")
                uploaded.append(body["session_id"])
            return {"status": "matched"}
        with patch.object(self.worker, "request", side_effect=request):
            self.worker.schedule_audit()
            self.worker.run()
            self.assertTrue((self.worker.queue / "audit.json").exists())
            self.worker.run()
        self.assertEqual(uploaded, ["s1", "failed", "s2"])

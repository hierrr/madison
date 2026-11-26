import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collector_worker", ROOT / "collector/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


# Claude Code·Codex 세션 안에서 돌리면 실제 계정 env가 섞인다 — 계정 판정에 영향을 주는 값은 모두 치운다.
AGENT_ENV_NAMES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENAI_API_KEY")
AGENT_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKE0000"
LOGIN = {"oauthAccount": {"emailAddress": "Alice@Example.com", "organizationType": "claude_max"}}


def agent_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in AGENT_ENV_NAMES and not k.startswith(AGENT_ENV_PREFIXES)}
    env.update(extra)
    return patch.dict(os.environ, env, clear=True)


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
        with patch.object(worker, "frontend", return_value="cli"), agent_env():
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

    # ── 계정 ────────────────────────────────────────────────
    def write_login(self, data=LOGIN, name=".claude.json", base=None):
        path = (base or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def enqueue_job(self, agent="claude-code", client="cli", env=None, hook=None, worker_obj=None):
        target = worker_obj or self.worker
        with patch.object(worker, "frontend", return_value=client), agent_env(**(env or {})):
            target.enqueue(agent, {"session_id": "s1", "cwd": str(self.root), "reason": "exit", **(hook or {})})
        return json.loads(next(target.queue.glob("*.json")).read_text())

    def test_enqueue_resolves_account_from_hook_process_env(self):
        self.write_login()
        job = self.enqueue_job()
        self.assertEqual(job["account"], "alice@example.com")
        self.assertTrue((self.directory / "cache").is_dir())   # 로그인 요약 캐시는 mad-dir/cache

    def test_enqueue_uses_hook_env_not_the_default_login(self):
        self.write_login()
        self.write_login({"oauthAccount": {"emailAddress": "Bob@Example.com", "organizationType": "claude_pro"}},
                         base=self.root / "cfg")
        job = self.enqueue_job(env={"CLAUDE_CONFIG_DIR": str(self.root / "cfg")})
        self.assertEqual(job["account"], "bob@example.com")

    def test_enqueue_headless_frontend_uses_headless_rule(self):
        tail = FAKE_KEY[-20:]
        self.write_login({**LOGIN, "customApiKeyResponses": {"rejected": [tail]}})
        interactive = self.enqueue_job(client="cli", env={"ANTHROPIC_API_KEY": FAKE_KEY})
        self.assertEqual(interactive["account"], "alice@example.com")
        for p in self.worker.queue.glob("*.json"):
            p.unlink()
        headless = self.enqueue_job(client="auto", env={"ANTHROPIC_API_KEY": FAKE_KEY})
        self.assertTrue(headless["account"].startswith("api:"))
        self.assertNotIn(FAKE_KEY, json.dumps(headless))
        self.assertNotIn(tail, json.dumps(headless))

    def test_enqueue_codex_account_follows_codex_home(self):
        def auth(email):
            import base64
            part = lambda v: base64.urlsafe_b64encode(json.dumps(v).encode()).decode().rstrip("=")
            return {"auth_mode": "chatgpt", "tokens": {"id_token": ".".join([part({"alg": "none"}), part({"email": email}), "c2ln"])}}
        self.write_login(auth("Carol@Example.com"), name="auth.json", base=self.root / ".codex")
        self.write_login(auth("Dave@Example.com"), name="auth.json", base=self.root / "alt")
        self.assertEqual(self.enqueue_job("codex-cli")["account"], "carol@example.com")
        for p in self.worker.queue.glob("*.json"):
            p.unlink()
        self.assertEqual(self.enqueue_job("codex-cli", env={"CODEX_HOME": str(self.root / "alt")})["account"],
                         "dave@example.com")

    def test_enqueue_omits_account_key_when_unknown(self):
        job = self.enqueue_job()   # 로그인 파일 없음
        self.assertNotIn("account", job)
        self.assertEqual(job["hook"]["session_id"], "s1")

    def test_send_end_posts_account_in_detail(self):
        self.write_login()
        job = self.enqueue_job()
        path = next(self.worker.queue.glob("*.json"))
        calls = []
        def request(route, body=None):
            calls.append((route, body))
            return {"status": "matched"}
        with patch.object(self.worker, "request", side_effect=request):
            self.assertTrue(self.worker.send_end(path, job))
        event = calls[0][1]
        self.assertEqual(calls[0][0], "/api/events")
        self.assertEqual(event["event"], "session_end")
        self.assertEqual(event["detail"]["account"], "alice@example.com")
        self.assertEqual(event["detail"]["frontend"], "cli")

    def test_send_end_runs_without_agent_env_using_the_stored_account(self):
        """전송은 env 없는 launchd 플러셔에서 일어난다 — 큐에 담긴 값만으로 보낸다(다시 구하지 않는다)."""
        self.write_login()
        job = self.enqueue_job()
        path = next(self.worker.queue.glob("*.json"))
        self.write_login({"oauthAccount": {"emailAddress": "Other@Example.com", "organizationType": "claude_max"}})
        calls = []
        with patch.object(self.worker, "request", side_effect=lambda r, b=None: calls.append(b) or {"status": "matched"}), \
                agent_env(CLAUDE_CODE_USE_BEDROCK="1"):
            self.worker.send_end(path, job)
        self.assertEqual(calls[0]["detail"]["account"], "alice@example.com")

    def test_old_job_without_account_key_is_processed_unchanged(self):
        job = self.enqueue_job()
        self.assertNotIn("account", job)
        path = next(self.worker.queue.glob("*.json"))
        calls = []
        with patch.object(self.worker, "request", side_effect=lambda r, b=None: calls.append(b) or {"status": "matched"}):
            self.worker.run()
        self.assertFalse(path.exists())
        self.assertNotIn("account", calls[0]["detail"])
        self.assertEqual(calls[0]["detail"]["reason"], "exit")

    def test_blank_or_non_string_account_in_job_is_not_sent(self):
        for value in ("", None, 7, ["x@example.com"]):
            job = self.enqueue_job()
            path = next(self.worker.queue.glob("*.json"))
            job["account"] = value
            calls = []
            with patch.object(self.worker, "request", side_effect=lambda r, b=None: calls.append(b) or {"status": "matched"}):
                self.worker.send_end(path, job)
            self.assertNotIn("account", calls[0]["detail"], repr(value))
            path.unlink()

    def test_enqueue_works_when_account_rule_file_is_missing(self):
        """부분 갱신으로 worker.py 옆에 account.py가 없는 기기 — 계정만 생략하고 정상 동작."""
        copy = self.root / "partial"
        copy.mkdir()
        shutil.copy(ROOT / "collector/worker.py", copy / "worker.py")
        spec = importlib.util.spec_from_file_location("collector_worker_partial", copy / "worker.py")
        partial = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(partial)
        self.write_login()
        other = partial.Worker(self.directory, user_root=self.root)
        with patch.object(partial, "frontend", return_value="cli"), agent_env():
            other.enqueue("claude-code", {"session_id": "s1", "cwd": str(self.root), "reason": "exit"})
        job = json.loads(next(other.queue.glob("*.json")).read_text())
        self.assertNotIn("account", job)
        self.assertEqual(job["hook"]["session_id"], "s1")
        self.assertIsNone(partial._account_module())

    def test_enqueue_works_when_account_rule_file_is_broken(self):
        copy = self.root / "broken"
        copy.mkdir()
        shutil.copy(ROOT / "collector/worker.py", copy / "worker.py")
        (copy / "account.py").write_text("raise RuntimeError('boom')\n")
        spec = importlib.util.spec_from_file_location("collector_worker_broken", copy / "worker.py")
        broken = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(broken)
        other = broken.Worker(self.directory, user_root=self.root)
        with patch.object(broken, "frontend", return_value="cli"), agent_env():
            other.enqueue("claude-code", {"session_id": "s1", "cwd": str(self.root)})
        self.assertNotIn("account", json.loads(next(other.queue.glob("*.json")).read_text()))

    def test_account_rule_is_loaded_from_beside_worker_not_from_sys_path(self):
        copy = self.root / "beside"
        copy.mkdir()
        shutil.copy(ROOT / "collector/worker.py", copy / "worker.py")
        shutil.copy(ROOT / "collector/account.py", copy / "account.py")
        spec = importlib.util.spec_from_file_location("collector_worker_beside", copy / "worker.py")
        beside = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(beside)
        module = beside._account_module()
        self.assertEqual(Path(module.__file__).resolve(), (copy / "account.py").resolve())
        self.assertIs(beside._account_module(), module)   # 한 번만 불러온다

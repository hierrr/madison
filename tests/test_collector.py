import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest


REPO = Path(__file__).resolve().parents[1]
REPORT = REPO / "collector" / "report.sh"
INSTALL = REPO / "collector" / "install.sh"


def executable(path: Path, text: str):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# Claude Code·Codex 세션 안에서 테스트를 돌리면 이 값들이 실제 계정으로 섞여 계정 판정이 달라진다.
AGENT_ENV_NAMES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENAI_API_KEY", "MADISON_SUPPRESS")
AGENT_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")


def jwt(claims: dict) -> str:
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join([part({"alg": "none"}), part(claims), "ZmFrZS1zaWduYXR1cmU"])


def sha8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


class CollectorTests(unittest.TestCase):
    @staticmethod
    def base_env():
        """호스트의 에이전트 설정이 섞이지 않은 하네스 env."""
        return {k: v for k, v in os.environ.items()
                if k not in AGENT_ENV_NAMES and not k.startswith(AGENT_ENV_PREFIXES)}

    def make_home(self, root: Path):
        home = root / "home"
        mad = home / ".claude" / "madison"
        (mad / "throttle").mkdir(parents=True)
        (mad / "bin").mkdir()
        (home / ".codex").mkdir()
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        (mad / "env").write_text(
            "MADISON_URL=http://madison.test\n"
            "MADISON_TOKEN=test-token\n"
            "MADISON_DEVICE=test-device\n"
            "MADISON_DEBUG=0\n"
        )
        return home, mad

    def fake_path(self, root: Path):
        bindir = root / "bin"
        bindir.mkdir()
        executable(
            bindir / "curl",
            "#!/bin/bash\n"
            "url=''; data=''; method='GET'; prev=''\n"
            "for a in \"$@\"; do\n"
            "  case \"$prev\" in\n"
            "    -X) method=\"$a\" ;;\n"
            "    --data-binary) data=\"$a\" ;;\n"
            "  esac\n"
            "  case \"$a\" in http*) url=\"$a\" ;; esac\n"
            "  prev=\"$a\"\n"
            "done\n"
            "if [ -n \"${MADISON_TEST_LOG:-}\" ]; then printf '%s %s\\n' \"$method\" \"$url\" >> \"$MADISON_TEST_LOG\"; fi\n"
            "if [ -n \"$data\" ] && [ -n \"${MADISON_TEST_CAPTURE:-}\" ]; then printf '%s' \"$data\" > \"$MADISON_TEST_CAPTURE\"; fi\n"
            "case \"$url\" in\n"
            "  *'/api/handoffs?mine=1'*) printf '%s' \"${MADISON_TEST_HANDOFFS:-[]}\" ;;\n"
            "  *'/api/collector/status') printf '200'; exit \"${MADISON_TEST_CURL_STATUS:-0}\" ;;\n"
            "  *) printf '200' ;;\n"
            "esac\n",
        )
        executable(bindir / "launchctl", "#!/bin/bash\nexit 0\n")
        # 부모 프로세스 탐색(프런트엔드 판별)을 결정적으로 만든다: 부모 명령줄은 MADISON_TEST_PCMD, 그 위는 없음
        executable(
            bindir / "ps",
            "#!/bin/bash\n"
            "case \"$*\" in\n"
            "  *'ppid='*) echo 1 ;;\n"
            "  *'command='*) printf '%s\\n' \"${MADISON_TEST_PCMD:-}\" ;;\n"
            "esac\n",
        )
        executable(
            bindir / "codex",
            "#!/bin/bash\n"
            "if [ \"${1:-}\" = 'features' ]; then printf 'hooks stable true\\n'; fi\n",
        )
        return bindir

    def run_report(self, originator: str, event: str, hook: dict, codex_index: str = ""):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            capture = root / "capture.json"
            if codex_index:
                (home / ".codex" / "session_index.jsonl").write_text(codex_index)
            transcript = root / "rollout.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"originator": originator, "source": "vscode" if originator == "Codex Desktop" else "cli"}}) + "\n"
                + json.dumps({"type": "turn_context", "payload": {"model": "gpt-rollout", "effort": "xhigh"}}) + "\n"
            )
            hook["transcript_path"] = str(transcript)
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}", "MADISON_TEST_CAPTURE": str(capture)})
            subprocess.run(
                ["bash", str(REPORT), event, "codex-cli"],
                input=json.dumps(hook), text=True, env=env, cwd=REPO, check=True,
            )
            return json.loads(capture.read_text())

    def run_report_with_transcript(self, agent: str, event: str, hook: dict, transcript_text: str):
        """임의 전사본으로 report.sh 실행 → 전송 payload. 토큰 추출 검증용."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            capture = root / "capture.json"
            transcript = root / "transcript.jsonl"
            transcript.write_text(transcript_text)
            hook["transcript_path"] = str(transcript)
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}", "MADISON_TEST_CAPTURE": str(capture)})
            subprocess.run(
                ["bash", str(REPORT), event, agent],
                input=json.dumps(hook), text=True, env=env, cwd=REPO, check=True,
            )
            return json.loads(capture.read_text())

    @staticmethod
    def assistant_line(model, usage):
        return json.dumps({"type": "assistant", "message": {"model": model, "usage": usage}})

    def test_claude_turn_done_carries_cumulative_tokens(self):
        usage_a = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1000,
                   "cache_creation_input_tokens": 200, "output_tokens_details": {"thinking_tokens": 30},
                   # iterations는 같은 값의 반복 기재 — 합산하면 이중 계상이므로 무시돼야 한다
                   "iterations": [{"input_tokens": 100, "output_tokens": 50}]}
        transcript = "\n".join([
            self.assistant_line("model-a", usage_a),
            # 사이드체인(서브에이전트)도 실소비 — 포함
            json.dumps({"type": "assistant", "isSidechain": True,
                        "message": {"model": "model-b", "usage": {"input_tokens": 10, "output_tokens": 5}}}),
            # 합성 메시지는 제외
            self.assistant_line("<synthetic>", {"input_tokens": 999, "output_tokens": 999}),
            self.assistant_line("model-a", {"input_tokens": 20, "output_tokens": 8}),
            json.dumps({"type": "user"}),
            "{broken json",   # 쓰다 만 마지막 라인 — 나머지 집계에 영향 없어야
        ]) + "\n"
        payload = self.run_report_with_transcript(
            "claude-code", "turn_done",
            {"session_id": "s1", "cwd": str(REPO), "last_assistant_message": "Done"},
            transcript)
        tok = payload["detail"]["tokens"]
        self.assertEqual(tok["v"], 1)
        self.assertEqual(tok["cum"]["model-a"], {"in": 120, "out": 58, "cr": 1000, "cw": 200, "th": 30})
        self.assertEqual(tok["cum"]["model-b"], {"in": 10, "out": 5, "cr": 0, "cw": 0, "th": 0})
        self.assertNotIn("<synthetic>", tok["cum"])

    def test_claude_model_fallback_skips_synthetic_lines(self):
        # 훅 입력에 model이 없으면 전사본 꼬리의 마지막 assistant 라인을 쓴다 — 인증 만료·API 오류 같은
        # <synthetic> 라인이 마지막이어도 직전 정상 응답의 모델을 골라야 한다
        hook = {"session_id": "s1", "cwd": str(REPO), "reason": "prompt_input_exit"}
        transcript = "\n".join([
            self.assistant_line("model-a", {"input_tokens": 20, "output_tokens": 8}),
            self.assistant_line("<synthetic>", {"input_tokens": 0, "output_tokens": 0}),
        ]) + "\n"
        payload = self.run_report_with_transcript("claude-code", "session_end", dict(hook), transcript)
        self.assertEqual(payload["detail"]["model"], "model-a")

        # 정상 응답이 하나도 없으면 빈 값 — 허브가 기존 값을 유지한다
        only_synthetic = self.assistant_line("<synthetic>", {"input_tokens": 0, "output_tokens": 0}) + "\n"
        payload = self.run_report_with_transcript("claude-code", "session_end", dict(hook), only_synthetic)
        self.assertEqual(payload["detail"]["model"], "")

    def test_claude_session_name_prefers_rename_over_auto_title(self):
        # /rename(custom-title)이 자동 제목(ai-title)보다 우선, 둘 다 마지막 기록이 이긴다. 줄바꿈은 공백으로
        hook = {"session_id": "s1", "cwd": str(REPO), "prompt": "hi"}
        transcript = "\n".join([
            json.dumps({"type": "ai-title", "aiTitle": "첫 자동 제목", "sessionId": "s1"}),
            json.dumps({"type": "custom-title", "customTitle": "옛 이름", "sessionId": "s1"}),
            json.dumps({"type": "user"}),
            json.dumps({"type": "ai-title", "aiTitle": "나중 자동 제목", "sessionId": "s1"}),
            json.dumps({"type": "custom-title", "customTitle": "새\n이름", "sessionId": "s1"}),
        ]) + "\n"
        payload = self.run_report_with_transcript("claude-code", "prompt", dict(hook), transcript)
        self.assertEqual(payload["detail"]["session_name"], "새 이름")

        # 이름을 안 정했으면 자동 제목(앱·/resume 목록의 그것) — 2026-10-01 결정: 자동 제목 포함
        only_ai = json.dumps({"type": "ai-title", "aiTitle": "자동 제목", "sessionId": "s1"}) + "\n"
        payload = self.run_report_with_transcript("claude-code", "prompt", dict(hook), only_ai)
        self.assertEqual(payload["detail"]["session_name"], "자동 제목")

        # 둘 다 없으면(헤드리스 실행 등) 키 자체가 없다 — 허브가 기존 값을 유지
        none = json.dumps({"type": "user"}) + "\n"
        payload = self.run_report_with_transcript("claude-code", "prompt", dict(hook), none)
        self.assertNotIn("session_name", payload["detail"])

    def test_codex_session_name_from_thread_index(self):
        index = "\n".join([
            json.dumps({"id": "other", "thread_name": "다른 스레드"}),
            json.dumps({"id": "codex-s1", "thread_name": "분석하고 전사 문제 파악"}),
        ]) + "\n"
        payload = self.run_report("codex-tui", "prompt", {"session_id": "codex-s1", "cwd": str(REPO), "prompt": "x"},
                                  codex_index=index)
        self.assertEqual(payload["detail"]["session_name"], "분석하고 전사 문제 파악")
        payload = self.run_report("codex-tui", "prompt", {"session_id": "codex-s2", "cwd": str(REPO), "prompt": "x"},
                                  codex_index=index)
        self.assertNotIn("session_name", payload["detail"])

    def test_codex_turn_done_uses_last_counter_and_splits_cached(self):
        transcript = "\n".join([
            json.dumps({"type": "session_meta", "payload": {"originator": "codex-tui", "source": "cli"}}),
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-rollout", "effort": "high"}}),
            json.dumps({"payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10,
                "reasoning_output_tokens": 4, "total_tokens": 110}}}}),
            # 마지막 카운터가 이긴다
            json.dumps({"payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 36,
                "reasoning_output_tokens": 22, "total_tokens": 336}}}}),
        ]) + "\n"
        payload = self.run_report_with_transcript(
            "codex-cli", "turn_done",
            {"session_id": "s1", "turn_id": "t1", "cwd": str(REPO), "model": "gpt-hook",
             "last_assistant_message": "Done"},
            transcript)
        self.assertEqual(payload["detail"]["tokens"]["cum"],
                         {"gpt-hook": {"in": 100, "out": 36, "cr": 200, "cw": 0, "th": 22}})

    def test_turn_done_without_token_data_omits_tokens_key(self):
        payload = self.run_report_with_transcript(
            "claude-code", "turn_done",
            {"session_id": "s1", "cwd": str(REPO), "last_assistant_message": "Done"},
            json.dumps({"type": "user"}) + "\n")
        self.assertNotIn("tokens", payload["detail"])
        self.assertEqual(payload["detail"]["summary"], "Done")

    def test_session_end_also_carries_tokens(self):
        payload = self.run_report_with_transcript(
            "claude-code", "session_end",
            {"session_id": "s1", "cwd": str(REPO), "reason": "exit"},
            self.assistant_line("model-a", {"input_tokens": 7, "output_tokens": 3}) + "\n")
        self.assertEqual(payload["detail"]["tokens"]["cum"]["model-a"]["in"], 7)

    def test_codex_cli_prompt_uses_hook_metadata(self):
        payload = self.run_report(
            "codex-tui",
            "prompt",
            {"session_id": "s1", "turn_id": "t1", "cwd": str(REPO), "model": "gpt-hook", "prompt": "Track this"},
        )
        self.assertEqual(payload["agent"], "codex-cli")
        self.assertEqual(payload["event_id"], "codex-s1-t1-prompt")
        self.assertEqual(payload["project"], "madison")
        self.assertEqual(payload["detail"]["frontend"], "cli")
        self.assertEqual(payload["detail"]["model"], "gpt-hook")
        self.assertEqual(payload["detail"]["effort"], "xhigh")
        self.assertEqual(payload["detail"]["collection_mode"], "hooks")

    def test_codex_app_turn_done_is_distinguished(self):
        payload = self.run_report(
            "Codex Desktop",
            "turn_done",
            {"session_id": "s2", "turn_id": "t2", "cwd": str(REPO), "last_assistant_message": "Done"},
        )
        self.assertEqual(payload["detail"]["frontend"], "app")
        self.assertEqual(payload["detail"]["summary"], "Done")
        self.assertEqual(payload["detail"]["model"], "gpt-rollout")

    def test_session_start_briefs_handoff_without_consuming(self):
        # 세션 시작 안내는 pending 핸드오프를 알려주기만 하고 상태(PATCH)는 건드리지 않는다.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            log = root / "curl.log"
            env = self.base_env()
            env.update({
                "HOME": str(home), "PATH": f"{bindir}:{env['PATH']}",
                "MADISON_TEST_CAPTURE": str(root / "capture.json"),
                "MADISON_TEST_LOG": str(log),
                "MADISON_TEST_HANDOFFS": json.dumps([{
                    "id": 7, "hf": "HF-007", "from_name": "studio", "repo": "madison",
                    "branch": "wip/x", "doc_path": "docs/handoffs/x.md", "summary": "테스트 이관",
                }]),
            })
            proc = subprocess.run(
                ["bash", str(REPORT), "session_start", "codex-cli"],
                input=json.dumps({"session_id": "s9", "cwd": str(REPO), "source": "startup"}),
                text=True, env=env, cwd=REPO, check=True, capture_output=True,
            )
            self.assertIn("HF-007", proc.stdout)
            self.assertIn("/pickup", proc.stdout)
            self.assertNotIn("PATCH", log.read_text())

    @unittest.skipUnless(sys.platform == "darwin", "osascript 알림은 macOS 전용")
    def test_flush_notifies_new_handoffs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            notified = root / "notify.log"
            executable(
                bindir / "osascript",
                # 호출당 1줄만 남긴다 — 마지막 인자가 알림 본문
                "#!/bin/bash\n"
                "for last in \"$@\"; do :; done\n"
                "printf '%s\\n' \"$last\" >> \"${MADISON_TEST_NOTIFY:-/dev/null}\"\n",
            )
            env = self.base_env()
            env.update({
                "HOME": str(home), "PATH": f"{bindir}:{env['PATH']}",
                "MADISON_TEST_NOTIFY": str(notified),
                "MADISON_TEST_HANDOFFS": json.dumps([{
                    "id": 3, "hf": "HF-003", "from_name": "studio", "repo": "madison",
                    "summary": "알림 테스트",
                }]),
            })
            for _ in range(2):
                subprocess.run(["bash", str(REPORT), "__flush"],
                               env=env, cwd=REPO, check=True, capture_output=True, text=True)
            lines = [line for line in notified.read_text().splitlines() if line]
            self.assertEqual(len(lines), 1)
            self.assertIn("HF-003", lines[0])
            self.assertEqual((mad / "notified-handoffs").read_text().strip(), "3")

    def test_installer_merges_hooks_and_repairs_old_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            wrapper = mad / "codex-notify-wrapper.sh"
            config = home / ".codex" / "config.toml"
            config.write_text(
                'model = "gpt-test"\n'
                '[mcp_servers.pencil]\n'
                'command = "pencil"\n'
                f'notify = ["{wrapper}"]\n'
            )
            (mad / "codex-orig-notify.json").write_text('["terminal-notifier"]')
            (mad / "bin" / "madison-codex-watch").write_text("old")
            wrapper.write_text("old")
            # 구형 P3 태스크 폴러(라벨 임의)는 plist 내용으로 식별해 제거, 무관한 잡은 보존
            la_dir = home / "Library" / "LaunchAgents"
            poller = la_dir / "com.example.madison.poll.plist"
            poller.write_text("<plist><dict><string>curl http://madison.test/api/tasks?mine=1</string></dict></plist>")
            keeper = la_dir / "com.other.job.plist"
            keeper.write_text("<plist><dict><key>Label</key><string>com.other.job</string></dict></plist>")
            existing_hooks = {
                "hooks": {
                    "PreToolUse": [{"matcher": "Custom", "hooks": [{"type": "command", "command": "custom-hook"}]}],
                    # 구버전 설치가 남긴 async 훅 — Codex가 스킵하므로 재실행 시 제거되어야 한다
                    "Stop": [{"hooks": [{"type": "command", "async": True, "timeout": 9,
                                         "command": "~/.claude/madison/report.sh turn_done codex-cli 2>/dev/null || true"}]}],
                }
            }
            hooks_path = home / ".codex" / "hooks.json"
            hooks_path.write_text(json.dumps(existing_hooks))
            settings_path = home / ".claude" / "settings.json"
            settings_path.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
                {"type": "command", "command": "~/.claude/madison/report.sh session_end 2>/dev/null || true", "statusMessage": "keep"},
                {"type": "command", "command": "custom-end", "timeout": 4}]}]}}))

            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}"})
            command = ["bash", str(INSTALL), "--name", "test-device", "--hub", "http://madison.test"]
            subprocess.run(command, env=env, cwd=REPO, check=True, capture_output=True, text=True)
            subprocess.run(command, env=env, cwd=REPO, check=True, capture_output=True, text=True)

            parsed = tomllib.loads(config.read_text())
            self.assertEqual(parsed["notify"], ["terminal-notifier"])
            self.assertNotIn("notify", parsed["mcp_servers"]["pencil"])
            self.assertFalse((mad / "bin" / "madison-codex-watch").exists())
            self.assertFalse(wrapper.exists())
            self.assertFalse(poller.exists())
            self.assertTrue(keeper.exists())

            hooks = json.loads(hooks_path.read_text())["hooks"]
            custom = [h for group in hooks["PreToolUse"] for h in group["hooks"] if h["command"] == "custom-hook"]
            self.assertEqual(len(custom), 1)
            for name in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop", "SessionEnd"):
                handlers = [h for group in hooks[name] for h in group["hooks"] if "madison" in h["command"]]
                self.assertEqual(len(handlers), 1, name)
                self.assertNotIn("async", handlers[0], name)
            stop_madison = [h for group in hooks["Stop"] for h in group.get("hooks", []) if "madison" in h["command"]]
            self.assertEqual(stop_madison[0]["timeout"], 5)
            claude_end = json.loads(settings_path.read_text())["hooks"]["SessionEnd"][0]["hooks"]
            self.assertEqual(claude_end[0]["timeout"], 10)
            self.assertEqual(claude_end[0]["statusMessage"], "keep")
            self.assertEqual(claude_end[1]["timeout"], 4)
            self.assertTrue((mad / "worker.py").exists())
            self.assertTrue((mad / "pending/install.json").exists())
            self.assertIn("WatchPaths", (la_dir / "dev.madison.flush.plist").read_text())

            for skill in ("handoff", "pickup"):
                self.assertTrue((home / ".codex" / "skills" / skill / "SKILL.md").exists(), skill)

    def test_installer_defaults_hub_from_env(self):
        # 등록된 기기에서 --hub 없이 재실행하면 env의 MADISON_URL을 쓴다 (placeholder로 가지 않음)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            log = root / "curl.log"
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}", "MADISON_TEST_LOG": str(log)})
            subprocess.run(["bash", str(INSTALL)], env=env, cwd=REPO, check=True, capture_output=True, text=True)
            log_text = log.read_text()
            self.assertIn("madison.test", log_text)
            self.assertNotIn("example.com", log_text)

    def test_installer_distinguishes_http_response_from_transfer_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _mad = self.make_home(root)
            bindir = self.fake_path(root)
            log = root / "curl.log"
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}",
                        "MADISON_TEST_LOG": str(log), "MADISON_TEST_CURL_STATUS": "28"})
            proc = subprocess.run(["bash", str(INSTALL)], env=env, cwd=REPO,
                                  check=True, capture_output=True, text=True)
            self.assertIn("HTTP=200, curl=28", proc.stdout)
            self.assertNotIn("200000", proc.stdout)
            self.assertIn("/api/collector/status", log.read_text())
            self.assertNotIn("/api/state", log.read_text())

    def test_installer_rerun_refreshes_skills_without_reenroll(self):
        # 허브 갱신 뒤 등록된 기기에서 재실행: enroll 없이 env(토큰)를 보존하고, 두 에이전트의 설치된 스킬을 정본으로 덮는다
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            log = root / "curl.log"
            original_env = (mad / "env").read_bytes()
            for agent in (".claude", ".codex"):
                for skill in ("handoff", "pickup"):
                    installed = home / agent / "skills" / skill / "SKILL.md"
                    installed.parent.mkdir(parents=True, exist_ok=True)
                    installed.write_text("구버전 스킬\n")
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}", "MADISON_TEST_LOG": str(log)})
            subprocess.run(["bash", str(INSTALL)], env=env, cwd=REPO, check=True, capture_output=True, text=True)
            self.assertNotIn("/api/enroll", log.read_text())
            self.assertEqual((mad / "env").read_bytes(), original_env)
            for agent in (".claude", ".codex"):
                for skill in ("handoff", "pickup"):
                    self.assertEqual((home / agent / "skills" / skill / "SKILL.md").read_bytes(),
                                     (REPO / "collector" / "skills" / skill / "SKILL.md").read_bytes(), f"{agent}/{skill}")

    HERMES_CODE_FILES = ("__init__.py", "madison_common.py", "plugin.yaml")
    HERMES_RUNTIME_FILES = {
        "env": b"MADISON_URL=http://plugin-hub.test\nMADISON_TOKEN=plugin-token\n",
        "state.json": b'{"cursor": 7}\n',
        "spool.jsonl": b'{"event": "queued"}\n',
    }

    def install_with_hermes_plugin(self, status=None, disable_rc=0, installed=True,
                                   enable_failures=0, broken_download=False):
        """임시 HOME에 (선택) 구버전 hermes 플러그인을 두고 실제 install.sh를 실행.
        status: fake hermes CLI의 `plugins info madison` Status 값 (None이면 CLI 자체가 없음).
        enable_failures: `plugins enable madison`이 처음 N번 실패한다.
        broken_download: collector 복사본에서 hermes/madison_common.py를 빼 curl 폴백을 강제하고,
        가짜 curl이 hermes 경로 요청을 전부 실패시킨다 (다운로드 중간 실패)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _mad = self.make_home(root)
            bindir = self.fake_path(root)
            plugin = home / ".hermes" / "plugins" / "madison"
            if installed:
                plugin.mkdir(parents=True)
                for name in self.HERMES_CODE_FILES:
                    (plugin / name).write_text("stale\n")
                    (plugin / name).chmod(0o600)
                for name, data in self.HERMES_RUNTIME_FILES.items():
                    (plugin / name).write_bytes(data)
                (plugin / ".madison-download.leftover").write_text("partial\n")
                (plugin / ".madison-stage.leftover").mkdir()
                (plugin / ".madison-stage.leftover" / "__init__.py").write_text("partial\n")
            hermes_log = root / "hermes.log"
            if status is not None:
                executable(
                    bindir / "hermes",
                    "#!/bin/bash\n"
                    "echo \"$*\" >> \"$MADISON_TEST_HERMES_LOG\"\n"
                    "case \"$*\" in\n"
                    f"  'plugins info madison') printf 'Name: madison\\nStatus: {status}\\n' ;;\n"
                    f"  'plugins disable madison') exit {disable_rc} ;;\n"
                    "  'plugins enable madison')\n"
                    "    n=$(cat \"$MADISON_TEST_HERMES_LOG.enable\" 2>/dev/null || echo 0)\n"
                    "    echo $((n + 1)) > \"$MADISON_TEST_HERMES_LOG.enable\"\n"
                    "    [ \"$n\" -lt \"$MADISON_TEST_ENABLE_FAILS\" ] && exit 1 ;;\n"
                    "esac\n"
                    "exit 0\n",
                )
            install = INSTALL
            extra_dirs = []
            if broken_download:
                src = root / "collector"
                shutil.copytree(REPO / "collector", src, ignore=shutil.ignore_patterns("__pycache__"))
                (src / "hermes" / "madison_common.py").unlink()
                install = src / "install.sh"
                failing = root / "failing-bin"
                failing.mkdir()
                real_curl = bindir / "curl"
                executable(
                    failing / "curl",
                    "#!/bin/bash\n"
                    "case \"$*\" in *'/collector/hermes/'*) exit 22 ;; esac\n"
                    f"exec '{real_curl}' \"$@\"\n",
                )
                extra_dirs = [str(bindir)]
                bindir = failing
            # 호스트에 실제 hermes가 있어도 영향받지 않도록 PATH에서 제외한다
            path_dirs = [d for d in os.environ["PATH"].split(os.pathsep) if d and not (Path(d) / "hermes").exists()]
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": os.pathsep.join([str(bindir)] + extra_dirs + path_dirs),
                        "MADISON_TEST_HERMES_LOG": str(hermes_log),
                        "MADISON_TEST_ENABLE_FAILS": str(enable_failures)})
            proc = subprocess.run(["bash", str(install)], env=env, cwd=REPO, capture_output=True, text=True)
            calls = hermes_log.read_text().splitlines() if hermes_log.exists() else []
            runtime = {name: (plugin / name).read_bytes() for name in self.HERMES_RUNTIME_FILES if (plugin / name).exists()}
            code = {name: (plugin / name).read_bytes() for name in self.HERMES_CODE_FILES if (plugin / name).exists()}
            leftovers = list(plugin.glob(".madison-*")) if plugin.exists() else []
            modes = {name: stat.S_IMODE((plugin / name).stat().st_mode) for name in self.HERMES_CODE_FILES if (plugin / name).exists()}
            return {"proc": proc, "calls": calls, "runtime": runtime, "code": code, "modes": modes,
                    "leftovers": leftovers, "plugin_exists": plugin.exists()}

    def assert_hermes_code_refreshed(self, result):
        for name in self.HERMES_CODE_FILES:
            self.assertEqual(result["code"][name], (REPO / "collector" / "hermes" / name).read_bytes(), name)
        self.assertEqual(result["runtime"], self.HERMES_RUNTIME_FILES)  # 토큰·런타임 상태는 그대로
        self.assertEqual(result["leftovers"], [])

    def test_installer_refreshes_enabled_hermes_plugin_and_reloads(self):
        r = self.install_with_hermes_plugin(status="enabled")
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr)
        self.assert_hermes_code_refreshed(r)
        self.assertEqual(r["calls"], ["plugins info madison", "plugins disable madison", "plugins enable madison"])
        self.assertIn("hermes 리포터 플러그인 갱신·리로드", r["proc"].stdout)

    def test_installer_refreshes_disabled_hermes_plugin_without_reload(self):
        r = self.install_with_hermes_plugin(status="disabled")
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr)
        self.assert_hermes_code_refreshed(r)
        self.assertEqual(r["calls"], ["plugins info madison"])  # disable/enable 호출 없음
        self.assertIn("리로드 없이 파일만 교체", r["proc"].stdout)

    def test_installer_refreshes_hermes_plugin_files_when_cli_missing(self):
        r = self.install_with_hermes_plugin(status=None)
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr)
        self.assert_hermes_code_refreshed(r)
        self.assertEqual(r["calls"], [])
        self.assertIn("리로드 없이 파일만 교체", r["proc"].stdout)

    def test_installer_continues_when_hermes_plugin_reload_fails(self):
        r = self.install_with_hermes_plugin(status="enabled", disable_rc=1)
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr)
        self.assert_hermes_code_refreshed(r)
        self.assertEqual(r["calls"], ["plugins info madison", "plugins disable madison"])  # 실패하면 enable로 가지 않음
        self.assertIn("경고", r["proc"].stdout)
        self.assertIn("리로드 실패", r["proc"].stdout)
        self.assertNotIn("갱신·리로드 (v", r["proc"].stdout)

    def test_installer_keeps_hermes_plugin_untouched_when_a_download_fails(self):
        # 3개 중 하나라도 못 받으면 아무것도 교체하지 않는다 (all-or-nothing)
        r = self.install_with_hermes_plugin(status="enabled", broken_download=True)
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr)
        for name in self.HERMES_CODE_FILES:
            self.assertEqual(r["code"][name], b"stale\n", name)
        self.assertEqual(r["runtime"], self.HERMES_RUNTIME_FILES)
        self.assertEqual(r["calls"], [])  # hermes CLI를 호출하지 않는다
        self.assertEqual(r["leftovers"], [])  # 스테이징 폴더·임시 파일 정리
        self.assertIn("경고: hermes 플러그인 갱신 실패 — 기존 파일 유지, 리로드 생략", r["proc"].stdout)

    def test_installer_retries_hermes_plugin_enable_once(self):
        r = self.install_with_hermes_plugin(status="enabled", enable_failures=1)
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr)
        self.assert_hermes_code_refreshed(r)
        self.assertEqual(r["calls"], ["plugins info madison", "plugins disable madison",
                                      "plugins enable madison", "plugins enable madison"])
        self.assertIn("hermes 리포터 플러그인 갱신·리로드", r["proc"].stdout)
        self.assertNotIn("비활성화 상태", r["proc"].stdout)

    def test_installer_warns_when_hermes_plugin_stays_disabled_after_retry(self):
        r = self.install_with_hermes_plugin(status="enabled", enable_failures=2)
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr)
        self.assert_hermes_code_refreshed(r)
        self.assertEqual(r["calls"], ["plugins info madison", "plugins disable madison",
                                      "plugins enable madison", "plugins enable madison"])
        self.assertIn("경고: hermes 플러그인이 현재 비활성화 상태입니다", r["proc"].stdout)
        self.assertIn("hermes plugins enable madison", r["proc"].stdout)
        self.assertNotIn("갱신·리로드 (v", r["proc"].stdout)

    def test_installer_sets_hermes_plugin_files_to_mode_0644(self):
        r = self.install_with_hermes_plugin(status="disabled")  # 기존 파일은 0600으로 시작
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr)
        self.assertEqual(r["modes"], {name: 0o644 for name in self.HERMES_CODE_FILES})

    def test_installer_does_not_create_hermes_plugin_dir_when_not_installed(self):
        r = self.install_with_hermes_plugin(status="enabled", installed=False)
        self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr)
        self.assertFalse(r["plugin_exists"])
        self.assertEqual(r["calls"], [])  # 미설치 기기에서는 hermes CLI도 건드리지 않는다

    def test_hub_serves_hermes_plugin_code_files_but_not_env_example(self):
        from server import app as hub
        for name in self.HERMES_CODE_FILES:
            rel = f"hermes/{name}"
            self.assertIn(rel, hub.COLLECTOR_FILES)
            self.assertTrue((hub.COLLECTOR_DIR / rel).is_file(), rel)
        self.assertNotIn("hermes/env.example", hub.COLLECTOR_FILES)

    def test_installer_requires_hub_on_first_install(self):
        # 첫 설치(env 없음)에서 --hub 미지정이면 터널 기본값으로 가지 않고 안내와 함께 거절한다
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            (mad / "env").unlink()
            bindir = self.fake_path(root)
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}"})
            proc = subprocess.run(
                ["bash", str(INSTALL), "--name", "test-device", "--secret", "s3"],
                env=env, cwd=REPO, capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--hub", proc.stderr)
            self.assertIn("내부망", proc.stderr)

    def test_installer_switches_hub_url_without_reenroll(self):
        # 등록된 기기에서 --hub를 명시해 재실행하면 재등록 없이 MADISON_URL만 바뀐다 (토큰 유지)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            log = root / "curl.log"
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}", "MADISON_TEST_LOG": str(log)})
            subprocess.run(
                ["bash", str(INSTALL), "--hub", "http://lan.test:8787"],
                env=env, cwd=REPO, check=True, capture_output=True, text=True,
            )
            env_text = (mad / "env").read_text()
            self.assertIn("MADISON_URL=http://lan.test:8787\n", env_text)
            self.assertIn("MADISON_TOKEN=test-token\n", env_text)
            self.assertNotIn("/api/enroll", log.read_text())

    def test_installer_repairs_notify_rechained_by_computer_use(self):
        # Computer Use가 MADISON 래퍼 위에 재체이닝하며 경로를 JSON 이스케이프(\/)로 품은 실측 케이스:
        # 래퍼 참조가 남은 notify를 제거하고 보존해둔 원본 notify를 복원해야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            config = home / ".codex" / "config.toml"
            wrapper_escaped = str(mad / "codex-notify-wrapper.sh").replace("/", "\\\\/")
            config.write_text(
                'model = "gpt-test"\n'
                'notify = [ "/Apps/SkyComputerUseClient", "turn-ended", "--previous-notify", "[\\"'
                + wrapper_escaped + '\\"]" ]\n'
            )
            (mad / "codex-orig-notify.json").write_text('["/Apps/SkyComputerUseClient", "turn-ended"]')

            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}"})
            subprocess.run(
                ["bash", str(INSTALL), "--name", "test-device", "--hub", "http://madison.test"],
                env=env, cwd=REPO, check=True, capture_output=True, text=True,
            )
            parsed = tomllib.loads(config.read_text())
            self.assertEqual(parsed["notify"], ["/Apps/SkyComputerUseClient", "turn-ended"])

    def test_suppressed_hub_worker_sends_nothing(self):
        """허브 워커(MADISON_SUPPRESS=1)는 이벤트를 보내지 않는다 — 토큰 원장이 워커를 llm_runs로만 계상하는 전제."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, _mad = self.make_home(root)
            bindir = self.fake_path(root)
            capture = root / "capture.json"
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}",
                        "MADISON_TEST_CAPTURE": str(capture), "MADISON_SUPPRESS": "1"})
            subprocess.run(["bash", str(REPORT), "prompt", "claude-code"],
                           input=json.dumps({"session_id": "s1", "prompt": "hi"}), text=True,
                           env=env, cwd=REPO, check=True)
            self.assertFalse(capture.exists())

    def test_windows_collector_has_the_same_suppress_early_exit(self):
        """report.ps1도 report.sh처럼 env 파일을 읽기 전에 MADISON_SUPPRESS=1이면 조용히 종료한다(실행 검증은 Windows 필요)."""
        ps1 = (REPO / "collector" / "report.ps1").read_text(encoding="utf-8")
        guard = ps1.index('if ($env:MADISON_SUPPRESS -eq "1") { Bye }')
        self.assertLess(guard, ps1.index("Test-Path $EnvFile"))
        sh = REPORT.read_text(encoding="utf-8")
        self.assertIn('[ "${MADISON_SUPPRESS:-0}" = "1" ] && exit 0', sh)


    # ── 계정 (detail.account) ───────────────────────────────
    CLAUDE_LOGIN = {"oauthAccount": {"emailAddress": "Alice@Example.com", "organizationType": "claude_max"}}
    CODEX_AUTH = {"auth_mode": "chatgpt",
                  "tokens": {"id_token": jwt({"email": "Carol@Example.com"}), "access_token": "fake-access"}}
    FAKE_API_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKE0000"
    TS_FORMAT = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{3})?Z$"

    def run_hook(self, agent, event, hook=None, *, files=None, extra_env=None, pcmd="", report=REPORT,
                 originator="codex-tui", prepare=None):
        """임시 HOME에서 report.sh를 실행해 전송 payload를 돌려준다(전송 안 했으면 None).
        files: root 기준 상대경로 → 내용(dict는 JSON). extra_env 값의 {root}는 임시 루트로 치환."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            capture = root / "capture.json"
            for rel, content in (files or {}).items():
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content if isinstance(content, str) else json.dumps(content))
            hook = dict(hook or {})
            hook.setdefault("session_id", "s1")
            hook.setdefault("cwd", str(REPO))
            if agent == "codex-cli":
                transcript = root / "rollout.jsonl"
                transcript.write_text(json.dumps({"type": "session_meta", "payload": {"originator": originator}}) + "\n")
                hook["transcript_path"] = str(transcript)
            if prepare:
                prepare(root, home, mad, bindir)
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}", "MADISON_TEST_CAPTURE": str(capture),
                        "MADISON_TEST_PCMD": pcmd})
            env.update({k: v.replace("{root}", str(root)) for k, v in (extra_env or {}).items()})
            subprocess.run(["bash", str(report), event, agent], input=json.dumps(hook), text=True,
                           env=env, cwd=REPO, check=True)
            if not capture.exists():
                return None
            payload = json.loads(capture.read_text())
            payload["_mad"] = {p.name: p.read_text() for p in (mad / "cache").glob("*")} if (mad / "cache").is_dir() else {}
            return payload

    def test_account_is_attached_to_session_start_prompt_and_turn_done_only(self):
        files = {"home/.claude.json": self.CLAUDE_LOGIN}
        for event in ("session_start", "prompt", "turn_done"):
            payload = self.run_hook("claude-code", event, files=files)
            self.assertEqual(payload["detail"]["account"], "alice@example.com", event)
            self.assertRegex(payload["ts"], self.TS_FORMAT)
        for event in ("heartbeat", "tool_start", "permission_request", "idle", "subagent_stop", "session_end"):
            payload = self.run_hook("claude-code", event, files=files)
            self.assertNotIn("account", payload["detail"], event)
            self.assertRegex(payload["ts"], self.TS_FORMAT)

    def test_account_is_omitted_when_unknown(self):
        for files in (None, {"home/.claude.json": "{not json"}, {"home/.claude.json": {"projects": {}}}):
            payload = self.run_hook("claude-code", "prompt", {"prompt": "hi"}, files=files)
            self.assertNotIn("account", payload["detail"])
            self.assertEqual(payload["detail"]["prompt"], "hi")
            self.assertRegex(payload["ts"], self.TS_FORMAT)

    def test_claude_config_dir_is_honoured(self):
        files = {"home/.claude.json": self.CLAUDE_LOGIN,
                 "cfg/.claude.json": {"oauthAccount": {"emailAddress": "Bob@Example.com", "organizationType": "claude_pro"}}}
        payload = self.run_hook("claude-code", "session_start", {"source": "startup"}, files=files,
                                extra_env={"CLAUDE_CONFIG_DIR": "{root}/cfg"})
        self.assertEqual(payload["detail"]["account"], "bob@example.com")

    def test_api_login_modes_and_headless_detection(self):
        tail = self.FAKE_API_KEY[-20:]
        rejected = {"home/.claude.json": {**self.CLAUDE_LOGIN, "customApiKeyResponses": {"rejected": [tail]}}}
        env = {"ANTHROPIC_API_KEY": self.FAKE_API_KEY}
        # 대화형: 사용자가 거절한 키는 쓰이지 않으므로 구독 이메일
        payload = self.run_hook("claude-code", "prompt", {"prompt": "x"}, files=rejected, extra_env=env)
        self.assertEqual(payload["detail"]["account"], "alice@example.com")
        self.assertEqual(payload["detail"]["frontend"], "cli")
        # 프런트엔드가 auto(claude -p)면 headless로 판정 — 프롬프트가 없으므로 키를 그대로 쓴다
        payload = self.run_hook("claude-code", "prompt", {"prompt": "x"}, files=rejected, extra_env=env,
                                pcmd="claude -p")
        self.assertEqual(payload["detail"]["frontend"], "auto")
        self.assertEqual(payload["detail"]["account"], "api:" + sha8(self.FAKE_API_KEY))
        # 거절 기록이 없으면 대화형에서도 키
        payload = self.run_hook("claude-code", "prompt", {"prompt": "x"},
                                files={"home/.claude.json": self.CLAUDE_LOGIN}, extra_env=env)
        self.assertEqual(payload["detail"]["account"], "api:" + sha8(self.FAKE_API_KEY))
        # 클라우드 플래그와 AUTH_TOKEN
        payload = self.run_hook("claude-code", "prompt", {"prompt": "x"}, files=rejected,
                                extra_env={"CLAUDE_CODE_USE_BEDROCK": "1"})
        self.assertEqual(payload["detail"]["account"], "api:bedrock")
        payload = self.run_hook("claude-code", "prompt", {"prompt": "x"}, files=rejected,
                                extra_env={"ANTHROPIC_AUTH_TOKEN": "fake-token"})
        self.assertEqual(payload["detail"]["account"], "api:" + sha8("fake-token"))

    def test_account_never_carries_secrets_into_event_or_cache(self):
        files = {"home/.claude.json": {**self.CLAUDE_LOGIN,
                                       "customApiKeyResponses": {"rejected": [self.FAKE_API_KEY[-20:]]}}}
        payload = self.run_hook("claude-code", "session_start", {"source": "startup"}, files=files,
                                extra_env={"ANTHROPIC_API_KEY": self.FAKE_API_KEY})
        self.assertNotIn(self.FAKE_API_KEY[-20:], json.dumps(payload["detail"]))
        self.assertTrue(payload["_mad"], "캐시 파일이 만들어져야 한다")
        for text in payload["_mad"].values():
            self.assertNotIn(self.FAKE_API_KEY[-20:], text)

    def test_codex_events_carry_account_and_honour_codex_home(self):
        files = {"home/.codex/auth.json": self.CODEX_AUTH,
                 "alt/auth.json": {"auth_mode": "chatgpt", "tokens": {"id_token": jwt({"email": "Dave@Example.com"})}}}
        for event in ("session_start", "prompt", "turn_done"):
            payload = self.run_hook("codex-cli", event, {"turn_id": "t1"}, files=files)
            self.assertEqual(payload["detail"]["account"], "carol@example.com", event)
        payload = self.run_hook("codex-cli", "heartbeat", {"turn_id": "t1", "tool_use_id": "u1"}, files=files)
        self.assertNotIn("account", payload["detail"])
        payload = self.run_hook("codex-cli", "prompt", {"turn_id": "t1"}, files=files,
                                extra_env={"CODEX_HOME": "{root}/alt"})
        self.assertEqual(payload["detail"]["account"], "dave@example.com")
        payload = self.run_hook("codex-cli", "prompt", {"turn_id": "t1"})   # auth.json 없음
        self.assertNotIn("account", payload["detail"])

    def test_codex_session_name_follows_codex_home(self):
        index = json.dumps({"id": "s1", "thread_name": "대체 홈의 이름"}) + "\n"
        payload = self.run_hook("codex-cli", "prompt", {"turn_id": "t1"}, files={"alt/session_index.jsonl": index},
                                extra_env={"CODEX_HOME": "{root}/alt"})
        self.assertEqual(payload["detail"]["session_name"], "대체 홈의 이름")

    def test_hook_without_account_rule_file_still_reports_with_plain_timestamp(self):
        """구버전 설치(account.py 없음)·깨진 account.py: TS는 기존 방식, 계정은 생략, 이벤트는 정상 전송."""
        for content in (None, "import sys\nsys.exit(1)\n", "print('garbage')\n", "raise RuntimeError('boom')\n"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home, mad = self.make_home(root)
                bindir = self.fake_path(root)
                (home / ".claude.json").write_text(json.dumps(self.CLAUDE_LOGIN))
                installed = root / "installed"   # report.sh 한 파일만 있는 부분 갱신 상태
                installed.mkdir()
                shutil.copy(REPORT, installed / "report.sh")
                if content is not None:
                    (installed / "account.py").write_text(content)
                capture = root / "capture.json"
                env = self.base_env()
                env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}", "MADISON_TEST_CAPTURE": str(capture)})
                subprocess.run(["bash", str(installed / "report.sh"), "prompt", "claude-code"],
                               input=json.dumps({"session_id": "s1", "cwd": str(REPO), "prompt": "hi"}),
                               text=True, env=env, cwd=REPO, check=True)
                payload = json.loads(capture.read_text())
                self.assertNotIn("account", payload["detail"], repr(content))
                self.assertEqual(payload["detail"]["prompt"], "hi")
                self.assertRegex(payload["ts"], self.TS_FORMAT, repr(content))

    def test_hook_python_call_count_is_unchanged(self):
        """TS 계산 호출을 account.py --ts로 바꿨을 뿐이라 이벤트당 python3 호출은 1회."""
        for event, expected in (("session_start", "account.py"), ("prompt", "account.py"), ("turn_done", "account.py"),
                                ("heartbeat", "-c"), ("tool_start", "-c")):
            self._assert_one_python_call(event, expected)

    def _assert_one_python_call(self, event, expected_script):
        real = shutil.which("python3")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            executable(bindir / "python3", f"#!/bin/bash\necho \"$1\" >> '{root}/py.log'\nexec '{real}' \"$@\"\n")
            (home / ".claude.json").write_text(json.dumps(self.CLAUDE_LOGIN))
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}",
                        "MADISON_TEST_CAPTURE": str(root / "capture.json")})
            subprocess.run(["bash", str(REPORT), event, "claude-code"],
                           input=json.dumps({"session_id": "s1", "cwd": str(REPO), "prompt": "x", "source": "startup"}),
                           text=True, env=env, cwd=REPO, check=True)
            calls = (root / "py.log").read_text().splitlines()
            self.assertEqual(len(calls), 1, (event, calls))
            self.assertTrue(calls[0].endswith(expected_script), (event, calls))

    def test_installer_places_account_rule_before_report_and_creates_cache(self):
        text = INSTALL.read_text(encoding="utf-8")
        fetches = [line for line in text.splitlines() if line.startswith("fetch ")]
        self.assertTrue(fetches[0].startswith("fetch account.py "), fetches[0])
        self.assertLess(text.index("fetch account.py"), text.index("fetch report.sh"))
        self.assertLess(text.index("fetch account.py"), text.index("fetch worker.py"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}"})
            subprocess.run(["bash", str(INSTALL)], env=env, cwd=REPO, check=True, capture_output=True, text=True)
            self.assertEqual((mad / "account.py").read_bytes(), (REPO / "collector" / "account.py").read_bytes())
            self.assertTrue((mad / "cache").is_dir())
            self.assertEqual(stat.S_IMODE((mad / "cache").stat().st_mode), 0o700)
            self.assertEqual(json.loads((mad / "version.json").read_text())["version"],
                             json.loads((REPO / "collector" / "version.json").read_text())["version"])

    def test_installer_aborts_before_replacing_anything_when_account_rule_cannot_be_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            # 로컬 체크아웃 복사본에서 account.py를 빼 허브 다운로드 경로를 강제하고, 그 다운로드를 실패시킨다
            src = root / "collector"
            shutil.copytree(REPO / "collector", src, ignore=shutil.ignore_patterns("__pycache__"))
            (src / "account.py").unlink()
            failing = root / "failing-bin"
            failing.mkdir()
            executable(failing / "curl",
                       "#!/bin/bash\n"
                       "case \"$*\" in *'/collector/account.py'*) exit 22 ;; esac\n"
                       f"exec '{bindir / 'curl'}' \"$@\"\n")
            stale = {"worker.py": "old worker\n", "report.sh": "old report\n", "version.json": '{"version":"old"}\n'}
            for name, text in stale.items():
                (mad / name).write_text(text)
            env = self.base_env()
            env.update({"HOME": str(home), "PATH": f"{failing}:{bindir}:{env['PATH']}"})
            proc = subprocess.run(["bash", str(src / "install.sh")], env=env, cwd=REPO, capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("account.py", proc.stderr)
            self.assertFalse((mad / "account.py").exists())
            for name, text in stale.items():
                self.assertEqual((mad / name).read_text(), text, name)   # 아무것도 교체되지 않았다
            self.assertFalse((home / ".claude" / "settings.json").exists())   # 훅도 병합되지 않았다
            self.assertFalse((mad / "pending" / "install.json").exists())
            self.assertEqual([p.name for p in mad.glob(".madison-download.*")], [])

    def test_windows_collector_ports_the_account_rule(self):
        """pwsh가 없어 실행은 검증하지 못한다 — report.sh와 같은 규칙이 이식됐는지 정적으로만 확인한다."""
        ps1 = (REPO / "collector" / "report.ps1").read_text(encoding="utf-8")
        for needle in (
            "[System.Security.Cryptography.SHA256]::Create()",          # SHA-256 (api:<sha8>, 거절 키 꼬리 비교)
            "$_.ToString(\"x2\")",
            "Replace(\"-\", \"+\").Replace(\"_\", \"/\")",               # JWT base64url 디코드
            "[Convert]::FromBase64String",
            "JavaScriptSerializer",                                      # PS 5.1 큰 JSON
            "$serializer.MaxJsonLength = [int]::MaxValue",
            "ConvertFrom-Json -AsHashtable",                             # PS 7+
            "$PSVersionTable.PSVersion.Major -ge 7",
            'Join-Path $MadDir "cache"',                                  # 캐시 위치
            "claude-login-",
            "$env:USERPROFILE",
            "CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "https://api.openai.com/profile",
            '$orgType.StartsWith("claude_")',                              # 1. claude_* -> 구독
            '$orgType.StartsWith("api")',                                  # 2. api* -> API
            '$billing = $login.billingType.Trim().ToLowerInvariant()',     # 3. 그 외(빈 값·처음 보는 값)는 billingType으로 판정
            '$billing.Contains("subscription")',
            '(-not $billing)',
            'function Import-WebExtensions',                               # Add-Type 가드
            '"System.Web.Script.Serialization.JavaScriptSerializer" -as [type]',
            'billingType = (Get-Text $oauth "billingType")',               # 캐시 요약에 billingType 추가
            'billingType = "$($cached.billingType)"',
            'billingType = $data.billingType',
            '$cached.version -eq 2',                                       # 옛(v1) 캐시 무효화
            '@{ version = 2; stamp',
            "$Headers",   # 기존 전송 경로는 그대로
        ):
            self.assertIn(needle, ps1)
        # 캐시 버전은 읽기와 쓰기가 같아야 하고, 옛 버전 판정이 남아 있으면 안 된다
        self.assertNotIn("$cached.version -eq 1", ps1)
        self.assertNotIn("version = 1;", ps1)
        self.assertNotIn("not $orgType -or", ps1)
        # Add-Type은 가드 함수 안에서만, 타입 검사 뒤에 한 번만 나온다 (훅마다 반복하지 않는다)
        self.assertEqual(ps1.count("Add-Type -AssemblyName"), 1)
        guard = ps1[ps1.index("function Import-WebExtensions"):]
        guard = guard[:guard.index("\n}\n")]
        self.assertLess(guard.index("-as [type]"), guard.index("Add-Type -AssemblyName System.Web.Extensions"))
        self.assertEqual(ps1.count("Import-WebExtensions\n"), 2)   # Read-JsonFile, Get-JwtClaims
        jwt = ps1[ps1.index("function Get-JwtClaims"):]
        jwt = jwt[:jwt.index("\n}\n")]
        self.assertIn("Import-WebExtensions", jwt)
        self.assertNotIn("Add-Type -AssemblyName", jwt)
        # 계정은 세 이벤트에만, 정해졌을 때만 싣는다
        block = ps1[ps1.index('if ($EventName -in @("session_start", "prompt", "turn_done"))'):]
        block = block[:block.index("\n}\n")]
        self.assertIn('Resolve-Account $AgentName ($frontend -eq "auto")', block)
        self.assertIn("if ($account) { $detail.account = $account }", block)
        # 함수 정의가 호출보다 앞, 그리고 stdin 처리보다 앞
        self.assertLess(ps1.index("function Resolve-Account"), ps1.index("[Console]::In.ReadToEnd()"))
        self.assertIn('$codexBase = if ($env:CODEX_HOME)', ps1)
        # 설치기는 cache 폴더를 만든다
        install_ps1 = (REPO / "collector" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('(Join-Path $MadDir "cache")', install_ps1)


if __name__ == "__main__":
    unittest.main()

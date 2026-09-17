import json
import os
from pathlib import Path
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


class CollectorTests(unittest.TestCase):
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
            "  *) printf '200' ;;\n"
            "esac\n",
        )
        executable(bindir / "launchctl", "#!/bin/bash\nexit 0\n")
        executable(
            bindir / "codex",
            "#!/bin/bash\n"
            "if [ \"${1:-}\" = 'features' ]; then printf 'hooks stable true\\n'; fi\n",
        )
        return bindir

    def run_report(self, originator: str, event: str, hook: dict):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            capture = root / "capture.json"
            transcript = root / "rollout.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"originator": originator, "source": "vscode" if originator == "Codex Desktop" else "cli"}}) + "\n"
                + json.dumps({"type": "turn_context", "payload": {"model": "gpt-rollout", "effort": "xhigh"}}) + "\n"
            )
            hook["transcript_path"] = str(transcript)
            env = os.environ.copy()
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
            env = os.environ.copy()
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
            env = os.environ.copy()
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
            env = os.environ.copy()
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

            env = os.environ.copy()
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

            for skill in ("handoff", "pickup"):
                self.assertTrue((home / ".codex" / "skills" / skill / "SKILL.md").exists(), skill)

    def test_installer_defaults_hub_from_env(self):
        # 등록된 기기에서 --hub 없이 재실행하면 env의 MADISON_URL을 쓴다 (placeholder로 가지 않음)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            bindir = self.fake_path(root)
            log = root / "curl.log"
            env = os.environ.copy()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}", "MADISON_TEST_LOG": str(log)})
            subprocess.run(["bash", str(INSTALL)], env=env, cwd=REPO, check=True, capture_output=True, text=True)
            log_text = log.read_text()
            self.assertIn("madison.test", log_text)
            self.assertNotIn("example.com", log_text)

    def test_installer_requires_hub_on_first_install(self):
        # 첫 설치(env 없음)에서 --hub 미지정이면 터널 기본값으로 가지 않고 안내와 함께 거절한다
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, mad = self.make_home(root)
            (mad / "env").unlink()
            bindir = self.fake_path(root)
            env = os.environ.copy()
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
            env = os.environ.copy()
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

            env = os.environ.copy()
            env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}"})
            subprocess.run(
                ["bash", str(INSTALL), "--name", "test-device", "--hub", "http://madison.test"],
                env=env, cwd=REPO, check=True, capture_output=True, text=True,
            )
            parsed = tomllib.loads(config.read_text())
            self.assertEqual(parsed["notify"], ["/Apps/SkyComputerUseClient", "turn-ended"])


if __name__ == "__main__":
    unittest.main()

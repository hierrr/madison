import json
import os
from pathlib import Path
import stat
import subprocess
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
            "data=''\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = '--data-binary' ]; then data=\"$2\"; shift 2; else shift; fi\n"
            "done\n"
            "if [ -n \"$data\" ] && [ -n \"${MADISON_TEST_CAPTURE:-}\" ]; then printf '%s' \"$data\" > \"$MADISON_TEST_CAPTURE\"; fi\n"
            "printf '200'\n",
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
            existing_hooks = {
                "hooks": {"PreToolUse": [{"matcher": "Custom", "hooks": [{"type": "command", "command": "custom-hook"}]}]}
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

            hooks = json.loads(hooks_path.read_text())["hooks"]
            custom = [h for group in hooks["PreToolUse"] for h in group["hooks"] if h["command"] == "custom-hook"]
            self.assertEqual(len(custom), 1)
            for name in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop", "SessionEnd"):
                commands = [h["command"] for group in hooks[name] for h in group["hooks"] if "madison" in h["command"]]
                self.assertEqual(len(commands), 1, name)


if __name__ == "__main__":
    unittest.main()

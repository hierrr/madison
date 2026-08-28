import unittest

from server import llm


class CommandTests(unittest.TestCase):
    def test_claude_text_mode_has_hygiene_flags_and_no_prompt_in_argv(self):
        cf = {"provider": "claude", "model": "claude-sonnet-5", "effort": "high",
              "claude_bin": "/usr/local/bin/claude", "codex_bin": "codex"}
        cmd = llm.command(cf)
        self.assertEqual(cmd[:2], ["/usr/local/bin/claude", "-p"])
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "text")
        for flag in ("--safe-mode", "--no-session-persistence", "--disable-slash-commands"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")          # 내장 도구 없음
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertNotIn("--json-schema", cmd)

    def test_claude_structured_mode_switches_to_json_envelope(self):
        cf = {"provider": "claude", "model": "", "effort": "", "claude_bin": "claude", "codex_bin": "codex"}
        cmd = llm.command(cf, schema_json='{"type":"object"}')
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertEqual(cmd[cmd.index("--json-schema") + 1], '{"type":"object"}')
        self.assertNotIn("--model", cmd)                              # 빈 모델 = CLI 기본

    def test_codex_command_uses_ephemeral_readonly_and_output_file(self):
        cf = {"provider": "codex", "model": "gpt-5", "effort": "medium", "claude_bin": "claude", "codex_bin": "/x/codex"}
        cmd = llm.command(cf, schema_path="/tmp/s.json", out_file="/tmp/o.json")
        self.assertEqual(cmd[:2], ["/x/codex", "exec"])
        for flag in ("--skip-git-repo-check", "--ephemeral"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertEqual(cmd[cmd.index("--output-schema") + 1], "/tmp/s.json")
        self.assertEqual(cmd[cmd.index("-o") + 1], "/tmp/o.json")
        self.assertEqual(cmd[cmd.index("-c") + 1], 'model_reasoning_effort="medium"')
        self.assertEqual(cmd[-1], "-")                                # 프롬프트 = stdin


class EnvelopeTests(unittest.TestCase):
    def test_structured_output_field_wins(self):
        data, err = llm._parse_claude_envelope('{"is_error":false,"subtype":"success","structured_output":{"a":1},"result":"{\\"a\\":1}"}')
        self.assertEqual((data, err), ({"a": 1}, ""))

    def test_result_string_is_parsed_when_structured_missing(self):
        data, err = llm._parse_claude_envelope('{"is_error":false,"subtype":"success","result":"{\\"a\\":2}"}')
        self.assertEqual(data, {"a": 2})

    def test_error_envelopes_are_rejected(self):
        data, err = llm._parse_claude_envelope('{"is_error":true,"subtype":"error_max_turns","result":"x"}')
        self.assertIsNone(data)
        self.assertIn("error_max_turns", err)
        self.assertEqual(llm._parse_claude_envelope("not json")[0], None)


class FencesTests(unittest.TestCase):
    def test_outer_fence_only(self):
        self.assertEqual(llm.strip_fences("```markdown\n- a\n- b\n```"), "- a\n- b")
        self.assertEqual(llm.strip_fences("- a\n```code```\n- b"), "- a\n```code```\n- b")
        self.assertEqual(llm.strip_fences("  plain  "), "plain")


if __name__ == "__main__":
    unittest.main()

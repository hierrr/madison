import unittest

from server import llm


class CommandTests(unittest.TestCase):
    def test_claude_text_mode_has_hygiene_flags_and_no_prompt_in_argv(self):
        cf = {"provider": "claude", "model": "claude-sonnet-5", "effort": "high",
              "claude_bin": "/usr/local/bin/claude", "codex_bin": "codex"}
        cmd = llm.command(cf)
        self.assertEqual(cmd[:2], ["/usr/local/bin/claude", "-p"])
        self.assertIn("--output-format", cmd)
        # 텍스트 호출도 json 봉투 — 봉투의 usage가 워커 토큰의 유일한 출처
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
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
        self.assertIn("--json", cmd)                                  # turn.completed 토큰 이벤트


class ConfTests(unittest.TestCase):
    """사이트별 실효 설정 — .env 기본 모델(claude 모델명)은 codex로 새지 않는다."""

    def setUp(self):
        import sqlite3
        from unittest.mock import patch
        from server import db
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.db_patch = patch.object(db, "_conn", self.conn)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.conn.close()

    def store(self, **kv):
        for k, v in kv.items():
            self.conn.execute("INSERT INTO settings (key, value) VALUES (?,?)", (f"llm.summary.{k}", v))

    def test_claude_without_model_uses_env_default(self):
        self.assertEqual(llm.conf("summary")["model"], llm.CFG.task_summary_model)

    def test_codex_without_model_leaves_it_to_cli(self):
        self.store(provider="codex")
        cf = llm.conf("summary")
        self.assertEqual((cf["provider"], cf["model"]), ("codex", ""))
        self.assertNotIn("--model", llm.command(cf))

    def test_codex_with_model_keeps_it(self):
        self.store(provider="codex", model="gpt-6-sol", effort="high")
        cf = llm.conf("summary")
        self.assertEqual((cf["model"], cf["effort"]), ("gpt-6-sol", "high"))


class WorkerUsageTests(unittest.TestCase):
    def test_claude_usage_prefers_model_usage_map(self):
        env = {"modelUsage": {"model-a": {"inputTokens": 10, "outputTokens": 73,
               "cacheReadInputTokens": 7559, "cacheCreationInputTokens": 5779, "thinkingTokens": 67}},
               "usage": {"input_tokens": 999}}
        self.assertEqual(llm._claude_usage(env, "cfg-model"),
                         {"model-a": {"in": 10, "out": 73, "cr": 7559, "cw": 5779, "th": 67}})

    def test_claude_usage_falls_back_to_configured_model(self):
        env = {"usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 30,
                         "cache_creation_input_tokens": 4, "output_tokens_details": {"thinking_tokens": 1}}}
        self.assertEqual(llm._claude_usage(env, "cfg-model"),
                         {"cfg-model": {"in": 5, "out": 2, "cr": 30, "cw": 4, "th": 1}})
        self.assertEqual(llm._claude_usage(env)["unknown"]["in"], 5)   # 모델 미설정 시
        self.assertEqual(llm._claude_usage(None), {})

    def test_codex_usage_sums_turn_completed_and_splits_cached(self):
        stdout = "\n".join([
            '{"type":"item.completed","item":{}}',
            '{"type":"turn.completed","usage":{"input_tokens":14352,"cached_input_tokens":14000,'
            '"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":2}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":0,"output_tokens":7}}',
            'garbage not json',
        ])
        self.assertEqual(llm._codex_usage(stdout, "gpt-x"),
                         {"gpt-x": {"in": 452, "out": 12, "cr": 14000, "cw": 0, "th": 2}})
        self.assertEqual(llm._codex_usage("", "gpt-x"), {})

    def test_codex_message_fallback_takes_last_agent_message(self):
        stdout = "\n".join([
            '{"type":"thread.started","thread_id":"t"}',
            '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"draft"}}',
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"final answer"}}',
            '{"type":"turn.completed","usage":{"input_tokens":1}}',
        ])
        self.assertEqual(llm._codex_message(stdout), "final answer")
        # 프로토콜 이벤트가 본문으로 새면 안 된다
        self.assertEqual(llm._codex_message('{"type":"turn.completed","usage":{}}'), "")

    def test_claude_text_unwrap_paths(self):
        # 정상 봉투 → result 본문
        self.assertEqual(llm._claude_text({"subtype": "success", "result": "hello"}, "raw"),
                         ("hello", ""))
        # 실패 봉투 → 에러 (본문 없음)
        text, err = llm._claude_text({"is_error": True, "subtype": "error_max_turns", "result": "x"}, "raw")
        self.assertEqual(text, "")
        self.assertIn("error_max_turns", err)
        # 봉투가 아니면 원문 그대로 (구버전 CLI 호환)
        self.assertEqual(llm._claude_text(None, "plain text output"), ("plain text output", ""))


class WorkerAccountingTests(unittest.TestCase):
    """_account_tokens → token_daily·token_hourly 배선 검증 (인메모리 DB + DEVICE_ENV 대체)."""

    def setUp(self):
        import sqlite3
        import tempfile
        from server import db, tokens
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
            " VALUES (1,'acme-mini','x',datetime('now'),datetime('now'))")
        self.envfile = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self.envfile.write("MADISON_URL=http://x\nMADISON_DEVICE=acme-mini\n")
        self.envfile.close()
        self._old_env = tokens.DEVICE_ENV
        from pathlib import Path
        tokens.DEVICE_ENV = Path(self.envfile.name)
        tokens._hub_dev.update(id=None, at=0.0)   # 캐시 초기화

    def tearDown(self):
        import os
        from pathlib import Path
        from server import tokens
        tokens.DEVICE_ENV = self._old_env
        tokens._hub_dev.update(id=None, at=0.0)
        os.unlink(self.envfile.name)
        self.conn.close()

    def test_usage_map_lands_in_token_daily_as_auto(self):
        llm._account_tokens("claude-code", {"model-a": {"in": 5, "out": 2, "cr": 30, "cw": 4, "th": 1}},
                            c=self.conn)
        r = self.conn.execute("SELECT * FROM token_daily").fetchone()
        self.assertEqual((r["agent"], r["model"], r["frontend"], r["source"], r["project"]),
                         ("claude-code", "model-a", "auto", "events", "madison"))
        self.assertEqual((r["input"], r["output"], r["cache_read"], r["cache_write"], r["thinking"]),
                         (5, 2, 30, 4, 1))

    def test_usage_lands_in_current_local_hour_of_both_ledgers(self):
        import time
        llm._account_tokens("codex-cli", {"gpt-a": {"in": 5, "out": 2, "cr": 30, "cw": 4, "th": 1}}, c=self.conn)
        h = self.conn.execute("SELECT * FROM token_hourly").fetchone()
        d = self.conn.execute("SELECT * FROM token_daily").fetchone()
        self.assertEqual(len(h["hour"]), 13)                       # 'YYYY-MM-DDTHH' 허브 로컬
        self.assertEqual(h["hour"][:10], time.strftime("%Y-%m-%d")[:10])
        self.assertEqual(d["day"], h["hour"][:10])
        cols = ("device_id", "agent", "project", "model", "frontend", "source",
                "input", "output", "cache_read", "cache_write", "thinking", "turns")
        self.assertEqual({k: h[k] for k in cols}, {k: d[k] for k in cols})
        self.assertEqual((h["agent"], h["frontend"], h["project"]), ("codex-cli", "auto", "madison"))

    def test_hub_project_constant_is_madison(self):
        from server import tokens
        self.assertEqual(tokens.HUB_PROJECT, "madison")

    def test_worker_tokens_are_booked_under_hub_project_in_both_ledgers(self):
        # 워커 cwd 이름(llm-cwd)이 아니라 허브 프로젝트 이름 — 두 원장 모두
        llm._account_tokens("claude-code", {"model-a": {"in": 5, "out": 2, "cr": 0, "cw": 0, "th": 0},
                                            "model-b": {"in": 1, "out": 1, "cr": 0, "cw": 0, "th": 0}},
                            c=self.conn)
        for table in ("token_daily", "token_hourly"):
            with self.subTest(table=table):
                rows = self.conn.execute(f"SELECT project, frontend FROM {table}").fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual({(r["project"], r["frontend"]) for r in rows}, {("madison", "auto")})

    def test_hub_project_does_not_follow_llm_cwd_name(self):
        from pathlib import Path
        from unittest import mock
        for name in ("custom-worker-dir", "madison", "llm-cwd"):
            with self.subTest(llm_cwd=name), mock.patch.object(llm.CFG, "llm_cwd", Path("/x") / name):
                self.conn.execute("DELETE FROM token_daily")
                self.conn.execute("DELETE FROM token_hourly")
                llm._account_tokens("codex-cli", {"gpt-a": {"in": 3, "out": 1, "cr": 0, "cw": 0, "th": 0}},
                                    c=self.conn)
                self.assertEqual(self.conn.execute("SELECT project FROM token_daily").fetchone()["project"], "madison")
                self.assertEqual(self.conn.execute("SELECT project FROM token_hourly").fetchone()["project"], "madison")

    def test_usage_lands_in_the_runs_started_at_hour_in_both_ledgers(self):
        """실시간 계상도 일회성 채우기(llm_runs.started_at)와 같은 시각 기준 — 호출이 늦게 끝나도 시작 시각의 시·일."""
        from server import tokens
        started = "2026-09-02T14:59:30Z"
        llm._account_tokens("claude-code", {"model-a": {"in": 5, "out": 2, "cr": 0, "cw": 0, "th": 0}},
                            c=self.conn, started_at=started)
        want = tokens.local_hour(started)
        self.assertEqual(self.conn.execute("SELECT hour FROM token_hourly").fetchone()["hour"], want)
        self.assertEqual(self.conn.execute("SELECT day FROM token_daily").fetchone()["day"], want[:10])

    def test_record_passes_started_at_to_the_ledgers(self):
        """_record가 llm_runs.started_at을 계상 시각으로 넘긴다 — 같은 값이 llm_runs와 원장 키가 된다."""
        from unittest import mock
        from contextlib import contextmanager
        from server import tokens

        @contextmanager
        def fake_tx():
            yield self.conn
        from datetime import datetime, timedelta, timezone
        # 30일 보존 정리(_record가 오래된 llm_runs를 지움)에 걸리지 않게 최근 시각
        started = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = llm.Result(ok=True, provider="claude", model="haiku")
        with mock.patch.object(llm.db, "tx", fake_tx):
            llm._record("summary", {"provider": "claude", "model": "haiku", "effort": ""}, "p", res, started, "",
                        False, {"haiku": {"in": 3, "out": 1, "cr": 0, "cw": 0, "th": 0}})
        self.assertEqual(self.conn.execute("SELECT hour FROM token_hourly").fetchone()["hour"],
                         tokens.local_hour(started))
        self.assertEqual(self.conn.execute("SELECT started_at FROM llm_runs").fetchone()["started_at"], started)

    def test_failure_midway_leaves_no_partial_rows_and_keeps_the_surrounding_transaction(self):
        """모델 둘 중 두 번째가 실패(정수 범위 초과)하면 첫 모델의 가산도 되돌린다 — 호출 결과·바깥 쓰기는 영향 없다."""
        self.conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")   # 바깥 트랜잭션의 쓰기
        with self.assertLogs("madison.llm", "ERROR"):
            llm._account_tokens("claude-code", {"model-a": {"in": 5, "out": 2, "cr": 0, "cw": 0, "th": 0},
                                                "model-b": {"in": 2 ** 63, "out": 0, "cr": 0, "cw": 0, "th": 0}},
                                c=self.conn)
        self.assertIsNone(self.conn.execute("SELECT * FROM token_daily").fetchone())
        self.assertIsNone(self.conn.execute("SELECT * FROM token_hourly").fetchone())
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='k'").fetchone()["value"], "v")

    def test_empty_or_zero_usage_writes_nothing(self):
        llm._account_tokens("claude-code", {}, c=self.conn)
        llm._account_tokens("claude-code", {"m": {"in": 0, "out": 0, "cr": 0, "cw": 0, "th": 0}}, c=self.conn)
        self.assertIsNone(self.conn.execute("SELECT * FROM token_daily").fetchone())
        self.assertIsNone(self.conn.execute("SELECT * FROM token_hourly").fetchone())


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

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from server import db, tokscan


def claude_line(model, i, o, cwd="/home/acme/.madison/llm-cwd"):
    return json.dumps({"type": "assistant", "cwd": cwd,
                       "message": {"model": model, "usage": {"input_tokens": i, "output_tokens": o}}})


def codex_lines(cwd, totals):
    lines = [json.dumps({"type": "session_meta", "payload": {"cwd": cwd, "originator": "codex_exec"}}),
             json.dumps({"type": "turn_context", "payload": {"model": "gpt-w"}})]
    for t in totals:
        lines.append(json.dumps({"payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": t, "cached_input_tokens": 0, "output_tokens": t // 10}}}}))
    return "\n".join(lines) + "\n"


class TokScanTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
            " VALUES (1,'acme-mini','x',datetime('now'),datetime('now'))")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.claude = root / "projects"
        self.codex = root / "sessions"
        (self.claude / "-home-acme--madison-llm-cwd").mkdir(parents=True)
        self.codex.mkdir()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def sweep(self, baseline=False):
        return tokscan.sweep(self.conn, 1, baseline=baseline,
                             claude_root=self.claude, codex_root=self.codex)

    def daily(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM token_daily ORDER BY agent, model")]

    def test_baseline_then_increment(self):
        f = self.claude / "-home-acme--madison-llm-cwd" / "s1.jsonl"
        f.write_text(claude_line("model-a", 100, 50) + "\n")
        self.sweep(baseline=True)
        self.assertEqual(self.daily(), [])   # 기준점만 — 과거분은 백필 몫
        # 새 턴 추가 → 증가분만 가산
        f.write_text(f.read_text() + claude_line("model-a", 40, 8) + "\n")
        self.assertEqual(self.sweep(), 1)
        r = self.daily()[0]
        self.assertEqual((r["input"], r["output"], r["frontend"], r["project"], r["source"]),
                         (40, 8, "auto", "llm-cwd", "events"))
        # 변화 없으면 재가산 없음
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.daily()[0]["input"], 40)

    def test_new_file_after_baseline_counts_fully(self):
        self.sweep(baseline=True)
        f = self.claude / "-home-acme--madison-llm-cwd" / "s2.jsonl"
        f.write_text(claude_line("model-a", 7, 3) + "\n")
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.daily()[0]["input"], 7)

    def test_codex_worker_rollout_and_normal_session_skip(self):
        w = self.codex / "rollout-worker.jsonl"
        w.write_text(codex_lines("/home/acme/.madison/llm-cwd", [100]))
        n = self.codex / "rollout-normal.jsonl"
        n.write_text(codex_lines("/home/acme/dev/acme-web", [500]))
        self.sweep(baseline=True)
        w.write_text(codex_lines("/home/acme/.madison/llm-cwd", [100, 250]))
        n.write_text(codex_lines("/home/acme/dev/acme-web", [500, 900]))
        self.assertEqual(self.sweep(), 1)   # 워커만 — 일반 세션은 훅 담당
        rows = self.daily()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["agent"], r["model"], r["input"], r["frontend"]),
                         ("codex-cli", "gpt-w", 150, "auto"))

    def test_summarizer_dir_is_scanned(self):
        d = self.claude / "-home-acme-summarizer"
        d.mkdir()
        self.sweep(baseline=True)
        (d / "s.jsonl").write_text(claude_line("model-a", 5, 2, cwd="/home/acme/summarizer") + "\n")
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.daily()[0]["project"], "summarizer")


if __name__ == "__main__":
    unittest.main()

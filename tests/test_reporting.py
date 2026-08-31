import json
import sqlite3
import unittest

from server import db, llm, report, reporting


def _mem_db():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.migrate(c)
    c.execute("INSERT INTO devices (id,name,token_hash,created_at) VALUES (1,'workstation','x','2026-08-27T00:00:00Z')")
    c.commit()
    return c


def _ev(c, ts, event, payload, session="s1", project="proj"):
    c.execute(
        "INSERT INTO events (device_id, agent, session_id, event_id, event, ts_device, ts_hub, project, payload)"
        " VALUES (1,'claude-code',?,?,?,?,?,?,?)",
        (session, f"{session}-{ts}-{event}", event, ts, ts, project, json.dumps(payload)))


class NormalizeTests(unittest.TestCase):
    def test_week_and_month_keys(self):
        self.assertEqual(reporting.norm_day("week", "2026-08-27"), "2026-08-24")   # 목 → 월
        self.assertEqual(reporting.norm_day("week", "2026-08-24"), "2026-08-24")
        self.assertEqual(reporting.norm_day("month", "2026-08-27"), "2026-08-01")
        self.assertEqual(reporting.norm_day("day", "2026-08-27"), "2026-08-27")
        self.assertEqual(reporting.norm_day("week", "garbage"), "garbage")
        self.assertEqual(reporting.norm_range("nope"), "day")


class StoreTests(unittest.TestCase):
    """store/mark_failed는 db.tx()의 전역 커넥션을 쓴다 — 테스트는 인메모리 DB로 바꿔 끼운다."""
    def setUp(self):
        self._saved = db._conn
        db._conn = _mem_db()

    def tearDown(self):
        db._conn.close()
        db._conn = self._saved

    def test_failure_keeps_existing_markdown_and_records_reason(self):
        res = llm.Result(ok=True, text="- 본문", model="m1", effort="high")
        reporting.store("day", "2026-08-27", "- 본문", started_at="2026-08-27T10:00:00Z", res=res)
        reporting.mark_failed("day", "2026-08-27", "timeout 900s", fallback="- 폴백")
        row = reporting.get_row(db._conn, "day", "2026-08-27")
        self.assertEqual(row["markdown"], "- 본문")                  # 좋은 리포트 유지
        self.assertEqual(row["fail_reason"], "timeout 900s")
        self.assertEqual(row["model"], "m1")
        self.assertEqual(row["prompt_version"], report.PROMPT_VERSION)
        # 다음 성공은 실패 표시를 지운다
        reporting.store("day", "2026-08-27", "- 새 본문", started_at="2026-08-27T11:00:00Z", res=res)
        row = reporting.get_row(db._conn, "day", "2026-08-27")
        self.assertIsNone(row["failed_at"])
        self.assertEqual(row["generated_at"], "2026-08-27T11:00:00Z")

    def test_failure_without_stored_report_shows_fallback(self):
        reporting.mark_failed("day", "2026-08-26", "exit 1", fallback="- 폴백")
        row = reporting.get_row(db._conn, "day", "2026-08-26")
        self.assertEqual(row["markdown"], "- 폴백")
        self.assertIsNone(row["generated_at"])
        self.assertEqual(reporting.stale_reason(db._conn, "day", "2026-08-26"), "retry-failed")   # 폴백만 있으면 재시도


class StaleTests(unittest.TestCase):
    def setUp(self):
        self.c = _mem_db()

    def tearDown(self):
        self.c.close()

    def _put(self, range_, day, gen, failed=None):
        self.c.execute("INSERT INTO reports (range, day, markdown, generated_at, failed_at)"
                       " VALUES (?,?,?,?,?)", (range_, day, "- x", gen, failed))

    def test_no_report_is_stale(self):
        self.assertEqual(reporting.stale_reason(self.c, "day", "2026-08-27"), "no-report")

    def test_no_new_events_means_not_stale(self):
        _ev(self.c, "2026-08-27T01:00:00Z", "prompt", {"prompt": "x"})
        self._put("day", "2026-08-27", "2026-08-27T02:00:00Z")
        self.assertIsNone(reporting.stale_reason(self.c, "day", "2026-08-27"))

    def test_new_events_after_generation_are_stale(self):
        self._put("day", "2026-08-27", "2026-08-27T02:00:00Z")
        _ev(self.c, "2026-08-27T03:00:00Z", "turn_done", {"summary": "y"})
        self.assertEqual(reporting.stale_reason(self.c, "day", "2026-08-27"), "new-events")

    def test_failed_after_generation_retries(self):
        self._put("day", "2026-08-27", "2026-08-27T02:00:00Z", failed="2026-08-27T02:30:00Z")
        self.assertEqual(reporting.stale_reason(self.c, "day", "2026-08-27"), "retry-failed")

    def test_period_is_stale_when_a_daily_was_regenerated(self):
        self._put("week", "2026-08-24", "2026-08-27T02:00:00Z")
        self._put("day", "2026-08-25", "2026-08-27T05:00:00Z")          # 주간 뒤에 갱신된 일일
        self.assertEqual(reporting.stale_reason(self.c, "week", "2026-08-24"), "daily-updated")

    def test_period_not_stale_when_dailies_older(self):
        self._put("week", "2026-08-24", "2026-08-27T09:00:00Z")
        self._put("day", "2026-08-25", "2026-08-27T05:00:00Z")
        _ev(self.c, "2026-08-25T03:00:00Z", "turn_done", {"summary": "y"})
        self.assertIsNone(reporting.stale_reason(self.c, "week", "2026-08-24"))


if __name__ == "__main__":
    unittest.main()


class RelabelTests(unittest.TestCase):
    def setUp(self):
        self._saved = db._conn
        db._conn = _mem_db()
        c = db._conn
        c.execute("INSERT INTO services (name, kind, status, cues, created_at) VALUES ('Acme','product','confirmed','[]','x'),"
                  " ('GitHub','ops','confirmed','[]','x')")
        c.execute("INSERT INTO reports (range, day, markdown, generated_at) VALUES ('day','2026-08-27','- Acme\n    - x','2026-08-27T10:00:00Z')")
        c.execute("INSERT INTO report_assignments (range, day, session_key, device, session_id, project, service, task, evidence, created_at)"
                  " VALUES ('day','2026-08-27','S1','w','s1','scratch','Acme','과제','근거','2026-08-27T10:00:00Z')")
        c.commit()

    def tearDown(self):
        db._conn.close()
        db._conn = self._saved

    def test_relabel_records_correction_and_marks_stale(self):
        reporting.relabel("day", "2026-08-27", "S1", "GitHub", "저장소 운영 작업")
        c = db._conn
        a = c.execute("SELECT service, evidence FROM report_assignments WHERE session_key='S1'").fetchone()
        self.assertEqual(a["service"], "GitHub")
        self.assertIn("사람이 옮김", a["evidence"])
        cor = reporting.recent_corrections(c, ["scratch"])
        self.assertEqual((cor[0]["before_service"], cor[0]["after_service"], cor[0]["reason"]), ("Acme", "GitHub", "저장소 운영 작업"))
        self.assertEqual(reporting.stale_reason(c, "day", "2026-08-27"), "relabeled")
        self.assertEqual(reporting.recent_corrections(c, ["other"]), [])
        # 프롬프트에 교정 사례가 들어간다
        p = report.build_day_prompt("2026-08-28", {"scratch": {"sessions": [], "turns": 0, "n_sessions": 0}},
                                    corrections=cor)
        self.assertIn("사람의 교정 기록", p)
        self.assertIn("'Acme'에서 'GitHub'(으)로", p)
        # 재생성 성공은 stale 표시를 지운다
        reporting.store("day", "2026-08-27", "- GitHub\n    - x", started_at="2026-08-27T12:00:00Z", res=None)
        self.assertIsNone(reporting.stale_reason(c, "day", "2026-08-27"))

    def test_relabel_rejects_unknown(self):
        with self.assertRaises(KeyError):
            reporting.relabel("day", "2026-08-27", "S9", "GitHub")
        with self.assertRaises(ValueError):
            reporting.relabel("day", "2026-08-27", "S1", "Nope")


class ProposalFilterTests(unittest.TestCase):
    def test_repo_names_are_not_accepted_as_services(self):
        from server import registry
        c = _mem_db()
        c.execute("INSERT INTO services (name, kind, status, cues, created_at) VALUES ('Acme','product','confirmed','[]','x')")
        _ev(c, "2026-08-27T01:00:00Z", "prompt", {"prompt": "x"}, project="agent-usage-metrics")
        reg = registry.snapshot(c)
        accepted = reporting.accept_proposals(c, [
            {"name": "agent-usage-metrics", "kind": "tool", "description": "저장소명", "cues": [], "evidence": "S1"},
            {"name": "Acme", "kind": "product", "description": "이미 있음", "cues": [], "evidence": "S1"},
            {"name": "Billing", "kind": "product", "description": "새 서비스", "cues": ["결제"], "evidence": "S2"},
        ], "2026-08-27", reg)
        self.assertEqual(accepted, ["Billing"])
        reg2 = registry.snapshot(c)
        self.assertIn("Billing", reg2.names())                    # 사람 확정 없이 바로 등록
        self.assertEqual(reg2.proposed_names(), [])
        # 사람이 거절한 이름은 다시 등록되지 않는다
        sid = registry.upsert_service(c, "Junk", status="proposed"); registry.decide(c, sid, "rejected")
        self.assertEqual(reporting.accept_proposals(c, [{"name": "Junk", "kind": "tool", "description": "", "cues": [], "evidence": ""}], "2026-08-27", reg2), [])

    def test_learn_weak_mapping_from_consistent_assignments(self):
        from server import registry
        c = _mem_db()
        acme = registry.upsert_service(c, "Acme")
        registry.upsert_service(c, "GitHub")
        work = {"scratch": {"sessions": [{"key": "S1"}, {"key": "S2"}]}, "tools": {"sessions": [{"key": "S3"}, {"key": "S4"}]},
                "web": {"sessions": [{"key": "S5"}]}}
        registry.set_project(c, "web", acme, "strong")
        reg = registry.snapshot(c)
        learned = reporting.learn_mappings(c, [
            {"session": "S1", "service": "GitHub"}, {"session": "S2", "service": "GitHub"},   # 일관 → 학습
            {"session": "S3", "service": "Acme"}, {"session": "S4", "service": "GitHub"},    # 갈림 → 학습 안 함
            {"session": "S5", "service": "Acme"}], work, reg)                               # 이미 매핑됨
        self.assertEqual(learned, [("scratch", "GitHub")])
        reg2 = registry.snapshot(c)
        self.assertEqual((reg2.service("scratch"), reg2.strength("scratch")), ("GitHub", "weak"))
        self.assertEqual(reg2.strength("tools"), "none")


class WorkerPidTests(unittest.TestCase):
    def test_foreign_process_with_live_pid_is_not_a_worker(self):
        import os, subprocess
        self.assertFalse(reporting._pid_alive(os.getpid()))            # 테스트 러너 자신 — 워커 아님
        self.assertFalse(reporting._pid_alive(999999))                 # 없는 pid
        p = subprocess.Popen(["python3", "-c", "import time; time.sleep(5)"])
        try:
            self.assertFalse(reporting._pid_alive(p.pid))              # 살아 있지만 genworker가 아님
        finally:
            p.kill(); p.wait()
        p = subprocess.Popen(["python3", "-c", "import time, sys; sys.argv=['server.gen'+'worker']; time.sleep(5)"])
        try:
            self.assertFalse(reporting._pid_alive(p.pid))              # argv 변조는 ps 명령줄에 안 보임 — 여전히 아님
        finally:
            p.kill(); p.wait()

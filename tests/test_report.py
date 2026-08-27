import json
import sqlite3
import unittest

from server import db, report


def _ev(c, ts, event, payload, session="s1", device=1, project="proj"):
    c.execute(
        "INSERT INTO events (device_id, agent, session_id, event_id, event, ts_device, ts_hub, project, payload)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (device, "claude-code", session, f"{session}-{ts}-{event}", event, ts, ts, project,
         json.dumps(payload)))


class GatherTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("INSERT INTO devices (id,name,token_hash,created_at) VALUES (1,'workstation','x','2026-08-27T00:00:00Z')")

    def tearDown(self):
        self.c.close()

    def test_pairs_prompt_with_response_and_groups_by_session(self):
        _ev(self.c, "2026-08-27T01:00:00Z", "prompt", {"prompt": "A를 고쳐줘"})
        _ev(self.c, "2026-08-27T01:05:00Z", "turn_done", {"summary": "A를 고쳤습니다"})
        _ev(self.c, "2026-08-27T02:00:00Z", "prompt", {"prompt": "B 조사"}, session="s2")
        _ev(self.c, "2026-08-27T02:03:00Z", "turn_done", {"summary": "B 조사 결과"}, session="s2")
        w = report.gather(self.c, "day", "2026-08-27")
        v = w["proj"]
        self.assertEqual(v["turns"], 2)
        self.assertEqual(v["n_sessions"], 2)
        self.assertEqual([len(s["turns"]) for s in v["sessions"]], [1, 1])
        t = v["sessions"][0]["turns"][0]
        self.assertEqual(t["prompts"], ["A를 고쳐줘"])
        self.assertEqual(t["response"], "A를 고쳤습니다")
        self.assertEqual(v["sessions"][0]["device"], "workstation")

    def test_notification_turn_keeps_title_and_drops_empty_response(self):
        note = '<task-notification><summary>Monitor event: "야간 배치 수집 감시"</summary></task-notification>'
        _ev(self.c, "2026-08-27T01:00:00Z", "prompt", {"prompt": note})
        _ev(self.c, "2026-08-27T01:00:01Z", "prompt", {"prompt": note.replace("Monitor event: ", "Monitor ")})
        _ev(self.c, "2026-08-27T01:02:00Z", "turn_done", {"summary": "수집이 끝나 전량 분석을 시작했습니다"})
        _ev(self.c, "2026-08-27T01:10:00Z", "prompt", {"prompt": note})
        _ev(self.c, "2026-08-27T01:10:01Z", "turn_done", {"summary": "No response requested."})
        w = report.gather(self.c, "day", "2026-08-27")
        turns = w["proj"]["sessions"][0]["turns"]
        self.assertEqual(len(turns), 1)                       # 빈 응답 턴은 버림
        self.assertEqual(turns[0]["notes"], ["야간 배치 수집 감시"])  # 시작·종료 알림 한 줄로
        self.assertEqual(turns[0]["prompts"], [])
        self.assertIn("전량 분석", turns[0]["response"])
        self.assertEqual(w["proj"]["turns"], 2)               # 턴 수 자체는 그대로 센다

    def test_queued_prompts_merge_into_one_turn(self):
        _ev(self.c, "2026-08-27T01:00:00Z", "prompt", {"prompt": "첫 지시"})
        _ev(self.c, "2026-08-27T01:00:30Z", "prompt", {"prompt": "이어서 둘째 지시"})
        _ev(self.c, "2026-08-27T01:05:00Z", "turn_done", {"summary": "둘 다 처리"})
        w = report.gather(self.c, "day", "2026-08-27")
        turns = w["proj"]["sessions"][0]["turns"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["prompts"], ["첫 지시", "이어서 둘째 지시"])

    def test_project_with_only_noise_is_dropped(self):
        _ev(self.c, "2026-08-27T01:00:00Z", "prompt", {"prompt": "<system-reminder>x</system-reminder>"})
        _ev(self.c, "2026-08-27T01:01:00Z", "turn_done", {"summary": "No response requested."})
        self.assertEqual(report.gather(self.c, "day", "2026-08-27"), {})

    def test_last_event_at(self):
        _ev(self.c, "2026-08-27T01:00:00Z", "prompt", {"prompt": "x"})
        _ev(self.c, "2026-08-27T03:00:00Z", "turn_done", {"summary": "y"})
        self.assertEqual(report.last_event_at(self.c, "2026-08-27"), "2026-08-27T03:00:00Z")
        self.assertIsNone(report.last_event_at(self.c, "2026-08-26"))


def _session(n, device="workstation"):
    return {"device": device, "start": "09:00", "end": "10:00",
            "turns": [{"prompts": [f"지시 {i} " + "x" * 200], "notes": [], "response": "응답 " + "y" * 200}
                      for i in range(n)]}


class CompressTests(unittest.TestCase):
    def test_digests_largest_session_until_under_budget(self):
        work = {"proj": {"sessions": [_session(2), _session(30), _session(10)], "turns": 42, "n_sessions": 3}}
        calls = []

        def llm(prompt):
            calls.append(prompt)
            return "요약 머리말\n- 압축 불릿 1\n- 압축 불릿 2\n끝"
        report.compress(work, llm, budget=6000)
        self.assertEqual(len(calls), 1)                        # 가장 긴 세션 하나로 충분
        self.assertIn("지시 29", calls[0])
        s = work["proj"]["sessions"][1]
        self.assertEqual(s["digest"], "- 압축 불릿 1\n- 압축 불릿 2")   # 머리말·맺음말 제거
        self.assertLessEqual(len(report._render_block("proj", work["proj"])), 6000)
        self.assertIn("(압축 요약)", report.build_day_prompt("2026-08-27", work))

    def test_under_budget_makes_no_calls_and_failed_llm_keeps_raw(self):
        work = {"proj": {"sessions": [_session(3)], "turns": 3, "n_sessions": 1}}
        report.compress(work, lambda p: self.fail("호출되면 안 됨"))
        report.compress(work, lambda p: "", budget=100)
        self.assertNotIn("digest", work["proj"]["sessions"][0])


class PromptTests(unittest.TestCase):
    def test_day_prompt_injects_previous_topics_only(self):
        prev_md = "- Acme\n    - 고객 데이터 확충\n        - 세부 한 일 A\n- MADISON\n    - 리포트 품질"
        work = {"proj": {"sessions": [_session(1)], "turns": 1, "n_sessions": 1}}
        p = report.build_day_prompt("2026-08-27", work, ["Acme"], prev=("2026-08-26", prev_md))
        self.assertIn("직전 업무일지(2026-08-26)", p)
        self.assertIn("    - 고객 데이터 확충", p)
        self.assertNotIn("세부 한 일 A", p)
        self.assertIn("[세션 · workstation · 09:00~10:00 · 턴 1]", p)
        self.assertIn("알려진 서비스", p)

    def test_period_prompt_uses_dailies(self):
        dailies = [("2026-08-24", "- Acme\n    - A"), ("2026-08-25", "- Acme\n    - B")]
        p = report.build_period_prompt("week", "2026-08-24", dailies, [])
        self.assertIn("=== 2026-08-24 (월) ===", p)
        self.assertIn("=== 2026-08-25 (화) ===", p)
        self.assertIn("주간보고", p)
        self.assertIn("업무일지의 최상위 불릿에 적힌", p)
        self.assertNotIn("로그 블록", p)

    def test_period_days_clamps_to_today(self):
        self.assertEqual(report.period_days("week", "2026-08-24", "2026-08-27"),
                         ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"])
        self.assertEqual(len(report.period_days("week", "2026-08-17", "2026-08-27")), 7)
        self.assertEqual(report.period_days("month", "2026-08-01", "2026-09-10")[-1], "2026-08-31")
        self.assertEqual(len(report.period_days("month", "2026-02-01", "2026-03-01")), 28)

    def test_fallbacks(self):
        work = {"proj": {"sessions": [_session(2)], "turns": 2, "n_sessions": 1}}
        self.assertTrue(report.fallback_md(work).startswith("- proj\n    - 응답 "))
        self.assertEqual(report.fallback_period_md([("2026-08-24", "- Acme\n    - A")]),
                         "- 2026-08-24 (월)\n    - Acme\n        - A")
        self.assertEqual(report.fallback_period_md([]), report.EMPTY_MD)


class KnownServicesTests(unittest.TestCase):
    def test_configured_services_are_listed_with_hints(self):
        from server.config import CFG
        old = CFG.report_known_services
        CFG.report_known_services = {"Acme Pro": "acme-pro, ap- 접두"}
        try:
            note = report._services_note(["Acme", "Acme Pro"])
            self.assertIn("- Acme\n- Acme Pro — acme-pro, ap- 접두", note)
            c = sqlite3.connect(":memory:")
            c.row_factory = sqlite3.Row
            c.executescript(db.SCHEMA)
            self.assertEqual(report.known_services(c), ["Acme Pro"])   # 프로젝트가 없어도 후보에
        finally:
            CFG.report_known_services = old


if __name__ == "__main__":
    unittest.main()

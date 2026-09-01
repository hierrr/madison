import json
import sqlite3
import unittest

from server import db, report
from server.registry import Registry


def _reg(names=(), pm=None):
    return Registry([{"id": i, "name": n, "kind": "product", "description": "", "cues": []}
                     for i, n in enumerate(names, 1)], (), pm or {})


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
        self.assertEqual([s["key"] for s in v["sessions"]], ["S1", "S2"])   # 프롬프트의 세션 표기
        self.assertEqual(v["sessions"][0]["session_id"], "s1")

    def test_long_response_keeps_head_and_tail(self):
        long = "머리" + "x" * 3000 + "꼬리"
        _ev(self.c, "2026-08-27T01:00:00Z", "prompt", {"prompt": "긴 작업"})
        _ev(self.c, "2026-08-27T01:05:00Z", "turn_done", {"summary": long})
        resp = report.gather(self.c, "day", "2026-08-27")["proj"]["sessions"][0]["turns"][0]["response"]
        self.assertTrue(resp.startswith("머리") and resp.endswith("꼬리"))
        self.assertIn("…(중략)…", resp)
        self.assertLess(len(resp), report.RESPONSE_CLIP + 20)

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

    def test_notification_keeps_event_excerpt_for_identification(self):
        start = ('<task-notification><summary>Monitor event: "야간 배치 수집 감시"</summary>'
                 '<event>01:00:00 [RUNNER] stage-start collect: scripts/collect.py --source feeds --rate 8</event>'
                 '</task-notification>')
        stop = ('<task-notification><summary>Monitor event: "야간 배치 수집 감시"</summary>'
                '<event>01:01:00 [RUNNER] stage-exit collect rc=75: QUOTA_EXHAUSTED</event>'
                '</task-notification>')
        _ev(self.c, "2026-08-27T01:00:00Z", "prompt", {"prompt": start})
        _ev(self.c, "2026-08-27T01:01:00Z", "prompt", {"prompt": stop})
        _ev(self.c, "2026-08-27T01:02:00Z", "turn_done", {"summary": "러너가 한도 대기로 전환됐습니다"})
        notes = report.gather(self.c, "day", "2026-08-27")["proj"]["sessions"][0]["turns"][0]["notes"]
        self.assertEqual(len(notes), 1)                       # 같은 제목의 알림은 첫 건만
        self.assertTrue(notes[0].startswith("야간 배치 수집 감시 — "))
        self.assertIn("scripts/collect.py", notes[0])         # 본문 발췌 = 대상 식별 단서

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


def _note_work(prompts, notes, response):
    return {"proj": {"sessions": [{"device": "w", "start": "09:00", "end": "09:10",
                                   "turns": [{"prompts": prompts, "notes": notes, "response": response}]}],
                     "turns": 1, "n_sessions": 1}}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)

    def tearDown(self):
        self.c.close()

    def _daily(self, day, md):
        self.c.execute("INSERT INTO reports (range, day, markdown) VALUES ('day', ?, ?)", (day, md))

    def test_finds_label_bridge_in_past_dailies(self):
        self._daily("2026-08-20", "- Acme\n    - 저자 논문 매칭\n        - 페이즈2 러너 재기동, 완료 예상 밤")
        self._daily("2026-08-22", "- Acme\n    - 다른 주제\n        - 무관한 세부")
        w = _note_work([], ["페이즈2 러너 로그: 중단·오류"], "한도 대기로 전환")
        out = report.archive_snippets(self.c, w, "2026-08-27")
        self.assertEqual(len(out), 1)                          # 무관한 일지는 안 걸림
        self.assertIn("[2026-08-20] Acme > 저자 논문 매칭: 페이즈2 러너 재기동", out[0])

    def test_skips_prev_day_dedupes_topic_and_caps(self):
        for d in ("2026-08-24", "2026-08-25", "2026-08-26"):
            self._daily(d, "- Acme\n    - 저자 논문 매칭\n        - 페이즈2 러너 진행\n        - 페이즈2 러너 검증\n"
                           "    - 수집 개편\n        - 페이즈2 러너 이관 예정")
        w = _note_work([], ["페이즈2 러너 로그"], "")
        out = report.archive_snippets(self.c, w, "2026-08-27", skip_day="2026-08-26")
        self.assertEqual(len(out), 2)                          # (서비스, 과제)당 한 줄 — 같은 과제 반복은 최신 것만
        self.assertTrue(all(o.startswith("- [2026-08-25]") for o in out))   # 직전 일지(08-26)는 제외
        self.assertEqual(len(report.archive_snippets(self.c, w, "2026-08-27", cap=1)), 1)

    def test_no_notes_no_query(self):
        self._daily("2026-08-25", "- Acme\n    - 저자 논문 매칭")
        self.assertEqual(report.archive_snippets(self.c, _note_work(["지시"], [], "응답"), "2026-08-27"), [])


class LabelGuardTests(unittest.TestCase):
    def test_flags_task_named_only_from_label(self):
        w = _note_work([], ["페이즈2 러너 로그: 중단"], "한도 소진으로 대기 모드 전환")
        md = "- Acme\n    - 페이즈2 배치 러너 운영\n        - 한도 대기 전환"
        p = report.label_only_topics(md, w)
        self.assertEqual(len(p), 1)
        self.assertIn("페이즈2", p[0])
        self.assertIn("내부 라벨에서만 유래", p[0])

    def test_paren_label_and_corpus_names_pass(self):
        w = _note_work([], ["페이즈2 러너 로그: 중단"], "한도 소진으로 대기 모드 전환")
        ok = "- Acme\n    - 저자 논문 매칭 러너(페이즈2)\n        - 한도 대기 전환"
        self.assertEqual(report.label_only_topics(ok, w), [])   # 괄호 보조는 면제
        bare = "- Acme\n    - 페이즈2 배치 러너 운영\n        - x"
        self.assertEqual(report.label_only_topics(bare, w, corpus="어제 주제: 페이즈2 정리"), [])

    def test_sessions_with_human_prompts_are_not_checked(self):
        w = _note_work(["페이즈2 러너 상태 봐줘"], ["페이즈2 러너 로그: 중단"], "확인했습니다")
        md = "- Acme\n    - 페이즈2 러너 점검\n        - x"
        self.assertEqual(report.label_only_topics(md, w), [])


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
        self.assertLessEqual(len(report._render_block("proj", work["proj"], _reg())), 6000)
        self.assertIn("(압축 요약)", report.build_day_prompt("2026-08-27", work, _reg()))

    def test_under_budget_makes_no_calls_and_failed_llm_keeps_raw(self):
        work = {"proj": {"sessions": [_session(3)], "turns": 3, "n_sessions": 1}}
        report.compress(work, lambda p: self.fail("호출되면 안 됨"))
        report.compress(work, lambda p: "", budget=100)
        self.assertNotIn("digest", work["proj"]["sessions"][0])


class PromptTests(unittest.TestCase):
    def test_day_prompt_injects_previous_topics_only(self):
        prev_md = "- Acme\n    - 고객 데이터 확충\n        - 세부 한 일 A\n- MADISON\n    - 리포트 품질"
        work = {"proj": {"sessions": [_session(1)], "turns": 1, "n_sessions": 1}}
        p = report.build_day_prompt("2026-08-27", work, _reg(["Acme"]), prev=("2026-08-26", prev_md))
        self.assertIn("직전 업무일지(2026-08-26)", p)
        self.assertIn("    - 고객 데이터 확충", p)
        self.assertNotIn("세부 한 일 A", p)
        self.assertIn("[S? · workstation · 09:00~10:00 · 턴 1]", p)
        self.assertIn("서비스 목록", p)
        self.assertIn("- Acme", p)
        self.assertIn("proposals", p)

    def test_topics_carries_only_ongoing_details(self):
        md = ("- Acme\n"
              "    - 고객 데이터 확충\n"
              "        - 세부 한 일 A를 마침\n"
              "        - 수집 러너 재기동, 완료 예상 내일 밤\n"
              "            - 후속 반영은 승인 대기\n"
              "- MADISON\n"
              "    - 리포트 품질\n")
        t = report.topics(md)
        self.assertNotIn("세부 한 일 A", t)                       # 끝난 세부는 여전히 제외
        self.assertIn("        - 수집 러너 재기동, 완료 예상 내일 밤", t)
        self.assertIn("        - 후속 반영은 승인 대기", t)        # 12칸도 8칸으로 정규화해 유지
        self.assertNotIn("            ", t)
        capped = report.topics(md, ongoing_cap=1)
        self.assertIn("수집 러너 재기동", capped)
        self.assertNotIn("승인 대기", capped)

    def test_day_prompt_carries_ongoing_detail_and_label_rule(self):
        prev_md = "- Acme\n    - 고객 데이터 확충\n        - 수집 러너 로그 감시 부착, 완료 예상 밤"
        work = {"proj": {"sessions": [_session(1)], "turns": 1, "n_sessions": 1}}
        p = report.build_day_prompt("2026-08-27", work, _reg(["Acme"]), prev=("2026-08-26", prev_md))
        self.assertIn("수집 러너 로그 감시 부착", p)               # 진행 중 세부가 이름 단서로 들어감
        self.assertIn("내부 라벨", p)                             # 알림 제목을 과제명으로 쓰지 않는 규칙

    def test_day_prompt_includes_archive_snippets(self):
        work = {"proj": {"sessions": [_session(1)], "turns": 1, "n_sessions": 1}}
        p = report.build_day_prompt("2026-08-27", work, _reg(["Acme"]),
                                    archive=["- [2026-08-20] Acme > 저자 논문 매칭: 페이즈2 러너 재기동"])
        self.assertIn("과거 일지에서 찾은 관련 항목", p)
        self.assertIn("저자 논문 매칭: 페이즈2 러너 재기동", p)
        self.assertNotIn("과거 일지에서 찾은",
                         report.build_day_prompt("2026-08-27", work, _reg(["Acme"])))

    def test_block_header_shows_mapping_strength(self):
        work = {"scratch": {"sessions": [_session(1)], "turns": 1, "n_sessions": 1}}
        weak = _reg(["Acme"], {"scratch": {"service": "Acme", "strength": "weak"}})
        self.assertIn("서비스: Acme (약한 기본값", report.build_day_prompt("2026-08-27", work, weak))
        strong = _reg(["Acme"], {"scratch": {"service": "Acme", "strength": "strong"}})
        self.assertIn("=== 서비스: Acme | 프로젝트: scratch", report.build_day_prompt("2026-08-27", work, strong))
        self.assertIn("서비스: scratch (매핑 없음", report.build_day_prompt("2026-08-27", work, _reg(["Acme"])))

    def test_period_prompt_uses_dailies(self):
        dailies = [("2026-08-24", "- Acme\n    - A"), ("2026-08-25", "- Acme\n    - B")]
        p = report.build_period_prompt("week", "2026-08-24", dailies, _reg())
        self.assertIn("=== 2026-08-24 (월) ===", p)
        self.assertIn("=== 2026-08-25 (화) ===", p)
        self.assertIn("주간보고", p)
        self.assertIn("업무일지의 최상위 불릿에 적힌", p)
        self.assertNotIn("로그:", p)

    def test_period_days_clamps_to_today(self):
        self.assertEqual(report.period_days("week", "2026-08-24", "2026-08-27"),
                         ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"])
        self.assertEqual(len(report.period_days("week", "2026-08-17", "2026-08-27")), 7)
        self.assertEqual(report.period_days("month", "2026-08-01", "2026-09-10")[-1], "2026-08-31")
        self.assertEqual(len(report.period_days("month", "2026-02-01", "2026-03-01")), 28)

    def test_fallbacks(self):
        work = {"proj": {"sessions": [_session(2)], "turns": 2, "n_sessions": 1}}
        self.assertTrue(report.fallback_md(work, _reg()).startswith("- proj\n    - 응답 "))
        self.assertEqual(report.fallback_period_md([("2026-08-24", "- Acme\n    - A")]),
                         "- 2026-08-24 (월)\n    - Acme\n        - A")
        self.assertEqual(report.fallback_period_md([]), report.EMPTY_MD)


class ExcludeMaskTests(unittest.TestCase):
    def setUp(self):
        self._saved = report._EXCL_RES
        report._EXCL_RES = (report._excl_pattern("acme-hunter"),)

    def tearDown(self):
        report._EXCL_RES = self._saved

    def test_masks_mention_with_separator_variants_but_keeps_the_line(self):
        for form in ("acme-hunter", "acme hunter", "acme_hunter", "AcmeHunter", "acme.hunter"):
            out = report.mask_excluded(f"오늘 {form} 배치를 손봤다")
            self.assertEqual(out, f"오늘 {report.EXCL_MASK} 배치를 손봤다", form)

    def test_word_boundary_prevents_partial_matches(self):
        self.assertEqual(report.mask_excluded("acme-hunters 팀"), "acme-hunters 팀")       # 뒤에 글자
        self.assertEqual(report.mask_excluded("xacme-hunter"), "xacme-hunter")           # 앞에 글자
        self.assertEqual(report.mask_excluded("(acme-hunter)"), f"({report.EXCL_MASK})")

    def test_gather_masks_instead_of_dropping(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        c.execute("INSERT INTO devices (id,name,token_hash,created_at) VALUES (1,'w','x','2026-08-27T00:00:00Z')")
        _ev(c, "2026-08-27T01:00:00Z", "prompt", {"prompt": "긴 지시 — acme-hunter 언급 포함, 본론은 결제 화면"})
        _ev(c, "2026-08-27T01:05:00Z", "turn_done", {"summary": "결제 화면 수정"})
        t = report.gather(c, "day", "2026-08-27")["proj"]["sessions"][0]["turns"][0]
        self.assertEqual(t["prompts"], [f"긴 지시 — {report.EXCL_MASK} 언급 포함, 본론은 결제 화면"])


class LastEventTests(unittest.TestCase):
    def test_period_last_event(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        c.execute("INSERT INTO devices (id,name,token_hash,created_at) VALUES (1,'w','x','2026-08-27T00:00:00Z')")
        _ev(c, "2026-08-25T01:00:00Z", "prompt", {"prompt": "x"})
        _ev(c, "2026-08-27T03:00:00Z", "turn_done", {"summary": "y"})
        _ev(c, "2026-09-02T03:00:00Z", "turn_done", {"summary": "z"})
        self.assertEqual(report.last_event_in(c, "week", "2026-08-24"), "2026-08-27T03:00:00Z")
        self.assertEqual(report.last_event_in(c, "month", "2026-08-01"), "2026-08-27T03:00:00Z")
        self.assertIsNone(report.last_event_in(c, "week", "2026-08-10"))


class ValidateTests(unittest.TestCase):
    ALLOWED = ["Acme", "MADISON"]

    def test_clean_report_passes(self):
        md = "- Acme\n    - 결제\n        - 카드 결제 오류 수정\n- MADISON\n    - 리포트\n        - 검증기 추가"
        self.assertEqual(report.validate(md, self.ALLOWED), [])

    def test_unknown_top_level_and_headers_and_indent(self):
        md = "## 제목\n- Acme\n    - 결제\n- 신규서비스\n   - 세 칸\n        - x\n일반 문장"
        probs = report.validate(md, self.ALLOWED)
        self.assertTrue(any("헤더" in x for x in probs))
        self.assertTrue(any("'신규서비스'" in x for x in probs))
        self.assertTrue(any("들여쓰기 3칸" in x for x in probs))
        self.assertTrue(any("불릿이 아닌 줄" in x for x in probs))

    def test_empty_top_level_meta_phrases_and_decoration(self):
        md = "- Acme\n- MADISON (허브)\n    - 로그가 잘려 있어 확인 가능한 범위까지만 정리"
        probs = report.validate(md, self.ALLOWED)
        self.assertTrue(any("빈 최상위 'Acme'" in x for x in probs))
        self.assertTrue(any("부연·볼드" in x for x in probs))
        self.assertTrue(any("메타 문구" in x for x in probs))

    def test_block_services_and_proposals_are_allowed(self):
        md = "- 새제안\n    - 과제\n- proj\n    - 과제"
        self.assertEqual(report.validate(md, self.ALLOWED + ["새제안"], block_services=["proj"]), [])

    def test_repair_prompt_lists_problems(self):
        p = report.repair_prompt("- x", ["1행: 헤더"])
        self.assertIn("- 1행: 헤더", p)
        self.assertIn("업무일지:\n- x", p)
        self.assertEqual(report.top_level_names("- A\n    - a\n- B"), ["A", "B"])


if __name__ == "__main__":
    unittest.main()


class MetaPhraseTests(unittest.TestCase):
    def test_ordinary_words_are_not_meta(self):
        md = "- Acme\n    - 광고 스트립 모바일 대응 — 고정 2행으로 글자 잘림 해소\n    - 이미지 중략 처리 옵션 추가"
        self.assertEqual(report.validate(md, ["Acme"]), [])

    def test_log_complaints_are_meta(self):
        for line in ("로그가 잘려 있어 확인 가능한 범위까지만 정리", "나머지 기록을 보내 주세요", "…(중략)… 이후 작업"):
            probs = report.validate(f"- Acme\n    - {line}", ["Acme"])
            self.assertTrue(any("메타 문구" in x for x in probs), line)

    def test_period_prompt_forbids_proposals(self):
        p = report.build_period_prompt("week", "2026-08-24", [("2026-08-24", "- Acme\n    - A")], _reg(["Acme"]))
        self.assertNotIn("proposals에", p)
        self.assertIn("목록에 없는 이름은 최상위로 쓰지 않는다", p)
        d = report.build_day_prompt("2026-08-24", {"proj": {"sessions": [_session(1)], "turns": 1, "n_sessions": 1}}, _reg(["Acme"]))
        self.assertIn("저장소·디렉터리 이름은", d)

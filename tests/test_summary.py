import contextlib
import importlib.util
import io
import re
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import db, llm, state, summary
from server.config import CFG

# 일회성 스크립트 — 패키지가 아니라 경로로 적재한다
_spec = importlib.util.spec_from_file_location(
    "resummarize_tasks", Path(__file__).resolve().parents[1] / "scripts" / "resummarize_tasks.py")
resummarize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resummarize)

# 요약 입력의 섹션 머리말 — 사양에 고정된 문구
H_PREV = "[이전 지시 — 오래된 순, 맥락 참고용]"
H_LAST = "[최신 지시 — 요약 대상]"
H_RESP = "[참고 — 에이전트의 최근 응답 앞부분]"
ESTIMATE = ("다음은 AI 에이전트의 마지막 응답이다. 어떤 작업/대화였는지 한 문장으로"
            " 추정 요약하라: ")


class SummaryDbCase(unittest.TestCase):
    """인메모리 DB를 db 모듈의 공용 연결로 바꿔 끼운다 — tick의 3-병렬 스레드도 같은 연결을 쓴다."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.db_patch = mock.patch.object(db, "_conn", self.conn)
        self.db_patch.start()
        for i, name in ((1, "device-a"), (2, "device-b")):
            self.conn.execute(
                "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at)"
                " VALUES (?,?,'x',datetime('now'),datetime('now'))", (i, name))

    def tearDown(self):
        self.db_patch.stop()
        self.conn.close()

    def prompt_event(self, prompt, *, device=1, agent="claude-code", session="s1",
                     event="prompt", raw=None):
        """events 행 직접 삽입 — 깨진 payload 같은 비정상 입력도 만들어야 해서 ingest를 거치지 않는다."""
        payload = raw if raw is not None else json.dumps({"prompt": prompt}, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO events (device_id, agent, session_id, event, ts_device, ts_hub, payload)"
            " VALUES (?,?,?,?,'2026-10-30T00:00:00Z','2026-10-30T00:00:00Z',?)",
            (device, agent, session, event, payload))

    def add_session(self, *, last_prompt, last_summary="", device=1, agent="claude-code",
                    session="s1", state_="awaiting_input"):
        self.conn.execute(
            "INSERT INTO sessions (device_id, agent, session_id, state, last_prompt, last_summary,"
            " last_seen_hub) VALUES (?,?,?,?,?,?,datetime('now'))",
            (device, agent, session, state_, last_prompt, last_summary))

    def row(self, *, device=1, agent="claude-code", session="s1"):
        return dict(self.conn.execute(
            "SELECT device_id, agent, session_id, last_prompt, last_summary FROM sessions"
            " WHERE device_id=? AND agent=? AND session_id=?", (device, agent, session)).fetchone())

    def session_flow(self, prompts, *, last_summary="", **kw):
        """prompt 이벤트를 순서대로 쌓고 마지막 것을 last_prompt로 둔 세션을 만든다."""
        for p in prompts:
            self.prompt_event(p, **kw)
        self.add_session(last_prompt=prompts[-1], last_summary=last_summary, **kw)
        return self.row(**kw)

    def build(self, r=None, **kw):
        return summary.build_input(self.conn, r or self.row(**kw))

    def stored(self, session="s1"):
        return dict(self.conn.execute(
            "SELECT task_summary, summary_source FROM sessions WHERE session_id=?",
            (session,)).fetchone())


class ConstantTests(unittest.TestCase):
    def test_limits_and_prompt_version(self):
        self.assertEqual(summary.PREV_MAX, 4)
        self.assertEqual(summary.PREV_CHARS, 200)
        # 입력 형식이 바뀐 판 — 캐시 키에 포함돼 옛 단일 지시 요약이 재사용되지 않는다
        self.assertEqual(summary.PROMPT_VERSION, "2026-10-30")


class SinglePromptInputTests(SummaryDbCase):
    def test_single_prompt_is_passed_through_byte_for_byte(self):
        text = "  로그인 화면의 오류 문구를 고쳐줘\n둘째 줄 \t끝  "
        r = self.session_flow([text], last_summary="수정했습니다")
        self.assertEqual(self.build(r), text)

    def test_session_without_any_events_is_passed_through(self):
        # 이벤트가 정리됐거나 훅 없이 들어온 세션 — 이전 지시가 없으므로 원문 그대로
        self.add_session(last_prompt="상태 점검해줘", last_summary="정상")
        self.assertEqual(self.build(), "상태 점검해줘")

    def test_attachment_placeholder_appends_reply_when_summary_exists(self):
        for ph in ("[Image #1]", "[Pasted text #2 +40 lines]", "[Attachment: a.png]"):
            with self.subTest(placeholder=ph):
                r = {"device_id": 1, "agent": "claude-code", "session_id": "none",
                     "last_prompt": f"{ph} 이거 봐줘", "last_summary": "표를 확인했습니다"}
                self.assertEqual(
                    summary.build_input(self.conn, r),
                    f"{ph} 이거 봐줘\n\n(참고 — 위 지시에 대한 에이전트 응답 앞부분: 표를 확인했습니다)")

    def test_placeholder_without_last_summary_is_plain(self):
        r = {"device_id": 1, "agent": "claude-code", "session_id": "none",
             "last_prompt": "[Image #1] 이거 봐줘", "last_summary": ""}
        self.assertEqual(summary.build_input(self.conn, r), "[Image #1] 이거 봐줘")
        r["last_summary"] = None
        self.assertEqual(summary.build_input(self.conn, r), "[Image #1] 이거 봐줘")

    def test_no_placeholder_never_appends_reply(self):
        r = {"device_id": 1, "agent": "claude-code", "session_id": "none",
             "last_prompt": "Image 파일 정리해줘", "last_summary": "정리 완료"}
        self.assertEqual(summary.build_input(self.conn, r), "Image 파일 정리해줘")

    def test_single_prompt_event_equal_to_last_prompt_is_not_a_previous_instruction(self):
        # 최신 지시 자신의 prompt 이벤트가 '이전 지시'로 새면 안 된다
        r = self.session_flow(["[Image #1] 화면 확인"], last_summary="확인함")
        out = self.build(r)
        self.assertNotIn(H_PREV, out)
        self.assertTrue(out.startswith("[Image #1] 화면 확인"))


class FollowUpInputTests(SummaryDbCase):
    def test_short_followups_are_listed_oldest_first_with_latest_once(self):
        r = self.session_flow(["작업 A 꺼줘", "중지", "제거"],
                              last_summary="작업 A를 제거했습니다")
        out = self.build(r)
        self.assertEqual(out, "\n".join([
            H_PREV,
            "- 작업 A 꺼줘",
            "- 중지",
            H_LAST,
            "제거",
            H_RESP,
            "작업 A를 제거했습니다",
        ]))
        self.assertEqual(out.count("\n제거\n"), 1)          # 최신 지시는 한 줄로 한 번만
        self.assertLess(out.index("작업 A 꺼줘"), out.index("- 중지"))

    def test_response_section_omitted_without_last_summary(self):
        for empty in ("", None):
            with self.subTest(last_summary=empty):
                self.conn.execute("DELETE FROM sessions")
                self.conn.execute("DELETE FROM events")
                r = self.session_flow(["첫 지시", "후속"], last_summary=empty)
                self.assertEqual(self.build(r), "\n".join([
                    H_PREV, "- 첫 지시", H_LAST, "후속"]))
                self.assertNotIn(H_RESP, self.build(r))

    def test_placeholder_with_previous_uses_section_format_not_parenthetical(self):
        r = self.session_flow(["버그 고쳐줘", "[Image #1] 이 화면"], last_summary="확인함")
        out = self.build(r)
        self.assertIn(H_PREV, out)
        self.assertNotIn("(참고 — 위 지시에 대한", out)
        self.assertTrue(out.endswith(f"{H_RESP}\n확인함"))

    def test_ingested_prompt_events_feed_the_input(self):
        # 실제 수집 경로(state.ingest)로 쌓인 prompt 이벤트도 그대로 읽힌다
        for i, p in enumerate(("서버 로그 정리해줘", "계속", "마무리")):
            state.ingest(self.conn, 1, {
                "agent": "claude-code", "session_id": "s1", "event_id": f"e{i}", "event": "prompt",
                "ts": f"2026-10-30T00:00:0{i}Z", "project": "demo", "branch": "main",
                "detail": {"prompt": p}})
        r = self.row()
        self.assertEqual(r["last_prompt"], "마무리")
        self.assertEqual(self.build(r), "\n".join([
            H_PREV, "- 서버 로그 정리해줘", "- 계속", H_LAST, "마무리"]))


class PreviousSelectionTests(SummaryDbCase):
    def prev_lines(self, out):
        head = out.split(f"\n{H_LAST}\n")[0]
        self.assertTrue(head.startswith(H_PREV + "\n"))
        return head.split("\n")[1:]

    def test_only_most_recent_prev_max_are_kept(self):
        prompts = [f"지시 {i}" for i in range(1, 8)]          # 7개 → 이전 6개 중 최근 4개
        r = self.session_flow(prompts)
        self.assertEqual(self.prev_lines(self.build(r)),
                         ["- 지시 3", "- 지시 4", "- 지시 5", "- 지시 6"])
        self.assertTrue(self.build(r).endswith(f"{H_LAST}\n지시 7"))

    def test_exactly_prev_max_previous_are_all_kept(self):
        r = self.session_flow([f"지시 {i}" for i in range(1, 6)])   # 이전 4개
        self.assertEqual(len(self.prev_lines(self.build(r))), 4)
        self.assertIn("- 지시 1", self.build(r))

    def test_prev_prompt_is_cut_to_prev_chars(self):
        long = "가" * 300
        r = self.session_flow([long, "나" * 200, "후속"])
        lines = self.prev_lines(self.build(r))
        self.assertEqual(lines[0], "- " + "가" * 200)
        self.assertEqual(lines[1], "- " + "나" * 200)            # 경계값은 그대로

    def test_latest_prompt_is_not_truncated(self):
        long = "다" * 500
        r = self.session_flow(["이전", long])
        self.assertTrue(self.build(r).endswith(f"{H_LAST}\n{long}"))

    def test_consecutive_duplicates_collapse(self):
        r = self.session_flow(["진행", "진행", "진행", "다른 지시", "다른 지시", "마지막"])
        self.assertEqual(self.prev_lines(self.build(r)), ["- 진행", "- 다른 지시"])

    def test_duplicates_collapse_before_applying_prev_max(self):
        # 중복이 한도를 잠식해 정보가 밀려나면 안 된다 — 중복 제거 후 최근 4개
        r = self.session_flow(["a1", "a1", "a1", "a2", "a3", "a4", "a4", "a4", "마지막"])
        self.assertEqual(self.prev_lines(self.build(r)), ["- a1", "- a2", "- a3", "- a4"])

    def test_broken_and_empty_payloads_are_ignored(self):
        self.prompt_event("첫 지시")
        self.prompt_event(None, raw="{not json")
        self.prompt_event("", raw=json.dumps({"prompt": ""}))
        self.prompt_event("", raw=json.dumps({"prompt": "   "}))
        self.prompt_event("", raw=json.dumps({"other": "x"}))
        self.prompt_event(None, raw="")
        self.prompt_event(None, raw="null")
        self.prompt_event("둘째 지시")
        self.prompt_event("마지막")
        self.add_session(last_prompt="마지막")
        self.assertEqual(self.prev_lines(self.build()), ["- 첫 지시", "- 둘째 지시"])

    def test_only_broken_prompts_before_latest_behaves_like_single(self):
        self.prompt_event(None, raw="{broken")
        self.prompt_event("", raw=json.dumps({"prompt": ""}))
        self.prompt_event("유일한 지시")
        self.add_session(last_prompt="유일한 지시", last_summary="완료")
        self.assertEqual(self.build(), "유일한 지시")

    def test_non_prompt_events_are_not_previous_instructions(self):
        self.prompt_event("진짜 이전 지시")
        self.prompt_event("턴 종료 이벤트의 prompt 키", event="turn_done")
        self.prompt_event("도구 이벤트", event="tool_start")
        self.prompt_event("마지막")
        self.add_session(last_prompt="마지막")
        self.assertEqual(self.prev_lines(self.build()), ["- 진짜 이전 지시"])

    def test_other_session_device_and_agent_are_not_mixed_in(self):
        self.prompt_event("같은 세션 이전")
        self.prompt_event("다른 세션", session="s2")
        self.prompt_event("다른 기기", device=2)
        self.prompt_event("다른 에이전트", agent="codex-cli")
        self.prompt_event("마지막")
        self.add_session(last_prompt="마지막")
        out = self.build()
        self.assertEqual(self.prev_lines(out), ["- 같은 세션 이전"])
        for other in ("다른 세션", "다른 기기", "다른 에이전트"):
            self.assertNotIn(other, out)

    def test_each_agent_sees_only_its_own_flow_for_same_session_id(self):
        self.session_flow(["클로드 시작", "클로드 후속"], agent="claude-code")
        self.session_flow(["코덱스 시작", "코덱스 후속"], agent="codex-cli")
        a = self.build(agent="claude-code")
        b = self.build(agent="codex-cli")
        self.assertIn("클로드 시작", a)
        self.assertNotIn("코덱스", a)
        self.assertIn("코덱스 시작", b)
        self.assertNotIn("클로드", b)



class InjectedPromptInputTests(SummaryDbCase):
    """주입 메시지(알림·타 세션 메시지)는 이전 지시 목록에 들어가지 않는다."""

    NOTE = '<task-notification><summary>Monitor event: "x"</summary></task-notification>'
    XSESSION = '<cross-session-message from="s2" from-name="peer-a">확인 부탁</cross-session-message>'

    def test_injected_messages_are_skipped_in_previous_list(self):
        self.prompt_event("스크립트 고쳐줘")
        self.prompt_event(self.NOTE)
        self.prompt_event(self.XSESSION)
        self.prompt_event("<system-reminder>r</system-reminder>")
        self.prompt_event('<agent-message from="impl-a">끝</agent-message>')
        self.prompt_event("이어서 테스트도")
        self.add_session(last_prompt="이어서 테스트도", last_summary="완료")
        out = self.build()
        self.assertEqual(out, "\n".join([H_PREV, "- 스크립트 고쳐줘", H_LAST, "이어서 테스트도", H_RESP, "완료"]))
        for tag in ("task-notification", "cross-session", "system-reminder", "agent-message"):
            self.assertNotIn(tag, out)

    def test_injected_between_equal_prompts_merges_them(self):
        # 건너뛰기가 연속 중복 제거보다 먼저 — A, 알림, A 는 A 하나
        for p in ("A 작업", self.NOTE, "A 작업", "B 작업", "최신"):
            self.prompt_event(p)
        self.add_session(last_prompt="최신")
        self.assertEqual(self.build(), "\n".join([H_PREV, "- A 작업", "- B 작업", H_LAST, "최신"]))

    def test_notification_after_latest_prompt_does_not_become_context(self):
        # ingest는 알림이 와도 last_prompt를 유지하므로 알림 이벤트가 최신 지시 뒤에 쌓인다
        for p in ("첫 지시", "최신 지시", self.NOTE, self.XSESSION):
            self.prompt_event(p)
        self.add_session(last_prompt="최신 지시", last_summary="응답")
        self.assertEqual(self.build(), "\n".join([
            H_PREV, "- 첫 지시", H_LAST, "최신 지시", H_RESP, "응답"]))

    def test_session_with_only_injected_besides_latest_is_single_prompt(self):
        for p in (self.NOTE, "유일한 지시", self.XSESSION):
            self.prompt_event(p)
        self.add_session(last_prompt="유일한 지시", last_summary="응답")
        self.assertEqual(self.build(), "유일한 지시")

    def test_question_reply_is_kept_as_human_instruction(self):
        reply = "<send_user_message_question_reply>예</send_user_message_question_reply>"
        for p in ("질문 유발 지시", reply, "최신"):
            self.prompt_event(p)
        self.add_session(last_prompt="최신")
        self.assertIn("- " + reply, self.build())

    def test_ingest_flow_end_to_end(self):
        # 실제 수집 경로: 알림이 섞여도 last_prompt와 입력이 사람 지시 기준으로 유지된다
        flow = ["서버 로그 정리해줘", "계속", self.NOTE, self.XSESSION]
        for i, p in enumerate(flow):
            state.ingest(self.conn, 1, {
                "agent": "claude-code", "session_id": "s1", "event_id": f"e{i}", "event": "prompt",
                "ts": f"2026-10-30T00:00:0{i}Z", "project": "demo", "branch": "main",
                "detail": {"prompt": p}})
        r = self.row()
        self.assertEqual(r["last_prompt"], "계속")
        self.assertEqual(self.build(r), "\n".join([H_PREV, "- 서버 로그 정리해줘", H_LAST, "계속"]))



NOTE = '<task-notification><summary>Monitor event: "x"</summary></task-notification>'
XSESSION = '<cross-session-message from="s2" from-name="peer-a">확인 부탁</cross-session-message>'


def seed_recovery_fixture(conn):
    """오염 세션(last_prompt가 주입 메시지) 여러 형태 + 건드리면 안 되는 세션."""
    conn.execute("INSERT OR IGNORE INTO devices (id,name,token_hash,created_at,last_seen_at)"
                 " VALUES (1,'device-a','x',datetime('now'),datetime('now'))")

    def flow(session, prompts, last_prompt, *, frontend="cli", task_summary="오염된 요약", last_summary=""):
        for p in prompts:
            conn.execute(
                "INSERT INTO events (device_id, agent, session_id, event, ts_device, ts_hub, payload)"
                " VALUES (1,'claude-code',?,'prompt','2026-10-30T00:00:00Z','2026-10-30T00:00:00Z',?)",
                (session, json.dumps({"prompt": p}, ensure_ascii=False)))
        conn.execute(
            "INSERT INTO sessions (device_id, agent, session_id, state, frontend, last_prompt, last_summary,"
            " task_summary, summary_source, last_seen_hub) VALUES (1,'claude-code',?,'awaiting_input',?,?,?,?,"
            " 'llm', datetime('now'))", (session, frontend, last_prompt, last_summary, task_summary))

    flow("rec", ["백업 작업 멈춰줘", "중지", NOTE, XSESSION], NOTE, last_summary="중지했습니다")
    flow("rec-null-fe", ["첫 지시", "둘째 지시", NOTE], NOTE, frontend=None)
    flow("rec-long", [NOTE, "가" * 700, XSESSION], XSESSION)
    flow("only-injected", [NOTE, XSESSION], NOTE)
    flow("auto", ["자동화 지시", NOTE], NOTE, frontend="auto")
    flow("healthy-multi", ["이전", "정상 최신"], "정상 최신", task_summary="정상 요약")
    flow("healthy-single", ["단일 지시"], "단일 지시", task_summary="단일 요약")


class RecoveryTests(SummaryDbCase):
    def setUp(self):
        super().setUp()
        seed_recovery_fixture(self.conn)
        # 운영 DB 경로로 새는 일이 없도록 존재하지 않는 임시 경로로 바꿔 둔다
        self.tmp = tempfile.TemporaryDirectory()
        self.path_patch = mock.patch.object(CFG, "db_path", Path(self.tmp.name) / "none.db")
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()
        super().tearDown()

    def snapshot(self, conn=None):
        conn = conn or self.conn
        return ([tuple(r) for r in conn.execute("SELECT * FROM sessions ORDER BY session_id")],
                conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def sess(self, sid):
        return dict(self.conn.execute(
            "SELECT last_prompt, task_summary, summary_source FROM sessions WHERE session_id=?",
            (sid,)).fetchone())

    def run_main(self, *argv):
        out = io.StringIO()
        calls = []

        def fake(site, prompt, **kw):
            calls.append(prompt)
            return llm.Result(ok=True, text="복구 후 재요약")

        with mock.patch.object(sys, "argv", ["resummarize_tasks.py", *argv]), \
                mock.patch.object(llm, "run", side_effect=fake), contextlib.redirect_stdout(out):
            resummarize.main()
        return out.getvalue(), calls

    def test_recoveries_pick_injected_last_prompt_of_human_sessions_only(self):
        recs = {r["session_id"]: r for r in resummarize.recoveries(self.conn, 0)}
        self.assertEqual(set(recs), {"rec", "rec-null-fe", "rec-long", "only-injected"})
        self.assertEqual(recs["rec"]["new_last_prompt"], "중지")            # 마지막 비주입 prompt
        self.assertEqual(recs["rec-null-fe"]["new_last_prompt"], "둘째 지시")  # frontend NULL도 사람 세션
        self.assertEqual(recs["rec-long"]["new_last_prompt"], "가" * 600)    # ingest와 같은 600자
        self.assertIsNone(recs["only-injected"]["new_last_prompt"])          # 되돌릴 지시 없음 → NULL

    def test_dry_run_changes_nothing_and_never_calls_llm(self):
        path = Path(self.tmp.name) / "copy.db"
        file_db = sqlite3.connect(path)
        file_db.row_factory = sqlite3.Row
        db.migrate(file_db)
        seed_recovery_fixture(file_db)
        file_db.commit()
        before = self.snapshot(file_db)
        file_db.close()
        with mock.patch.object(CFG, "db_path", path):
            out, calls = self.run_main()
        self.assertEqual(calls, [])
        self.assertIn("dry-run", out)
        check = sqlite3.connect(path)
        check.row_factory = sqlite3.Row
        try:
            self.assertEqual(self.snapshot(check), before)
        finally:
            check.close()

    def test_apply_restores_last_prompt_clears_summary_and_resummarizes(self):
        out, calls = self.run_main("--apply")
        self.assertEqual(self.sess("rec"), {"last_prompt": "중지", "task_summary": "복구 후 재요약",
                                            "summary_source": "llm"})
        self.assertEqual(self.sess("rec-null-fe")["last_prompt"], "둘째 지시")
        self.assertEqual(self.sess("rec-long")["last_prompt"], "가" * 600)
        # 되돌린 last_prompt 기준 입력으로 요약했고 주입 메시지는 입력에 없다
        rec_in = next(c for c in calls if "백업 작업 멈춰줘" in c)
        self.assertIn("- 백업 작업 멈춰줘", rec_in)
        self.assertIn(f"{H_LAST}\n중지", rec_in)
        for c in calls:
            self.assertNotIn("task-notification", c)
            self.assertNotIn("cross-session-message", c)

    def file_db_copy(self):
        path = Path(self.tmp.name) / "copy.db"
        file_db = sqlite3.connect(path)
        file_db.row_factory = sqlite3.Row
        db.migrate(file_db)
        seed_recovery_fixture(file_db)
        file_db.commit()
        file_db.close()
        return path

    def test_dry_run_target_count_equals_apply_count(self):
        # 회귀: 복구됐지만 사람 지시가 1개뿐인 세션('rec-long')이 apply에서만 빠져 task_summary가 NULL로 남던 문제
        with mock.patch.object(CFG, "db_path", self.file_db_copy()):
            dry_out, _ = self.run_main()
        dry_n = int(re.search(r"재요약 대상 세션 (\d+)개", dry_out).group(1))
        out, calls = self.run_main("--apply")
        apply_n = int(re.search(r"대상 세션 (\d+)개 재요약 시작", out).group(1))
        self.assertEqual(dry_n, apply_n)
        self.assertEqual(self.sess("rec-long")["task_summary"], "복구 후 재요약")   # 지시 1개뿐인 복구 세션도 재요약

    def test_limit_caps_resummary_targets_but_not_recovery(self):
        out, calls = self.run_main("--apply", "--limit", "1")
        self.assertIn("복구 4개", out)
        self.assertEqual(int(re.search(r"대상 세션 (\d+)개 재요약 시작", out).group(1)), 1)
        self.assertEqual(self.sess("rec")["last_prompt"], "중지")             # 복구는 --limit과 무관하게 전체
        self.assertEqual(self.sess("rec-null-fe")["last_prompt"], "둘째 지시")
        self.assertIsNone(self.sess("only-injected")["last_prompt"])

    def test_dry_run_unopenable_db_exits_with_guidance(self):
        with mock.patch.object(CFG, "db_path", Path(self.tmp.name) / "missing.db"):
            with self.assertRaises(SystemExit) as cm:
                self.run_main()
        self.assertIn("허브가 떠 있지 않으면", str(cm.exception))

    def test_apply_session_with_only_injected_gets_null_last_prompt(self):
        self.run_main("--apply")
        got = self.sess("only-injected")
        self.assertIsNone(got["last_prompt"])
        self.assertIsNone(got["task_summary"])               # 요약할 재료가 없어 비운 채로 둔다

    def test_apply_leaves_auto_and_healthy_sessions_alone(self):
        self.run_main("--apply")
        self.assertEqual(self.sess("auto")["last_prompt"], NOTE)             # 자동화 세션은 사람 세션이 아니다
        self.assertEqual(self.sess("auto")["task_summary"], "오염된 요약")
        self.assertEqual(self.sess("healthy-single"),
                         {"last_prompt": "단일 지시", "task_summary": "단일 요약", "summary_source": "llm"})

    def test_apply_is_rerunnable(self):
        self.run_main("--apply")
        self.assertEqual(resummarize.recoveries(self.conn, 0), [])           # 복구된 세션은 더 이상 대상 아님
        before = self.snapshot()
        _, calls = self.run_main("--apply")
        self.assertEqual(self.snapshot()[1], before[1])
        self.assertEqual(self.sess("rec")["last_prompt"], "중지")

    def test_prompt_changed_after_recovery_is_not_overwritten(self):
        def new_prompt_arrives(site, prompt, **kw):
            if "백업 작업 멈춰줘" in prompt:        # 3-병렬 워커가 같은 연결을 쓰므로 해당 세션 호출에서만, 락 안에서
                with db.tx() as c:
                    c.execute("UPDATE sessions SET last_prompt='그 사이 새 지시' WHERE session_id='rec'")
            return llm.Result(ok=True, text="낡은 요약")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["resummarize_tasks.py", "--apply"]), \
                mock.patch.object(llm, "run", side_effect=new_prompt_arrives), contextlib.redirect_stdout(out):
            resummarize.main()
        got = self.sess("rec")
        self.assertEqual(got["last_prompt"], "그 사이 새 지시")
        self.assertIsNone(got["task_summary"])


class EmptyPromptInputTests(SummaryDbCase):
    def test_empty_last_prompt_falls_back_to_reply_estimate(self):
        for empty in ("", None):
            with self.subTest(last_prompt=empty):
                r = {"device_id": 1, "agent": "codex-cli", "session_id": "none",
                     "last_prompt": empty, "last_summary": "테스트를 추가했습니다"}
                self.assertEqual(summary.build_input(self.conn, r),
                                 ESTIMATE + "테스트를 추가했습니다")

    def test_empty_last_prompt_ignores_stored_prompt_events(self):
        # 지시가 비어 있으면 응답 추정이 우선 — 옛 지시를 끌어와 섞지 않는다
        self.prompt_event("옛 지시")
        self.add_session(last_prompt="", last_summary="앱 응답")
        out = self.build()
        self.assertEqual(out, ESTIMATE + "앱 응답")
        self.assertNotIn(H_PREV, out)


class TickTests(SummaryDbCase):
    def run_tick(self, side_effect):
        calls = []

        def fake(site, prompt, **kw):
            calls.append(prompt)
            return side_effect(prompt)

        with mock.patch.object(llm, "run", side_effect=fake):
            n = summary.tick()
        return n, calls

    @staticmethod
    def ok(text):
        return lambda prompt: llm.Result(ok=True, text=text)

    def test_llm_receives_previous_instructions_and_result_is_stored(self):
        self.session_flow(["작업 A 꺼줘", "중지", "제거"], last_summary="제거 완료")
        n, calls = self.run_tick(self.ok("작업 A를 제거"))
        self.assertEqual(n, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn(H_PREV, calls[0])
        self.assertIn("- 작업 A 꺼줘", calls[0])
        self.assertIn("- 중지", calls[0])
        self.assertIn(f"{H_LAST}\n제거\n", calls[0])
        self.assertEqual(self.stored(),
                         {"task_summary": "작업 A를 제거", "summary_source": "llm"})

    def test_single_prompt_session_sends_prompt_only(self):
        self.session_flow(["README 오탈자 고쳐줘"])
        _, calls = self.run_tick(self.ok("README 오탈자 수정"))
        self.assertEqual(len(calls), 1)
        self.assertIn("README 오탈자 고쳐줘", calls[0])
        self.assertNotIn(H_PREV, calls[0])
        self.assertEqual(self.stored()["summary_source"], "llm")

    def test_llm_failure_fills_with_prompt_head_as_fallback(self):
        long = "긴 지시 " * 40
        self.session_flow(["이전", long])
        _, calls = self.run_tick(lambda p: llm.Result(ok=False, error="timeout"))
        self.assertEqual(len(calls), 1)
        got = self.stored()
        self.assertEqual(got["summary_source"], "fallback")
        # 요약 입력 전체(이전 지시 포함)가 아니라 최신 지시 원문의 앞부분으로 채운다
        self.assertEqual(got["task_summary"], long[:90])
        self.assertNotIn(H_PREV, got["task_summary"])

    def test_fallback_without_prompt_uses_reply_head(self):
        self.add_session(last_prompt="", last_summary="응답 " * 60)
        self.run_tick(lambda p: llm.Result(ok=False))
        got = self.stored()
        self.assertEqual(got["summary_source"], "fallback")
        self.assertEqual(got["task_summary"], ("응답 " * 60)[:90])

    def test_failure_is_not_cached(self):
        self.session_flow(["일회성 지시"])
        self.run_tick(lambda p: llm.Result(ok=False))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM summary_cache").fetchone()[0], 0)

    def test_prompt_changed_during_summarize_is_not_overwritten(self):
        self.session_flow(["오래 걸리는 요약 대상"])

        def new_prompt_arrives(prompt):
            # 요약 중에 새 지시가 도착 — 낡은 입력의 요약이 새 지시 위에 저장되면 안 된다
            self.conn.execute("UPDATE sessions SET last_prompt='그 사이 온 새 지시' WHERE session_id='s1'")
            return llm.Result(ok=True, text="낡은 요약")

        self.run_tick(new_prompt_arrives)
        got = self.stored()
        self.assertIsNone(got["task_summary"])
        self.assertIsNone(got["summary_source"])
        self.assertEqual(self.row()["last_prompt"], "그 사이 온 새 지시")

        # 다음 틱에서 새 지시 기준으로 다시 요약된다
        self.run_tick(self.ok("새 지시 요약"))
        self.assertEqual(self.stored(), {"task_summary": "새 지시 요약", "summary_source": "llm"})

    def test_prompt_changed_during_failed_summarize_is_not_filled_either(self):
        self.session_flow(["원래 지시"])

        def changed(prompt):
            self.conn.execute("UPDATE sessions SET last_prompt='바뀐 지시' WHERE session_id='s1'")
            return llm.Result(ok=False)

        self.run_tick(changed)
        self.assertIsNone(self.stored()["task_summary"])

    def test_session_with_summary_is_not_resummarized(self):
        self.session_flow(["지시"])
        self.run_tick(self.ok("첫 요약"))
        n, calls = self.run_tick(self.ok("두 번째"))
        self.assertEqual((n, calls), (0, []))
        self.assertEqual(self.stored()["task_summary"], "첫 요약")


class CacheKeyTests(SummaryDbCase):
    def run_tick(self, side_effect):
        calls = []

        def fake(site, prompt, **kw):
            calls.append(prompt)
            return llm.Result(ok=True, text=side_effect(prompt))

        with mock.patch.object(llm, "run", side_effect=fake):
            summary.tick()
        return calls

    def test_same_latest_prompt_with_different_history_gets_separate_summaries(self):
        # 회귀: 최신 지시만 같다고 전역 캐시로 같은 요약이 복사되던 문제
        self.session_flow(["백업 작업 멈춰줘", "중지", "제거"], session="backup")
        self.session_flow(["오래된 로그 파일 확인해줘", "정리", "제거"], session="logs")

        def by_history(prompt):
            return "백업 작업 제거" if "백업 작업 멈춰줘" in prompt else "오래된 로그 제거"

        calls = self.run_tick(by_history)
        self.assertEqual(len(calls), 2)                      # 둘 다 LLM을 탔다(캐시 공유 없음)
        self.assertEqual(self.stored("backup")["task_summary"], "백업 작업 제거")
        self.assertEqual(self.stored("logs")["task_summary"], "오래된 로그 제거")
        keys = self.conn.execute("SELECT COUNT(DISTINCT phash) FROM summary_cache").fetchone()[0]
        self.assertEqual(keys, 2)

    def test_same_single_prompt_in_two_sessions_reuses_cache(self):
        template = "정기 점검 템플릿: 서버 상태를 확인하라"
        self.session_flow([template], session="a")
        self.run_tick(lambda p: "서버 상태 점검")
        # 병렬 틱의 동시 미스를 피하려고 두 번째 세션은 첫 요약이 캐시된 뒤에 도착시킨다
        self.session_flow([template], session="b")
        calls = self.run_tick(lambda p: "호출되면 안 됨")
        self.assertEqual(calls, [])
        self.assertEqual(self.stored("a")["task_summary"], "서버 상태 점검")
        self.assertEqual(self.stored("b"), {"task_summary": "서버 상태 점검", "summary_source": "llm"})

    def test_cache_key_includes_prompt_version(self):
        self.session_flow(["버전 키 확인"])
        with mock.patch.object(summary, "PROMPT_VERSION", "v-old"):
            self.run_tick(lambda p: "옛 판 요약")
        self.conn.execute("UPDATE sessions SET task_summary=NULL, summary_source=NULL")
        calls = self.run_tick(lambda p: "새 판 요약")          # 판이 다르면 캐시 미스
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.stored()["task_summary"], "새 판 요약")


if __name__ == "__main__":
    unittest.main()

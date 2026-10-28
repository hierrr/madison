"""자동화·태스크 탭 세션 이력 필터 — _session_conds의 상태 필터(특히 'stale' 신호 없음 판정)와 검색(q)."""
import json
import sqlite3
import unittest

from server import app as hub
from server import db
from server.config import CFG


class StaleFilterTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        # 기기 1은 온라인(방금 신호), 기기 2는 오프라인(어제 신호)
        self.conn.execute(
            "INSERT INTO devices (id,name,token_hash,created_at,last_seen_at) VALUES"
            " (1,'online','x',datetime('now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')),"
            " (2,'offline','y',datetime('now'),strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day'))")
        rows = [
            # (device, sid, state, last_seen 상대값, frontend)
            (1, "fresh-working", "working", "-1 minutes", "cli"),
            (1, "zombie-working", "working", f"-{CFG.ttl_stale_min + 5} minutes", "cli"),
            (1, "await-online", "awaiting_input", "-3 hours", "cli"),      # 기기 온라인이면 대기 유휴는 정상
            (2, "await-offline", "awaiting_input", "-3 hours", "cli"),     # 기기 오프라인 → 신호 없음
            (1, "ended", "ended", "-2 hours", "cli"),
            # 헤드리스는 기다릴 사람이 없어 대기 상태도 TTL로 판정 — 기기가 온라인이어도 (2026-10-14 좀비 사례)
            (1, "auto-await-zombie", "awaiting_input", f"-{CFG.ttl_stale_min + 5} minutes", "auto"),
            (1, "auto-await-fresh", "awaiting_input", "-1 minutes", "auto"),
        ]
        for dev, sid, st, rel, fe in rows:
            self.conn.execute(
                "INSERT INTO sessions (device_id, agent, session_id, state, turns, last_prompt, frontend,"
                " last_seen_hub, started_at) VALUES (?, 'claude-code', ?, ?, 1, 'p', ?,"
                " strftime('%Y-%m-%dT%H:%M:%SZ','now',?), strftime('%Y-%m-%dT%H:%M:%SZ','now',?))",
                (dev, sid, st, fe, rel, rel))
        # 종료 사유별 필터용: other(정상) / lost(체크로 정리) / 사유 없음(구형 수집)
        self.conn.execute("UPDATE sessions SET end_reason='other' WHERE session_id='ended'")
        for sid, reason in (("ended-lost", "lost"), ("ended-noreason", None)):
            self.conn.execute(
                "INSERT INTO sessions (device_id, agent, session_id, state, end_reason, turns, last_prompt, frontend,"
                " last_seen_hub, started_at) VALUES (1, 'claude-code', ?, 'ended', ?, 1, 'p', 'auto',"
                " strftime('%Y-%m-%dT%H:%M:%SZ','now','-3 hours'), strftime('%Y-%m-%dT%H:%M:%SZ','now','-3 hours'))",
                (sid, reason))

    def tearDown(self):
        self.conn.close()

    def query(self, **kw):
        conds, args = hub._session_conds(**kw)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return sorted(r[0] for r in self.conn.execute(
            "SELECT s.session_id FROM sessions s JOIN devices d ON d.id=s.device_id" + where, args))

    def test_stale_matches_overlay_rule(self):
        # working은 TTL 초과만, 대기 상태는 기기 오프라인일 때만, 헤드리스는 상태 무관 TTL 초과 — 현황의 '신호 없음'과 같은 집합
        self.assertEqual(self.query(state="stale"), ["auto-await-zombie", "await-offline", "zombie-working"])

    def test_plain_state_filter_is_exact(self):
        self.assertEqual(self.query(state="working"), ["fresh-working", "zombie-working"])
        self.assertEqual(self.query(state="ended"), ["ended", "ended-lost", "ended-noreason"])
        self.assertEqual(len(self.query()), 9)

    def test_end_reason_filter(self):
        # 'ended:<사유>' — 사유 하나만, '-'는 사유 없음(NULL·빈 문자열)
        self.assertEqual(self.query(state="ended:lost"), ["ended-lost"])
        self.assertEqual(self.query(state="ended:other"), ["ended"])
        self.assertEqual(self.query(state="ended:-"), ["ended-noreason"])
        self.assertEqual(self.query(state="ended:nope"), [])

    def test_stale_compares_same_timestamp_format(self):
        # 저장 형식이 'T…Z'라 datetime('now')의 공백 구분 문자열과 섞어 비교하면 같은 날짜에서 어긋난다 —
        # 같은 날짜 안의 경계값(TTL+1분)도 잡혀야 한다
        self.conn.execute(
            "UPDATE sessions SET last_seen_hub = strftime('%Y-%m-%dT%H:%M:%SZ','now',?) WHERE session_id='zombie-working'",
            (f"-{CFG.ttl_stale_min + 1} minutes",))
        self.assertIn("zombie-working", self.query(state="stale"))

    def test_overlay_returns_rows_with_flag(self):
        # 이력 API가 그대로 응답에 싣는다 — 반환이 빠지면 rows:null로 탭이 '불러오기 실패'(2026-10-01 실사고)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT s.session_id, s.state, s.frontend, s.last_seen_hub, d.last_seen_at AS device_seen_at"
            " FROM sessions s JOIN devices d ON d.id=s.device_id ORDER BY s.session_id")]
        out = hub._overlay_unconfirmed(rows)
        self.assertIs(out, rows)
        flags = {r["session_id"]: r["unconfirmed"] for r in out}
        self.assertTrue(flags["zombie-working"]); self.assertTrue(flags["await-offline"])
        self.assertFalse(flags["fresh-working"]); self.assertFalse(flags["await-online"])
        self.assertFalse(flags["ended"]); self.assertFalse(flags["ended-lost"])
        self.assertTrue(flags["auto-await-zombie"]); self.assertFalse(flags["auto-await-fresh"])
        self.assertNotIn("device_seen_at", out[0])

    def test_manual_end_records_lost_only_for_silent_sessions(self):
        # 체크로 종료 처리할 때 사유: 신호 끊긴 세션은 lost(신호 끊김), 살아 있는 세션을 닫으면 manual
        self.assertEqual(hub.end_reason_for(self.conn, 1, "claude-code", "zombie-working"), "lost")
        self.assertEqual(hub.end_reason_for(self.conn, 2, "claude-code", "await-offline"), "lost")
        self.assertEqual(hub.end_reason_for(self.conn, 1, "claude-code", "fresh-working"), "manual")
        self.assertEqual(hub.end_reason_for(self.conn, 1, "claude-code", "await-online"), "manual")
        # 헤드리스 좀비는 기기가 온라인이어도 신호 끊김(lost)으로 기록 — 종전엔 manual로 남아 사유가 틀렸다
        self.assertEqual(hub.end_reason_for(self.conn, 1, "claude-code", "auto-await-zombie"), "lost")
        self.assertEqual(hub.end_reason_for(self.conn, 1, "claude-code", "auto-await-fresh"), "manual")
        self.assertIsNone(hub.end_reason_for(self.conn, 1, "claude-code", "nope"))


class SearchTests(unittest.TestCase):
    """태스크 탭 검색(q) — 세션 행 컬럼 + 이벤트 원장의 지시·응답 요약, 단어마다 AND."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.conn.execute("INSERT INTO devices (id,name,token_hash,created_at) VALUES (1,'dev-a','x',datetime('now'))")
        sessions = [
            # (sid, task_summary, session_name) — 마지막 프롬프트·요약은 무관한 내용
            ("toggle", "어드민 화면 설명", "acme-archive"),
            ("named", "다른 작업", "평일 야간 배치 스케줄"),
            ("other", "리포트 버그 수정", None),
            ("percent", "할인율 100% 표기", None),
        ]
        for sid, ts, name in sessions:
            self.conn.execute(
                "INSERT INTO sessions (device_id, agent, session_id, state, turns, last_prompt, task_summary,"
                " session_name, frontend, last_seen_hub, started_at) VALUES (1, 'claude-code', ?, 'ended', 3, 'p', ?, ?,"
                " 'cli', '2026-10-22T00:00:00Z', '2026-10-20T00:00:00Z')", (sid, ts, name))
        events = [
            ("toggle", "prompt", {"prompt": "배포 토글 시 메신저 채널에 알림", "frontend": "cli"}),
            ("toggle", "turn_done", {"summary": "acme_toggle_job에 알림을 넣었습니다", "frontend": "cli"}),
            # 단어가 서로 다른 턴에 흩어진 세션
            ("other", "prompt", {"prompt": "배포 결과 확인", "frontend": "cli"}),
            ("other", "turn_done", {"summary": "토글은 건드리지 않았습니다", "frontend": "cli"}),
            # 지시·요약이 아닌 이벤트의 payload는 검색 대상이 아니다
            ("named", "tool_start", {"tool": "메신저", "frontend": "cli"}),
        ]
        for i, (sid, ev, p) in enumerate(events):
            self.conn.execute(
                "INSERT INTO events (device_id, agent, session_id, event_id, event, ts_device, ts_hub, payload)"
                " VALUES (1, 'claude-code', ?, ?, ?, ?, ?, ?)",
                (sid, f"e{i}", ev, f"2026-10-22T00:0{i}:00Z", f"2026-10-22T00:0{i}:00Z",
                 json.dumps(p, ensure_ascii=False)))

    def tearDown(self):
        self.conn.close()

    def query(self, q):
        conds, args = hub._session_conds(q=q)
        return sorted(r[0] for r in self.conn.execute(
            "SELECT s.session_id FROM sessions s JOIN devices d ON d.id=s.device_id WHERE " + " AND ".join(conds), args))

    def test_matches_mid_session_prompt_and_summary(self):
        # 세션 행은 무관한 마지막 요약만 들고 있어도 원장의 지시·응답 요약으로 찾힌다
        self.assertEqual(self.query("메신저"), ["toggle"])
        self.assertEqual(self.query("ACME_TOGGLE"), ["toggle"])   # ASCII는 대소문자 무시

    def test_matches_session_columns(self):
        self.assertEqual(self.query("평일"), ["named"])
        self.assertEqual(self.query("리포트"), ["other"])

    def test_terms_are_anded_across_turns(self):
        self.assertEqual(self.query("배포 토글"), ["other", "toggle"])
        self.assertEqual(self.query("배포 메신저"), ["toggle"])
        self.assertEqual(self.query("배포 없는말"), [])

    def test_payload_keys_and_other_events_do_not_match(self):
        # 'cli'는 모든 payload의 frontend 값, '메신저'은 tool_start payload에만 — 둘 다 오탐하면 안 된다
        self.assertEqual(self.query("cli"), [])
        self.assertNotIn("named", self.query("메신저"))

    def test_like_wildcards_are_literal(self):
        self.assertEqual(self.query("100%"), ["percent"])
        self.assertEqual(self.query("%"), ["percent"])
        self.assertEqual(self.query("acme_t"), ["toggle"])
        self.assertEqual(self.query("acme_x"), [])

    def test_snippet_prefers_turn_with_all_terms(self):
        m = hub._match_snippet(self.conn, "dev-a", "claude-code", "toggle", ["토글", "메신저"])
        self.assertEqual(m["field"], "prompt")
        self.assertEqual(m["text"], "배포 토글 시 메신저 채널에 알림")
        # 단어가 흩어진 세션은 첫 단어가 든 턴
        m = hub._match_snippet(self.conn, "dev-a", "claude-code", "other", ["토글", "배포"])
        self.assertEqual((m["field"], m["text"]), ("summary", "토글은 건드리지 않았습니다"))
        # 세션 컬럼으로만 맞은 세션은 발췌 없음
        self.assertIsNone(hub._match_snippet(self.conn, "dev-a", "claude-code", "named", ["평일"]))

    def test_excerpt_window(self):
        text = "가" * 100 + "토글" + "나" * 200
        out = hub._excerpt(text, "토글")
        self.assertTrue(out.startswith("…" + "가" * 40 + "토글"))
        self.assertTrue(out.endswith("…"))
        self.assertEqual(hub._excerpt("줄\n바꿈  토글", "토글"), "줄 바꿈 토글")


if __name__ == "__main__":
    unittest.main()

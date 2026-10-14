"""자동화·태스크 탭 세션 이력 필터 — _session_conds의 상태 필터, 특히 'stale'(신호 없음) 판정."""
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


if __name__ == "__main__":
    unittest.main()

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
            # (device, sid, state, last_seen 상대값)
            (1, "fresh-working", "working", "-1 minutes"),
            (1, "zombie-working", "working", f"-{CFG.ttl_stale_min + 5} minutes"),
            (1, "await-online", "awaiting_input", "-3 hours"),      # 기기 온라인이면 대기 유휴는 정상
            (2, "await-offline", "awaiting_input", "-3 hours"),     # 기기 오프라인 → 신호 없음
            (1, "ended", "ended", "-2 hours"),
        ]
        for dev, sid, st, rel in rows:
            self.conn.execute(
                "INSERT INTO sessions (device_id, agent, session_id, state, turns, last_prompt, frontend,"
                " last_seen_hub, started_at) VALUES (?, 'claude-code', ?, ?, 1, 'p', 'auto',"
                " strftime('%Y-%m-%dT%H:%M:%SZ','now',?), strftime('%Y-%m-%dT%H:%M:%SZ','now',?))",
                (dev, sid, st, rel, rel))

    def tearDown(self):
        self.conn.close()

    def query(self, **kw):
        conds, args = hub._session_conds(**kw)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return sorted(r[0] for r in self.conn.execute(
            "SELECT s.session_id FROM sessions s JOIN devices d ON d.id=s.device_id" + where, args))

    def test_stale_matches_overlay_rule(self):
        # working은 TTL 초과만, 대기 상태는 기기 오프라인일 때만 — 현황의 '신호 없음'과 같은 집합
        self.assertEqual(self.query(state="stale"), ["await-offline", "zombie-working"])

    def test_plain_state_filter_is_exact(self):
        self.assertEqual(self.query(state="working"), ["fresh-working", "zombie-working"])
        self.assertEqual(self.query(state="ended"), ["ended"])
        self.assertEqual(len(self.query()), 5)

    def test_stale_compares_same_timestamp_format(self):
        # 저장 형식이 'T…Z'라 datetime('now')의 공백 구분 문자열과 섞어 비교하면 같은 날짜에서 어긋난다 —
        # 같은 날짜 안의 경계값(TTL+1분)도 잡혀야 한다
        self.conn.execute(
            "UPDATE sessions SET last_seen_hub = strftime('%Y-%m-%dT%H:%M:%SZ','now',?) WHERE session_id='zombie-working'",
            (f"-{CFG.ttl_stale_min + 1} minutes",))
        self.assertIn("zombie-working", self.query(state="stale"))


if __name__ == "__main__":
    unittest.main()

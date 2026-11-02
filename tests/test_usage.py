import asyncio
import http.client
import io
import sqlite3
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from server import db, usage


class ParseTests(unittest.TestCase):
    def test_claude_limits_shape_and_order(self):
        data = {"limits": [
            {"kind": "weekly_scoped", "percent": 80.2, "resets_at": "2026-08-31T15:00:00Z", "scope": {"model": {"display_name": "Fable"}}},
            {"kind": "session", "percent": 92, "resets_at": "2026-08-28T09:00:00Z"},
            {"kind": "weekly_all", "percent": 62, "resets_at": "2026-08-31T15:00:00Z"}]}
        w = usage._finish(usage.parse_claude(data), now=usage.to_epoch("2026-08-28T08:46:00Z"))
        self.assertEqual([x["title"] for x in w], ["5h", "7d all models", "7d Fable"])
        self.assertEqual((w[0]["pct"], w[0]["left"]), (92, "14m"))
        self.assertEqual(w[1]["left"], "3d06h")
        self.assertEqual(w[0]["value"], "92% · 14m left")
        self.assertIn("resets_at", w[0])

    def test_claude_legacy_shape(self):
        w = usage.parse_claude({"five_hour": {"utilization": 10, "resets_at": 0}, "seven_day": {"utilization": 5}})
        self.assertEqual([(x["title"], x["pct"]) for x in w], [("5h", 10), ("7d all models", 5)])
        self.assertEqual(usage.parse_claude({}), [])

    def test_codex_rpc_shape(self):
        result = {"rateLimitsByLimitId": {"codex": {"planType": "team", "limitName": "Codex",
                  "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1756400000},
                  "secondary": {"usedPercent": 12.6, "windowDurationMins": 10080, "resetsAt": 1756900000}}},
                  "rateLimitResetCredits": {"availableCount": 1}}
        parsed = usage.parse_codex_rpc(result)
        self.assertEqual(parsed["plan"], "team")
        self.assertEqual(parsed["reset_credits"], 1)
        w = usage._finish(parsed["windows"], now=1756399000)
        self.assertEqual([(x["title"], x["pct"]) for x in w], [("5h", 0), ("7d", 13)])
        self.assertEqual(w[0]["left"], "16m")

    def test_codex_transcript_shape_and_remaining(self):
        parsed = usage.parse_codex_transcript({"plan_type": "plus",
            "primary": {"used_percent": 40, "window_minutes": 300, "resets_at": 100},
            "secondary": {"used_percent": 3, "window_minutes": 10080, "resets_at": 100}})
        self.assertEqual([x["title"] for x in parsed["windows"]], ["5h", "7d"])
        self.assertEqual(usage.format_remaining(50, now=100), "resetting")
        self.assertEqual(usage.format_remaining(100 + 2 * 86400 + 5 * 3600, now=100), "2d05h")
        self.assertEqual(usage.format_remaining(100 + 2 * 3600 + 31 * 60, now=100), "2h31m")
        self.assertEqual(usage.format_remaining(None), "")

    def test_snapshot_empty_until_collected(self):
        with usage._lock:
            saved = dict(usage._snap)
            usage._snap.update({p: None for p in usage.PROVIDERS})
        try:
            self.assertEqual(usage.snapshot(), {"claude": None, "codex": None})
        finally:
            with usage._lock:
                usage._snap.update(saved)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def win(title, pct, resets=None):
        return {"title": title, "pct": pct, "resets_at": resets}

    def test_append_only_on_change_per_window(self):
        n = usage.record_history(self.conn, "claude", [self.win("5h", 10), self.win("7d all models", 3)],
                                 ts="2026-09-01T00:00:00Z")
        self.assertEqual(n, 2)  # 첫 관측은 모두 기록
        n = usage.record_history(self.conn, "claude", [self.win("5h", 10), self.win("7d all models", 3)],
                                 ts="2026-09-01T00:03:00Z")
        self.assertEqual(n, 0)  # 동일 pct는 건너뜀
        n = usage.record_history(self.conn, "claude", [self.win("5h", 12), self.win("7d all models", 3)],
                                 ts="2026-09-01T00:06:00Z")
        self.assertEqual(n, 1)  # 바뀐 창만
        rows = self.conn.execute("SELECT win, pct FROM usage_history ORDER BY id").fetchall()
        self.assertEqual([(r["win"], r["pct"]) for r in rows],
                         [("5h", 10), ("7d all models", 3), ("5h", 12)])

    def test_providers_are_independent(self):
        usage.record_history(self.conn, "claude", [self.win("5h", 10)], ts="2026-09-01T00:00:00Z")
        n = usage.record_history(self.conn, "codex", [self.win("5h", 10)], ts="2026-09-01T00:00:00Z")
        self.assertEqual(n, 1)

    def test_history_carry_in_and_shape(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * 86400))
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        usage.record_history(self.conn, "claude", [self.win("5h", 40)], ts=old)
        usage.record_history(self.conn, "claude", [self.win("5h", 55)], ts=recent)
        h = usage.history(self.conn, days=7)
        series = h["claude"]["windows"]["5h"]
        self.assertEqual(len(series), 2)
        self.assertEqual(series[0][1], 40)   # 범위 직전 값이 carry-in으로 시작 레벨
        self.assertEqual(series[0][0], h["from"])
        self.assertEqual(series[1][1], 55)
        self.assertEqual(h["codex"]["windows"], {})

    def test_history_all_time(self):
        usage.record_history(self.conn, "codex", [self.win("7d", 5)], ts="2026-09-01T00:00:00Z")
        h = usage.history(self.conn, days=0)
        self.assertIsNone(h["from"])
        self.assertEqual(h["codex"]["windows"]["7d"][0][1], 5)

    def test_downsample_uses_data_span_and_last_row_per_bucket(self):
        # 실제 데이터 폭 20일 → 요청 범위가 1년이어도 시간별 해상도로 내려간다
        t0 = time.time() - 20 * 86400
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        # 같은 시간 버킷 안에서 70 → 40: 버킷 값은 마지막 행(40)이어야 한다 (MAX면 70이 나옴)
        usage.record_history(self.conn, "claude", [self.win("5h", 70)], ts=iso(t0))
        usage.record_history(self.conn, "claude", [self.win("5h", 40)], ts=iso(t0 + 60))
        usage.record_history(self.conn, "claude", [self.win("5h", 55)], ts=iso(t0 + 5 * 86400))
        h = usage.history(self.conn, days=365)
        series = h["claude"]["windows"]["5h"]
        self.assertEqual([p[1] for p in series], [40, 55])

    def test_reset_detection_early_vs_scheduled(self):
        far = usage.to_epoch("2026-09-05T00:00:00Z")     # 예정 리셋은 한참 뒤
        usage.record_history(self.conn, "claude", [self.win("7d all models", 40, far)],
                             ts="2026-09-01T00:00:00Z")
        usage.record_history(self.conn, "claude", [self.win("7d all models", 62, far)],
                             ts="2026-09-01T06:00:00Z")
        # 예정(09-05)보다 훨씬 이른 하락 → 프로바이더 발 조기 리셋
        usage.record_history(self.conn, "claude", [self.win("7d all models", 0, usage.to_epoch("2026-09-08T00:00:00Z"))],
                             ts="2026-09-01T12:00:00Z")
        usage.record_history(self.conn, "claude", [self.win("7d all models", 50, usage.to_epoch("2026-09-08T00:00:00Z"))],
                             ts="2026-09-02T00:00:00Z")
        # 예정 시각(09-08)이 지난 뒤의 하락 → 정상 리셋
        usage.record_history(self.conn, "claude", [self.win("7d all models", 3)],
                             ts="2026-09-08T01:00:00Z")
        h = usage.history(self.conn, days=0)
        resets = h["claude"]["resets"]["7d all models"]
        self.assertEqual([(r["from"], r["to"], r["early"]) for r in resets],
                         [(62, 0, True), (50, 3, False)])
        # 상승만 있는 창에는 리셋 없음
        self.assertNotIn("5h", h["claude"]["resets"])

    def test_reset_detection_carries_baseline_across_range_edge(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * 86400))
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        far = time.time() + 5 * 86400
        usage.record_history(self.conn, "claude", [self.win("5h", 70, far)], ts=old)
        usage.record_history(self.conn, "claude", [self.win("5h", 10, far)], ts=recent)
        h = usage.history(self.conn, days=7)   # 하락의 기준행(70)은 범위 밖
        resets = h["claude"]["resets"]["5h"]
        self.assertEqual([(r["from"], r["to"], r["early"]) for r in resets], [(70, 10, True)])

    def test_expired_window_not_recorded(self):
        # 관측 시점에 resets_at이 이미 지난 창 = 묵은 스냅샷 — 기록하지 않는다
        n = usage.record_history(self.conn, "codex", [self.win("7d", 9, usage.to_epoch("2026-08-31T02:25:58Z"))],
                                 ts="2026-09-28T01:43:36Z")
        self.assertEqual(n, 0)

    def test_stale_rows_already_stored_are_ignored(self):
        # 09-28 실측 패턴: 실값 16%(10-05 리셋) 사이사이 08-31에 끝난 창의 9%가 끼어 16→9 조기 리셋이 잡혔다
        real, stale = usage.to_epoch("2026-10-05T02:47:34Z"), usage.to_epoch("2026-08-31T02:25:58Z")
        rows = [("2026-09-28T03:38:58Z", 16, real), ("2026-09-28T06:22:49Z", 9, stale),
                ("2026-09-28T06:28:51Z", 16, real), ("2026-09-28T07:11:33Z", 9, stale),
                ("2026-09-28T07:17:35Z", 16, real), ("2026-09-28T08:49:17Z", 18, real)]
        self.conn.executemany("INSERT INTO usage_history (ts, provider, win, pct, resets_at) VALUES (?,'codex','7d',?,?)",
                              rows)
        h = usage.history(self.conn, days=0)
        self.assertNotIn("7d", h["codex"]["resets"])
        self.assertEqual([p[1] for p in h["codex"]["windows"]["7d"]], [16, 16, 16, 18])
        # 다음 기록의 비교 기준도 만료 행이 아니라 마지막 실값(18)
        self.assertEqual(usage.record_history(self.conn, "codex", [self.win("7d", 18, real)],
                                             ts="2026-09-28T09:00:00Z"), 0)

    def test_rewound_window_is_not_a_reset(self):
        # 아직 만료 전이지만 이전 창 값(끝 시각이 뒤로 감) — 리셋 아님, 이후 기준행으로도 쓰지 않는다
        old_end, cur_end = usage.to_epoch("2026-10-05T02:47:34Z"), usage.to_epoch("2026-10-06T05:32:27Z")
        usage.record_history(self.conn, "codex", [self.win("7d", 26, cur_end)], ts="2026-09-30T04:00:00Z")
        usage.record_history(self.conn, "codex", [self.win("7d", 20, old_end)], ts="2026-09-30T04:06:00Z")
        usage.record_history(self.conn, "codex", [self.win("7d", 26, cur_end)], ts="2026-09-30T04:12:00Z")
        new_end = usage.to_epoch("2026-10-08T00:00:00Z")
        usage.record_history(self.conn, "codex", [self.win("7d", 0, new_end)], ts="2026-10-01T00:00:00Z")
        resets = usage.history(self.conn, days=0)["codex"]["resets"]["7d"]
        self.assertEqual([(r["from"], r["to"], r["early"]) for r in resets], [(26, 0, True)])


class CodexFallbackTests(unittest.TestCase):
    def setUp(self):
        with usage._lock:
            self.saved = usage._snap["codex"]
        self.orig = usage.codex_transcript_snapshot
        self.now = usage.to_epoch("2026-09-28T02:00:00Z")

    def tearDown(self):
        usage.codex_transcript_snapshot = self.orig
        with usage._lock:
            usage._snap["codex"] = self.saved

    def use(self, rl, seen_iso, last_iso):
        usage.codex_transcript_snapshot = lambda: (rl, usage.to_epoch(seen_iso))
        with usage._lock:
            usage._snap["codex"] = {"updated": last_iso} if last_iso else None

    def test_older_than_last_collection_is_rejected(self):
        rl = {"primary": {"used_percent": 27, "window_minutes": 10080, "resets_at": 1791168454}}
        self.use(rl, "2026-09-28T01:00:00Z", "2026-09-28T01:55:00Z")
        with self.assertRaisesRegex(usage.UsageError, "오래됨"):
            usage.codex_fallback(usage.UsageError("timeout"), self.now)

    def test_newer_snapshot_used_and_expired_windows_dropped(self):
        rl = {"primary": {"used_percent": 71, "window_minutes": 300, "resets_at": 1790000000},   # 이미 지남
              "secondary": {"used_percent": 27, "window_minutes": 10080, "resets_at": 1791168454}}
        self.use(rl, "2026-09-28T01:59:00Z", "2026-09-28T01:55:00Z")
        parsed = usage.codex_fallback(usage.UsageError("timeout"), self.now)
        self.assertEqual([(w["title"], w["pct"]) for w in parsed["windows"]], [("7d", 27)])
        self.assertEqual(parsed["seen_at"], usage.to_epoch("2026-09-28T01:59:00Z"))

    def test_no_prior_snapshot_still_drops_expired(self):
        rl = {"primary": {"used_percent": 9, "window_minutes": 10080, "resets_at": 1788143158}}
        self.use(rl, "2026-08-24T03:35:00Z", None)
        with self.assertRaisesRegex(usage.UsageError, "만료"):
            usage.codex_fallback(usage.UsageError("timeout"), self.now)


class EpochTests(unittest.TestCase):
    def test_naive_iso_is_utc(self):
        self.assertEqual(usage.to_epoch("2026-08-28T00:00:00"), usage.to_epoch("2026-08-28T00:00:00Z"))
        self.assertEqual(usage.to_epoch("2026-08-28T09:00:00+09:00"), usage.to_epoch("2026-08-28T00:00:00Z"))
        self.assertIsNone(usage.to_epoch("nope")); self.assertIsNone(usage.to_epoch(True))


class PlanHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_record_only_on_change_and_skip_empty(self):
        self.assertTrue(usage.record_plan(self.conn, "codex", "Team", ts="2026-09-03T00:00:00Z"))
        self.assertFalse(usage.record_plan(self.conn, "codex", "Team", ts="2026-09-04T00:00:00Z"))
        self.assertFalse(usage.record_plan(self.conn, "codex", "", ts="2026-09-05T00:00:00Z"))   # 응답에 플랜 없음
        self.assertTrue(usage.record_plan(self.conn, "codex", "Prolite", ts="2026-10-12T03:20:15Z"))
        rows = self.conn.execute("SELECT plan FROM usage_plan_history WHERE provider='codex' ORDER BY id").fetchall()
        self.assertEqual([r["plan"] for r in rows], ["Team", "Prolite"])

    def test_last_plan_by_ts_not_id(self):
        # 백필: 현재 플랜이 먼저(id 작게) 들어가고 과거 플랜이 나중에 들어가도 마지막 행은 ts 기준
        usage.record_plan(self.conn, "codex", "Prolite", ts="2026-10-12T03:20:15Z")
        self.conn.execute("INSERT INTO usage_plan_history (ts, provider, plan) VALUES (?,?,?)",
                          ("2026-09-03T00:00:00Z", "codex", "Team"))
        self.assertFalse(usage.record_plan(self.conn, "codex", "Prolite", ts="2026-10-12T03:23:00Z"))

    def test_changes_within_range_with_carry_in(self):
        usage.record_plan(self.conn, "codex", "Team", ts="2026-09-03T00:00:00Z")
        usage.record_plan(self.conn, "codex", "Prolite", ts="2026-10-12T03:20:15Z")
        usage.record_plan(self.conn, "claude", "Max", ts="2026-09-03T00:00:00Z")
        # 범위가 변경 이전에서 시작하면 직전 플랜을 범위 밖에서 이어받는다
        ch = usage.plan_changes(self.conn, "codex", usage.to_epoch("2026-10-01T00:00:00Z"))
        self.assertEqual(ch, [{"ts": int(usage.to_epoch("2026-10-12T03:20:15Z")), "from": "Team", "to": "Prolite", "drops": []}])
        # 범위가 변경 이후에서 시작하면 변경 없음; 첫 관측만 있는 프로바이더도 변경 없음
        self.assertEqual(usage.plan_changes(self.conn, "codex", usage.to_epoch("2026-10-12T04:00:00Z")), [])
        self.assertEqual(usage.plan_changes(self.conn, "claude", None), [])
        # 전체 범위
        self.assertEqual(len(usage.plan_changes(self.conn, "codex", None)), 1)
        # history()에 실린다
        h = usage.history(self.conn, days=0)
        self.assertEqual(h["codex"]["plan_changes"][0]["to"], "Prolite")
        self.assertEqual(h["claude"]["plan_changes"], [])

    def test_drop_at_plan_change_is_not_an_early_reset(self):
        # 2026-10-12 실측: Team 7d 16%(리셋 예정 10-15) → 플랜 변경 1초 뒤 Prolite 7d 2% — 조기 리셋이 아니라 새 플랜의 한도
        win = HistoryTests.win
        usage.record_plan(self.conn, "codex", "Team", ts="2026-09-03T00:00:00Z")
        usage.record_history(self.conn, "codex", [win("7d", 16, usage.to_epoch("2026-10-15T08:34:34Z"))], ts="2026-10-08T09:05:19Z")
        usage.record_plan(self.conn, "codex", "Prolite", ts="2026-10-12T03:20:15Z")
        usage.record_history(self.conn, "codex", [win("7d", 2, usage.to_epoch("2026-10-19T02:50:34Z"))], ts="2026-10-12T03:20:16Z")
        # 플랜과 무관한 진짜 조기 리셋(변경 하루 뒤)은 그대로 남는다
        usage.record_history(self.conn, "codex", [win("7d", 40, usage.to_epoch("2026-10-19T02:50:34Z"))], ts="2026-10-13T00:00:00Z")
        usage.record_history(self.conn, "codex", [win("7d", 0, usage.to_epoch("2026-10-20T00:00:00Z"))], ts="2026-10-13T06:00:00Z")
        h = usage.history(self.conn, days=0)
        self.assertEqual(h["codex"]["plan_changes"][0]["drops"], [{"win": "7d", "from": 16, "to": 2}])
        early = [(r["from"], r["to"]) for r in h["codex"]["resets"]["7d"] if r["early"]]
        self.assertEqual(early, [(40, 0)])


# ── Claude 플랜(profile)·크레딧(prepaid/credits) ─────────────────────────────
# 실측 응답 형태를 식별자만 지운 픽스처로 쓴다.
PROFILE = {"organization": {"uuid": "org-x", "plan_display_name": "Ultra", "plan_display_label": "Ultra plan",
                            "organization_type": "claude_max"}}
CREDITS = {"amount": 0, "currency": "USD", "balance": {"money": None, "credits": {"amount_minor": 0, "exponent": 2}},
           "tranches": [], "promo_tranches": [], "next_expires_at": None}
USAGE = {"five_hour": None,
         "limits": [{"kind": "weekly_all", "percent": 62, "resets_at": "2026-11-02T16:00:00Z"},
                    {"kind": "weekly_scoped", "percent": 99, "resets_at": "2026-11-02T16:00:00Z",
                     "scope": {"model": {"display_name": "Fable"}}}],
         "extra_usage": {"is_enabled": False}}


class ProfileCreditsParseTests(unittest.TestCase):
    def test_parse_profile(self):
        self.assertEqual(usage.parse_profile(PROFILE), ("Ultra", "org-x"))
        self.assertEqual(usage.parse_profile({"organization": {"plan_display_name": "  Pro ", "uuid": " o "}}), ("Pro", "o"))
        self.assertEqual(usage.parse_profile({"organization": {"uuid": "org-x"}}), ("", "org-x"))
        self.assertEqual(usage.parse_profile({"organization": {"plan_display_name": 5, "uuid": None}}), ("", ""))
        for bad in ({}, {"organization": None}, {"organization": "x"}, None, [], "x"):
            self.assertEqual(usage.parse_profile(bad), ("", ""))

    def test_format_money(self):
        self.assertEqual(usage.format_money(0, "USD"), "$0.00")
        self.assertEqual(usage.format_money(1234, "USD"), "$12.34")
        self.assertEqual(usage.format_money(5, "usd"), "$0.05")            # 소문자·한 자리 센트 0 채움
        self.assertEqual(usage.format_money(-150, "USD"), "-$1.50")
        self.assertEqual(usage.format_money(-5, "USD"), "-$0.05")          # 음수 소액에서 부호 유지
        self.assertEqual(usage.format_money(1234, "EUR"), "12.34 EUR")
        self.assertEqual(usage.format_money(-1234, "eur"), "-12.34 EUR")
        self.assertEqual(usage.format_money(1234, "JPY", 0), "1234 JPY")   # exponent 0은 소수점 없음
        self.assertEqual(usage.format_money(12345, "USD", 3), "$12.345")
        self.assertEqual(usage.format_money(1234, "USD", None), "$12.34")  # 잘못된 exponent → 2
        self.assertEqual(usage.format_money(1234, "USD", -1), "$12.34")
        self.assertEqual(usage.format_money(1234, "USD", True), "$12.34")
        self.assertEqual(usage.format_money(1234, ""), "12.34")            # 통화 없음 → 꼬리 공백 없음

    def test_parse_credits_real_sample(self):
        self.assertEqual(usage.parse_credits(CREDITS, False), {
            "title": "Credits", "value": "$0.00", "amount": 0, "currency": "USD", "enabled": False, "expires_at": None})

    def test_parse_credits_amount_missing_is_none(self):
        for bad in ({}, {"amount": None}, {"amount": "5"}, {"amount": True}, {"currency": "USD"}, None, [], "x"):
            self.assertIsNone(usage.parse_credits(bad, True), bad)

    def test_parse_credits_exponent_fallback(self):
        self.assertEqual(usage.parse_credits({"amount": 1250, "currency": "USD"}, None)["value"], "$12.50")   # balance 없음
        for bal in ({"credits": {"exponent": "2"}}, {"credits": {"exponent": None}}, {"credits": {"exponent": True}},
                    {"credits": None}, {"credits": {}}, "x"):
            self.assertEqual(usage.parse_credits({"amount": 1250, "currency": "USD", "balance": bal}, None)["value"],
                             "$12.50", bal)
        got = usage.parse_credits({"amount": 1250, "currency": "USD", "balance": {"credits": {"exponent": 3}}}, None)
        self.assertEqual(got["value"], "$1.250")
        self.assertEqual(got["amount"], 1250)

    def test_parse_credits_currency(self):
        self.assertEqual(usage.parse_credits({"amount": 100}, None)["currency"], "USD")          # 없으면 USD
        self.assertEqual(usage.parse_credits({"amount": 100, "currency": "  "}, None)["currency"], "USD")
        got = usage.parse_credits({"amount": -250, "currency": "eur"}, None)
        self.assertEqual((got["currency"], got["value"], got["amount"]), ("EUR", "-2.50 EUR", -250))

    def test_parse_credits_amount_float_truncated_to_int(self):
        got = usage.parse_credits({"amount": 1999.0, "currency": "USD"}, None)
        self.assertEqual((got["amount"], got["value"]), (1999, "$19.99"))

    def test_parse_credits_non_finite_amount_is_none(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(usage.parse_credits({"amount": bad, "currency": "USD"}, True), bad)
        import json
        parsed = json.loads('{"amount": NaN, "currency": "USD"}')   # json.load는 NaN/Infinity를 받아들인다
        self.assertIsNone(usage.parse_credits(parsed, True))
        self.assertIsNone(usage.parse_credits(json.loads('{"amount": Infinity}'), True))

    def test_parse_credits_expires_iso_to_epoch(self):
        got = usage.parse_credits({"amount": 100, "currency": "USD", "next_expires_at": "2026-12-01T00:00:00Z"}, True)
        self.assertEqual(got["expires_at"], usage.to_epoch("2026-12-01T00:00:00Z"))
        self.assertIsInstance(got["expires_at"], float)
        self.assertIsNone(usage.parse_credits({"amount": 100, "next_expires_at": "garbage"}, True)["expires_at"])

    def test_parse_credits_enabled_passthrough(self):
        self.assertIs(usage.parse_credits({"amount": 1}, True)["enabled"], True)
        self.assertIs(usage.parse_credits({"amount": 1}, False)["enabled"], False)
        for odd in (None, "yes", 1, 0):                                  # bool이 아니면 None(알 수 없음)
            self.assertIsNone(usage.parse_credits({"amount": 1}, odd)["enabled"], odd)


class ClaudeCollectBase(unittest.TestCase):
    """collect_claude — 키체인·HTTP를 모두 대체한다. 모듈 캐시·_snap·백오프는 매 테스트 초기화/복원."""

    def setUp(self):
        with usage._lock:
            self._saved_snap = usage._snap["claude"]
            usage._snap["claude"] = None
        self._saved_backoff = usage._claude_backoff_until
        self._saved_pcache = dict(usage._claude_profile_cache)
        self._saved_ccache = dict(usage._claude_credits_cache)
        self.reset_caches()
        # 로그인 시점 값이라 stale — 이 값이 플랜으로 새면 안 된다
        p = mock.patch.object(usage, "claude_oauth", return_value={"accessToken": "tok", "subscriptionType": "max"})
        p.start(); self.addCleanup(p.stop)
        self.responses = {"usage": USAGE, "profile": PROFILE, "credits": CREDITS}   # label → dict | Exception
        self.calls = []
        p = mock.patch.object(usage, "claude_get", side_effect=self.fake_get)
        p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        usage._claude_backoff_until = self._saved_backoff
        usage._claude_profile_cache.clear(); usage._claude_profile_cache.update(self._saved_pcache)
        usage._claude_credits_cache.clear(); usage._claude_credits_cache.update(self._saved_ccache)
        with usage._lock:
            usage._snap["claude"] = self._saved_snap

    @staticmethod
    def reset_caches():
        usage._claude_backoff_until = 0.0
        usage._claude_profile_cache.update(at=0.0, plan=None, org="", fp="", failed_at=0.0)
        usage._claude_credits_cache.update(at=0.0, entry=None, fp="", failed_at=0.0)

    def fake_get(self, url, token, label="usage", timeout=15, backoff=False):
        self.calls.append((label, url))
        r = self.responses[label]
        if isinstance(r, Exception):
            raise r
        return r

    def labels(self):
        return [c[0] for c in self.calls]

    def expire_caches(self):
        """TTL 만료 + 실패 재시도 대기 해제 — 다음 회차에 다시 조회하게 한다."""
        usage._claude_profile_cache.update(at=0.0, failed_at=0.0)
        usage._claude_credits_cache.update(at=0.0, failed_at=0.0)


class ClaudeCollectTests(ClaudeCollectBase):
    def test_plan_from_profile_not_subscription_type(self):
        d = usage.collect_claude()
        self.assertEqual(d["plan"], "Ultra")                       # oauth subscriptionType은 "max"
        self.assertNotEqual(d["plan"].lower(), "max")

    def test_no_five_hour_window_and_windows_present(self):
        d = usage.collect_claude()
        titles = [w["title"] for w in d["windows"]]
        self.assertNotIn("5h", titles)
        self.assertEqual(len(titles), 2)
        self.assertEqual({w["pct"] for w in d["windows"]}, {62, 99})

    def test_credits_extra_present_and_disabled(self):
        d = usage.collect_claude()
        self.assertEqual(d["extras"], [{"title": "Credits", "value": "$0.00", "amount": 0, "currency": "USD",
                                        "enabled": False, "expires_at": None}])

    def test_credits_url_uses_org_uuid_from_profile(self):
        usage.collect_claude()
        urls = dict(self.calls)
        self.assertEqual(urls["profile"], usage.CLAUDE_PROFILE_URL)
        self.assertEqual(urls["credits"], "https://api.anthropic.com/api/oauth/organizations/org-x/prepaid/credits")

    def test_credits_enabled_follows_usage_response(self):
        self.responses["usage"] = {**USAGE, "extra_usage": {"is_enabled": True}}
        self.assertIs(usage.collect_claude()["extras"][0]["enabled"], True)
        # 잔액은 TTL 캐시에서 오더라도 enabled는 매 회차 usage 값으로 갱신
        self.responses["usage"] = {**USAGE, "extra_usage": {"is_enabled": False}}
        n = self.calls.count(("credits", usage.CLAUDE_CREDITS_URL.format(org="org-x")))
        d = usage.collect_claude()
        self.assertEqual(self.calls.count(("credits", usage.CLAUDE_CREDITS_URL.format(org="org-x"))), n)  # 재조회 없음
        self.assertIs(d["extras"][0]["enabled"], False)

    def test_enabled_unknown_when_usage_has_no_extra_usage(self):
        self.responses["usage"] = {k: v for k, v in USAGE.items() if k != "extra_usage"}
        self.assertIsNone(usage.collect_claude()["extras"][0]["enabled"])

    def test_profile_and_credits_are_cached(self):
        usage.collect_claude(); usage.collect_claude()
        self.assertEqual(self.labels().count("usage"), 2)
        self.assertEqual(self.labels().count("profile"), 1)
        self.assertEqual(self.labels().count("credits"), 1)

    def test_caches_refetch_after_ttl(self):
        usage.collect_claude()
        usage._claude_profile_cache["at"] -= usage.CLAUDE_PROFILE_TTL + 1
        usage._claude_credits_cache["at"] -= usage.CLAUDE_CREDITS_TTL + 1
        self.responses["profile"] = {"organization": {"uuid": "org-x", "plan_display_name": "Pro"}}
        self.responses["credits"] = {**CREDITS, "amount": 1234}
        d = usage.collect_claude()
        self.assertEqual(d["plan"], "Pro")
        self.assertEqual(d["extras"][0]["value"], "$12.34")

    def test_plan_change_in_profile_is_followed(self):
        self.assertEqual(usage.collect_claude()["plan"], "Ultra")
        self.expire_caches()
        self.responses["profile"] = {"organization": {"uuid": "org-x", "plan_display_name": "Max 5x"}}
        self.assertEqual(usage.collect_claude()["plan"], "Max 5x")


class ClaudeFallbackTests(ClaudeCollectBase):
    def seed_snap(self, plan="Ultra", extras=None):
        with usage._lock:
            usage._snap["claude"] = {"plan": plan, "windows": [], "extras": extras or []}

    def test_profile_failure_keeps_snapshot_plan(self):
        self.seed_snap("Ultra")
        self.responses["profile"] = usage.UsageError("profile HTTP 500")
        d = usage.collect_claude()
        self.assertEqual(d["plan"], "Ultra")                       # subscriptionType("max")로 떨어지면 안 됨
        self.assertEqual(len(d["windows"]), 2)

    def test_profile_failure_without_anything_known_is_empty_never_max(self):
        self.responses["profile"] = usage.UsageError("profile HTTP 500")
        d = usage.collect_claude()
        self.assertEqual(d["plan"], "")
        self.assertNotIn(d["plan"].lower(), ("max",))
        self.assertEqual(len(d["windows"]), 2)

    def test_profile_failure_prefers_module_cache_over_snapshot(self):
        usage.collect_claude()                                     # 캐시에 Ultra
        self.seed_snap("Stale")
        self.expire_caches()
        self.responses["profile"] = usage.UsageError("profile 조회 실패")
        self.assertEqual(usage.collect_claude()["plan"], "Ultra")

    def test_profile_without_plan_name_keeps_last_known(self):
        self.seed_snap("Ultra")
        self.responses["profile"] = {"organization": {"uuid": "org-x"}}
        self.assertEqual(usage.collect_claude()["plan"], "Ultra")

    def test_plan_stable_across_failure_so_record_plan_does_not_flip_flop(self):
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(db.SCHEMA)
        for i, fail in enumerate((False, True, True, False, True)):
            self.expire_caches()
            self.responses["profile"] = usage.UsageError("boom") if fail else PROFILE
            d = usage.collect_claude()
            with usage._lock:
                usage._snap["claude"] = d                          # 수집 루프가 하는 일
            usage.record_plan(conn, "claude", d["plan"], ts=f"2026-11-02T00:0{i}:00Z")
        rows = [r["plan"] for r in conn.execute("SELECT plan FROM usage_plan_history WHERE provider='claude' ORDER BY id")]
        self.assertEqual(rows, ["Ultra"])
        self.assertEqual(usage.plan_changes(conn, "claude", None), [])

    def test_failure_with_no_org_skips_credits_call_but_still_collects(self):
        self.responses["profile"] = usage.UsageError("profile HTTP 500")
        d = usage.collect_claude()
        self.assertNotIn("credits", self.labels())                 # 조직 uuid를 모름
        self.assertEqual(d["extras"], [])
        self.assertEqual(len(d["windows"]), 2)

    def test_credits_failure_keeps_prior_module_entry(self):
        first = usage.collect_claude()["extras"][0]
        self.expire_caches()
        self.responses["credits"] = usage.UsageError("credits HTTP 500")
        d = usage.collect_claude()
        self.assertEqual(d["extras"], [first])
        self.assertEqual(len(d["windows"]), 2)
        self.assertEqual(d["plan"], "Ultra")

    def test_credits_failure_keeps_prior_snapshot_entry(self):
        prior = {"title": "Credits", "value": "$5.00", "amount": 500, "currency": "USD", "enabled": True, "expires_at": None}
        self.seed_snap("Ultra", extras=[{"title": "Other", "value": "x"}, prior])
        self.responses["credits"] = usage.UsageError("credits HTTP 500")
        d = usage.collect_claude()
        self.assertEqual(d["extras"], [{**prior, "enabled": False}])   # 잔액은 이어받고 on/off는 이번 usage 값

    def test_credits_unparsable_response_keeps_prior_entry(self):
        first = usage.collect_claude()["extras"][0]
        self.expire_caches()
        self.responses["credits"] = {"currency": "USD"}            # amount 없음
        self.assertEqual(usage.collect_claude()["extras"], [first])

    def test_credits_failure_with_nothing_known_gives_no_extras(self):
        self.responses["credits"] = usage.UsageError("credits HTTP 404")
        d = usage.collect_claude()
        self.assertEqual(d["extras"], [])
        self.assertEqual(d["plan"], "Ultra")
        self.assertEqual(len(d["windows"]), 2)

    def test_usage_failure_still_raises(self):
        self.responses["usage"] = usage.UsageError("usage HTTP 500")
        with self.assertRaises(usage.UsageError):
            usage.collect_claude()
        self.assertNotIn("profile", self.labels())


class ClaudeAuxResilienceTests(ClaudeCollectBase):
    """부가 호출(profile·credits)의 실패 처리 — 임의 예외, 재시도 간격, 계정 전환."""

    def test_non_usage_error_on_profile_still_returns_windows_and_keeps_plan(self):
        usage.collect_claude()                                     # Ultra 캐시
        self.expire_caches()
        self.responses["profile"] = RuntimeError("boom")
        d = usage.collect_claude()
        self.assertEqual(d["plan"], "Ultra")
        self.assertEqual(len(d["windows"]), 2)
        self.assertEqual(len(d["extras"]), 1)

    def test_non_usage_error_on_profile_with_nothing_known(self):
        self.responses["profile"] = RuntimeError("boom")
        d = usage.collect_claude()
        self.assertEqual((d["plan"], d["extras"]), ("", []))
        self.assertEqual(len(d["windows"]), 2)

    def test_non_usage_error_on_credits_keeps_last_entry(self):
        first = usage.collect_claude()["extras"][0]
        self.expire_caches()
        self.responses["credits"] = ValueError("bad payload")
        d = usage.collect_claude()
        self.assertEqual(d["extras"], [first])
        self.assertEqual(d["plan"], "Ultra")
        self.assertEqual(len(d["windows"]), 2)

    def test_non_usage_error_on_credits_with_nothing_known(self):
        self.responses["credits"] = KeyError("x")
        d = usage.collect_claude()
        self.assertEqual(d["extras"], [])
        self.assertEqual(len(d["windows"]), 2)

    def test_profile_failure_not_retried_within_aux_retry_window(self):
        self.responses["profile"] = usage.UsageError("profile HTTP 500")
        usage.collect_claude(); usage.collect_claude(); usage.collect_claude()
        self.assertEqual(self.labels().count("profile"), 1)
        self.assertEqual(self.labels().count("usage"), 3)

    def test_profile_retried_after_aux_retry_window(self):
        self.responses["profile"] = usage.UsageError("profile HTTP 500")
        self.assertEqual(usage.collect_claude()["plan"], "")
        usage._claude_profile_cache["failed_at"] -= usage.CLAUDE_AUX_RETRY_SEC + 1
        self.responses["profile"] = PROFILE
        self.assertEqual(usage.collect_claude()["plan"], "Ultra")
        self.assertEqual(self.labels().count("profile"), 2)
        self.assertEqual(usage._claude_profile_cache["failed_at"], 0.0)   # 성공하면 실패 표시 해제

    def test_profile_without_plan_name_counts_as_failure(self):
        self.responses["profile"] = {"organization": {"uuid": "org-x"}}
        usage.collect_claude(); usage.collect_claude()
        self.assertEqual(self.labels().count("profile"), 1)

    def test_credits_failure_not_retried_within_window_and_retried_after(self):
        self.responses["credits"] = usage.UsageError("credits HTTP 500")
        usage.collect_claude(); usage.collect_claude()
        self.assertEqual(self.labels().count("credits"), 1)
        usage._claude_credits_cache["failed_at"] -= usage.CLAUDE_AUX_RETRY_SEC + 1
        self.responses["credits"] = CREDITS
        d = usage.collect_claude()
        self.assertEqual(self.labels().count("credits"), 2)
        self.assertEqual(d["extras"][0]["value"], "$0.00")

    def test_credits_without_amount_counts_as_failure(self):
        self.responses["credits"] = {"currency": "USD"}
        usage.collect_claude(); usage.collect_claude()
        self.assertEqual(self.labels().count("credits"), 1)

    def test_credits_non_finite_amount_counts_as_failure(self):
        self.responses["credits"] = {"amount": float("nan"), "currency": "USD"}
        d = usage.collect_claude()
        self.assertEqual(d["extras"], [])

    def test_aux_failure_does_not_block_usage_calls(self):
        self.responses["profile"] = usage.UsageError("x")
        self.responses["credits"] = usage.UsageError("x")
        for _ in range(3):
            self.assertEqual(len(usage.collect_claude()["windows"]), 2)
        self.assertEqual(self.labels().count("usage"), 3)

    def test_token_change_does_not_serve_other_accounts_cache(self):
        self.assertEqual(usage.collect_claude()["plan"], "Ultra")  # 토큰 "tok"의 계정
        self.assertEqual(usage._claude_profile_cache["fp"], usage._fingerprint("tok"))
        with mock.patch.object(usage, "claude_oauth", return_value={"accessToken": "tok-b", "subscriptionType": "max"}):
            self.responses["profile"] = usage.UsageError("profile HTTP 500")
            self.responses["credits"] = usage.UsageError("credits HTTP 500")
            d = usage.collect_claude()
        self.assertEqual(d["plan"], "")                            # 다른 계정의 Ultra·크레딧이 새지 않는다
        self.assertEqual(d["extras"], [])
        self.assertEqual(usage._claude_profile_cache["fp"], usage._fingerprint("tok-b"))
        self.assertEqual(usage._claude_credits_cache["fp"], usage._fingerprint("tok-b"))
        self.assertEqual(len(d["windows"]), 2)

    def test_token_change_falls_back_to_snapshot_only(self):
        usage.collect_claude()                                     # 모듈 캐시: 이전 계정의 Ultra
        snap_credits = {"title": "Credits", "value": "$7.00", "amount": 700, "currency": "USD", "enabled": True,
                        "expires_at": None}
        with usage._lock:
            usage._snap["claude"] = {"plan": "Pro", "windows": [], "extras": [snap_credits]}
        with mock.patch.object(usage, "claude_oauth", return_value={"accessToken": "tok-b"}):
            self.responses["profile"] = RuntimeError("boom")
            self.responses["credits"] = usage.UsageError("credits HTTP 500")
            d = usage.collect_claude()
        self.assertEqual(d["plan"], "Pro")                         # _snap 폴백만
        self.assertEqual(d["extras"], [{**snap_credits, "enabled": False}])

    def test_token_change_clears_failed_at_and_refetches(self):
        self.responses["profile"] = usage.UsageError("x")
        usage.collect_claude()
        self.assertGreater(usage._claude_profile_cache["failed_at"], 0)
        self.responses["profile"] = {"organization": {"uuid": "org-b", "plan_display_name": "Pro"}}
        with mock.patch.object(usage, "claude_oauth", return_value={"accessToken": "tok-b"}):
            d = usage.collect_claude()
        self.assertEqual(d["plan"], "Pro")                         # 이전 계정의 실패 대기가 새 계정을 막지 않는다
        self.assertTrue(any(c == ("credits", usage.CLAUDE_CREDITS_URL.format(org="org-b")) for c in self.calls))

    def test_token_change_refetches_even_inside_ttl(self):
        usage.collect_claude()
        n_profile, n_credits = self.labels().count("profile"), self.labels().count("credits")
        self.responses["profile"] = {"organization": {"uuid": "org-b", "plan_display_name": "Pro"}}
        self.responses["credits"] = {**CREDITS, "amount": 4200}
        with mock.patch.object(usage, "claude_oauth", return_value={"accessToken": "tok-b"}):
            d = usage.collect_claude()
        self.assertEqual(self.labels().count("profile"), n_profile + 1)
        self.assertEqual(self.labels().count("credits"), n_credits + 1)
        self.assertEqual((d["plan"], d["extras"][0]["value"]), ("Pro", "$42.00"))

    def test_org_is_url_quoted_in_credits_url(self):
        self.responses["profile"] = {"organization": {"uuid": "a/b?c d#e", "plan_display_name": "Ultra"}}
        usage.collect_claude()
        url = dict(self.calls)["credits"]
        self.assertEqual(url, "https://api.anthropic.com/api/oauth/organizations/a%2Fb%3Fc%20d%23e/prepaid/credits")


class ClaudeBackoffTests(unittest.TestCase):
    """claude_get의 429 → 공통 백오프. urlopen만 대체한다."""

    def setUp(self):
        self._saved_backoff = usage._claude_backoff_until
        usage._claude_backoff_until = 0.0
        self._saved_pcache = dict(usage._claude_profile_cache)
        self._saved_ccache = dict(usage._claude_credits_cache)
        usage._claude_profile_cache.update(at=0.0, plan=None, org="", fp="", failed_at=0.0)
        usage._claude_credits_cache.update(at=0.0, entry=None, fp="", failed_at=0.0)
        with usage._lock:
            self._saved_snap = usage._snap["claude"]
            usage._snap["claude"] = None

    def tearDown(self):
        usage._claude_backoff_until = self._saved_backoff
        usage._claude_profile_cache.clear(); usage._claude_profile_cache.update(self._saved_pcache)
        usage._claude_credits_cache.clear(); usage._claude_credits_cache.update(self._saved_ccache)
        with usage._lock:
            usage._snap["claude"] = self._saved_snap

    def http_error(self, code, url="https://example.invalid/x"):
        err = urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b"{}"))
        self.addCleanup(err.close)
        return err

    @staticmethod
    def ok(payload):
        import json
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
        return resp

    def test_usage_429_sets_shared_backoff(self):
        before = time.time()
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(429, usage.CLAUDE_USAGE_URL)):
            with self.assertRaisesRegex(usage.UsageError, "usage HTTP 429"):
                usage.claude_fetch("tok")
        self.assertGreaterEqual(usage._claude_backoff_until, before + usage.CLAUDE_BACKOFF_SEC)

    def test_aux_429_does_not_set_backoff(self):
        for url, label in ((usage.CLAUDE_PROFILE_URL, "profile"),
                           (usage.CLAUDE_CREDITS_URL.format(org="org-x"), "credits")):
            with mock.patch("urllib.request.urlopen", side_effect=self.http_error(429, url)):
                with self.assertRaisesRegex(usage.UsageError, f"{label} HTTP 429"):
                    usage.claude_get(url, "tok", label)
            self.assertEqual(usage._claude_backoff_until, 0.0, label)

    def test_claude_get_backoff_flag_controls_429(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(429)):
            with self.assertRaises(usage.UsageError):
                usage.claude_get(usage.CLAUDE_PROFILE_URL, "tok", "profile")        # 기본 backoff=False
            self.assertEqual(usage._claude_backoff_until, 0.0)
            with self.assertRaises(usage.UsageError):
                usage.claude_get(usage.CLAUDE_PROFILE_URL, "tok", "profile", backoff=True)
            self.assertGreater(usage._claude_backoff_until, time.time())

    def test_http_client_exceptions_become_usage_error(self):
        for exc in (http.client.IncompleteRead(b"par"), http.client.BadStatusLine("junk")):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(usage.UsageError, msg=type(exc).__name__):
                    usage.claude_get(usage.CLAUDE_PROFILE_URL, "tok", "profile")
            self.assertEqual(usage._claude_backoff_until, 0.0)

    def test_incomplete_read_while_reading_body_becomes_usage_error(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"{")
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(usage.UsageError):
                usage.claude_get(usage.CLAUDE_USAGE_URL, "tok", "usage")

    def test_other_http_errors_do_not_set_backoff(self):
        for code in (401, 403, 500):
            with mock.patch("urllib.request.urlopen", side_effect=self.http_error(code)):
                with self.assertRaises(usage.UsageError):
                    usage.claude_get(usage.CLAUDE_PROFILE_URL, "tok", "profile")
            self.assertEqual(usage._claude_backoff_until, 0.0, code)

    def test_non_object_response_is_error(self):
        with mock.patch("urllib.request.urlopen", return_value=self.ok([1, 2])):
            with self.assertRaisesRegex(usage.UsageError, "profile"):
                usage.claude_get(usage.CLAUDE_PROFILE_URL, "tok", "profile")

    def test_backoff_blocks_collect_claude(self):
        usage._claude_backoff_until = time.time() + 300
        with mock.patch.object(usage, "claude_oauth") as oauth:
            with self.assertRaisesRegex(usage.UsageError, "백오프"):
                usage.collect_claude()
        oauth.assert_not_called()

    def urlopen_for(self, calls, **by_url):
        """URL → 응답 dict 또는 HTTP 오류 코드(int). 호출한 URL을 calls에 쌓는다."""
        def fake_urlopen(req, timeout=None):
            url = req.full_url
            calls.append(url)
            r = by_url[url]
            if isinstance(r, int):
                raise self.http_error(r, url)
            return self.ok(r)
        return fake_urlopen

    OAUTH = {"accessToken": "tok", "subscriptionType": "max"}

    def test_usage_429_blocks_next_collect(self):
        calls = []
        fake = self.urlopen_for(calls, **{usage.CLAUDE_USAGE_URL: 429})
        with mock.patch.object(usage, "claude_oauth", return_value=self.OAUTH), mock.patch("urllib.request.urlopen", side_effect=fake):
            with self.assertRaisesRegex(usage.UsageError, "usage HTTP 429"):
                usage.collect_claude()
            self.assertGreater(usage._claude_backoff_until, time.time())
            with self.assertRaisesRegex(usage.UsageError, "백오프"):
                usage.collect_claude()
        self.assertEqual(calls, [usage.CLAUDE_USAGE_URL])

    def test_profile_429_does_not_set_backoff_and_collection_completes(self):
        calls = []
        fake = self.urlopen_for(calls, **{usage.CLAUDE_USAGE_URL: USAGE, usage.CLAUDE_PROFILE_URL: 429})
        with mock.patch.object(usage, "claude_oauth", return_value=self.OAUTH), mock.patch("urllib.request.urlopen", side_effect=fake):
            d = usage.collect_claude()
            self.assertEqual(d["plan"], "")                        # subscriptionType("max")로 대체하지 않음
            self.assertEqual(len(d["windows"]), 2)
            self.assertEqual(d["extras"], [])
            self.assertEqual(usage._claude_backoff_until, 0.0)
            self.assertEqual(calls, [usage.CLAUDE_USAGE_URL, usage.CLAUDE_PROFILE_URL])   # org 모름 → credits 호출 없음
            d2 = usage.collect_claude()                            # "429 백오프 중"이 아니다
            self.assertEqual(len(d2["windows"]), 2)
        # 두 번째 회차: usage만 다시 부르고 실패한 profile은 재호출하지 않음
        self.assertEqual(calls, [usage.CLAUDE_USAGE_URL, usage.CLAUDE_PROFILE_URL, usage.CLAUDE_USAGE_URL])

    def test_credits_429_does_not_set_backoff_and_collection_completes(self):
        calls = []
        credits_url = usage.CLAUDE_CREDITS_URL.format(org="org-x")
        fake = self.urlopen_for(calls, **{usage.CLAUDE_USAGE_URL: USAGE, usage.CLAUDE_PROFILE_URL: PROFILE, credits_url: 429})
        with mock.patch.object(usage, "claude_oauth", return_value=self.OAUTH), mock.patch("urllib.request.urlopen", side_effect=fake):
            d = usage.collect_claude()
            self.assertEqual(d["plan"], "Ultra")
            self.assertEqual(len(d["windows"]), 2)
            self.assertEqual(d["extras"], [])
            self.assertEqual(usage._claude_backoff_until, 0.0)
            d2 = usage.collect_claude()                            # 백오프 아님, credits 재호출 없음
            self.assertEqual(len(d2["windows"]), 2)
        self.assertEqual(calls.count(credits_url), 1)
        self.assertEqual(calls.count(usage.CLAUDE_USAGE_URL), 2)
        self.assertEqual(calls.count(usage.CLAUDE_PROFILE_URL), 1)


class UsageHistoryLiveTests(unittest.TestCase):
    """/api/usage/history의 live — 라우트 함수를 직접 호출(인증·DB는 대체, TestClient 없음)."""

    def setUp(self):
        with usage._lock:
            self._saved = dict(usage._snap)

    def tearDown(self):
        with usage._lock:
            usage._snap.update(self._saved)

    def call(self):
        from server import app as hub
        empty = {p: {"windows": {}, "resets": {}, "plan_changes": []} for p in usage.PROVIDERS}
        with mock.patch.object(hub, "_require"), mock.patch.object(hub.db, "tx"), \
                mock.patch.object(hub.usage, "history", return_value=empty):
            return asyncio.run(hub.get_usage_history(mock.MagicMock(), 30))

    def win(self, title):
        return {"title": title, "pct": 10, "left": "", "resets_at": None, "value": "10%"}

    def test_live_lists_current_window_titles_and_none_without_snapshot(self):
        with usage._lock:
            usage._snap["claude"] = {"plan": "Ultra", "windows": [self.win("7d"), self.win("7d Fable")], "extras": []}
            usage._snap["codex"] = None
        h = self.call()
        self.assertEqual(h["claude"]["live"], ["7d", "7d Fable"])   # 5h는 사라졌으므로 없음
        self.assertNotIn("5h", h["claude"]["live"])
        self.assertEqual(h["claude"]["plan"], "Ultra")
        self.assertIsNone(h["codex"]["live"])
        self.assertEqual(h["codex"]["plan"], "")

    def test_live_empty_list_is_distinct_from_none(self):
        with usage._lock:
            usage._snap["claude"] = {"plan": "", "windows": [], "extras": []}
            usage._snap["codex"] = {"plan": "Prolite", "windows": [self.win("5h"), self.win("7d")], "extras": []}
        h = self.call()
        self.assertEqual(h["claude"]["live"], [])
        self.assertEqual(h["codex"]["live"], ["5h", "7d"])


# ── Codex 크레딧 잔액 ─────────────────────────────────

# 실제 응답 형태(식별자는 제거). RPC는 camelCase, 전사본 rate_limits는 snake_case.
CODEX_LIMIT_RPC = {"limitId": "codex",
                   "primary": {"usedPercent": 17, "windowDurationMins": 10080, "resetsAt": 1793774138},
                   "secondary": None,
                   "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                   "planType": "prolite"}
CODEX_RL_TRANSCRIPT = {"limit_id": "codex",
                       "primary": {"used_percent": 34.0, "window_minutes": 10080, "resets_at": 1793774138},
                       "secondary": None,
                       "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                       "plan_type": "prolite"}


def codex_rpc_result(credits="keep", reset_available=2, by_id=True):
    limit = dict(CODEX_LIMIT_RPC)
    if credits != "keep":
        if credits is None:
            limit.pop("credits")
        else:
            limit["credits"] = credits
    result = {"rateLimits": limit,
              "rateLimitResetCredits": {"availableCount": reset_available, "credits": []}}
    if by_id:
        result["rateLimitsByLimitId"] = {"codex": limit}
    if reset_available is None:
        result.pop("rateLimitResetCredits")
    return result


class CreditCountFormatTests(unittest.TestCase):
    def test_strings_trim_trailing_zeros(self):
        for raw, want in [("0", "0"), ("12", "12"), ("12.50", "12.5"), ("12.5", "12.5"), ("12.00", "12"),
                          ("0.10", "0.1"), ("100", "100"), ("100.00", "100"), ("1e3", "1000"), ("1E+3", "1000"),
                          ("  7 ", "7"), ("1234567.891", "1234567.89")]:
            with self.subTest(raw=raw):
                self.assertEqual(usage.format_credit_count(raw), want)

    def test_rounds_half_up_to_two_decimals(self):
        for raw, want in [("12.345", "12.35"), ("12.344", "12.34"), ("0.005", "0.01"), ("0.004", "0"),
                          ("2.675", "2.68"), ("0.995", "1"), ("9.999", "10")]:
            with self.subTest(raw=raw):
                self.assertEqual(usage.format_credit_count(raw), want)

    def test_negative_and_negative_zero(self):
        self.assertEqual(usage.format_credit_count("-3.50"), "-3.5")
        self.assertEqual(usage.format_credit_count("-0.001"), "0")      # '-0' 표시 금지
        self.assertEqual(usage.format_credit_count("-0"), "0")

    def test_numbers(self):
        self.assertEqual(usage.format_credit_count(5), "5")
        self.assertEqual(usage.format_credit_count(0), "0")
        self.assertEqual(usage.format_credit_count(12.5), "12.5")
        self.assertEqual(usage.format_credit_count(1e3), "1000")
        self.assertEqual(usage.format_credit_count(12.345), "12.35")    # str(float)로 변환하므로 이진 오차 없음

    def test_unusable_values_are_none(self):
        for raw in [None, True, False, "", "   ", "abc", "12abc", "nan", "NaN", "sNaN", "inf", "-Infinity",
                    float("nan"), float("inf"), float("-inf"), [], {}, (1,), b"5", object()]:
            with self.subTest(raw=raw):
                self.assertIsNone(usage.format_credit_count(raw))

    def test_huge_magnitude_is_none_not_an_exception(self):
        # Decimal.quantize는 유효숫자 28자리를 넘으면 InvalidOperation을 던진다 → None으로 처리돼야 한다.
        for raw in ["1e30", "1e40", "123456789012345678901234567890", 1e30]:
            with self.subTest(raw=raw):
                self.assertIsNone(usage.format_credit_count(raw))


class NormalizeCreditsTests(unittest.TestCase):
    def test_rpc_camel_case(self):
        self.assertEqual(usage._normalize_credits({"hasCredits": False, "unlimited": False, "balance": "0"}),
                         {"balance": "0", "unlimited": False, "has_credits": False})
        self.assertEqual(usage._normalize_credits({"hasCredits": True, "unlimited": False, "balance": "12.50"}),
                         {"balance": "12.50", "unlimited": False, "has_credits": True})

    def test_transcript_snake_case(self):
        self.assertEqual(usage._normalize_credits({"has_credits": True, "unlimited": False, "balance": "3"}),
                         {"balance": "3", "unlimited": False, "has_credits": True})

    def test_unlimited_without_balance(self):
        self.assertEqual(usage._normalize_credits({"hasCredits": True, "unlimited": True, "balance": None}),
                         {"balance": None, "unlimited": True, "has_credits": True})
        self.assertEqual(usage._normalize_credits({"has_credits": True, "unlimited": True}),
                         {"balance": None, "unlimited": True, "has_credits": True})

    def test_numeric_balance_becomes_string(self):
        self.assertEqual(usage._normalize_credits({"unlimited": False, "balance": 5})["balance"], "5")
        self.assertEqual(usage._normalize_credits({"unlimited": False, "balance": 2.5})["balance"], "2.5")

    def test_flags_are_strict_booleans(self):
        got = usage._normalize_credits({"hasCredits": "yes", "unlimited": 1, "balance": "1"})
        self.assertEqual(got, {"balance": "1", "unlimited": False, "has_credits": False})

    def test_camel_case_wins_when_both_present(self):
        got = usage._normalize_credits({"hasCredits": True, "has_credits": False, "unlimited": False, "balance": "1"})
        self.assertTrue(got["has_credits"])

    def test_missing_or_malformed_is_none(self):
        for raw in [None, [], "x", 5, True, {}, {"hasCredits": True},
                    {"balance": None}, {"balance": True}, {"balance": [], "unlimited": "no"},
                    {"balance": {"v": 1}}, {"hasCredits": True, "balance": None, "unlimited": None}]:
            with self.subTest(raw=raw):
                self.assertIsNone(usage._normalize_credits(raw))

    def test_garbage_balance_with_boolean_unlimited_false_is_none(self):
        # 쓸 만한 잔액도 unlimited=true도 없으면 폴백 후보에서 빠지도록 None
        self.assertIsNone(usage._normalize_credits({"unlimited": False, "balance": []}))
        self.assertIsNone(usage._normalize_credits({"unlimited": False}))
        self.assertIsNone(usage._normalize_credits({"hasCredits": True, "unlimited": False, "balance": None}))

    def test_normalized_balance_is_formattable_only_when_numeric(self):
        got = usage._normalize_credits({"unlimited": False, "balance": "abc"})
        self.assertEqual(got["balance"], "abc")
        self.assertIsNone(usage.format_credit_count(got["balance"]))


class CodexCreditsParseTests(unittest.TestCase):
    def test_rpc_real_sample(self):
        parsed = usage.parse_codex_rpc(codex_rpc_result())
        self.assertEqual(parsed["credits"], {"balance": "0", "unlimited": False, "has_credits": False})
        self.assertEqual(parsed["reset_credits"], 2)
        self.assertEqual(parsed["plan"], "prolite")
        self.assertEqual([(w["title"], w["pct"]) for w in parsed["windows"]], [("7d", 17)])

    def test_rpc_top_level_only_when_no_by_id(self):
        parsed = usage.parse_codex_rpc(codex_rpc_result(credits={"hasCredits": True, "unlimited": False, "balance": "8"},
                                                        by_id=False))
        self.assertEqual(parsed["credits"], {"balance": "8", "unlimited": False, "has_credits": True})

    def test_rpc_missing_credits(self):
        parsed = usage.parse_codex_rpc(codex_rpc_result(credits=None))
        self.assertIsNone(parsed["credits"])
        self.assertEqual(len(parsed["windows"]), 1)         # 크레딧이 없어도 창은 그대로

    def test_rpc_malformed_credits(self):
        for bad in ["oops", 5, [], {}, {"balance": True}]:
            with self.subTest(bad=bad):
                parsed = usage.parse_codex_rpc(codex_rpc_result(credits=bad))
                self.assertIsNone(parsed["credits"])
                self.assertEqual(len(parsed["windows"]), 1)

    def test_rpc_multi_limit_picks_first_with_credits(self):
        no_credits = {"limitId": "a", "planType": "pro",
                      "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 1793770000}}
        bad_credits = {"limitId": "b", "credits": {"balance": True},
                       "primary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1793770000}}
        first = {"limitId": "c", "credits": {"hasCredits": True, "unlimited": False, "balance": "4"},
                 "primary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": 1793770000}}
        second = {"limitId": "d", "credits": {"hasCredits": True, "unlimited": False, "balance": "99"}}
        parsed = usage.parse_codex_rpc({"rateLimitsByLimitId": {"a": no_credits, "b": bad_credits, "c": first, "d": second}})
        self.assertEqual(parsed["credits"]["balance"], "4")

    def test_rpc_by_id_without_credits_falls_back_to_top_level(self):
        by_id_limit = {k: v for k, v in CODEX_LIMIT_RPC.items() if k != "credits"}
        top = {"credits": {"hasCredits": True, "unlimited": False, "balance": "6"}}
        parsed = usage.parse_codex_rpc({"rateLimitsByLimitId": {"codex": by_id_limit}, "rateLimits": top})
        self.assertEqual(parsed["credits"]["balance"], "6")

    def test_rpc_by_id_credits_win_over_top_level(self):
        top = {"credits": {"hasCredits": True, "unlimited": False, "balance": "6"}}
        parsed = usage.parse_codex_rpc({"rateLimitsByLimitId": {"codex": CODEX_LIMIT_RPC}, "rateLimits": top})
        self.assertEqual(parsed["credits"]["balance"], "0")

    def test_rpc_non_dict_limits_are_skipped(self):
        parsed = usage.parse_codex_rpc({"rateLimitsByLimitId": {"x": None, "codex": CODEX_LIMIT_RPC}})
        self.assertEqual(parsed["credits"]["balance"], "0")

    def test_rpc_empty_result_has_no_credits(self):
        parsed = usage.parse_codex_rpc({})
        self.assertIsNone(parsed["credits"])
        self.assertEqual(parsed["windows"], [])

    def test_transcript_real_sample(self):
        parsed = usage.parse_codex_transcript(CODEX_RL_TRANSCRIPT)
        self.assertEqual(parsed["credits"], {"balance": "0", "unlimited": False, "has_credits": False})
        self.assertIsNone(parsed["reset_credits"])
        self.assertEqual(parsed["plan"], "prolite")

    def test_transcript_missing_or_malformed_credits(self):
        base = {k: v for k, v in CODEX_RL_TRANSCRIPT.items() if k != "credits"}
        self.assertIsNone(usage.parse_codex_transcript(base)["credits"])
        self.assertIsNone(usage.parse_codex_transcript({**base, "credits": None})["credits"])
        self.assertIsNone(usage.parse_codex_transcript({**base, "credits": "n/a"})["credits"])
        self.assertIsNone(usage.parse_codex_transcript({**base, "credits": {"balance": False}})["credits"])


class CodexCollectCreditsTests(unittest.TestCase):
    NOW = 1793770000.0      # CODEX_*의 resets_at(1793774138)보다 이르다

    def setUp(self):
        with usage._lock:
            self._snap = usage._snap["codex"]
            usage._snap["codex"] = None
        self._saved_codex_backoff = dict(usage._codex_backoff)      # RPC 실패가 다음 테스트를 백오프에 가두지 않게
        usage._codex_backoff.update(until=0.0, reason="")

    def tearDown(self):
        with usage._lock:
            usage._snap["codex"] = self._snap
        usage._codex_backoff.clear()
        usage._codex_backoff.update(self._saved_codex_backoff)

    def collect(self, result=None, now=None, rpc=None):
        with mock.patch.object(usage, "codex_rpc", rpc or mock.Mock(return_value=result)):
            return usage.collect_codex(now or self.NOW)

    def titles(self, out):
        return [e["title"] for e in out["extras"]]

    def entry(self, out, title):      # 순서와 무관하게 제목으로 찾는다 — 순서는 순서 테스트가 따로 고정
        return next(e for e in out["extras"] if e["title"] == title)

    def test_reset_credits_precede_credits_entry(self):
        out = self.collect(codex_rpc_result(credits={"hasCredits": True, "unlimited": False, "balance": "12.50"}))
        self.assertEqual(self.titles(out), ["Reset credits", "Credits"])      # 사용자 요청: 초기화 행이 크레딧 위
        self.assertEqual(out["extras"][0], {"title": "Reset credits", "value": "2 available", "count": 2})
        self.assertEqual(out["extras"][1], {"title": "Credits", "value": "12.5", "unlimited": False, "has_credits": True})

    def test_real_sample_zero_balance_is_shown(self):
        out = self.collect(codex_rpc_result())
        self.assertEqual(self.entry(out, "Credits"), {"title": "Credits", "value": "0", "unlimited": False, "has_credits": False})
        self.assertEqual(self.titles(out), ["Reset credits", "Credits"])
        self.assertEqual(out["plan"], "Prolite")

    def test_only_credits(self):
        out = self.collect(codex_rpc_result(reset_available=None))
        self.assertEqual(self.titles(out), ["Credits"])

    def test_only_reset_credits_when_credits_missing(self):
        out = self.collect(codex_rpc_result(credits=None))
        self.assertEqual(out["extras"], [{"title": "Reset credits", "value": "2 available", "count": 2}])

    def test_reset_credits_zero_is_still_listed(self):
        out = self.collect(codex_rpc_result(reset_available=0))
        self.assertEqual(self.titles(out), ["Reset credits", "Credits"])
        self.assertEqual(self.entry(out, "Reset credits")["count"], 0)

    def test_neither(self):
        out = self.collect(codex_rpc_result(credits=None, reset_available=None))
        self.assertEqual(out["extras"], [])

    def test_unlimited(self):
        out = self.collect(codex_rpc_result(credits={"hasCredits": True, "unlimited": True, "balance": None}))
        self.assertEqual(self.entry(out, "Credits"), {"title": "Credits", "value": "unlimited", "unlimited": True, "has_credits": True})
        self.assertEqual(self.titles(out), ["Reset credits", "Credits"])

    def test_unlimited_wins_over_balance(self):
        out = self.collect(codex_rpc_result(credits={"hasCredits": True, "unlimited": True, "balance": "5"}))
        self.assertEqual(self.entry(out, "Credits")["value"], "unlimited")

    def test_unformattable_balance_omitted(self):
        for bad in ["abc", "NaN", "inf", ""]:
            with self.subTest(balance=bad):
                out = self.collect(codex_rpc_result(credits={"hasCredits": True, "unlimited": False, "balance": bad}))
                self.assertEqual(self.titles(out), ["Reset credits"])

    def test_missing_balance_not_unlimited_omitted(self):
        out = self.collect(codex_rpc_result(credits={"hasCredits": True, "unlimited": False, "balance": None}))
        self.assertEqual(self.titles(out), ["Reset credits"])

    def test_unlimited_with_unformattable_balance_still_shown(self):
        out = self.collect(codex_rpc_result(credits={"hasCredits": True, "unlimited": True, "balance": "abc"}))
        self.assertEqual(self.entry(out, "Credits")["value"], "unlimited")

    def test_balance_is_rounded_for_display(self):
        out = self.collect(codex_rpc_result(credits={"hasCredits": True, "unlimited": False, "balance": "12.345"}))
        self.assertEqual(self.entry(out, "Credits")["value"], "12.35")

    def test_windows_unaffected_by_credits(self):
        out = self.collect(codex_rpc_result())
        self.assertEqual([(w["title"], w["pct"]) for w in out["windows"]], [("7d", 17)])

    def test_every_collect_hits_rpc_and_reflects_latest_balance(self):
        rpc = mock.Mock(side_effect=[
            codex_rpc_result(credits={"hasCredits": True, "unlimited": False, "balance": "7"}),
            codex_rpc_result(credits={"hasCredits": True, "unlimited": False, "balance": "6.5"}),
            codex_rpc_result(credits={"hasCredits": True, "unlimited": False, "balance": "6"})])
        with mock.patch.object(usage, "codex_rpc", rpc):
            values = [self.entry(usage.collect_codex(self.NOW + i), "Credits")["value"] for i in range(3)]   # 1초 간격 호출도 캐시 없음
        self.assertEqual(values, ["7", "6.5", "6"])
        self.assertEqual(rpc.call_count, 3)

    def test_credits_row_disappears_when_a_later_rpc_drops_it(self):
        rpc = mock.Mock(side_effect=[codex_rpc_result(), codex_rpc_result(credits=None)])
        with mock.patch.object(usage, "codex_rpc", rpc):
            first = usage.collect_codex(self.NOW)
            second = usage.collect_codex(self.NOW + 1)
        self.assertEqual(self.titles(first), ["Reset credits", "Credits"])
        self.assertEqual(self.titles(second), ["Reset credits"])        # 이전 값이 남지 않는다

    def test_collect_has_no_module_cache(self):
        self.assertFalse(hasattr(usage, "_codex_cache"))
        self.assertFalse(hasattr(usage, "CODEX_RPC_TTL"))

    def test_transcript_fallback_carries_credits(self):
        rl = {**CODEX_RL_TRANSCRIPT, "credits": {"has_credits": True, "unlimited": False, "balance": "3.10"}}
        seen = usage.to_epoch("2026-10-30T00:00:00Z")
        with mock.patch.object(usage, "codex_rpc", side_effect=usage.UsageError("timeout")), \
                mock.patch.object(usage, "codex_transcript_snapshot", return_value=(rl, seen)):
            out = usage.collect_codex(self.NOW)
        self.assertEqual(out["extras"], [{"title": "Credits", "value": "3.1", "unlimited": False, "has_credits": True}])
        self.assertEqual(out["updated"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seen)))

    def test_transcript_fallback_real_sample_has_zero_credits_and_no_reset_row(self):
        seen = usage.to_epoch("2026-10-30T00:00:00Z")
        with mock.patch.object(usage, "codex_rpc", side_effect=usage.UsageError("timeout")), \
                mock.patch.object(usage, "codex_transcript_snapshot", return_value=(CODEX_RL_TRANSCRIPT, seen)):
            out = usage.collect_codex(self.NOW)
        self.assertEqual(out["extras"], [{"title": "Credits", "value": "0", "unlimited": False, "has_credits": False}])

    def test_transcript_fallback_does_not_retry_rpc_within_backoff_window(self):
        rl = {**CODEX_RL_TRANSCRIPT, "credits": {"has_credits": True, "unlimited": True}}
        seen = usage.to_epoch("2026-10-30T00:00:00Z")
        rpc = mock.Mock(side_effect=usage.UsageError("timeout"))
        with mock.patch.object(usage, "codex_rpc", rpc), \
                mock.patch.object(usage, "codex_transcript_snapshot", return_value=(rl, seen)) as snap:
            first = usage.collect_codex(self.NOW)
            self.assertEqual((rpc.call_count, snap.call_count), (1, 1))
            self.assertEqual(self.entry(first, "Credits")["value"], "unlimited")
            with self.assertRaises(usage.UsageError):                   # 창 안: RPC도 전사본 스캔도 없다
                usage.collect_codex(self.NOW + 60)
            self.assertEqual((rpc.call_count, snap.call_count), (1, 1))
            second = usage.collect_codex(self.NOW + usage.CODEX_BACKOFF_SEC)   # 창이 끝나면 RPC를 다시 시도하고 다시 대체
        self.assertEqual((rpc.call_count, snap.call_count), (2, 2))
        self.assertEqual(second["extras"], first["extras"])
        self.assertEqual(self.entry(second, "Credits")["value"], "unlimited")

    def test_rpc_recovers_after_fallback(self):
        rl = {**CODEX_RL_TRANSCRIPT, "credits": {"has_credits": True, "unlimited": False, "balance": "1"}}
        seen = usage.to_epoch("2026-10-30T00:00:00Z")
        rpc = mock.Mock(side_effect=[usage.UsageError("timeout"), codex_rpc_result()])
        with mock.patch.object(usage, "codex_rpc", rpc), \
                mock.patch.object(usage, "codex_transcript_snapshot", return_value=(rl, seen)):
            first = usage.collect_codex(self.NOW)
            second = usage.collect_codex(self.NOW + usage.CODEX_BACKOFF_SEC)      # 백오프 창이 끝난 뒤에야 RPC를 다시 부른다
        self.assertEqual(self.entry(first, "Credits")["value"], "1")
        self.assertEqual(self.titles(second), ["Reset credits", "Credits"])
        self.assertEqual(self.entry(second, "Credits")["value"], "0")

    def test_transcript_without_credits_has_no_credit_row(self):
        rl = {k: v for k, v in CODEX_RL_TRANSCRIPT.items() if k != "credits"}
        with mock.patch.object(usage, "codex_rpc", side_effect=usage.UsageError("timeout")), \
                mock.patch.object(usage, "codex_transcript_snapshot",
                                  return_value=(rl, usage.to_epoch("2026-10-30T00:00:00Z"))):
            out = usage.collect_codex(self.NOW)
        self.assertEqual(out["extras"], [])

    def test_fallback_dict_keeps_credits_after_window_filtering(self):
        rl = {**CODEX_RL_TRANSCRIPT, "credits": {"has_credits": True, "unlimited": False, "balance": "9"}}
        with mock.patch.object(usage, "codex_transcript_snapshot",
                               return_value=(rl, usage.to_epoch("2026-10-30T00:00:00Z"))):
            parsed = usage.codex_fallback(usage.UsageError("timeout"), self.NOW)
        self.assertEqual(parsed["credits"]["balance"], "9")

    def test_huge_balance_omits_credits_only(self):
        out = self.collect(codex_rpc_result(credits={"hasCredits": True, "unlimited": False, "balance": "1e30"}))
        self.assertEqual(self.titles(out), ["Reset credits"])
        self.assertEqual(len(out["windows"]), 1)

    def test_partial_by_id_credits_fall_back_to_top_level_balance(self):
        limit = {k: v for k, v in CODEX_LIMIT_RPC.items() if k != "credits"}
        result = {"rateLimitsByLimitId": {"codex": {**limit, "credits": {"unlimited": False}}},
                  "rateLimits": {**limit, "credits": {"hasCredits": True, "unlimited": False, "balance": "3"}},
                  "rateLimitResetCredits": {"availableCount": 2}}
        out = self.collect(result)
        self.assertEqual(self.entry(out, "Credits"), {"title": "Credits", "value": "3", "unlimited": False, "has_credits": True})
        self.assertEqual(self.titles(out), ["Reset credits", "Credits"])

    def test_multi_limit_partial_first_then_balance_second(self):
        win = {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": 1793774138}
        result = {"rateLimitsByLimitId": {
            "a": {"limitId": "a", "primary": win, "credits": {"unlimited": False}},
            "b": {"limitId": "b", "credits": {"hasCredits": True, "unlimited": False, "balance": "7"}}}}
        out = self.collect(result)
        self.assertEqual(out["extras"], [{"title": "Credits", "value": "7", "unlimited": False, "has_credits": True}])

    def test_exception_building_credits_entry_keeps_windows_and_reset_row(self):
        with mock.patch.object(usage, "format_credit_count", side_effect=RuntimeError("boom")):
            out = self.collect(codex_rpc_result())
        self.assertEqual(out["extras"], [{"title": "Reset credits", "value": "2 available", "count": 2}])
        self.assertEqual([(w["title"], w["pct"]) for w in out["windows"]], [("7d", 17)])
        self.assertEqual(out["plan"], "Prolite")


class CodexBackoffTests(unittest.TestCase):
    NOW = 1793770000.0
    END = NOW + usage.CODEX_BACKOFF_SEC

    def setUp(self):
        self._saved_codex_backoff = dict(usage._codex_backoff)
        usage._codex_backoff.update(until=0.0, reason="")
        self.addCleanup(self._restore)

    def _restore(self):
        usage._codex_backoff.clear()
        usage._codex_backoff.update(self._saved_codex_backoff)

    def fail_once(self, reason="timeout"):
        """NOW에 RPC를 실패시켜 백오프를 건다(대체도 실패). (rpc, fb, 전파된 예외)를 돌려준다."""
        rpc = mock.Mock(side_effect=usage.UsageError(reason))
        fb = mock.Mock(side_effect=usage.UsageError("no transcript"))
        raised = None
        with mock.patch.object(usage, "codex_rpc", rpc), mock.patch.object(usage, "codex_fallback", fb):
            try:
                usage.collect_codex(self.NOW)
            except usage.UsageError as exc:
                raised = exc
        return rpc, fb, raised

    def test_constants(self):
        self.assertEqual(usage.CODEX_BACKOFF_SEC, 300)

    def test_rpc_failure_sets_window_and_reason(self):
        self.fail_once("boom")
        self.assertEqual(usage._codex_backoff, {"until": self.END, "reason": "boom"})

    def test_backoff_skips_rpc_and_fallback_and_raises(self):
        self.fail_once("boom")
        rpc, fb = mock.Mock(), mock.Mock()
        with mock.patch.object(usage, "codex_rpc", rpc), mock.patch.object(usage, "codex_fallback", fb), \
                mock.patch.object(usage, "codex_transcript_snapshot") as snap:
            for dt in (1, 60, 299):
                with self.assertRaises(usage.UsageError):
                    usage.collect_codex(self.NOW + dt)
        rpc.assert_not_called()
        fb.assert_not_called()
        snap.assert_not_called()

    def test_error_text_identical_within_window_and_contains_reason(self):
        self.fail_once("rateLimits/read 실패: boom")
        msgs = []
        with mock.patch.object(usage, "codex_rpc"), mock.patch.object(usage, "codex_fallback"):
            for dt in (1, 100, 299):
                with self.assertRaises(usage.UsageError) as cm:
                    usage.collect_codex(self.NOW + dt)
                msgs.append(str(cm.exception))
        self.assertEqual(len(set(msgs)), 1)
        self.assertIn("rateLimits/read 실패: boom", msgs[0])
        self.assertIn("5분", msgs[0])

    def test_window_boundary(self):
        self.fail_once("boom")
        rpc = mock.Mock(return_value=codex_rpc_result())
        with mock.patch.object(usage, "codex_rpc", rpc):
            with self.assertRaises(usage.UsageError):
                usage.collect_codex(self.END - 1)
            rpc.assert_not_called()
            out = usage.collect_codex(self.END)                         # now == until → 재시도
        rpc.assert_called_once_with()
        self.assertEqual(out["plan"], "Prolite")

    def test_rpc_success_after_window_clears_backoff(self):
        self.fail_once("boom")
        with mock.patch.object(usage, "codex_rpc", return_value=codex_rpc_result()):
            usage.collect_codex(self.END + 10)
            self.assertEqual(usage._codex_backoff, {"until": 0.0, "reason": ""})
            usage.collect_codex(self.END + 11)                          # 즉시 다음 호출도 RPC를 부른다
        self.assertEqual(usage._codex_backoff["until"], 0.0)

    def test_rpc_failing_again_after_window_starts_new_window_with_new_reason(self):
        self.fail_once("first")
        rpc = mock.Mock(side_effect=usage.UsageError("second"))
        with mock.patch.object(usage, "codex_rpc", rpc), \
                mock.patch.object(usage, "codex_fallback", side_effect=usage.UsageError("x")):
            with self.assertRaises(usage.UsageError):
                usage.collect_codex(self.END)
            with self.assertRaises(usage.UsageError) as cm:
                usage.collect_codex(self.END + 1)
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(usage._codex_backoff, {"until": self.END + usage.CODEX_BACKOFF_SEC, "reason": "second"})
        self.assertIn("second", str(cm.exception))
        self.assertNotIn("first", str(cm.exception))

    def test_fallback_success_still_sets_backoff(self):
        rpc = mock.Mock(side_effect=usage.UsageError("boom"))
        seen = usage.to_epoch("2026-10-30T00:00:00Z")
        real_fb = usage.codex_fallback
        fb = mock.Mock(side_effect=real_fb)
        with mock.patch.object(usage, "codex_rpc", rpc), mock.patch.object(usage, "codex_fallback", fb), \
                mock.patch.object(usage, "codex_transcript_snapshot", return_value=(CODEX_RL_TRANSCRIPT, seen)):
            out = usage.collect_codex(self.NOW)                         # 대체 성공 → 값은 나온다
            self.assertEqual([w["title"] for w in out["windows"]], ["7d"])
            self.assertEqual(usage._codex_backoff["until"], self.END)
            with self.assertRaises(usage.UsageError):                   # 그러나 다음 호출은 RPC를 안 부른다
                usage.collect_codex(self.NOW + 60)
        self.assertEqual((rpc.call_count, fb.call_count), (1, 1))
        self.assertEqual(fb.call_args.args[1], self.NOW)

    def test_fallback_failure_propagates_and_still_sets_backoff(self):
        rpc, fb, raised = self.fail_once("boom")
        self.assertEqual((rpc.call_count, fb.call_count), (1, 1))
        self.assertIsInstance(raised, usage.UsageError)                 # 대체 실패가 호출자까지 전파된다
        self.assertEqual(str(raised), "no transcript")
        self.assertEqual(usage._codex_backoff, {"until": self.END, "reason": "boom"})

    # ── RPC는 성공했지만 한도 창이 없는 응답: RPC 실패와 똑같이 백오프 + 대체 1회 ──
    EMPTY_REASON = "rateLimits/read 응답에 한도 창이 없음"

    def empty_rpc(self):
        return mock.Mock(return_value={"rateLimits": {"planType": "prolite"}})      # 창 슬롯 없음

    def test_empty_windows_rpc_sets_backoff_and_tries_fallback_once(self):
        rpc, fb = self.empty_rpc(), mock.Mock(side_effect=usage.UsageError("no transcript"))
        with mock.patch.object(usage, "codex_rpc", rpc), mock.patch.object(usage, "codex_fallback", fb):
            with self.assertRaises(usage.UsageError) as cm:
                usage.collect_codex(self.NOW)
        self.assertEqual((rpc.call_count, fb.call_count), (1, 1))
        self.assertEqual(usage._codex_backoff, {"until": self.END, "reason": self.EMPTY_REASON})
        self.assertEqual(str(cm.exception), "no transcript")             # 대체 실패 예외가 그대로 전파
        self.assertEqual(str(fb.call_args.args[0]), self.EMPTY_REASON)   # 대체에는 빈 응답 사유가 전달된다
        self.assertEqual(fb.call_args.args[1], self.NOW)

    def test_empty_windows_rpc_fallback_success_returns_fallback_data(self):
        rpc = self.empty_rpc()
        seen = usage.to_epoch("2026-10-30T00:00:00Z")
        with mock.patch.object(usage, "codex_rpc", rpc), \
                mock.patch.object(usage, "codex_transcript_snapshot", return_value=(CODEX_RL_TRANSCRIPT, seen)):
            out = usage.collect_codex(self.NOW)
        self.assertEqual([w["title"] for w in out["windows"]], ["7d"])
        self.assertEqual(out["updated"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seen)))
        self.assertEqual(usage._codex_backoff, {"until": self.END, "reason": self.EMPTY_REASON})

    def test_empty_windows_rpc_next_call_in_window_raises_without_rpc(self):
        with mock.patch.object(usage, "codex_rpc", self.empty_rpc()), \
                mock.patch.object(usage, "codex_fallback", side_effect=usage.UsageError("x")):
            with self.assertRaises(usage.UsageError):
                usage.collect_codex(self.NOW)
        rpc, fb = mock.Mock(), mock.Mock()
        with mock.patch.object(usage, "codex_rpc", rpc), mock.patch.object(usage, "codex_fallback", fb), \
                mock.patch.object(usage, "codex_transcript_snapshot") as snap:
            for dt in (1, 150, 299):
                with self.assertRaises(usage.UsageError) as cm:
                    usage.collect_codex(self.NOW + dt)
                self.assertIn(self.EMPTY_REASON, str(cm.exception))
                self.assertIn("5분", str(cm.exception))
        rpc.assert_not_called()
        fb.assert_not_called()
        snap.assert_not_called()

    def test_empty_windows_rpc_does_not_clear_existing_backoff_reason_until_usable(self):
        self.fail_once("boom")
        # 창 만료 후 빈 응답 → 백오프가 새 사유로 다시 걸리고, 이어 정상 응답이 와야만 풀린다
        with mock.patch.object(usage, "codex_rpc", self.empty_rpc()), \
                mock.patch.object(usage, "codex_fallback", side_effect=usage.UsageError("x")):
            with self.assertRaises(usage.UsageError):
                usage.collect_codex(self.END)
        self.assertEqual(usage._codex_backoff, {"until": self.END + usage.CODEX_BACKOFF_SEC, "reason": self.EMPTY_REASON})
        with mock.patch.object(usage, "codex_rpc", return_value=codex_rpc_result()):
            usage.collect_codex(self.END + usage.CODEX_BACKOFF_SEC)
        self.assertEqual(usage._codex_backoff, {"until": 0.0, "reason": ""})

    def test_fallback_with_no_windows_still_raises(self):
        # 대체가 창 없는 값을 돌려주면(방어 경로) 마지막 가드가 "Codex 응답에 한도 창이 없음"을 올린다
        with mock.patch.object(usage, "codex_rpc", self.empty_rpc()), \
                mock.patch.object(usage, "codex_fallback", return_value={"windows": [], "seen_at": self.NOW}):
            with self.assertRaises(usage.UsageError) as cm:
                usage.collect_codex(self.NOW)
        self.assertEqual(str(cm.exception), "Codex 응답에 한도 창이 없음")
        self.assertEqual(usage._codex_backoff["until"], self.END)

    def test_non_usage_error_from_rpc_does_not_set_backoff(self):
        with mock.patch.object(usage, "codex_rpc", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                usage.collect_codex(self.NOW)
        self.assertEqual(usage._codex_backoff, {"until": 0.0, "reason": ""})

    def test_rpc_success_leaves_backoff_untouched_when_none_was_set(self):
        with mock.patch.object(usage, "codex_rpc", return_value=codex_rpc_result()):
            usage.collect_codex(self.NOW)
        self.assertEqual(usage._codex_backoff["until"], 0.0)


class CodexBackoffRefreshTests(unittest.TestCase):
    NOW = 1793770000.0

    def setUp(self):
        p = mock.patch.object(usage.time, "time", return_value=self.NOW)       # 실시계 의존 제거
        p.start()
        self.addCleanup(p.stop)
        with usage._lock:
            self.saved = dict(usage._snap)
            usage._snap.update(
                claude={"plan": "Max", "windows": [{"title": "5h", "pct": 3}], "extras": [], "updated": "c", "error": "keep"},
                codex={"plan": "Prolite", "windows": [{"title": "7d", "pct": 9}], "extras": [], "updated": "2026-11-02T00:00:00Z", "error": ""})
        self.claude_before = dict(usage._snap["claude"])
        self.codex_before = dict(usage._snap["codex"])
        for target in ("_persist", "record_history", "record_plan"):
            p = mock.patch.object(usage, target)
            setattr(self, target, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(usage.db, "tx")
        p.start()
        self.addCleanup(p.stop)
        self._saved_codex_backoff = dict(usage._codex_backoff)
        usage._codex_backoff.update(until=self.NOW + 250, reason="app-server down")

    def tearDown(self):
        with usage._lock:
            usage._snap.update(self.saved)
        usage._codex_backoff.clear()
        usage._codex_backoff.update(self._saved_codex_backoff)

    def test_refresh_during_backoff_keeps_snapshot_and_sets_error(self):
        with mock.patch.object(usage, "codex_rpc") as rpc, mock.patch.object(usage, "codex_fallback") as fb:
            usage.refresh(("codex",))
        rpc.assert_not_called()
        fb.assert_not_called()
        got = usage._snap["codex"]
        self.assertEqual(got["windows"], self.codex_before["windows"])
        self.assertEqual(got["updated"], self.codex_before["updated"])
        self.assertEqual(got["plan"], "Prolite")
        self.assertIn("app-server down", got["error"])
        self.assertIn("5분", got["error"])
        self.assertEqual(usage._snap["claude"], self.claude_before)
        self._persist.assert_not_called()
        self.record_history.assert_not_called()

    def test_refresh_error_text_is_stable_across_calls(self):
        errors = []
        with mock.patch.object(usage, "codex_rpc"), mock.patch.object(usage, "codex_fallback"):
            for _ in range(3):
                usage.refresh(("codex",))
                errors.append(usage._snap["codex"]["error"])
        self.assertEqual(len(set(errors)), 1)


class CodexRpcCommunicationTests(unittest.TestCase):
    class NotUsageRuntimeError(RuntimeError):
        pass

    def fake_proc(self, write_exc):
        proc = mock.Mock()
        proc.stdin.write.side_effect = write_exc
        proc.stdout = iter([])                      # pump 스레드가 바로 끝난다
        proc.terminate.return_value = None
        proc.wait.return_value = 0
        return proc

    def run_rpc(self, proc):
        with mock.patch.object(usage, "_codex_bin", return_value="/nonexistent/codex"), \
                mock.patch.object(usage.subprocess, "Popen", return_value=proc):
            return usage.codex_rpc(timeout=2)

    def test_broken_pipe_on_stdin_write_becomes_usage_error_and_cleans_up(self):
        proc = self.fake_proc(BrokenPipeError(32, "Broken pipe"))
        with self.assertRaises(usage.UsageError) as cm:
            self.run_rpc(proc)
        self.assertIn("codex app-server 통신 실패", str(cm.exception))
        self.assertIsInstance(cm.exception.__cause__, BrokenPipeError)
        proc.terminate.assert_called_once_with()
        proc.wait.assert_called_once()

    def test_value_error_closed_file_becomes_usage_error(self):
        proc = self.fake_proc(ValueError("I/O operation on closed file"))
        with self.assertRaises(usage.UsageError) as cm:
            self.run_rpc(proc)
        self.assertIn("통신 실패", str(cm.exception))
        proc.terminate.assert_called_once_with()

    def test_other_exceptions_are_not_converted(self):
        proc = self.fake_proc(self.NotUsageRuntimeError("bug"))
        with self.assertRaises(self.NotUsageRuntimeError):
            self.run_rpc(proc)
        proc.terminate.assert_called_once_with()    # 변환은 안 해도 정리는 한다

    def test_startup_failure_message_unchanged(self):
        with mock.patch.object(usage, "_codex_bin", return_value="/nonexistent/codex"), \
                mock.patch.object(usage.subprocess, "Popen", side_effect=FileNotFoundError("no such file")):
            with self.assertRaises(usage.UsageError) as cm:
                usage.codex_rpc(timeout=2)
        self.assertIn("codex app-server 시작 실패", str(cm.exception))

    def test_cleanup_kills_when_terminate_fails(self):
        proc = self.fake_proc(BrokenPipeError())
        proc.terminate.side_effect = ProcessLookupError()
        with self.assertRaises(usage.UsageError):
            self.run_rpc(proc)
        proc.kill.assert_called_once_with()


# ── 수집 주기·스케줄 ─────────────────────────────────

class DueProvidersTests(unittest.TestCase):
    def test_empty_means_everything_due_in_providers_order(self):
        self.assertEqual(usage.due_providers({}, 0.0), ["claude", "codex"])

    def test_boundary_is_inclusive(self):
        nd = {"claude": 180.0, "codex": 60.0}
        self.assertEqual(usage.due_providers(nd, 59.999), [])
        self.assertEqual(usage.due_providers(nd, 60.0), ["codex"])          # next_due <= now
        self.assertEqual(usage.due_providers(nd, 179.9), ["codex"])
        self.assertEqual(usage.due_providers(nd, 180.0), ["claude", "codex"])
        self.assertEqual(usage.due_providers(nd, 1e9), ["claude", "codex"])

    def test_only_claude_due(self):
        self.assertEqual(usage.due_providers({"claude": 10.0, "codex": 100.0}, 10.0), ["claude"])

    def test_missing_key_counts_as_due(self):
        self.assertEqual(usage.due_providers({"claude": 500.0}, 1.0), ["codex"])


class PollIntervalTests(unittest.TestCase):
    def test_reads_config_each_call(self):
        with mock.patch.object(usage.CFG, "usage_poll_sec", 180), mock.patch.object(usage.CFG, "usage_codex_poll_sec", 60):
            self.assertEqual(usage.poll_intervals(), {"claude": 180, "codex": 60})
        with mock.patch.object(usage.CFG, "usage_poll_sec", 300), mock.patch.object(usage.CFG, "usage_codex_poll_sec", 30):
            self.assertEqual(usage.poll_intervals(), {"claude": 300, "codex": 30})


class CodexPollConfigTests(unittest.TestCase):
    """USAGE_CODEX_POLL_SEC — test_config.py는 _find_codex만 다뤄서(설정 값 테스트 선례 없음) 여기에 둔다."""

    def build(self, **env):
        from server import config
        with mock.patch.object(config, "_load_env", return_value={}), \
                mock.patch.dict(config.os.environ, {f"MADISON_{k}": v for k, v in env.items()}):
            for k in ("USAGE_CODEX_POLL_SEC", "USAGE_POLL_SEC"):
                if k not in env:
                    config.os.environ.pop(f"MADISON_{k}", None)
            return config.Config()

    def test_defaults(self):
        cfg = self.build()
        self.assertEqual(cfg.usage_codex_poll_sec, 60)
        self.assertEqual(cfg.usage_poll_sec, 180)

    def test_override(self):
        self.assertEqual(self.build(USAGE_CODEX_POLL_SEC="90").usage_codex_poll_sec, 90)

    def test_clamped_to_at_least_30(self):
        self.assertEqual(self.build(USAGE_CODEX_POLL_SEC="5").usage_codex_poll_sec, 30)
        self.assertEqual(self.build(USAGE_CODEX_POLL_SEC="30").usage_codex_poll_sec, 30)
        self.assertEqual(self.build(USAGE_CODEX_POLL_SEC="-10").usage_codex_poll_sec, 30)

    def test_claude_interval_still_clamped_to_60(self):
        self.assertEqual(self.build(USAGE_POLL_SEC="10").usage_poll_sec, 60)

    def test_env_file_value_is_used(self):
        from server import config
        with mock.patch.object(config, "_load_env", return_value={"USAGE_CODEX_POLL_SEC": "45"}):
            self.assertEqual(config.Config().usage_codex_poll_sec, 45)


class RefreshProvidersTests(unittest.TestCase):
    def setUp(self):
        with usage._lock:
            self.saved = dict(usage._snap)
            usage._snap.update(claude={"plan": "Max", "windows": [], "extras": [], "error": "old"}, codex=None)
        self.claude_before = dict(usage._snap["claude"])
        for target in ("_persist", "record_history", "record_plan"):
            p = mock.patch.object(usage, target)
            setattr(self, target, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(usage.db, "tx")
        p.start()
        self.addCleanup(p.stop)
        self._saved_codex_backoff = dict(usage._codex_backoff)
        usage._codex_backoff.update(until=0.0, reason="")

    def tearDown(self):
        with usage._lock:
            usage._snap.update(self.saved)
        usage._codex_backoff.clear()
        usage._codex_backoff.update(self._saved_codex_backoff)

    def codex_data(self):
        return {"plan": "Prolite", "windows": [{"title": "7d", "pct": 1}], "extras": [], "updated": "2026-11-02T00:00:00Z"}

    def test_codex_only_does_not_touch_claude(self):
        with mock.patch.object(usage, "collect_claude") as cl, \
                mock.patch.object(usage, "collect_codex", return_value=self.codex_data()) as cx:
            usage.refresh(("codex",))
        cl.assert_not_called()
        cx.assert_called_once_with()
        self.assertEqual(usage._snap["claude"], self.claude_before)
        self.assertEqual(usage._snap["codex"]["plan"], "Prolite")
        self.assertEqual(usage._snap["codex"]["error"], "")
        self.assertEqual([c.args[1] for c in self.record_history.call_args_list], ["codex"])
        self.assertEqual([c.args[1] for c in self.record_plan.call_args_list], ["codex"])
        self._persist.assert_called_once_with()

    def test_claude_only_does_not_call_codex(self):
        data = {"plan": "Max", "windows": [{"title": "5h", "pct": 2}], "extras": [], "updated": "x"}
        with mock.patch.object(usage, "collect_claude", return_value=data) as cl, \
                mock.patch.object(usage, "collect_codex") as cx:
            usage.refresh(("claude",))
        cx.assert_not_called()
        cl.assert_called_once_with()
        self.assertIsNone(usage._snap["codex"])

    def test_default_collects_both(self):
        with mock.patch.object(usage, "collect_claude", return_value={"plan": "", "windows": [], "extras": []}) as cl, \
                mock.patch.object(usage, "collect_codex", return_value=self.codex_data()) as cx:
            usage.refresh()
        self.assertEqual((cl.call_count, cx.call_count), (1, 1))

    def test_empty_providers_collects_nothing_and_does_not_persist(self):
        with mock.patch.object(usage, "collect_claude") as cl, mock.patch.object(usage, "collect_codex") as cx:
            usage.refresh(())
        self.assertEqual((cl.call_count, cx.call_count), (0, 0))
        self._persist.assert_not_called()

    def test_codex_failure_keeps_last_value_and_sets_error_without_touching_claude(self):
        with usage._lock:
            usage._snap["codex"] = {"plan": "Prolite", "windows": [], "extras": [], "error": ""}
        with mock.patch.object(usage, "collect_claude") as cl, \
                mock.patch.object(usage, "collect_codex", side_effect=usage.UsageError("timeout")):
            usage.refresh(("codex",))
        cl.assert_not_called()
        self.assertEqual(usage._snap["codex"]["error"], "timeout")
        self.assertEqual(usage._snap["codex"]["plan"], "Prolite")
        self.assertEqual(usage._snap["claude"], self.claude_before)
        self._persist.assert_not_called()

    def test_unknown_provider_is_a_key_error(self):
        with self.assertRaises(KeyError):
            usage.refresh(("gemini",))


class LoopScheduleTests(unittest.TestCase):
    class Stop(Exception):
        pass

    def setUp(self):
        self.t = 0.0
        self.calls = []       # (clock 시각, 프로바이더들)
        self.sleeps = []
        for target, kw in (("load_persisted", {}),):
            p = mock.patch.object(usage, target, **kw)
            p.start()
            self.addCleanup(p.stop)
        for attr, val in (("usage_poll_sec", 180), ("usage_codex_poll_sec", 60)):
            p = mock.patch.object(usage.CFG, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def run_loop(self, iterations, refresh_cost=0.0, refresh_side_effect=None):
        def fake_refresh(providers=usage.PROVIDERS):
            self.calls.append((self.t, tuple(providers)))
            self.t += refresh_cost
            if refresh_side_effect:
                raise refresh_side_effect
            return {}

        def fake_sleep(sec):
            self.sleeps.append(sec)
            if len(self.sleeps) >= iterations:
                raise self.Stop()
            self.t += sec

        with mock.patch.object(usage, "refresh", side_effect=fake_refresh):
            with self.assertRaises(self.Stop):
                usage.loop(sleep=fake_sleep, clock=lambda: self.t)

    def test_codex_every_60_claude_every_180(self):
        self.run_loop(7)
        self.assertEqual(self.calls, [
            (0.0, ("claude", "codex")), (60.0, ("codex",)), (120.0, ("codex",)),
            (180.0, ("claude", "codex")), (240.0, ("codex",)), (300.0, ("codex",)), (360.0, ("claude", "codex"))])
        self.assertEqual(self.sleeps, [60.0] * 7)

    def test_first_iteration_collects_both_and_loads_persisted_first(self):
        self.run_loop(1)
        self.assertEqual(self.calls, [(0.0, ("claude", "codex"))])
        usage.load_persisted.assert_called_once_with()

    def test_slow_refresh_does_not_drift_the_schedule(self):
        self.run_loop(7, refresh_cost=20.0)
        self.assertEqual([c[0] for c in self.calls], [0.0, 60.0, 120.0, 180.0, 240.0, 300.0, 360.0])
        self.assertEqual([c[1] for c in self.calls][3], ("claude", "codex"))
        self.assertEqual(self.sleeps, [40.0] * 7)          # 60 - 20: 시도 시작 기준

    def test_refresh_slower_than_interval_runs_again_immediately(self):
        self.run_loop(3, refresh_cost=100.0)
        self.assertEqual(self.sleeps[0], 0.0)              # 음수 sleep 없음
        self.assertTrue(all(s >= 0 for s in self.sleeps))
        self.assertEqual([c[0] for c in self.calls][:2], [0.0, 100.0])
        self.assertEqual(self.calls[1][1], ("codex",))     # 100 < 180: claude는 아직

    def test_refresh_error_does_not_stop_loop_or_change_schedule(self):
        with self.assertLogs(usage.log, "ERROR") as logs:       # log.exception 출력을 테스트 로그에서 숨긴다
            self.run_loop(3, refresh_side_effect=RuntimeError("boom"))
        self.assertEqual(len(logs.records), 3)
        self.assertEqual([c[0] for c in self.calls], [0.0, 60.0, 120.0])

    def test_interval_change_in_config_applies_to_next_attempt(self):
        # 설정을 매 회차 읽는다 — 첫 회차(0초) 뒤 코덱스 주기를 30초로 바꾸면 그 회차부터 적용
        orig_sleep_calls = []

        def fake_refresh(providers=usage.PROVIDERS):
            self.calls.append((self.t, tuple(providers)))
            return {}

        def fake_sleep(sec):
            orig_sleep_calls.append(sec)
            if len(orig_sleep_calls) >= 4:
                raise self.Stop()
            if len(orig_sleep_calls) == 1:
                usage.CFG.usage_codex_poll_sec = 30
            self.t += sec

        with mock.patch.object(usage, "refresh", side_effect=fake_refresh), self.assertRaises(self.Stop):
            usage.loop(sleep=fake_sleep, clock=lambda: self.t)
        self.assertEqual([c[0] for c in self.calls], [0.0, 60.0, 90.0, 120.0])
        self.assertEqual(orig_sleep_calls[0], 60.0)
        self.assertEqual(orig_sleep_calls[1:], [30.0, 30.0, 30.0])

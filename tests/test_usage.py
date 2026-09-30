import sqlite3
import time
import unittest

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

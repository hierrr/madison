import unittest

from server import usage


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


if __name__ == "__main__":
    unittest.main()


class EpochTests(unittest.TestCase):
    def test_naive_iso_is_utc(self):
        self.assertEqual(usage.to_epoch("2026-08-28T00:00:00"), usage.to_epoch("2026-08-28T00:00:00Z"))
        self.assertEqual(usage.to_epoch("2026-08-28T09:00:00+09:00"), usage.to_epoch("2026-08-28T00:00:00Z"))
        self.assertIsNone(usage.to_epoch("nope")); self.assertIsNone(usage.to_epoch(True))

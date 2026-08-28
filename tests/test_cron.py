import datetime
import unittest

from server import cron, reporting
from server.config import CFG


def _dt(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")


class CronTests(unittest.TestCase):
    def test_hourly_and_steps(self):
        self.assertTrue(cron.matches("0 * * * *", _dt("2026-08-28 13:00")))
        self.assertFalse(cron.matches("0 * * * *", _dt("2026-08-28 13:01")))
        self.assertTrue(cron.matches("15 */4 * * *", _dt("2026-08-28 08:15")))
        self.assertFalse(cron.matches("15 */4 * * *", _dt("2026-08-28 09:15")))
        self.assertTrue(cron.matches("45 */8 * * *", _dt("2026-08-28 16:45")))
        self.assertTrue(cron.matches("30 6,12,18,22 * * *", _dt("2026-08-28 22:30")))
        self.assertTrue(cron.matches("0 9-18 * * 1-5", _dt("2026-08-28 10:00")))   # 금요일
        self.assertFalse(cron.matches("0 9-18 * * 1-5", _dt("2026-08-30 10:00")))  # 일요일
        self.assertTrue(cron.matches("0 22 * * 0", _dt("2026-08-30 22:00")))
        self.assertTrue(cron.matches("0 22 * * 7", _dt("2026-08-30 22:00")))

    def test_invalid(self):
        for bad in ("0 * * *", "60 * * * *", "0 25 * * *", "a b c d e", "0 */0 * * *"):
            self.assertFalse(cron.valid(bad), bad)
        self.assertTrue(cron.valid("*/10 * * * *"))

    def test_due_rules(self):
        saved = CFG.report_daily_cron
        CFG.report_daily_cron = "0 * * * *"
        try:
            slots = {}
            self.assertTrue(reporting.due("day", "no-report", _dt("2026-08-28 13:07"), slots))     # 없으면 즉시
            self.assertFalse(reporting.due("day", "new-events", _dt("2026-08-28 13:07"), slots))   # 크론 아님
            self.assertTrue(reporting.due("day", "new-events", _dt("2026-08-28 14:00"), slots))
            self.assertFalse(reporting.due("day", "new-events", _dt("2026-08-28 14:00"), slots))   # 같은 분 중복 금지
            self.assertFalse(reporting.due("day", None, _dt("2026-08-28 15:00"), slots))           # 바뀐 것 없음
        finally:
            CFG.report_daily_cron = saved


if __name__ == "__main__":
    unittest.main()

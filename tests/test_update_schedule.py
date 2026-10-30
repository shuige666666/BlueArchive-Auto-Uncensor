"""验证北京时间周四检查与每日兜底，不受宿主机时区影响。"""

from datetime import datetime, timedelta, timezone
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from serve_resources import next_weekly_check, update_wait_seconds


class UpdateScheduleTests(unittest.TestCase):
    """固定时刻与耗时任务交错时不遗漏检查或产生重复循环。"""

    def test_thursday_16_and_18_beijing(self):
        """UTC 07:59 对应北京时间周四 15:59，下一轮在一分钟后。"""
        now = datetime(2026, 10, 29, 7, 59, tzinfo=timezone.utc)
        self.assertEqual(next_weekly_check(now), now + timedelta(minutes=1))
        at_16 = now + timedelta(minutes=1)
        self.assertEqual(next_weekly_check(at_16), at_16 + timedelta(hours=2))
        at_18 = at_16 + timedelta(hours=2)
        self.assertEqual(next_weekly_check(at_18), at_16 + timedelta(days=7))

    def test_daily_fallback_and_crossed_scheduled_time(self):
        """平日保留每日检查；构建跨过定点时刻，结束后立即补查一次。"""
        monday = datetime(2026, 10, 26, 1, tzinfo=timezone.utc)
        self.assertEqual(update_wait_seconds(86400, monday, monday + timedelta(minutes=20)), 86400)
        thursday = datetime(2026, 10, 29, 7, 59, tzinfo=timezone.utc)
        finished = thursday + timedelta(minutes=20)
        self.assertEqual(update_wait_seconds(86400, thursday, finished), 0)
        self.assertGreater(update_wait_seconds(86400, finished, finished + timedelta(seconds=5)), 0)
        self.assertEqual(update_wait_seconds(60, monday, monday), 60)


if __name__ == "__main__":
    unittest.main()

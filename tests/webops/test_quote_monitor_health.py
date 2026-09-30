import datetime as dt
import unittest
from unittest import mock

from pylon.tasks.quotes import monitor


class QuoteMonitorHealthTests(unittest.TestCase):
    def test_idle_retry_persists_silent_health_recovery(self):
        now = dt.datetime(2026, 9, 30, 18, 0, tzinfo=monitor.BJ)
        with (
            mock.patch.object(monitor, "bj_now", return_value=now),
            mock.patch.object(monitor, "should_retry", return_value=""),
            mock.patch("pylon.tasks.quotes.health.inspect", return_value={"available": True, "validated_at": 1, "reason": "", "error": ""}),
            mock.patch("pylon.tasks.quotes.health.check") as check,
            mock.patch.object(monitor.events, "result"),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(monitor.main(["--mode", "retry"]), 0)
        check.assert_called_once_with(monitor.DB_PATH, notify=False)


if __name__ == "__main__":
    unittest.main()

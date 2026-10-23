import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slackstatus import compute_expiration, next_occurrence, parse_duration  # noqa: E402


class ParseDurationTests(unittest.TestCase):
    def test_valid(self):
        cases = {
            "30m": 30 * 60,
            "1h": 3600,
            "2h30m": 2 * 3600 + 30 * 60,
            "1d": 86400,
            "1d2h": 86400 + 2 * 3600,
            "1d1h1m": 86400 + 3600 + 60,
            "90m": 90 * 60,
            " 1H ": 3600,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), expected)

    def test_invalid(self):
        for text in ["", "  ", "abc", "1x", "h", "30", "1m2h", "1.5h", "-1h", "0m", "0h0m"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_duration(text)


class NextOccurrenceTests(unittest.TestCase):
    def test_later_today(self):
        now = datetime(2026, 3, 10, 10, 0)
        self.assertEqual(next_occurrence("17:00", now), datetime(2026, 3, 10, 17, 0))

    def test_already_passed_rolls_to_tomorrow(self):
        now = datetime(2026, 3, 10, 18, 0)
        self.assertEqual(next_occurrence("17:00", now), datetime(2026, 3, 11, 17, 0))

    def test_exactly_now_rolls_to_tomorrow(self):
        now = datetime(2026, 3, 10, 17, 0, 0)
        self.assertEqual(next_occurrence("17:00", now), datetime(2026, 3, 11, 17, 0))

    def test_seconds_before_target_stays_today(self):
        now = datetime(2026, 3, 10, 16, 59, 30)
        self.assertEqual(next_occurrence("17:00", now), datetime(2026, 3, 10, 17, 0))

    def test_crosses_month_and_year(self):
        now = datetime(2026, 12, 31, 23, 0)
        self.assertEqual(next_occurrence("01:00", now), datetime(2027, 1, 1, 1, 0))

    def test_single_digit_hour_and_midnight(self):
        now = datetime(2026, 3, 10, 8, 0)
        self.assertEqual(next_occurrence("9:30", now), datetime(2026, 3, 10, 9, 30))
        self.assertEqual(next_occurrence("00:00", now), datetime(2026, 3, 11, 0, 0))

    def test_invalid(self):
        now = datetime(2026, 3, 10, 10, 0)
        for text in ["", "17", "5pm", "24:00", "17:60", "17:5", "17-00", "117:00"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    next_occurrence(text, now)


class ComputeExpirationTests(unittest.TestCase):
    now = datetime(2026, 3, 10, 10, 0)

    def test_no_expiry(self):
        self.assertEqual(compute_expiration(now=self.now), 0)

    def test_for(self):
        self.assertEqual(
            compute_expiration(duration="1h", now=self.now),
            int(self.now.timestamp()) + 3600,
        )

    def test_until(self):
        self.assertEqual(
            compute_expiration(until="17:00", now=self.now),
            int(datetime(2026, 3, 10, 17, 0).timestamp()),
        )


if __name__ == "__main__":
    unittest.main()

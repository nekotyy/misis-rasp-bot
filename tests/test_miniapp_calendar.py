import unittest
from datetime import UTC, date, datetime

from src.miniapp_calendar import (
    default_history_week,
    history_bounds,
    history_week_days,
    schedule_dates,
    subtract_calendar_months,
    today_in_timezone,
    week_start,
)


class MiniappCalendarTests(unittest.TestCase):
    def test_three_calendar_months_clamp_at_month_end_and_leap_year(self):
        self.assertEqual(subtract_calendar_months(date(2026, 5, 31), 3), date(2026, 2, 28))
        self.assertEqual(subtract_calendar_months(date(2024, 5, 31), 3), date(2024, 2, 29))
        self.assertEqual(history_bounds(date(2026, 1, 4)), (date(2025, 10, 4), date(2026, 1, 3)))
        with self.assertRaises(ValueError):
            subtract_calendar_months(date(2026, 1, 1), -1)

    def test_schedule_includes_weekend_and_new_year(self):
        self.assertEqual(schedule_dates(date(2026, 12, 31)), (date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)))
        self.assertEqual(schedule_dates(date(2026, 11, 6)), (date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8)))

    def test_history_uses_chronological_six_days_and_current_monday(self):
        self.assertEqual(
            history_week_days(date(2026, 11, 4), date(2026, 11, 2)),
            tuple(date(2026, 11, d) for d in range(2, 8)),
        )
        self.assertEqual(default_history_week(date(2026, 11, 2)), date(2026, 11, 2))
        self.assertEqual(default_history_week(date(2026, 11, 4)), date(2026, 11, 2))

    def test_boundary_weeks_are_clipped_and_invalid_start_is_rejected(self):
        today = date(2026, 11, 4)
        self.assertEqual(
            history_week_days(today, week_start(date(2026, 8, 4))),
            tuple(date(2026, 8, d) for d in (3, 4, 5, 6, 7, 8)),
        )
        self.assertEqual(history_week_days(today, date(2026, 5, 18)), ())
        with self.assertRaises(ValueError):
            history_week_days(today, date(2026, 11, 3))

    def test_timezone_controls_day_and_naive_datetime_is_rejected(self):
        moment = datetime(2026, 11, 3, 22, 30, tzinfo=UTC)
        self.assertEqual(today_in_timezone("Europe/Moscow", now=moment), date(2026, 11, 4))
        self.assertEqual(today_in_timezone("America/New_York", now=moment), date(2026, 11, 3))
        with self.assertRaises(ValueError):
            today_in_timezone("Europe/Moscow", now=datetime(2026, 11, 4))

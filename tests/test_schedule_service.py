from __future__ import annotations

import unittest
from datetime import datetime

from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.schedule_service import ScheduleComparator


class ScheduleComparatorTests(unittest.TestCase):
    def test_compare_returns_summary_when_day_changes(self) -> None:
        target = datetime.now().date()
        previous = {
            "content": {
                "days": [
                    {
                        "date_iso": target.isoformat(),
                        "date_label": "Сегодня",
                        "lessons": [
                            {
                                "number": 1,
                                "subject": "Математика",
                                "teacher": "Иванов",
                                "classroom": "101",
                            }
                        ],
                    }
                ]
            }
        }
        current = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime.now(),
            days=[
                DaySchedule(
                    date_iso=target.isoformat(),
                    date_label="Сегодня",
                    lessons=[
                        Lesson(number=1, subject="Физика", teacher="Петров", classroom="202"),
                    ],
                )
            ],
        )

        summary = ScheduleComparator.compare(previous, current)

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.changed_dates, ["Сегодня"])
        self.assertIn("Обнаружены изменения", summary.message)
        self.assertIn("Физика", summary.message)
        self.assertIn("<b>Обнаружены изменения", summary.telegram_message)

    def test_compare_ignores_unchanged_schedule(self) -> None:
        target = datetime.now().date()
        previous = {
            "content": {
                "days": [
                    {
                        "date_iso": target.isoformat(),
                        "date_label": "Сегодня",
                        "lessons": [
                            {
                                "number": 1,
                                "subject": "Математика",
                                "teacher": "Иванов",
                                "classroom": "101",
                            }
                        ],
                    }
                ]
            }
        }
        current = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime.now(),
            days=[
                DaySchedule(
                    date_iso=target.isoformat(),
                    date_label="Сегодня",
                    lessons=[
                        Lesson(number=1, subject="Математика", teacher="Иванов", classroom="101"),
                    ],
                )
            ],
        )

        summary = ScheduleComparator.compare(previous, current)

        self.assertIsNone(summary)


if __name__ == "__main__":
    unittest.main()

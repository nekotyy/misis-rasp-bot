from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.schedule_service import ScheduleComparator, get_day_by_offset, get_day_by_offset_from_content


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


class GetDayByOffsetTests(unittest.TestCase):
    """Регресс на баг: кнопка «сегодня» показывала завтрашний день, если в снимке
    (например, из OCR-фото) вообще не было записи на сегодняшнюю дату — код брал
    просто первый день из будущих по счёту, а не день с точным сегодняшним date_iso."""

    def test_from_content_returns_none_when_today_is_missing_even_if_tomorrow_exists(self) -> None:
        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        content = {
            "days": [
                {
                    "date_iso": tomorrow,
                    "date_label": "Завтра",
                    "lessons": [
                        {"number": 2, "subject": "Физическая культура", "teacher": "Черкасских М.С.", "classroom": "ФОК"},
                    ],
                }
            ]
        }

        today_result = get_day_by_offset_from_content(content, 0)
        self.assertIsNone(today_result, "Если на сегодня нет данных, нельзя молча подставлять завтрашний день")

        tomorrow_result = get_day_by_offset_from_content(content, 1)
        self.assertIsNotNone(tomorrow_result)
        self.assertEqual(tomorrow_result.date_iso, tomorrow)

    def test_from_content_matches_exact_calendar_date_regardless_of_order(self) -> None:
        today = datetime.now().date().isoformat()
        day_after = (datetime.now().date() + timedelta(days=2)).isoformat()
        content = {
            "days": [
                {"date_iso": day_after, "date_label": "Послезавтра", "lessons": []},
                {"date_iso": today, "date_label": "Сегодня", "lessons": [
                    {"number": 1, "subject": "Математика", "teacher": "Иванов", "classroom": "101"},
                ]},
            ]
        }

        result = get_day_by_offset_from_content(content, 0)

        self.assertIsNotNone(result)
        self.assertEqual(result.date_iso, today)
        self.assertEqual(result.lessons[0].subject, "Математика")

    def test_snapshot_variant_matches_same_semantics(self) -> None:
        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime.now(),
            days=[DaySchedule(date_iso=tomorrow, date_label="Завтра", lessons=[])],
        )

        self.assertIsNone(get_day_by_offset(snapshot, 0))
        result = get_day_by_offset(snapshot, 1)
        self.assertIsNotNone(result)
        self.assertEqual(result.date_iso, tomorrow)


if __name__ == "__main__":
    unittest.main()

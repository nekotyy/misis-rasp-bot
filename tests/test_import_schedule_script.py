"""Тесты ручной заливки расписания из JSON.

Резервный путь на случай, когда и сайт недоступен, и распознавание не работает:
расписание вбивается руками, но идёт по тому же конвейеру, что данные с сайта.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.import_schedule import build_snapshot, parse_date
from src.schedule_service import ScheduleFormatter

REAL_SCHEDULE = Path(__file__).resolve().parents[1] / "storage" / "schedule_isp-25-1.json"


class ParseDateTests(unittest.TestCase):
    def test_russian_format(self) -> None:
        self.assertEqual(parse_date("01.09.2026"), ("2026-09-01", "01.09.2026"))

    def test_iso_format(self) -> None:
        self.assertEqual(parse_date("2026-09-01"), ("2026-09-01", "01.09.2026"))

    def test_short_year(self) -> None:
        self.assertEqual(parse_date("01.09.26"), ("2026-09-01", "01.09.2026"))

    def test_surrounding_spaces(self) -> None:
        self.assertEqual(parse_date("  02.09.2026 "), ("2026-09-02", "02.09.2026"))

    def test_garbage_is_rejected(self) -> None:
        for bad in ("вчера", "32.13.2026", "", "01/09/2026"):
            with self.assertRaises(ValueError, msg=bad):
                parse_date(bad)


class BuildSnapshotTests(unittest.TestCase):
    def test_minimal_payload(self) -> None:
        snapshot = build_snapshot({
            "group": "ИСП-25-1",
            "days": [{"date": "01.09.2026", "lessons": [
                {"number": 1, "subject": "Физика", "teacher": "Иванов И.И.", "classroom": "301"},
            ]}],
        })

        self.assertEqual(snapshot.group_name, "ИСП-25-1")
        self.assertEqual(len(snapshot.days), 1)
        lesson = snapshot.days[0].lessons[0]
        self.assertEqual((lesson.number, lesson.subject, lesson.classroom), (1, "Физика", "301"))

    def test_days_and_lessons_are_sorted(self) -> None:
        snapshot = build_snapshot({
            "group": "ИСП-25-1",
            "days": [
                {"date": "03.09.2026", "lessons": []},
                {"date": "01.09.2026", "lessons": [
                    {"number": 2, "subject": "Физика"},
                    {"number": 1, "subject": "Математика"},
                ]},
            ],
        })

        self.assertEqual([day.date_iso for day in snapshot.days], ["2026-09-01", "2026-09-03"])
        self.assertEqual([lesson.number for lesson in snapshot.days[0].lessons], [1, 2])

    def test_empty_day_is_allowed(self) -> None:
        snapshot = build_snapshot({"group": "ИСП-25-1", "days": [{"date": "03.09.2026", "lessons": []}]})
        self.assertEqual(snapshot.days[0].lessons, [])

    def test_optional_teacher_and_classroom(self) -> None:
        snapshot = build_snapshot({
            "group": "ИСП-25-1",
            "days": [{"date": "01.09.2026", "lessons": [{"number": 1, "subject": "Консульт."}]}],
        })
        lesson = snapshot.days[0].lessons[0]
        self.assertEqual((lesson.teacher, lesson.classroom), ("", ""))

    def test_missing_group_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_snapshot({"days": [{"date": "01.09.2026", "lessons": []}]})

    def test_missing_days_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_snapshot({"group": "ИСП-25-1"})

    def test_empty_subject_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_snapshot({
                "group": "ИСП-25-1",
                "days": [{"date": "01.09.2026", "lessons": [{"number": 1, "subject": "  "}]}],
            })

    def test_duplicate_lesson_numbers_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_snapshot({
                "group": "ИСП-25-1",
                "days": [{"date": "01.09.2026", "lessons": [
                    {"number": 1, "subject": "Физика"},
                    {"number": 1, "subject": "Математика"},
                ]}],
            })


class RealScheduleFileTests(unittest.TestCase):
    """Готовый файл с распознанным расписанием должен заливаться как есть."""

    def setUp(self) -> None:
        self.snapshot = build_snapshot(json.loads(REAL_SCHEDULE.read_text(encoding="utf-8")))

    def test_file_covers_four_days(self) -> None:
        self.assertEqual(
            [day.date_iso for day in self.snapshot.days],
            ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"],
        )

    def test_lessons_match_photo(self) -> None:
        by_date = {day.date_iso: day for day in self.snapshot.days}

        self.assertEqual(
            ScheduleFormatter.format_day_plain(by_date["2026-09-01"]),
            "Расписание на 1 сентября 2026 года\n"
            "\n"
            "1. в 301 по Операционные системы и среды у Кубанева Е.А.\n"
            "2. в с-з по Физическая культура у Кузьминова И.Н.",
        )
        self.assertEqual(
            ScheduleFormatter.format_day_plain(by_date["2026-09-02"]),
            "Расписание на 2 сентября 2026 года\n"
            "\n"
            "1. в 511/2М по Основы алгоритмизации и прогр. у Коренькова Т.Н.\n"
            "2. в 301 по Операционные системы и среды у Кубанева Е.А.",
        )

    def test_free_days_stay_empty(self) -> None:
        by_date = {day.date_iso: day for day in self.snapshot.days}
        self.assertEqual(by_date["2026-09-03"].lessons, [])
        self.assertEqual(by_date["2026-09-04"].lessons, [])


if __name__ == "__main__":
    unittest.main()

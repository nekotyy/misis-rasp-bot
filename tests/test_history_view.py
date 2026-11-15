from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from src.db import Database
from src.history_view import history_week, parse_history_date
from src.models import DaySchedule, Lesson
from src.schedule_archive import ScheduleDayArchive


class HistoryViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.db"
        await Database(self.path).initialize()
        self.archive = ScheduleDayArchive(self.path)

    async def test_week_displays_six_days_and_only_requested_group(self) -> None:
        day = DaySchedule("Пн", "2026-11-02", [Lesson(1, "Физика", "Иванов", "101")])
        await self.archive.save_day("group:1", "group", day, observed_at=datetime(2026, 11, 2, tzinfo=UTC), origin="site", completeness="unknown")
        await self.archive.save_day("group:2", "group", DaySchedule("Вт", "2026-11-03", [Lesson(1, "Чужая пара", "Петров", "102")]),
                                    observed_at=datetime(2026, 11, 3, tzinfo=UTC), origin="site", completeness="unknown")
        text, previous, following = await history_week(self.archive, "group:1", "Группа", date(2026, 11, 2), date(2026, 11, 15))
        self.assertIn("Физика", text)
        self.assertNotIn("Чужая пара", text)
        self.assertIn("Расписание за этот день не сохранено", text)
        self.assertIn("Понедельник, 02.11.2026", text)
        self.assertIn("Суббота, 07.11.2026", text)
        self.assertNotIn("Воскресенье", text)
        self.assertTrue(previous)
        self.assertTrue(following)

    async def test_empty_unknown_is_not_reported_as_no_classes(self) -> None:
        day = DaySchedule("Пн", "2026-11-02", [])
        await self.archive.save_day("group:1", "group", day, observed_at=datetime(2026, 11, 2, tzinfo=UTC), origin="site", completeness="unknown")
        text, _, _ = await history_week(self.archive, "group:1", "Группа", date(2026, 11, 2), date(2026, 11, 15))
        self.assertIn("Пары не сохранены", text)
        self.assertNotIn("Пар нет.", text)

    async def test_outside_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await history_week(self.archive, "group:1", "Группа", date(2025, 1, 6), date(2026, 11, 15))
        with self.assertRaises(ValueError):
            parse_history_date("01.01.2025", date(2026, 11, 15))
        self.assertEqual(parse_history_date("08.11.2026", date(2026, 11, 15)), date(2026, 11, 2))

    async def test_university_variants_are_distinguished(self) -> None:
        for variant, subject in ((1, "Числитель"), (2, "Знаменатель")):
            day = DaySchedule("Пн", "2026-11-02", [Lesson(1, subject, "Иванов", "101")], week_variant=variant)
            await self.archive.save_day("group:-1", "group", day, observed_at=datetime(2026, 11, 2, tzinfo=UTC),
                                        origin="site", completeness="unknown")
        text, _, _ = await history_week(self.archive, "group:-1", "Университет", date(2026, 11, 2), date(2026, 11, 15))
        self.assertIn("Числитель:", text)
        self.assertIn("Знаменатель:", text)

    async def test_confirmed_empty_is_distinct_from_missing(self) -> None:
        day = DaySchedule("Пн", "2026-11-02", [])
        await self.archive.save_day("group:1", "group", day, observed_at=datetime(2026, 11, 2, tzinfo=UTC),
                                    origin="site", completeness="complete")
        text, _, _ = await history_week(self.archive, "group:1", "Группа", date(2026, 11, 2), date(2026, 11, 15))
        self.assertIn("Пар нет.", text)
        self.assertIn("Расписание за этот день не сохранено.", text)


if __name__ == "__main__":
    unittest.main()

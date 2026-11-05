import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from src.db import Database
from src.models import DaySchedule, Lesson
from src.schedule_archive import ScheduleDayArchive


class ScheduleDayArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        await Database(self.path).initialize()
        self.archive = ScheduleDayArchive(self.path)
        self.day = DaySchedule("5 ноября", "2026-11-05", [Lesson(1, "Физика", "Иванов", "101")])
        self.now = datetime(2026, 11, 5, 9, tzinfo=UTC)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def save(self, day, observed_at):
        return await self.archive.save_day("group:1", "group", day, observed_at=observed_at, origin="manual", completeness="complete")

    async def test_newer_wins_and_old_or_equal_time_cannot_overwrite(self):
        self.assertTrue(await self.save(self.day, self.now))
        changed = DaySchedule("5 ноября", "2026-11-05", [Lesson(1, "Химия", "Петров", "102")])
        self.assertFalse(await self.save(changed, self.now.astimezone(timezone(timedelta(hours=3)))))
        self.assertFalse(await self.save(changed, self.now - timedelta(seconds=1)))
        self.assertTrue(await self.save(changed, self.now + timedelta(seconds=1)))
        result = await self.archive.get_day("group:1", date(2026, 11, 5))
        self.assertEqual(result["content"]["lessons"][0]["subject"], "Химия")
        self.assertIsNone(await self.archive.get_day("group:2", date(2026, 11, 5)))

    async def test_dates_and_week_variants_are_independent(self):
        await self.save(self.day, self.now)
        other = DaySchedule("5 ноября", "2026-11-05", [], week_variant=2)
        await self.save(other, self.now)
        await self.save(DaySchedule("6 ноября", "2026-11-06", []), self.now)
        self.assertEqual((await self.archive.get_day("group:1", date(2026, 11, 5), 2))["content"]["lessons"], [])
        self.assertEqual(len((await self.archive.get_day("group:1", date(2026, 11, 5)))["content"]["lessons"]), 1)
        self.assertIsNotNone(await self.archive.get_day("group:1", date(2026, 11, 6)))

    async def test_rejects_unzoned_time_and_invalid_day_before_writing(self):
        with self.assertRaises(ValueError):
            await self.save(self.day, datetime(2026, 11, 5))
        with self.assertRaises(ValueError):
            await self.save(DaySchedule("x", "2026-02-30", []), self.now)
        self.assertIsNone(await self.archive.get_day("group:1", date(2026, 11, 5)))

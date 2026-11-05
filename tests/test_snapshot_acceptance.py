import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from src.db import Database
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.schedule_archive import ScheduleDayArchive
from src.snapshot_acceptance import save_admin_snapshot


class AdminSnapshotAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.db"
        self.db = Database(self.path)
        await self.db.initialize()
        self.source = {
            "source_type": "group", "source_key": "group:1", "source_title": "Группа",
            "source_url": "rasp:1", "schedule_id": 1, "group_name": "Группа",
        }

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_admin_current_is_archived_and_glitch_is_not_saved(self):
        today = date.today().isoformat()
        normal = ScheduleSnapshot("Группа", datetime.now(), [DaySchedule("Сегодня", today, [Lesson(1, "Физика", "Иванов", "101")])])
        self.assertTrue(await save_admin_snapshot(self.db, self.source, normal, "first", snapshot_type="current"))
        empty = ScheduleSnapshot("Группа", datetime.now(), [DaySchedule("Сегодня", today, [])])
        self.assertFalse(await save_admin_snapshot(self.db, self.source, empty, "empty", snapshot_type="current"))
        self.assertEqual((await self.db.get_latest_snapshot("current", source_key="group:1"))["snapshot_hash"], "first")
        archived = await ScheduleDayArchive(self.path).get_day("group:1", date.today())
        self.assertEqual(archived["content"]["lessons"][0]["subject"], "Физика")

    async def test_baseline_does_not_overwrite_archive_or_visual_reference(self):
        today = date.today().isoformat()
        current = ScheduleSnapshot("Группа", datetime.now(), [DaySchedule("Сегодня", today, [Lesson(1, "Физика", "Иванов", "101")])])
        await save_admin_snapshot(self.db, self.source, current, "first", snapshot_type="current")
        baseline = ScheduleSnapshot("Группа", datetime.now(), [DaySchedule("Сегодня", today, [Lesson(1, "Химия", "Петров", "102")])])
        await save_admin_snapshot(self.db, self.source, baseline, "baseline", snapshot_type="daily_baseline")
        archived = await ScheduleDayArchive(self.path).get_day("group:1", date.today())
        self.assertEqual(archived["content"]["lessons"][0]["subject"], "Физика")

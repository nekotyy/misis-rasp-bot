import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import aiosqlite

from src.archive_backfill import ArchiveBackfill
from src.db import Database
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.schedule_archive import ScheduleDayArchive


class ArchiveBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "legacy.db"
        self.db = Database(self.path)
        await self.db.initialize()
        self.archive = ScheduleDayArchive(self.path)
        self.backfill = ArchiveBackfill(self.path, "Europe/Moscow")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def snapshot(self, when, days, kind="current"):
        snapshot = ScheduleSnapshot("Группа", datetime.fromisoformat(when), days)
        await self.db.save_snapshot(kind, when, snapshot, 1, "Группа", source_type="group", source_key="group:1")

    async def test_resumes_per_batch_without_erasing_missing_dates(self):
        first = DaySchedule("1", "2026-09-01", [Lesson(1, "Физика", "Иванов", "101")])
        second = DaySchedule("2", "2026-09-02", [Lesson(1, "Химия", "Петров", "102")])
        changed = DaySchedule("1", "2026-09-01", [Lesson(1, "История", "Иванов", "103")])
        await self.snapshot("2026-09-01T10:00:00", [first, second])
        await self.snapshot("2026-09-01T11:00:00", [changed])
        self.assertFalse((await self.backfill.run_batch(1))["done"])
        self.assertEqual((await self.archive.get_day("group:1", date(2026, 9, 1)))["content"]["lessons"][0]["subject"], "Физика")
        self.assertFalse((await self.backfill.run_batch(1))["done"])
        self.assertTrue((await self.backfill.run_batch(1))["done"])
        self.assertEqual((await self.archive.get_day("group:1", date(2026, 9, 1)))["content"]["lessons"][0]["subject"], "История")
        self.assertEqual((await self.archive.get_day("group:1", date(2026, 9, 2)))["content"]["lessons"][0]["subject"], "Химия")
        self.assertEqual((await self.backfill.run_batch())["processed"], 2)

    async def test_ambiguous_or_invalid_days_are_skipped_and_empty_is_unknown(self):
        raw = {"days": [
            {"date_iso": "2026-09-01", "lessons": []},
            {"date_iso": "2026-09-01", "lessons": []},
            {"date_iso": "2026-09-02", "lessons": []},
            {"date_iso": "2026-02-30", "lessons": []},
            {"date_iso": "2026-09-03", "week_variant": [], "lessons": []},
        ]}
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO schedule_snapshots (snapshot_type, source_type, source_key, snapshot_hash, content_json, fetched_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("current", "group", "group:1", "h", json.dumps(raw), "2026-09-01T10:00:00", "2026-09-01T10:00:00"),
            )
            await db.commit()
        result = await self.backfill.run_batch()
        self.assertGreaterEqual(result["skipped"], 3)
        self.assertIsNone(await self.archive.get_day("group:1", date(2026, 9, 1)))
        self.assertEqual((await self.archive.get_day("group:1", date(2026, 9, 2)))["completeness"], "unknown")
        self.assertIsNone(await self.archive.get_day("group:1", date(2026, 9, 3)))

    async def test_failed_batch_rolls_back_and_can_resume(self):
        first = DaySchedule("1", "2026-09-01", [Lesson(1, "Физика", "Иванов", "101")])
        second = DaySchedule("2", "2026-09-02", [Lesson(1, "Химия", "Петров", "102")])
        await self.snapshot("2026-09-01T10:00:00", [first, second])
        async with aiosqlite.connect(self.path) as db:
            await db.execute("CREATE TRIGGER fail_backfill BEFORE INSERT ON schedule_day_archive WHEN NEW.date_iso = '2026-09-02' BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
            await db.commit()
        with self.assertRaises(aiosqlite.IntegrityError):
            await self.backfill.run_batch()
        self.assertIsNone(await self.archive.get_day("group:1", date(2026, 9, 1)))
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DROP TRIGGER fail_backfill")
            await db.commit()
        result = await self.backfill.run_batch()
        self.assertEqual(result["processed"], 1)
        self.assertIsNotNone(await self.archive.get_day("group:1", date(2026, 9, 2)))

    async def test_baseline_is_fallback_and_older_snapshot_does_not_win(self):
        baseline = DaySchedule("1", "2026-09-01", [Lesson(1, "Эталон", "Иванов", "101")])
        current = DaySchedule("1", "2026-09-01", [Lesson(1, "Текущее", "Иванов", "102")])
        stale = DaySchedule("1", "2026-09-01", [Lesson(1, "Старое", "Иванов", "103")])
        await self.snapshot("2026-09-01T10:00:00", [baseline], "daily_baseline")
        await self.snapshot("2026-09-01T10:00:00", [current])
        await self.snapshot("2026-09-01T09:00:00", [stale])
        await self.backfill.run_batch()
        row = await self.archive.get_day("group:1", date(2026, 9, 1))
        self.assertEqual(row["content"]["lessons"][0]["subject"], "Текущее")
        self.assertEqual(row["origin"], "legacy_current")

    async def test_university_template_is_not_used_as_proof_of_past_day(self):
        days = [DaySchedule("1", "2026-09-01", [], week_variant=1), DaySchedule("2", "2026-09-02", [], week_variant=1)]
        snapshot = ScheduleSnapshot("Группа", datetime(2026, 9, 2, 10), days)
        await self.db.save_snapshot("current", "h", snapshot, -1, "Группа", source_type="group", source_key="group:-1")
        await self.backfill.run_batch()
        self.assertIsNone(await self.archive.get_day("group:-1", date(2026, 9, 1), 1))
        self.assertIsNotNone(await self.archive.get_day("group:-1", date(2026, 9, 2), 1))

    async def test_run_processes_all_batches_and_stays_idempotent(self):
        await self.snapshot("2026-09-01T10:00:00", [DaySchedule("1", "2026-09-01", [])])
        await self.snapshot("2026-09-01T11:00:00", [DaySchedule("2", "2026-09-02", [])])
        self.assertEqual((await self.backfill.run(1))["processed"], 2)
        self.assertEqual((await self.backfill.run(1))["processed"], 2)

import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from src.db import Database
from src.models import DaySchedule, Lesson, ScheduleSnapshot
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

    async def test_history_interval_filters_source_dates_and_orders_days(self):
        for day in (4, 5, 6, 7):
            await self.save(DaySchedule(str(day), f"2026-11-{day:02}", []), self.now)
        await self.archive.save_day(
            "group:2", "group", self.day, observed_at=self.now, origin="site", completeness="unknown",
        )
        records = await self.archive.get_days("group:1", date(2026, 11, 5), date(2026, 11, 6))
        self.assertEqual([item["date_iso"] for item in records], ["2026-11-06", "2026-11-05"])
        self.assertEqual(await self.archive.get_days("group:missing", date(2026, 11, 5), date(2026, 11, 6)), [])
        with self.assertRaises(ValueError):
            await self.archive.get_days("group:1", date(2026, 11, 6), date(2026, 11, 5))

    async def test_accepted_snapshot_and_reference_are_atomic_and_baseline_does_not_move(self):
        first = ScheduleSnapshot("Группа", datetime(2026, 11, 5, 9), [self.day])
        newer_day = DaySchedule("5 ноября", "2026-11-05", [Lesson(1, "Химия", "Иванов", "102")])
        newer = ScheduleSnapshot("Группа", datetime(2026, 11, 5, 10), [newer_day])
        with patch.dict("os.environ", {"APP_TIMEZONE": "Europe/Moscow"}):
            for snapshot in (first, newer):
                await Database(self.path).save_snapshot(
                    "current", "h", snapshot, 1, "Группа", source_type="group", source_key="group:1", archive_origin="site",
                )
        archived = await self.archive.get_day("group:1", date(2026, 11, 5))
        self.assertEqual(archived["content"]["lessons"][0]["subject"], "Химия")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT reference_kind, content_json FROM schedule_day_references ORDER BY reference_kind")
            rows = await cursor.fetchall()
        self.assertEqual([row[0] for row in rows], ["day_start", "first_seen"])
        self.assertTrue(all("Физика" in row[1] for row in rows))
        reference = await self.archive.get_reference("group:1", date(2026, 11, 5), today=date(2026, 11, 5))
        self.assertEqual(reference["reference_kind"], "day_start")
        self.assertEqual(reference["content"]["lessons"][0]["subject"], "Физика")
        self.assertIsNone(await self.archive.get_reference("group:1", date(2026, 11, 5), today=date(2026, 11, 6)))

    async def test_snapshot_and_archive_both_roll_back_on_archive_failure(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("CREATE TRIGGER archive_fail BEFORE INSERT ON schedule_day_archive BEGIN SELECT RAISE(ABORT, 'failed'); END")
            await db.commit()
        snapshot = ScheduleSnapshot("Группа", datetime(2026, 11, 5, 9), [self.day])
        with self.assertRaises(aiosqlite.IntegrityError):
            await Database(self.path).save_snapshot(
                "current", "h", snapshot, 1, "Группа", source_type="group", source_key="group:1", archive_origin="site",
            )
        self.assertIsNone(await Database(self.path).get_latest_snapshot("current", source_key="group:1"))

    async def test_manual_merge_only_archives_explicit_dates(self):
        retained = DaySchedule("6 ноября", "2026-11-06", [Lesson(1, "История", "Петров", "103")])
        snapshot = ScheduleSnapshot("Группа", datetime(2026, 11, 5, 9), [self.day, retained])
        await Database(self.path).save_snapshot(
            "current", "h", snapshot, 1, "Группа", source_type="group", source_key="group:1",
            archive_origin="manual", archive_dates={"2026-11-05"},
        )
        self.assertIsNotNone(await self.archive.get_day("group:1", date(2026, 11, 5)))
        self.assertIsNone(await self.archive.get_day("group:1", date(2026, 11, 6)))

    async def test_future_reference_is_independent_of_first_version_today(self):
        future = DaySchedule("6 ноября", "2026-11-06", [Lesson(1, "Физика", "Иванов", "101")])
        snapshot = ScheduleSnapshot("Группа", datetime(2026, 11, 5, 9), [future])
        await Database(self.path).save_snapshot(
            "current", "h", snapshot, 1, "Группа", source_type="group", source_key="group:1", archive_origin="site",
        )
        reference = await self.archive.get_reference("group:1", date(2026, 11, 6), today=date(2026, 11, 5))
        self.assertEqual(reference["reference_kind"], "first_seen")
        self.assertIsNone(await self.archive.get_reference("group:1", date(2026, 11, 6), today=date(2026, 11, 6)))

    async def test_promote_pending_transfers_archive_days_and_references(self):
        db = Database(self.path)
        await db.upsert_user("tg", 10, "user", "User", "group", "group-pending:isp", "ISP", "url")
        snapshot = ScheduleSnapshot("ISP", datetime(2026, 11, 5, 9), [self.day])
        await db.save_snapshot(
            "current", "h", snapshot, None, "ISP", source_type="group", source_key="group-pending:isp", archive_origin="ocr",
        )
        self.assertIsNotNone(await self.archive.get_day("group-pending:isp", date(2026, 11, 5)))
        moved = await db.promote_pending_group_subscription("group-pending:isp", "ИСП-25", 601)
        self.assertEqual(moved, 1)
        self.assertIsNone(await self.archive.get_day("group-pending:isp", date(2026, 11, 5)))
        promoted = await self.archive.get_day("group:601", date(2026, 11, 5))
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted["content"]["lessons"][0]["subject"], "Физика")

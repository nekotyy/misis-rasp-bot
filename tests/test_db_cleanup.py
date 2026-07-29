import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

from src.db import Database
from src.message_broker import DatabaseCleanupJob, DatabaseCleanupJobBroker
from src.scheduler import ScheduleJobs


class DatabaseCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_cleanup.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_cleanup_old_records_preserves_latest_snapshot(self):
        now = datetime.now()
        fresh_date = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        old_date = (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")

        async with aiosqlite.connect(self.db_path) as conn:
            # 1. delivery_events
            await conn.execute(
                "INSERT INTO delivery_events (campaign_type, platform, user_id, via_broker, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("notification", "telegram", 1, 0, "sent", fresh_date),
            )
            await conn.execute(
                "INSERT INTO delivery_events (campaign_type, platform, user_id, via_broker, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("notification", "telegram", 2, 0, "sent", old_date),
            )

            # 2. change_events
            await conn.execute(
                "INSERT INTO change_events (snapshot_hash, message, changed_dates_json, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                ("hash1", "Fresh change", "[]", "{}", fresh_date),
            )
            await conn.execute(
                "INSERT INTO change_events (snapshot_hash, message, changed_dates_json, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                ("hash2", "Old change", "[]", "{}", old_date),
            )

            # 3. schedule_snapshots (two snapshots for Group A, two for Group B)
            # Group A has an old and a new snapshot
            await conn.execute(
                "INSERT INTO schedule_snapshots (snapshot_type, source_key, snapshot_hash, content_json, fetched_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("current", "group:1", "old_hash_a", "{}", old_date, old_date),
            )
            await conn.execute(
                "INSERT INTO schedule_snapshots (snapshot_type, source_key, snapshot_hash, content_json, fetched_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("current", "group:1", "new_hash_a", "{}", fresh_date, fresh_date),
            )

            # Group B has ONLY an OLD snapshot (e.g. untouched over summer)
            await conn.execute(
                "INSERT INTO schedule_snapshots (snapshot_type, source_key, snapshot_hash, content_json, fetched_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("current", "group:2", "sole_old_hash_b", "{}", old_date, old_date),
            )
            await conn.commit()

        # Run cleanup for records older than 90 days
        res = await self.db.cleanup_old_records(days=90)
        self.assertEqual(res["delivery_events"], 1)
        self.assertEqual(res["change_events"], 1)
        # Group A's old snapshot should be deleted, Group B's sole old snapshot MUST BE PRESERVED
        self.assertEqual(res["schedule_snapshots"], 1)

        async with aiosqlite.connect(self.db_path) as conn:
            # Check delivery_events
            async with conn.execute("SELECT user_id FROM delivery_events") as cursor:
                rows = await cursor.fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], 1)

            # Check schedule_snapshots: Group A new and Group B sole old must remain
            async with conn.execute("SELECT source_key, snapshot_hash FROM schedule_snapshots ORDER BY id ASC") as cursor:
                rows = await cursor.fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0], ("group:1", "new_hash_a"))
                self.assertEqual(rows[1], ("group:2", "sole_old_hash_b"))

    async def test_scheduler_cleanup_job_trigger(self):
        mock_db = MagicMock()
        mock_db.cleanup_old_records = AsyncMock(return_value={"delivery_events": 5})

        mock_broker = MagicMock(spec=DatabaseCleanupJobBroker)
        mock_broker.enabled = True
        mock_broker.queue_name = "misis_db_cleanup"
        mock_broker.publish = AsyncMock(return_value=True)

        jobs = ScheduleJobs(
            db=mock_db,
            parser=MagicMock(),
            broadcaster=MagicMock(),
            timezone="Europe/Moscow",
            db_cleanup_broker=mock_broker,
        )

        await jobs.enqueue_or_run_db_cleanup()

        # Broker should have published the DatabaseCleanupJob
        mock_broker.publish.assert_awaited_once()
        published_job = mock_broker.publish.call_args[0][0]
        self.assertIsInstance(published_job, DatabaseCleanupJob)
        self.assertEqual(published_job.days, 90)

        # Test handler execution
        await jobs.handle_db_cleanup_job(published_job)
        mock_db.cleanup_old_records.assert_awaited_once_with(days=90)


if __name__ == "__main__":
    unittest.main()

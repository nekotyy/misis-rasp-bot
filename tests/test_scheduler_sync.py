from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.db import Database
from src.models import ChangeSummary, DaySchedule, Lesson, ScheduleSnapshot


class TestSyncSource(unittest.IsolatedAsyncioTestCase):
    """Тесты для _sync_source — центрального цикла синхронизации."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_sync.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

        self.mock_broadcaster = AsyncMock()
        self.mock_broadcaster.broadcast = AsyncMock()

        self.mock_parser = MagicMock()
        self.snapshot = ScheduleSnapshot(
            group_name="КИБ-24-1",
            fetched_at=datetime(2026, 8, 5, 12, 0, 0),
            days=[DaySchedule(date_label="Пн", date_iso="2026-08-05", lessons=[
                Lesson(number=1, subject="Математика", teacher="Иванов", classroom="301"),
            ])],
        )

        self.source = {
            "source_type": "group",
            "source_key": "600",
            "source_title": "КИБ-24-1",
            "source_url": "http://example.com/600",
            "schedule_id": 600,
            "group_name": "КИБ-24-1",
        }

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_first_sync_saves_baseline_no_broadcast(self) -> None:
        """Первый sync — сохраняет baseline, но НЕ рассылает."""
        from src.scheduler import ScheduleJobs
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster
        jobs._parse_source = AsyncMock(return_value=(self.snapshot, "hash_first"))

        await jobs._sync_source(self.source)

        self.mock_broadcaster.broadcast.assert_not_called()
        baseline = await self.db.get_latest_snapshot("daily_baseline", schedule_id=600)
        self.assertIsNotNone(baseline)
        self.assertEqual(baseline["snapshot_hash"], "hash_first")

    async def test_no_change_no_broadcast(self) -> None:
        """Если расписание не изменилось — broadcast не вызывается."""
        from src.scheduler import ScheduleJobs
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster

        # Первый sync — baseline
        jobs._parse_source = AsyncMock(return_value=(self.snapshot, "hash_same"))
        await jobs._sync_source(self.source)

        # Второй sync — тот же hash
        jobs._parse_source = AsyncMock(return_value=(self.snapshot, "hash_same"))
        with patch("src.scheduler.ScheduleComparator") as mock_comp:
            mock_comp.compare.return_value = None  # Нет изменений
            await jobs._sync_source(self.source)

        self.mock_broadcaster.broadcast.assert_not_called()

    async def test_change_triggers_broadcast(self) -> None:
        """Если расписание изменилось — broadcast вызывается."""
        from src.scheduler import ScheduleJobs
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster

        # Первый sync — baseline
        jobs._parse_source = AsyncMock(return_value=(self.snapshot, "hash_v1"))
        await jobs._sync_source(self.source)

        # Второй sync — другой hash + compare возвращает изменение
        changed_snapshot = ScheduleSnapshot(
            group_name="КИБ-24-1",
            fetched_at=datetime(2026, 8, 5, 13, 0, 0),
            days=[DaySchedule(date_label="Пн", date_iso="2026-08-05", lessons=[
                Lesson(number=1, subject="Физика", teacher="Петров", classroom="201"),
            ])],
        )
        jobs._parse_source = AsyncMock(return_value=(changed_snapshot, "hash_v2"))

        change = ChangeSummary(
            changed_dates=["2026-08-05"],
            message="Изменения на понедельник",
            payload={"test": True},
            telegram_message="<b>Изменения</b>",
            vk_message="Изменения",
        )
        with patch("src.scheduler.ScheduleComparator") as mock_comp:
            mock_comp.compare.return_value = change
            await jobs._sync_source(self.source)

        self.mock_broadcaster.broadcast.assert_called_once()
        call_kwargs = self.mock_broadcaster.broadcast.call_args
        self.assertIn("Изменения на понедельник", call_kwargs.args or [call_kwargs[0][0]])

    def test_scheduler_configure_auto_daily_lesson_counter_jobs(self) -> None:
        """Проверяем, что задачи автоподсчета пар регистрируются как корутины с правильными kwargs."""
        from src.scheduler import ScheduleJobs

        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.scheduler = MagicMock()
        jobs.lesson_counters_enabled = True
        jobs.lesson_counter_service = MagicMock()
        jobs.admin_backup_enabled = False
        jobs.save_daily_baseline = AsyncMock()
        jobs.save_daily_baseline_fallback = AsyncMock()
        jobs.sync_current_snapshot = AsyncMock()
        jobs.count_today_lessons = AsyncMock()
        jobs.enqueue_or_run_auto_daily_lesson_counter = AsyncMock()
        jobs.enqueue_or_run_db_cleanup = AsyncMock()

        jobs.configure()

        # Check all add_job calls
        registered_funcs = [call.args[0] for call in jobs.scheduler.add_job.call_args_list if call.args]
        for func in registered_funcs:
            # None of the registered jobs should be a sync lambda returning an unawaited coroutine
            self.assertFalse(func.__name__ == "<lambda>", "Job must not be an unawaited sync lambda")


if __name__ == "__main__":
    unittest.main()

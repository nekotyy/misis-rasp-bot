"""Плановое обновление каталога групп с сайта (раз в несколько месяцев)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.db import Database
from src.group_catalog import GroupCatalog
from src.scheduler import ScheduleJobs


class RefreshGroupCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_calls_group_catalog_with_force(self) -> None:
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.group_catalog = MagicMock(spec=GroupCatalog)
        jobs.group_catalog.refresh = AsyncMock()
        jobs.group_catalog.last_error = None
        jobs.group_catalog.__len__ = MagicMock(return_value=42)

        await jobs.refresh_group_catalog()

        jobs.group_catalog.refresh.assert_awaited_once_with(force=True)

    async def test_refresh_survives_missing_catalog(self) -> None:
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.group_catalog = None

        await jobs.refresh_group_catalog()  # не должно бросать

    async def test_refresh_logs_but_does_not_raise_on_failure(self) -> None:
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.group_catalog = MagicMock(spec=GroupCatalog)
        jobs.group_catalog.refresh = AsyncMock()
        jobs.group_catalog.last_error = RuntimeError("сайт лежит")
        jobs.group_catalog.__len__ = MagicMock(return_value=10)

        await jobs.refresh_group_catalog()  # не должно бросать, только залогировать


class ConfigureRegistersRefreshJobTests(unittest.IsolatedAsyncioTestCase):
    """Джоба должна регистрироваться только когда каталог групп вообще передан."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _build_jobs(self, *, with_catalog: bool) -> ScheduleJobs:
        group_catalog = GroupCatalog("http://test-schedule.local", db=self.db) if with_catalog else None
        return ScheduleJobs(
            db=self.db,
            parser=MagicMock(),
            broadcaster=MagicMock(),
            timezone="Europe/Moscow",
            group_catalog=group_catalog,
        )

    def test_job_registered_when_catalog_present(self) -> None:
        jobs = self._build_jobs(with_catalog=True)
        jobs.configure()

        refresh_jobs = [job for job in jobs.scheduler.get_jobs() if job.func == jobs.refresh_group_catalog]
        self.assertEqual(len(refresh_jobs), 1)
        self.assertEqual(refresh_jobs[0].trigger.interval.days, 90)

    def test_job_not_registered_without_catalog(self) -> None:
        jobs = self._build_jobs(with_catalog=False)
        jobs.configure()

        refresh_jobs = [job for job in jobs.scheduler.get_jobs() if job.func == jobs.refresh_group_catalog]
        self.assertEqual(refresh_jobs, [])

    def test_refresh_interval_is_configurable(self) -> None:
        group_catalog = GroupCatalog("http://test-schedule.local", db=self.db)
        jobs = ScheduleJobs(
            db=self.db,
            parser=MagicMock(),
            broadcaster=MagicMock(),
            timezone="Europe/Moscow",
            group_catalog=group_catalog,
            group_catalog_refresh_days=30,
        )
        jobs.configure()

        refresh_jobs = [job for job in jobs.scheduler.get_jobs() if job.func == jobs.refresh_group_catalog]
        self.assertEqual(refresh_jobs[0].trigger.interval.days, 30)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.scheduler import ScheduleJobs


class ScheduleSyncRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.source = {
            "source_type": "group", "source_key": "group:1", "source_title": "Группа",
            "schedule_id": 1,
        }
        self.jobs = ScheduleJobs.__new__(ScheduleJobs)
        self.jobs.db = MagicMock(get_active_sources=AsyncMock(return_value=[self.source]))
        self.jobs.alert_manager = None
        self.jobs._pending_sync_retries = {}
        self.jobs._parse_source = AsyncMock(return_value=("snapshot", "hash"))
        self.jobs.apply_snapshot = AsyncMock()

    async def test_failed_sync_is_queued_once_and_retried(self) -> None:
        self.jobs._parse_source.side_effect = httpx.ReadTimeout("timeout")
        await self.jobs._run_for_active_sources("sync", self.jobs._sync_source)
        await self.jobs._run_for_active_sources("sync", self.jobs._sync_source)
        self.assertEqual(list(self.jobs._pending_sync_retries), ["group:1"])

        self.jobs._parse_source.side_effect = None
        await self.jobs.retry_failed_sync()

        self.jobs.apply_snapshot.assert_awaited_once_with(self.source, "snapshot", "hash")
        self.assertEqual(self.jobs._pending_sync_retries, {})

    async def test_retry_timeout_requeues_without_applying_snapshot(self) -> None:
        self.jobs._pending_sync_retries = {"group:1": self.source}
        self.jobs._parse_source = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        await self.jobs.retry_failed_sync()
        self.assertIn("group:1", self.jobs._pending_sync_retries)
        self.jobs.apply_snapshot.assert_not_awaited()

    async def test_initial_http_server_error_is_retried(self) -> None:
        request = httpx.Request("GET", "http://example.test/")
        response = httpx.Response(503, request=request)
        self.jobs._parse_source.side_effect = httpx.HTTPStatusError("unavailable", request=request, response=response)
        with self.assertRaises(httpx.HTTPStatusError):
            await self.jobs._sync_source(self.source)
        self.assertIn("group:1", self.jobs._pending_sync_retries)
        self.jobs.apply_snapshot.assert_not_awaited()

    async def test_retry_interrupts_unresponsive_fetch_and_keeps_queue(self) -> None:
        self.jobs._pending_sync_retries = {"group:1": self.source}

        async def hang(_source):
            await asyncio.Event().wait()

        self.jobs._parse_source = AsyncMock(side_effect=hang)
        with patch("src.scheduler.SYNC_RETRY_TIMEOUT_SECONDS", 0.01):
            await self.jobs.retry_failed_sync()
        self.assertIn("group:1", self.jobs._pending_sync_retries)
        self.jobs.apply_snapshot.assert_not_awaited()

    async def test_initial_sync_queues_unresponsive_fetch(self) -> None:
        async def hang(_source):
            await asyncio.Event().wait()

        self.jobs._parse_source = AsyncMock(side_effect=hang)
        with patch("src.scheduler.SYNC_RETRY_TIMEOUT_SECONDS", 0.01), self.assertRaises(TimeoutError):
            await self.jobs._sync_source(self.source)
        self.assertIn("group:1", self.jobs._pending_sync_retries)
        self.jobs.apply_snapshot.assert_not_awaited()

    async def test_failed_notification_does_not_queue_duplicate_sync(self) -> None:
        self.jobs.apply_snapshot.side_effect = httpx.ReadTimeout("delivery timeout")
        with self.assertRaises(httpx.ReadTimeout):
            await self.jobs._sync_source(self.source)
        self.assertNotIn("group:1", self.jobs._pending_sync_retries)

    async def test_retry_queue_rotates_failed_sources(self) -> None:
        other = {**self.source, "source_key": "group:2", "schedule_id": 2}
        self.jobs._pending_sync_retries = {"group:1": self.source, "group:2": other}
        self.jobs.db.get_active_sources.return_value = [self.source, other]
        self.jobs._parse_source.side_effect = httpx.ReadTimeout("timeout")
        await self.jobs.retry_failed_sync()
        self.assertEqual(list(self.jobs._pending_sync_retries), ["group:2", "group:1"])

    def test_retry_job_runs_every_15_seconds_without_overlap(self) -> None:
        jobs = ScheduleJobs(MagicMock(), MagicMock(), MagicMock(), timezone="UTC")
        jobs.configure()
        retry_job = next(job for job in jobs.scheduler.get_jobs() if job.func == jobs.retry_failed_sync)
        self.assertEqual(retry_job.trigger.interval.total_seconds(), 15)
        self.assertEqual(retry_job.max_instances, 1)

    async def test_unsubscribed_source_is_removed(self) -> None:
        self.jobs._pending_sync_retries = {"group:1": self.source}
        self.jobs.db.get_active_sources.return_value = []
        await self.jobs.retry_failed_sync()
        self.assertEqual(self.jobs._pending_sync_retries, {})
        self.jobs._parse_source.assert_not_awaited()

    async def test_university_retry_can_fetch_both_weeks(self) -> None:
        source = {**self.source, "source_url": "http://r.sf-misis.ru/group/42"}
        self.jobs._pending_sync_retries = {"group:1": source}
        self.jobs.db.get_active_sources.return_value = [source]
        wait_for = asyncio.wait_for
        with patch("src.scheduler.asyncio.wait_for", wraps=wait_for) as timed:
            await self.jobs.retry_failed_sync()
        self.assertEqual(timed.call_args.kwargs["timeout"], 30)
        self.jobs.apply_snapshot.assert_awaited_once_with(source, "snapshot", "hash")

    async def test_cancelled_retry_stays_queued(self) -> None:
        self.jobs._pending_sync_retries = {"group:1": self.source}
        started = asyncio.Event()

        async def wait_for_source(_source):
            started.set()
            await asyncio.Event().wait()

        self.jobs._parse_source = AsyncMock(side_effect=wait_for_source)
        task = asyncio.create_task(self.jobs.retry_failed_sync())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn("group:1", self.jobs._pending_sync_retries)

    async def test_retry_waits_for_existing_sync_and_skips_duplicate(self) -> None:
        self.jobs._pending_sync_retries = {"group:1": self.source}
        started = asyncio.Event()
        release = asyncio.Event()

        async def parse(_source):
            started.set()
            await release.wait()
            return "snapshot", "hash"

        self.jobs._parse_source = AsyncMock(side_effect=parse)
        sync = asyncio.create_task(self.jobs._sync_source(self.source))
        await started.wait()
        retry = asyncio.create_task(self.jobs.retry_failed_sync())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(sync, retry)
        self.jobs._parse_source.assert_awaited_once()
        self.jobs.apply_snapshot.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

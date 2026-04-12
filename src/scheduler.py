from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.db import Database
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.schedule_service import ScheduleComparator

logger = logging.getLogger(__name__)

SourceRow = dict[str, Any]
SourceWorker = Callable[..., Awaitable[None]]


class ScheduleJobs:
    def __init__(
        self,
        db: Database,
        parser: ScheduleParser,
        broadcaster: Broadcaster,
        timezone: str,
        request_delay_seconds: float = 8.0,
        request_jitter_seconds: float = 4.0,
    ) -> None:
        self.db = db
        self.parser = parser
        self.broadcaster = broadcaster
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.request_jitter_seconds = max(0.0, request_jitter_seconds)
        self._sync_lock = asyncio.Lock()
        self._baseline_lock = asyncio.Lock()

    def configure(self) -> None:
        self.scheduler.add_job(self.save_daily_baseline, CronTrigger(hour=0, minute=0), max_instances=1, coalesce=True)
        self.scheduler.add_job(
            self.save_daily_baseline_fallback,
            CronTrigger(hour=5, minute=0),
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(self.sync_current_snapshot, CronTrigger(minute=10), max_instances=1, coalesce=True)
        self.scheduler.add_job(
            self.sync_current_snapshot,
            CronTrigger(hour="9-13", minute=40),
            max_instances=1,
            coalesce=True,
        )

    def start(self) -> None:
        self.configure()
        self.scheduler.start()

    async def save_daily_baseline(self) -> None:
        if self._baseline_lock.locked():
            logger.info("Baseline task skipped because previous run is still active.")
            return
        async with self._baseline_lock:
            await self._run_for_active_sources("baseline", self._save_baseline_for_source)

    async def save_daily_baseline_fallback(self) -> None:
        if self._baseline_lock.locked():
            logger.info("Baseline fallback skipped because previous run is still active.")
            return
        async with self._baseline_lock:
            today_prefix = datetime.now().date().isoformat()
            await self._run_for_active_sources(
                "baseline-fallback",
                self._save_baseline_fallback_for_source,
                today_prefix=today_prefix,
            )

    async def sync_current_snapshot(self) -> None:
        if self._sync_lock.locked():
            logger.info("Sync task skipped because previous run is still active.")
            return
        async with self._sync_lock:
            await self._run_for_active_sources("sync", self._sync_source)

    async def _run_for_active_sources(self, job_name: str, worker: SourceWorker, **kwargs: Any) -> None:
        sources = await self.db.get_active_sources()
        if not sources:
            logger.info("Task %s skipped because there are no active sources.", job_name)
            return

        for index, source in enumerate(sources):
            if index:
                await self._sleep_between_sources(job_name, str(source["source_title"]))
            try:
                await worker(source, **kwargs)
            except Exception as exc:
                logger.warning("Task %s failed for %s: %s", job_name, source["source_title"], exc)

    async def _sleep_between_sources(self, job_name: str, source_title: str) -> None:
        delay = self.request_delay_seconds + random.uniform(0, self.request_jitter_seconds)
        if delay <= 0:
            return
        logger.info("Task %s waits %.1f seconds before next source (%s).", job_name, delay, source_title)
        await asyncio.sleep(delay)

    async def _parse_source(self, source: SourceRow):
        if source["source_type"] == "teacher":
            return await self.parser.parse_from_url(source["source_url"])
        return await self.parser.parse(source["schedule_id"])

    async def _save_baseline_for_source(self, source: SourceRow, **_: Any) -> None:
        snapshot, snapshot_hash = await self._parse_source(source)
        await self.db.save_snapshot(
            "daily_baseline",
            snapshot_hash,
            snapshot,
            schedule_id=source["schedule_id"],
            group_name=source.get("group_name"),
            source_type=source["source_type"],
            source_key=source["source_key"],
            source_title=source["source_title"],
            source_url=source["source_url"],
        )

    async def _save_baseline_fallback_for_source(
        self,
        source: SourceRow,
        *,
        today_prefix: str,
        **_: Any,
    ) -> None:
        if await self.db.has_baseline_for_date(
            today_prefix,
            schedule_id=source["schedule_id"],
            source_key=source["source_key"],
        ):
            return
        await self._save_baseline_for_source(source)

    async def _sync_source(self, source: SourceRow, **_: Any) -> None:
        snapshot, snapshot_hash = await self._parse_source(source)
        baseline = await self.db.get_latest_snapshot(
            "daily_baseline",
            schedule_id=source["schedule_id"],
            source_key=source["source_key"],
        )
        await self.db.save_snapshot(
            "current",
            snapshot_hash,
            snapshot,
            schedule_id=source["schedule_id"],
            group_name=source.get("group_name"),
            source_type=source["source_type"],
            source_key=source["source_key"],
            source_title=source["source_title"],
            source_url=source["source_url"],
        )

        if baseline is None:
            logger.info("Source %s has no baseline yet. Saving first baseline automatically.", source["source_title"])
            await self.db.save_snapshot(
                "daily_baseline",
                snapshot_hash,
                snapshot,
                schedule_id=source["schedule_id"],
                group_name=source.get("group_name"),
                source_type=source["source_type"],
                source_key=source["source_key"],
                source_title=source["source_title"],
                source_url=source["source_url"],
            )
            return

        change_summary = ScheduleComparator.compare(baseline, snapshot)
        if change_summary is None:
            return

        await self.db.record_change(
            snapshot_hash=snapshot_hash,
            message=change_summary.message,
            changed_dates=change_summary.changed_dates,
            payload=change_summary.payload,
            schedule_id=source["schedule_id"],
            group_name=source.get("group_name"),
            source_type=source["source_type"],
            source_key=source["source_key"],
            source_title=source["source_title"],
            source_url=source["source_url"],
        )
        await self.broadcaster.broadcast(
            change_summary.message,
            telegram_message=change_summary.telegram_message,
            vk_message=change_summary.vk_message,
            schedule_id=source["schedule_id"],
            subscription_key=source["source_key"],
        )
        await self.db.save_snapshot(
            "daily_baseline",
            snapshot_hash,
            snapshot,
            schedule_id=source["schedule_id"],
            group_name=source.get("group_name"),
            source_type=source["source_type"],
            source_key=source["source_key"],
            source_title=source["source_title"],
            source_url=source["source_url"],
        )

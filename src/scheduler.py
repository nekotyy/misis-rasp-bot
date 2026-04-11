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

GroupRow = dict[str, Any]
GroupWorker = Callable[..., Awaitable[None]]


class ScheduleJobs:
    """Фоновые задачи для baseline и постепенной синхронизации расписания."""

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
        self.scheduler.add_job(
            self.save_daily_baseline,
            CronTrigger(hour=0, minute=0),
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.save_daily_baseline_fallback,
            CronTrigger(hour=5, minute=0),
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.sync_current_snapshot,
            CronTrigger(minute=10),
            max_instances=1,
            coalesce=True,
        )
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
            logger.info("Сохранение baseline пропущено: предыдущая задача еще выполняется.")
            return
        async with self._baseline_lock:
            await self._run_for_active_groups("baseline", self._save_baseline_for_group)

    async def save_daily_baseline_fallback(self) -> None:
        if self._baseline_lock.locked():
            logger.info("Fallback baseline пропущен: предыдущая задача еще выполняется.")
            return
        async with self._baseline_lock:
            today_prefix = datetime.now().date().isoformat()
            await self._run_for_active_groups(
                "baseline-fallback",
                self._save_baseline_fallback_for_group,
                today_prefix=today_prefix,
            )

    async def sync_current_snapshot(self) -> None:
        if self._sync_lock.locked():
            logger.info("Синхронизация пропущена: предыдущая задача еще выполняется.")
            return
        async with self._sync_lock:
            await self._run_for_active_groups("sync", self._sync_group)

    async def _run_for_active_groups(self, job_name: str, worker: GroupWorker, **kwargs: Any) -> None:
        groups = await self.db.get_active_groups()
        if not groups:
            logger.info("Задача %s: активных групп нет, парсинг не требуется.", job_name)
            return

        for index, group in enumerate(groups):
            if index:
                await self._sleep_between_groups(job_name, group["group_name"])
            try:
                await worker(group, **kwargs)
            except Exception as exc:
                logger.warning(
                    "Задача %s: не удалось обработать группу %s: %s",
                    job_name,
                    group["group_name"],
                    exc,
                )

    async def _sleep_between_groups(self, job_name: str, group_name: str) -> None:
        delay = self.request_delay_seconds + random.uniform(0, self.request_jitter_seconds)
        if delay <= 0:
            return
        logger.info(
            "Задача %s: жду %.1f с перед следующим запросом (%s).",
            job_name,
            delay,
            group_name,
        )
        await asyncio.sleep(delay)

    async def _save_baseline_for_group(self, group: GroupRow, **_: Any) -> None:
        snapshot, snapshot_hash = await self.parser.parse(group["schedule_id"])
        await self.db.save_snapshot(
            "daily_baseline",
            snapshot_hash,
            snapshot,
            schedule_id=group["schedule_id"],
            group_name=group["group_name"],
        )

    async def _save_baseline_fallback_for_group(
        self,
        group: GroupRow,
        *,
        today_prefix: str,
        **_: Any,
    ) -> None:
        if await self.db.has_baseline_for_date(today_prefix, group["schedule_id"]):
            return
        await self._save_baseline_for_group(group)

    async def _sync_group(self, group: GroupRow, **_: Any) -> None:
        snapshot, snapshot_hash = await self.parser.parse(group["schedule_id"])
        baseline = await self.db.get_latest_snapshot("daily_baseline", group["schedule_id"])
        await self.db.save_snapshot(
            "current",
            snapshot_hash,
            snapshot,
            schedule_id=group["schedule_id"],
            group_name=group["group_name"],
        )

        if baseline is None:
            logger.info(
                "Группа %s стала активной без baseline. Сохраняю первый эталон автоматически.",
                group["group_name"],
            )
            await self.db.save_snapshot(
                "daily_baseline",
                snapshot_hash,
                snapshot,
                schedule_id=group["schedule_id"],
                group_name=group["group_name"],
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
            schedule_id=group["schedule_id"],
            group_name=group["group_name"],
        )
        await self.broadcaster.broadcast(
            change_summary.message,
            telegram_message=change_summary.telegram_message,
            vk_message=change_summary.vk_message,
            schedule_id=group["schedule_id"],
        )
        await self.db.save_snapshot(
            "daily_baseline",
            snapshot_hash,
            snapshot,
            schedule_id=group["schedule_id"],
            group_name=group["group_name"],
        )

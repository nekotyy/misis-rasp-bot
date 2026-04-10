from __future__ import annotations

from datetime import datetime
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.db import Database
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.schedule_service import ScheduleComparator

logger = logging.getLogger(__name__)


class ScheduleJobs:
    """Планировщик фоновых задач: ежедневно сохраняет эталонное расписание,
    периодически сверяет текущую версию с эталоном и оповещает об изменениях."""
    def __init__(self, db: Database, parser: ScheduleParser, broadcaster: Broadcaster, timezone: str) -> None:
        self.db = db
        self.parser = parser
        self.broadcaster = broadcaster
        self.scheduler = AsyncIOScheduler(timezone=timezone)

    def configure(self) -> None:
        self.scheduler.add_job(self.save_daily_baseline, CronTrigger(hour=0, minute=0))
        self.scheduler.add_job(self.save_daily_baseline_fallback, CronTrigger(hour=5, minute=0))
        self.scheduler.add_job(self.sync_current_snapshot, CronTrigger(minute=10))

    def start(self) -> None:
        self.configure()
        self.scheduler.start()

    async def save_daily_baseline(self) -> None:
        for group in await self.db.get_active_groups():
            try:
                snapshot, snapshot_hash = await self.parser.parse(group["schedule_id"])
                await self.db.save_snapshot(
                    "daily_baseline",
                    snapshot_hash,
                    snapshot,
                    schedule_id=group["schedule_id"],
                    group_name=group["group_name"],
                )
            except Exception as exc:
                logger.warning("Не удалось сохранить эталон для %s: %s", group["group_name"], exc)

    async def save_daily_baseline_fallback(self) -> None:
        today_prefix = datetime.now().date().isoformat()
        for group in await self.db.get_active_groups():
            if await self.db.has_baseline_for_date(today_prefix, group["schedule_id"]):
                continue
            try:
                snapshot, snapshot_hash = await self.parser.parse(group["schedule_id"])
                await self.db.save_snapshot(
                    "daily_baseline",
                    snapshot_hash,
                    snapshot,
                    schedule_id=group["schedule_id"],
                    group_name=group["group_name"],
                )
            except Exception as exc:
                logger.warning("Не удалось сохранить fallback-эталон для %s: %s", group["group_name"], exc)

    async def sync_current_snapshot(self) -> None:
        for group in await self.db.get_active_groups():
            try:
                snapshot, snapshot_hash = await self.parser.parse(group["schedule_id"])
                baseline = await self.db.get_latest_snapshot("daily_baseline", group["schedule_id"])
                change_summary = ScheduleComparator.compare(baseline, snapshot)
                await self.db.save_snapshot(
                    "current",
                    snapshot_hash,
                    snapshot,
                    schedule_id=group["schedule_id"],
                    group_name=group["group_name"],
                )

                if change_summary is None:
                    continue

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
            except Exception as exc:
                logger.warning("Не удалось синхронизировать группу %s: %s", group["group_name"], exc)

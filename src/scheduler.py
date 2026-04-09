from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.db import Database
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.schedule_service import ScheduleComparator


class ScheduleJobs:
    def __init__(self, db: Database, parser: ScheduleParser, broadcaster: Broadcaster, timezone: str) -> None:
        self.db = db
        self.parser = parser
        self.broadcaster = broadcaster
        self.scheduler = AsyncIOScheduler(timezone=timezone)

    def configure(self) -> None:
        self.scheduler.add_job(self.save_daily_baseline, CronTrigger(hour=0, minute=0))
        self.scheduler.add_job(self.save_daily_baseline_fallback, CronTrigger(hour=5, minute=0))
        self.scheduler.add_job(self.sync_current_snapshot, CronTrigger(minute=0))

    def start(self) -> None:
        self.configure()
        self.scheduler.start()

    async def save_daily_baseline(self) -> None:
        snapshot, snapshot_hash = await self.parser.parse()
        await self.db.save_snapshot("daily_baseline", snapshot_hash, snapshot)

    async def save_daily_baseline_fallback(self) -> None:
        today_prefix = datetime.now().date().isoformat()
        if await self.db.has_baseline_for_date(today_prefix):
            return
        await self.save_daily_baseline()

    async def sync_current_snapshot(self) -> None:
        snapshot, snapshot_hash = await self.parser.parse()
        previous = await self.db.get_latest_snapshot("current")
        change_summary = ScheduleComparator.compare(previous, snapshot)
        await self.db.save_snapshot("current", snapshot_hash, snapshot)

        if change_summary is None:
            return

        await self.db.record_change(
            snapshot_hash=snapshot_hash,
            message=change_summary.message,
            changed_dates=change_summary.changed_dates,
            payload=change_summary.payload,
        )
        await self.broadcaster.broadcast(change_summary.message)

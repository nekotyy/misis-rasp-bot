from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.db import Database
from src.lesson_counters import LessonCounterService
from src.message_broker import (
    AutoDailyLessonCounterJob,
    AutoDailyLessonCounterJobBroker,
    DatabaseCleanupJob,
    DatabaseCleanupJobBroker,
    LessonCounterJob,
    LessonCounterJobBroker,
)
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.schedule_service import ScheduleComparator

logger = logging.getLogger(__name__)

SourceRow = dict[str, Any]
SourceWorker = Callable[..., Awaitable[None]]


def format_bytes(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count} Б"
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.2f} КБ"
    return f"{bytes_count / (1024 * 1024):.2f} МБ"


def format_db_cleanup_admin_report(res: dict, html: bool = True) -> str:
    started_at = res.get("started_at", "-")
    finished_at = res.get("finished_at", "-")
    elapsed = res.get("elapsed_seconds", 0.0)
    cutoff_days = res.get("cutoff_days", 90)
    deleted_counts = res.get("deleted_counts", {})
    total_deleted = res.get("total_deleted", 0)

    size_before = format_bytes(res.get("size_before_bytes", 0))
    size_after = format_bytes(res.get("size_after_bytes", 0))
    freed = format_bytes(res.get("freed_bytes", 0))

    delivery_cnt = deleted_counts.get("delivery_events", 0)
    change_cnt = deleted_counts.get("change_events", 0)
    snapshots_cnt = deleted_counts.get("schedule_snapshots", 0)

    if html:
        return "\n".join([
            "🧹 <b>Автоматическая очистка БД завершена</b>",
            "",
            "ℹ️ <b>Служебная информация:</b>",
            f"• <b>Время запуска:</b> {started_at}",
            f"• <b>Время окончания:</b> {finished_at}",
            f"• <b>Длительность:</b> {elapsed} сек.",
            f"• <b>Условие очистки:</b> записи старше {cutoff_days} дней",
            "",
            "🗑️ <b>Удалено записей:</b>",
            f"• <code>delivery_events</code>: {delivery_cnt:,}".replace(",", " "),
            f"• <code>change_events</code>: {change_cnt:,}".replace(",", " "),
            f"• <code>schedule_snapshots</code>: {snapshots_cnt:,}".replace(",", " "),
            f"• <b>Всего удалено:</b> {total_deleted:,} шт.".replace(",", " "),
            "",
            "💾 <b>Размер базы данных:</b>",
            f"• <b>До очистки:</b> {size_before}",
            f"• <b>После очистки:</b> {size_after}",
            f"• <b>Освобождено на диске:</b> {freed}",
        ])
    else:
        return "\n".join([
            "Автоматическая очистка БД завершена",
            "",
            "Служебная информация:",
            f"• Время запуска: {started_at}",
            f"• Время окончания: {finished_at}",
            f"• Длительность: {elapsed} сек.",
            f"• Условие очистки: записи старше {cutoff_days} дней",
            "",
            "Удалено записей:",
            f"• delivery_events: {delivery_cnt}",
            f"• change_events: {change_cnt}",
            f"• schedule_snapshots: {snapshots_cnt}",
            f"• Всего удалено: {total_deleted} шт.",
            "",
            "Размер базы данных:",
            f"• До очистки: {size_before}",
            f"• После очистки: {size_after}",
            f"• Освобождено на диске: {freed}",
        ])


class ScheduleJobs:
    def __init__(
        self,
        db: Database,
        parser: ScheduleParser,
        broadcaster: Broadcaster,
        timezone: str,
        request_delay_seconds: float = 8.0,
        request_jitter_seconds: float = 4.0,
        lesson_counters_enabled: bool = False,
        lesson_counter_service: LessonCounterService | None = None,
        lesson_counter_broker: LessonCounterJobBroker | None = None,
        db_cleanup_broker: DatabaseCleanupJobBroker | None = None,
        auto_daily_lesson_counter_broker: AutoDailyLessonCounterJobBroker | None = None,
        admin_backup_enabled: bool = False,
        admin_backup_interval_days: int = 2,
        admin_telegram_id: int | None = None,
        lesson_counters_path: Path | None = None,
        database_path: Path | None = None,
    ) -> None:
        self.db = db
        self.parser = parser
        self.broadcaster = broadcaster
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.request_jitter_seconds = max(0.0, request_jitter_seconds)
        self.lesson_counters_enabled = lesson_counters_enabled
        self.lesson_counter_service = lesson_counter_service
        self.lesson_counter_broker = lesson_counter_broker
        self.db_cleanup_broker = db_cleanup_broker
        self.auto_daily_lesson_counter_broker = auto_daily_lesson_counter_broker
        self.admin_backup_enabled = admin_backup_enabled
        self.admin_backup_interval_days = max(1, admin_backup_interval_days)
        self.admin_telegram_id = admin_telegram_id
        self.lesson_counters_path = lesson_counters_path
        self.database_path = database_path
        self._sync_lock = asyncio.Lock()
        self._baseline_lock = asyncio.Lock()
        self._lesson_counter_lock = asyncio.Lock()

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
        if self.lesson_counters_enabled and self.lesson_counter_service is not None:
            self.scheduler.add_job(self.count_today_lessons, CronTrigger(hour=23, minute=0), max_instances=1, coalesce=True)
            self.scheduler.add_job(self.count_today_lessons, CronTrigger(hour=23, minute=40), max_instances=1, coalesce=True)

            # Auto daily lesson counter at 23:20, and control checks at 23:50, 01:00, 05:00
            self.scheduler.add_job(
                self.enqueue_or_run_auto_daily_lesson_counter,
                CronTrigger(hour=23, minute=20),
                kwargs={"target_date_offset": 0},
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.enqueue_or_run_auto_daily_lesson_counter,
                CronTrigger(hour=23, minute=50),
                kwargs={"target_date_offset": 0},
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.enqueue_or_run_auto_daily_lesson_counter,
                CronTrigger(hour=1, minute=0),
                kwargs={"target_date_offset": -1},
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.enqueue_or_run_auto_daily_lesson_counter,
                CronTrigger(hour=5, minute=0),
                kwargs={"target_date_offset": -1},
                max_instances=1,
                coalesce=True,
            )
        if self.admin_backup_enabled:
            self.scheduler.add_job(
                self.send_admin_backup,
                IntervalTrigger(days=self.admin_backup_interval_days),
                max_instances=1,
                coalesce=True,
            )
        self.scheduler.add_job(
            self.enqueue_or_run_db_cleanup,
            CronTrigger(day_of_week="mon", hour=4, minute=30),
            max_instances=1,
            coalesce=True,
        )

    def start(self) -> None:
        self.configure()
        self.scheduler.start()

    async def start_lesson_counter_consumer(self) -> None:
        if not self.lesson_counters_enabled or self.lesson_counter_service is None:
            return
        if self.lesson_counter_broker is None or not self.lesson_counter_broker.enabled:
            return
        await self.lesson_counter_broker.start_consumer(self.handle_lesson_counter_job)

    async def start_db_cleanup_consumer(self) -> None:
        if self.db_cleanup_broker is None or not self.db_cleanup_broker.enabled:
            return
        await self.db_cleanup_broker.start_consumer(self.handle_db_cleanup_job)

    async def enqueue_or_run_db_cleanup(self) -> None:
        job = DatabaseCleanupJob(days=90)
        if self.db_cleanup_broker is not None and self.db_cleanup_broker.enabled:
            try:
                published = await self.db_cleanup_broker.publish(job)
                if published:
                    logger.info("Database cleanup job published to RabbitMQ queue %s", self.db_cleanup_broker.queue_name)
                    return
            except Exception as exc:
                logger.warning("Failed to publish db cleanup job to RabbitMQ: %s", exc)

        await self.handle_db_cleanup_job(job)

    async def handle_db_cleanup_job(self, job: DatabaseCleanupJob) -> None:
        try:
            res = await self.db.cleanup_old_records(days=job.days)
            logger.info("Database cleanup (days=%s) completed: %s", job.days, res)
            tg_report = format_db_cleanup_admin_report(res, html=True)
            vk_report = format_db_cleanup_admin_report(res, html=False)
            await self.broadcaster.notify_admins(telegram_message=tg_report, vk_message=vk_report)
        except Exception as exc:
            logger.error("Database cleanup (days=%s) failed: %s", job.days, exc)
            raise

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

    async def count_today_lessons(self) -> None:
        if not self.lesson_counters_enabled or self.lesson_counter_service is None:
            logger.info("Lesson counters task skipped because feature is disabled.")
            return
        if self._lesson_counter_lock.locked():
            logger.info("Lesson counters task skipped because previous run is still active.")
            return
        schedule_ids = await self.lesson_counter_service.configured_schedule_ids()
        if not schedule_ids:
            logger.info("Lesson counters task skipped because no groups are configured.")
            return
        async with self._lesson_counter_lock:
            broker_enabled = self.lesson_counter_broker is not None and self.lesson_counter_broker.enabled
            for index, schedule_id in enumerate(schedule_ids):
                if broker_enabled:
                    try:
                        await self.lesson_counter_broker.publish(LessonCounterJob(schedule_id=schedule_id))
                        continue
                    except Exception:
                        logger.exception(
                            "Lesson counter RabbitMQ publish failed for schedule_id=%s. Counting directly.",
                            schedule_id,
                        )
                        if index:
                            await self._sleep_between_sources("lesson-counters", str(schedule_id))
                        await self.handle_lesson_counter_job(LessonCounterJob(schedule_id=schedule_id))
                else:
                    if index:
                        await self._sleep_between_sources("lesson-counters", str(schedule_id))
                    await self.handle_lesson_counter_job(LessonCounterJob(schedule_id=schedule_id))
            if broker_enabled:
                logger.info("Lesson counters: enqueued %s group job(s).", len(schedule_ids))

    async def handle_lesson_counter_job(self, job: LessonCounterJob) -> None:
        if not self.lesson_counters_enabled or self.lesson_counter_service is None:
            return
        await self._count_lessons_for_schedule_id(job.schedule_id)

    async def send_admin_backup(self) -> None:
        if self.admin_telegram_id is None:
            logger.info("Admin backup skipped: ADMIN_TELEGRAM_ID is not set.")
            return
        if self.broadcaster.telegram_bot is None:
            logger.info("Admin backup skipped: Telegram bot is not running.")
            return

        files: list[tuple[str, Path]] = []
        if self.lesson_counters_path is not None and self.lesson_counters_path.exists():
            files.append(("lesson_counters.json", self.lesson_counters_path))
        else:
            logger.warning("Admin backup: lesson counters file is missing: %s", self.lesson_counters_path)
        if self.database_path is not None and self.database_path.exists():
            files.append(("bot.db", self.database_path))
        else:
            logger.warning("Admin backup: database file is missing: %s", self.database_path)

        if not files:
            logger.warning("Admin backup skipped: no files to send.")
            return

        from aiogram.types import FSInputFile

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            await self.broadcaster.telegram_bot.send_message(
                chat_id=self.admin_telegram_id,
                text=f"Автобекап за {timestamp}.",
            )
            for label, path in files:
                await self.broadcaster.telegram_bot.send_document(
                    chat_id=self.admin_telegram_id,
                    document=FSInputFile(path),
                    caption=label,
                )
        except Exception:
            logger.exception("Admin backup failed to deliver.")

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
        delay = self.request_delay_seconds + random.uniform(0, self.request_jitter_seconds)  # noqa: S311
        if delay <= 0:
            return
        logger.info("Task %s waits %.1f seconds before next source (%s).", job_name, delay, source_title)
        await asyncio.sleep(delay)

    async def _parse_source(self, source: SourceRow):
        if source["source_type"] in {"teacher", "audience"}:
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

        change_recorded = await self.db.record_change(
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
        if not change_recorded:
            logger.info(
                "Duplicate change for source %s and snapshot %s skipped.",
                source["source_title"],
                snapshot_hash,
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
            return
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

    async def _count_lessons_for_schedule_id(self, schedule_id: int) -> None:
        snapshot, snapshot_hash = await self.parser.parse(schedule_id)
        await self.db.save_snapshot(
            "current",
            snapshot_hash,
            snapshot,
            schedule_id=schedule_id,
            group_name=snapshot.group_name,
            source_type="group",
            source_key=f"group:{schedule_id}",
            source_title=snapshot.group_name,
            source_url=f"rasp:{schedule_id}",
        )
        await self.lesson_counter_service.count_today_for_snapshot(schedule_id, snapshot)

    async def start_auto_daily_lesson_counter_consumer(self) -> None:
        if not self.lesson_counters_enabled or self.lesson_counter_service is None:
            return
        if self.auto_daily_lesson_counter_broker is None or not self.auto_daily_lesson_counter_broker.enabled:
            return
        await self.auto_daily_lesson_counter_broker.start_consumer(self.handle_auto_daily_lesson_counter_job)

    async def enqueue_or_run_auto_daily_lesson_counter(self, target_date_offset: int = 0) -> None:
        from datetime import timedelta
        target_date = (datetime.now() + timedelta(days=target_date_offset)).date().isoformat()
        job = AutoDailyLessonCounterJob(target_date_iso=target_date)

        if self.auto_daily_lesson_counter_broker and self.auto_daily_lesson_counter_broker.enabled:
            published = await self.auto_daily_lesson_counter_broker.publish(job)
            if published:
                logger.info("Enqueued AutoDailyLessonCounterJob for %s to RabbitMQ", target_date)
                return

        logger.info("Executing AutoDailyLessonCounterJob locally for %s", target_date)
        await self.handle_auto_daily_lesson_counter_job(job)

    async def handle_auto_daily_lesson_counter_job(self, job: AutoDailyLessonCounterJob) -> None:
        if not self.lesson_counters_enabled or self.lesson_counter_service is None:
            return

        from collections import defaultdict
        target_date_iso = job.target_date_iso
        sources = await self.db.get_active_sources()
        group_sources = [s for s in sources if s.get("source_type") == "group" and s.get("schedule_id")]
        if not group_sources:
            return

        for source in group_sources:
            schedule_id = source["schedule_id"]
            group_name = str(source.get("group_name") or source.get("source_title") or f"Группа #{schedule_id}")

            # Idempotency check: 100% protection against duplicate incrementing
            if await self.db.is_daily_counter_processed(target_date_iso, group_name):
                logger.debug("Group %s for date %s already processed for lesson counters. Skipping.", group_name, target_date_iso)
                continue

            try:
                snapshot, _ = await self.parser.parse(schedule_id)
                day_item = next((day for day in snapshot.days if day.date_iso == target_date_iso), None)

                if day_item is not None and day_item.lessons:
                    # Count frequency of each lesson (subject, teacher) for +1, +2, +3 etc.
                    counts: dict[tuple[str, str], int] = defaultdict(int)
                    for lesson in day_item.lessons:
                        subj = lesson.subject.strip()
                        teach = lesson.teacher.strip()
                        if subj:
                            counts[(subj, teach)] += 1

                    for (subj, teach), cnt in counts.items():
                        self.lesson_counter_service.auto_increment_or_create_subject_in_json(
                            group_name=group_name,
                            schedule_id=schedule_id,
                            subject=subj,
                            teacher=teach,
                            count=cnt,
                        )

                # Mark as processed idempotently
                await self.db.mark_daily_counter_processed(target_date_iso, group_name)
                logger.info("Auto daily lesson counter: processed %s for %s", group_name, target_date_iso)
            except Exception as exc:
                logger.warning("Failed to auto-process daily lesson counters for group %s (%s): %s", group_name, target_date_iso, exc)

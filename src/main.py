from __future__ import annotations

import asyncio
import logging

import aio_pika
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.config import Settings
from src.db import Database
from src.db_migrations import apply_migrations
from src.group_catalog import GroupCatalog
from src.lesson_counters import LessonCounterService
from src.message_broker import (
    AutoDailyLessonCounterJobBroker,
    DatabaseCleanupJobBroker,
    LessonCounterJobBroker,
    RabbitMQBroker,
)
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.scheduler import ScheduleJobs
from src.schedule_search import ScheduleSearchCatalog
from src.telegram_bot import build_dispatcher
from src.vk_bot import build_vk_bot


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def start_background_task(name: str, coro) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)

    def _log_result(done_task: asyncio.Task) -> None:
        try:
            done_task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logging.exception("%s stopped unexpectedly.", name)

    task.add_done_callback(_log_result)
    return task


async def run_forever(name: str, runner, restart_delay_seconds: float = 15.0) -> None:
    while True:
        try:
            await runner()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("%s failed. Restarting in %.0f seconds.", name, restart_delay_seconds)
            await asyncio.sleep(restart_delay_seconds)
            continue
        logging.warning("%s stopped without exception. Restarting in %.0f seconds.", name, restart_delay_seconds)
        await asyncio.sleep(restart_delay_seconds)


def start_telegram_polling(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster,
    group_catalog: GroupCatalog,
    search_catalog: ScheduleSearchCatalog,
    schedule_jobs: ScheduleJobs | None = None,
) -> None:
    if not settings.telegram_bot_token:
        logging.warning("TELEGRAM_BOT_TOKEN не задан. Telegram-бот не будет запущен.")
        return

    async def _run_once() -> None:
        bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        broadcaster.telegram_bot = bot
        dispatcher = build_dispatcher(settings, db, parser, broadcaster, group_catalog, search_catalog, schedule_jobs)
        try:
            await dispatcher.start_polling(bot)
        finally:
            await bot.session.close()

    start_background_task("telegram-supervisor", run_forever("Telegram bot", _run_once))


def start_vk_polling(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster,
    group_catalog: GroupCatalog,
    search_catalog: ScheduleSearchCatalog,
    schedule_jobs: ScheduleJobs | None = None,
) -> None:
    if not settings.vk_bot_token:
        logging.warning("VK_BOT_TOKEN не задан. VK-бот не будет запущен.")
        return

    async def _run_once() -> None:
        vk_bot = build_vk_bot(settings, db, parser, broadcaster, group_catalog, search_catalog, schedule_jobs)
        if vk_bot is None:
            return
        broadcaster.vk_bot = vk_bot
        # vkbottle in this version expects to own the loop unless we mark it as already running.
        vk_bot.loop_wrapper.loop = asyncio.get_running_loop()
        vk_bot.loop_wrapper._running = True
        await vk_bot.run_polling()

    start_background_task("vk-supervisor", run_forever("VK bot", _run_once))


async def main() -> None:
    settings = Settings.from_env()
    if not settings.rabbitmq_url:
        logging.error("RABBITMQ_URL is not set. RabbitMQ consumers are disabled; direct delivery fallback remains available.")
    if not settings.telegram_bot_token and not settings.vk_bot_token:
        logging.error("Neither TELEGRAM_BOT_TOKEN nor VK_BOT_TOKEN is set. Bot polling is disabled, background jobs keep running.")

    apply_migrations(settings.database_path)
    db = Database(settings.database_path)
    await db.initialize()

    group_catalog = GroupCatalog(settings.schedule_url)
    await group_catalog.ensure_loaded()
    search_catalog = ScheduleSearchCatalog(settings.schedule_url, group_catalog)
    parser = ScheduleParser(settings.schedule_url)
    lesson_counter_service = LessonCounterService(db)
    if settings.lesson_counters_enabled:
        lesson_counter_config = await lesson_counter_service.load_config_file(settings.lesson_counters_path, group_catalog)
        await lesson_counter_service.sync_config(lesson_counter_config)
    broker = RabbitMQBroker(
        url=settings.rabbitmq_url,
        queue_name=settings.rabbitmq_queue,
        prefetch_count=settings.rabbitmq_prefetch_count,
    )
    lesson_counter_broker = LessonCounterJobBroker(
        url=settings.rabbitmq_url,
        queue_name=settings.lesson_counters_queue,
        prefetch_count=1,
    )
    db_cleanup_broker = DatabaseCleanupJobBroker(
        url=settings.rabbitmq_url,
        queue_name=settings.db_cleanup_queue,
        prefetch_count=1,
    )
    try:
        auto_daily_lesson_counter_broker = AutoDailyLessonCounterJobBroker(
            url=settings.rabbitmq_url,
            queue_name=settings.auto_daily_lesson_counter_queue,
            prefetch_count=1,
        )
    except (aio_pika.exceptions.AMQPError, ConnectionError, OSError) as exc:
        logging.error("Failed to initialize AutoDailyLessonCounterJobBroker: %s. Direct fallback will be used.", exc)
        auto_daily_lesson_counter_broker = None
    broadcaster = Broadcaster(
        db=db,
        telegram_bot=None,
        admin_telegram_id=settings.admin_telegram_id,
        admin_vk_id=settings.admin_vk_id,
        broker=broker,
    )
    jobs = ScheduleJobs(
        db=db,
        parser=parser,
        broadcaster=broadcaster,
        timezone=settings.app_timezone,
        request_delay_seconds=settings.schedule_request_delay_seconds,
        request_jitter_seconds=settings.schedule_request_jitter_seconds,
        lesson_counters_enabled=settings.lesson_counters_enabled,
        lesson_counter_service=lesson_counter_service,
        lesson_counter_broker=lesson_counter_broker,
        db_cleanup_broker=db_cleanup_broker,
        auto_daily_lesson_counter_broker=auto_daily_lesson_counter_broker,
        admin_backup_enabled=bool(settings.admin_telegram_id),
        admin_backup_interval_days=2,
        admin_telegram_id=settings.admin_telegram_id,
        lesson_counters_path=settings.lesson_counters_path,
        database_path=settings.database_path,
    )
    jobs.start()

    start_telegram_polling(settings, db, parser, broadcaster, group_catalog, search_catalog, jobs)
    start_vk_polling(settings, db, parser, broadcaster, group_catalog, search_catalog, jobs)

    try:
        await broadcaster.start()
    except (aio_pika.exceptions.AMQPError, ConnectionError, OSError):
        logging.exception("RabbitMQ consumer failed on startup. Direct delivery fallback remains available.")
    try:
        await jobs.start_lesson_counter_consumer()
    except (aio_pika.exceptions.AMQPError, ConnectionError, OSError):
        logging.exception("Lesson counter RabbitMQ consumer failed on startup. Scheduled direct fallback remains available.")
    try:
        await jobs.start_db_cleanup_consumer()
    except (aio_pika.exceptions.AMQPError, ConnectionError, OSError):
        logging.exception("Database cleanup RabbitMQ consumer failed on startup. Scheduled direct fallback remains available.")
    try:
        await jobs.start_auto_daily_lesson_counter_consumer()
    except (aio_pika.exceptions.AMQPError, ConnectionError, OSError):
        logging.exception("Auto daily lesson counter RabbitMQ consumer failed on startup. Scheduled direct fallback remains available.")
    try:
        await jobs.sync_current_snapshot()
    except Exception:
        logging.exception("Initial schedule sync failed. Background scheduler will retry later.")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

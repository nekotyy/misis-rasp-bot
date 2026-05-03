from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.config import Settings
from src.db import Database
from src.db_migrations import apply_migrations
from src.group_catalog import GroupCatalog
from src.lesson_counters import LessonCounterService
from src.message_broker import LessonCounterJobBroker, RabbitMQBroker
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


async def run_telegram_polling(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster,
    group_catalog: GroupCatalog,
    search_catalog: ScheduleSearchCatalog,
) -> Bot | None:
    if not settings.telegram_bot_token:
        logging.warning("TELEGRAM_BOT_TOKEN не задан. Telegram-бот не будет запущен.")
        return None
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    broadcaster.telegram_bot = bot
    dispatcher = build_dispatcher(settings, db, parser, broadcaster, group_catalog, search_catalog)
    asyncio.create_task(dispatcher.start_polling(bot))
    return bot


async def run_vk_polling(vk_bot) -> None:
    if vk_bot is None:
        logging.warning("VK_BOT_TOKEN не задан. VK-бот не будет запущен.")
        return

    async def _run_vk() -> None:
        # vkbottle in this version expects to own the loop unless we mark it as already running.
        vk_bot.loop_wrapper.loop = asyncio.get_running_loop()
        vk_bot.loop_wrapper._running = True
        await vk_bot.run_polling()

    asyncio.create_task(_run_vk())


async def main() -> None:
    settings = Settings.from_env()
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
    broadcaster = Broadcaster(
        db=db,
        telegram_bot=None,
        admin_telegram_id=settings.admin_telegram_id,
        admin_vk_id=settings.admin_vk_id,
        broker=broker,
    )
    telegram_bot = await run_telegram_polling(settings, db, parser, broadcaster, group_catalog, search_catalog)
    vk_bot = build_vk_bot(settings, db, parser, broadcaster, group_catalog, search_catalog)
    await run_vk_polling(vk_bot)

    broadcaster.telegram_bot = telegram_bot
    broadcaster.vk_bot = vk_bot
    await broadcaster.start()
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
    )
    jobs.start()
    await jobs.start_lesson_counter_consumer()
    await jobs.sync_current_snapshot()

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.config import Settings
from src.db import Database
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.scheduler import ScheduleJobs
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
) -> Bot | None:
    if not settings.telegram_bot_token:
        logging.warning("TELEGRAM_BOT_TOKEN не задан. Telegram-бот не будет запущен.")
        return None
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    broadcaster.telegram_bot = bot
    dispatcher = build_dispatcher(settings, db, parser, broadcaster)
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
    db = Database(settings.database_path)
    await db.initialize()

    parser = ScheduleParser(settings.schedule_url)
    broadcaster = Broadcaster(db=db, telegram_bot=None)
    telegram_bot = await run_telegram_polling(settings, db, parser, broadcaster)
    vk_bot = build_vk_bot(settings, db, parser)
    await run_vk_polling(vk_bot)

    broadcaster.telegram_bot = telegram_bot
    broadcaster.vk_bot = vk_bot
    jobs = ScheduleJobs(db=db, parser=parser, broadcaster=broadcaster, timezone=settings.app_timezone)
    jobs.start()
    await jobs.sync_current_snapshot()

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

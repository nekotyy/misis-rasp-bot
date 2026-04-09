from __future__ import annotations

from aiohttp import TCPConnector
from vkbottle import API
from vkbottle import Keyboard, Text
from vkbottle.http import AiohttpClient
from vkbottle.bot import Bot, Message

from src.config import Settings
from src.db import Database
from src.schedule_service import ScheduleFormatter, get_day_by_offset_from_content


def main_keyboard() -> Keyboard:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("Сегодня"))
    keyboard.add(Text("Завтра"))
    keyboard.row()
    keyboard.add(Text("2 дня"))
    return keyboard


def build_vk_bot(settings: Settings, db: Database, parser) -> Bot | None:
    if not settings.vk_bot_token:
        return None

    api = None
    if settings.vk_disable_ssl_verify:
        api = API(
            settings.vk_bot_token,
            http_client=AiohttpClient(connector=TCPConnector(ssl=False)),
        )

    bot = Bot(token=settings.vk_bot_token, api=api)

    async def register_user(message: Message) -> None:
        if message.from_id is None:
            return
        await db.upsert_user(
            platform="vk",
            user_id=message.from_id,
            username=None,
            full_name=None,
            is_admin=False,
        )

    async def get_saved_snapshot() -> dict | None:
        return await db.get_latest_snapshot("current")

    def empty_day_text(label: str) -> str:
        return f"Расписание на {label}\n\nПар нет."

    @bot.on.message(text=["/start", "start", "Начать"])
    async def start_handler(message: Message) -> None:
        await register_user(message)
        await message.answer(
            f"Расписание группы {settings.group_name}\n\nВыбери день кнопками ниже.",
            keyboard=main_keyboard().get_json(),
        )

    @bot.on.message(text="Сегодня")
    async def today_handler(message: Message) -> None:
        await register_user(message)
        snapshot_row = await get_saved_snapshot()
        if snapshot_row is None:
            await message.answer("Сохраненное расписание пока отсутствует.", keyboard=main_keyboard().get_json())
            return
        day = get_day_by_offset_from_content(snapshot_row["content"], 0)
        text = ScheduleFormatter.format_day(day) if day else empty_day_text("сегодня")
        await message.answer(text, keyboard=main_keyboard().get_json())

    @bot.on.message(text="Завтра")
    async def tomorrow_handler(message: Message) -> None:
        await register_user(message)
        snapshot_row = await get_saved_snapshot()
        if snapshot_row is None:
            await message.answer("Сохраненное расписание пока отсутствует.", keyboard=main_keyboard().get_json())
            return
        day = get_day_by_offset_from_content(snapshot_row["content"], 1)
        text = ScheduleFormatter.format_day(day) if day else empty_day_text("завтра")
        await message.answer(text, keyboard=main_keyboard().get_json())

    @bot.on.message(text="2 дня")
    async def two_days_handler(message: Message) -> None:
        await register_user(message)
        snapshot_row = await get_saved_snapshot()
        if snapshot_row is None:
            await message.answer("Сохраненное расписание пока отсутствует.", keyboard=main_keyboard().get_json())
            return
        day = get_day_by_offset_from_content(snapshot_row["content"], 2)
        text = ScheduleFormatter.format_day(day) if day else empty_day_text("послезавтра")
        await message.answer(text, keyboard=main_keyboard().get_json())

    @bot.on.message(text="Админка")
    async def admin_handler(message: Message) -> None:
        await register_user(message)
        await message.answer("Админка в этой версии доступна в Telegram.", keyboard=main_keyboard().get_json())

    return bot

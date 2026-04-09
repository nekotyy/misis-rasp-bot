from __future__ import annotations

from html import escape

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.config import Settings
from src.db import Database
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.schedule_service import ScheduleFormatter, get_day_by_offset, get_day_by_offset_from_content


MAIN_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Расписание на сегодня", callback_data="schedule:today")],
        [InlineKeyboardButton(text="Расписание на завтра", callback_data="schedule:tomorrow")],
        [InlineKeyboardButton(text="Расписание на 2 дня", callback_data="schedule:day_after")],
    ]
)

ADMIN_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Статус", callback_data="admin:status"),
            InlineKeyboardButton(text="Перепарсить", callback_data="admin:refresh"),
        ],
        [
            InlineKeyboardButton(text="Пользователи", callback_data="admin:users"),
            InlineKeyboardButton(text="Тестовая рассылка", callback_data="admin:test"),
        ],
        [InlineKeyboardButton(text="Последнее изменение", callback_data="admin:last_change")],
        [InlineKeyboardButton(text="Закрыть админку", callback_data="admin:close")],
    ]
)


def build_dispatcher(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()

    async def register_message_user(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or None
        is_admin = bool(settings.admin_telegram_id and user.id == settings.admin_telegram_id)
        await db.upsert_user(
            platform="telegram",
            user_id=user.id,
            username=user.username,
            full_name=full_name,
            is_admin=is_admin,
        )

    async def register_callback_user(callback: CallbackQuery) -> None:
        user = callback.from_user
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or None
        await db.upsert_user(
            platform="telegram",
            user_id=user.id,
            username=user.username,
            full_name=full_name,
            is_admin=user_is_admin(user.id),
        )

    def user_is_admin(user_id: int | None) -> bool:
        return bool(user_id and settings.admin_telegram_id and user_id == settings.admin_telegram_id)

    async def get_saved_snapshot() -> dict | None:
        return await db.get_latest_snapshot("current")

    def format_welcome() -> str:
        return (
            f"<b>Расписание группы {escape(settings.group_name)}</b>\n\n"
            "Используй кнопки под сообщением, чтобы открыть сохраненное расписание."
        )

    def format_admin_panel() -> str:
        return (
            "<b>Админ-панель</b>\n\n"
            "Здесь можно вручную перепарсить сайт, проверить состояние бота и посмотреть пользователей."
        )

    def empty_day_text(label: str) -> str:
        return f"Расписание на {escape(label)}\n\nПар нет."

    @dispatcher.message(CommandStart())
    async def handle_start(message: Message) -> None:
        await register_message_user(message)
        await message.answer(format_welcome(), reply_markup=MAIN_KEYBOARD)

    @dispatcher.message(Command("admin"))
    async def handle_admin_command(message: Message) -> None:
        await register_message_user(message)
        if not user_is_admin(message.from_user.id if message.from_user else None):
            await message.answer("Команда доступна только администратору.")
            await message.answer(format_welcome(), reply_markup=MAIN_KEYBOARD)
            return
        await message.answer(format_admin_panel(), reply_markup=ADMIN_KEYBOARD)

    @dispatcher.callback_query(F.data.startswith("schedule:"))
    async def handle_schedule_callback(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if callback.message is None:
            await callback.answer()
            return

        snapshot_row = await get_saved_snapshot()
        if snapshot_row is None:
            await callback.message.edit_text(
                "Сохраненное расписание пока отсутствует. Подожди первую автоматическую синхронизацию.",
                reply_markup=MAIN_KEYBOARD,
            )
            await callback.answer()
            return

        action = callback.data.split(":", 1)[1]
        if action == "today":
            day = get_day_by_offset_from_content(snapshot_row["content"], 0)
            text = ScheduleFormatter.format_day_card(day, "сегодня") if day else empty_day_text("сегодня")
        elif action == "tomorrow":
            day = get_day_by_offset_from_content(snapshot_row["content"], 1)
            text = ScheduleFormatter.format_day_card(day, "завтра") if day else empty_day_text("завтра")
        else:
            day = get_day_by_offset_from_content(snapshot_row["content"], 2)
            text = ScheduleFormatter.format_day_card(day, "2 дня") if day else empty_day_text("2 дня")

        await callback.message.edit_text(text, reply_markup=MAIN_KEYBOARD)
        await callback.answer()

    @dispatcher.callback_query(F.data.startswith("admin:"))
    async def handle_admin_callback(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not user_is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return

        action = callback.data.split(":", 1)[1]
        if action == "close":
            await callback.message.edit_text(format_welcome(), reply_markup=MAIN_KEYBOARD)
            await callback.answer()
            return

        if action == "status":
            users = await db.list_users()
            last_change = await db.get_last_change()
            last_change_at = escape(last_change["created_at"]) if last_change else "пока не было"
            text = (
                "<b>Статус бота</b>\n\n"
                f"Группа: <b>{escape(settings.group_name)}</b>\n"
                f"Пользователей: <b>{len(users)}</b>\n"
                f"Последнее изменение: <b>{last_change_at}</b>"
            )
        elif action == "users":
            users = await db.list_users()
            if not users:
                text = "<b>Пользователи</b>\n\nПока никто не зарегистрирован."
            else:
                lines = ["<b>Пользователи</b>", ""]
                for user in users:
                    display = user.full_name or user.username or "Без имени"
                    lines.append(f"- {escape(user.platform)} | {escape(display)} | <b>{user.user_id}</b>")
                text = "\n".join(lines)
        elif action == "last_change":
            last_change = await db.get_last_change()
            if not last_change:
                text = "<b>Последнее изменение</b>\n\nИзменений пока не было."
            else:
                text = (
                    "<b>Последнее изменение</b>\n\n"
                    f"<b>{escape(last_change['created_at'])}</b>\n\n"
                    f"{escape(last_change['message'])}"
                )
        elif action == "refresh":
            snapshot, snapshot_hash = await parser.parse()
            await db.save_snapshot("current", snapshot_hash, snapshot)
            day = get_day_by_offset(snapshot, 0)
            preview = ScheduleFormatter.format_day_card(day, "сегодня") if day else empty_day_text("сегодня")
            text = "<b>Расписание перепарсено</b>\n\n" + preview
        else:
            if broadcaster is not None:
                await broadcaster.broadcast_test_message()
            text = "<b>Тестовая рассылка</b>\n\nСообщение отправлено всем зарегистрированным пользователям."

        await callback.message.edit_text(text, reply_markup=ADMIN_KEYBOARD)
        await callback.answer()

    @dispatcher.message()
    async def handle_fallback(message: Message) -> None:
        await register_message_user(message)
        await message.answer(
            "Используй кнопки под сообщением, чтобы открыть сохраненное расписание.",
            reply_markup=MAIN_KEYBOARD,
        )

    return dispatcher

from __future__ import annotations

from collections import defaultdict
import asyncio
from datetime import datetime
from html import escape
from traceback import format_exception

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, PhotoSize, Video

from src.attachment_storage import AttachmentStorage
from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
from src.homework_service import SUBJECTS, format_homework_message, format_homework_notification, format_homework_preview, get_subject
from src.models import HomeworkAttachment, HomeworkDraft
from src.notifier import Broadcaster
from src.parser import ScheduleParser
from src.schedule_search import ScheduleSearchCatalog
from src.schedule_service import ScheduleFormatter, get_day_by_offset, get_day_by_offset_from_content

HOMEWORK_GROUP_NAME = "ИСП-25-1"
HOMEWORK_SCHEDULE_ID = 600
SUPPORT_CONTACT = "tg: @nekoty vk: vk.com/nekoteevich"


SCHEDULE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Расписание на сегодня", callback_data="schedule:today")],
        [InlineKeyboardButton(text="Расписание на завтра", callback_data="schedule:tomorrow")],
        [InlineKeyboardButton(text="Расписание на 2 дня", callback_data="schedule:day_after")],
        [InlineKeyboardButton(text="Найти расписание", callback_data="schedule:find")],
        [InlineKeyboardButton(text="Назад", callback_data="menu:start")],
    ]
)

HOMEWORK_BACK_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Вернуться к списку ДЗ", callback_data="menu:homework")],
    ]
)

START_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Узнать расписание", callback_data="start:rasp")],
        [InlineKeyboardButton(text="Узнать ДЗ", callback_data="start:homework")],
        [InlineKeyboardButton(text="Настройки", callback_data="menu:settings")],
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
            InlineKeyboardButton(text="Редакторы", callback_data="admin:editors"),
        ],
        [
            InlineKeyboardButton(text="Тестовая рассылка", callback_data="admin:test"),
            InlineKeyboardButton(text="Последнее изменение", callback_data="admin:last_change"),
        ],
        [InlineKeyboardButton(text="Закрыть админку", callback_data="admin:close")],
    ]
)

ADMIN_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Статус", callback_data="admin:status"),
            InlineKeyboardButton(text="Перепарсить", callback_data="admin:refresh"),
        ],
        [
            InlineKeyboardButton(text="Сохранить эталон", callback_data="admin:baseline"),
            InlineKeyboardButton(text="Последнее изменение", callback_data="admin:last_change"),
        ],
        [
            InlineKeyboardButton(text="Пользователи", callback_data="admin:users"),
            InlineKeyboardButton(text="Редакторы", callback_data="admin:editors"),
        ],
        [
            InlineKeyboardButton(text="Удалить ДЗ", callback_data="admin:homework_delete"),
            InlineKeyboardButton(text="Тестовая рассылка", callback_data="admin:test"),
        ],
        [InlineKeyboardButton(text="Закрыть админку", callback_data="admin:close")],
    ]
)

def build_dispatcher(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster | None = None,
    attachment_storage: AttachmentStorage | None = None,
    group_catalog: GroupCatalog | None = None,
    search_catalog: ScheduleSearchCatalog | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()
    homework_drafts: dict[int, HomeworkDraft] = {}
    context_messages: dict[int, dict[str, list[int]]] = defaultdict(dict)
    search_results: dict[int, dict[str, object]] = {}
    awaiting_schedule_search: set[int] = set()

    def build_homework_subjects_keyboard(mode: str) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for subject in SUBJECTS:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=subject["subject"],
                        callback_data=(
                            f"homework:view:{subject['key']}"
                            if mode == "homework"
                            else f"dz:subject:{subject['key']}"
                        ),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:start")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def build_editors_keyboard(users: list) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for user in users:
            if user.platform != "telegram":
                continue
            display = user.full_name or user.username or str(user.user_id)
            prefix = "Убрать редактора" if user.is_editor else "Сделать редактором"
            rows.append(
                [InlineKeyboardButton(text=f"{prefix}: {display}", callback_data=f"editor:toggle:{user.user_id}")]
            )
        rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin:back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def build_homework_preview_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Добавить вложения", callback_data="dz:add_attachments")],
                [InlineKeyboardButton(text="Опубликовать", callback_data="dz:save")],
                [InlineKeyboardButton(text="Отменить", callback_data="dz:cancel")],
            ]
        )

    def build_homework_attachment_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Опубликовать", callback_data="dz:save")],
                [InlineKeyboardButton(text="Отменить", callback_data="dz:cancel")],
            ]
        )

    def build_admin_homework_subjects_keyboard() -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(text=subject["subject"], callback_data=f"admin:hw_subject:{subject['key']}")]
            for subject in SUBJECTS
        ]
        rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin:back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def build_admin_homework_entries_keyboard(subject_key: str, entries: list[dict]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for entry in entries:
            preview = entry["text"].strip().replace("\n", " ")
            if len(preview) > 24:
                preview = f"{preview[:24].rstrip()}..."
            label = f"Удалить #{entry['id']} {preview or 'без текста'}"
            rows.append([InlineKeyboardButton(text=label, callback_data=f"admin:hw_delete:{entry['id']}:{subject_key}")])
        rows.append([InlineKeyboardButton(text="Назад к предметам", callback_data="admin:homework_delete")])
        rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin:back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def register_message_user(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or None
        is_admin = bool(settings.admin_telegram_id and user.id == settings.admin_telegram_id)
        existing = await db.get_user("telegram", user.id)
        await db.upsert_user(
            platform="telegram",
            user_id=user.id,
            username=user.username,
            full_name=full_name,
            group_name=existing.group_name if existing else None,
            schedule_id=existing.schedule_id if existing else None,
            is_admin=is_admin,
            is_editor=existing.is_editor if existing else False,
        )

    async def register_callback_user(callback: CallbackQuery) -> None:
        user = callback.from_user
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or None
        existing = await db.get_user("telegram", user.id)
        await db.upsert_user(
            platform="telegram",
            user_id=user.id,
            username=user.username,
            full_name=full_name,
            group_name=existing.group_name if existing else None,
            schedule_id=existing.schedule_id if existing else None,
            is_admin=user_is_admin(user.id),
            is_editor=existing.is_editor if existing else False,
        )

    def user_is_admin(user_id: int | None) -> bool:
        return bool(user_id and settings.admin_telegram_id and user_id == settings.admin_telegram_id)

    async def user_is_editor(user_id: int | None) -> bool:
        if user_id is None:
            return False
        user = await db.get_user("telegram", user_id)
        return bool(user and user.is_editor)

    async def get_user_record(user_id: int | None):
        if user_id is None:
            return None
        return await db.get_user("telegram", user_id)

    async def user_has_homework_access(user_id: int | None) -> bool:
        user = await get_user_record(user_id)
        return bool(user and user.schedule_id == HOMEWORK_SCHEDULE_ID)

    async def get_saved_snapshot(user_id: int | None) -> dict | None:
        user = await get_user_record(user_id)
        if user is None or user.schedule_id is None:
            return None
        snapshot = await db.get_latest_snapshot("current", user.schedule_id)
        if snapshot is not None:
            return snapshot
        snapshot_obj, snapshot_hash = await parser.parse(user.schedule_id)
        await db.save_snapshot("current", snapshot_hash, snapshot_obj, user.schedule_id, user.group_name or snapshot_obj.group_name)
        return await db.get_latest_snapshot("current", user.schedule_id)

    def format_group_prompt(error_text: str | None = None) -> str:
        lines = [
            "<b>Укажи свою группу</b>",
            "",
            "Напиши ее в формате, как на сайте колледжа.",
            "Например: <b>ИСП-25-1</b>",
            "",
            "Регистр не важен.",
        ]
        if error_text:
            lines.extend(["", error_text])
        return "\n".join(lines)

    def format_search_prompt(error_text: str | None = None) -> str:
        lines = [
            "<b>Поиск расписания</b>",
            "",
            "Поиск осуществляется по группам, преподавателям и аудиториям!",
            "",
            "Напиши группу, фамилию преподавателя или аудиторию.",
        ]
        if error_text:
            lines.extend(["", error_text])
        return "\n".join(lines)

    def build_search_result_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Найти расписание", callback_data="schedule:find")],
                [InlineKeyboardButton(text="Назад", callback_data="menu:start")],
            ]
        )

    def format_welcome(group_name: str | None, is_editor: bool = False) -> str:
        lines = ["<b>Привет! Я бот расписания колледжа</b>"]
        if group_name:
            lines.extend(["", f"Твоя группа: <b>{escape(group_name)}</b>"])
        lines.extend(
            [
                "",
                "/rasp — посмотреть расписание",
                "/homework — посмотреть домашние задания",
                "/settings — настройки",
            ]
        )
        return "\n".join(lines)

    async def format_settings_text(user_id: int) -> str:
        user = await db.get_user("telegram", user_id)
        notifications = "включены" if (user.homework_notifications_enabled if user else True) else "выключены"
        lines = [
            "<b>Настройки</b>",
            "",
            f"Группа: <b>{escape(user.group_name) if user and user.group_name else 'не выбрана'}</b>",
            f"Уведомления о новом ДЗ: <b>{notifications}</b>",
        ]
        return "\n".join(lines)

    async def build_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
        user = await db.get_user("telegram", user_id)
        toggle_label = (
            "Выключить уведомления о ДЗ"
            if (user.homework_notifications_enabled if user else True)
            else "Включить уведомления о ДЗ"
        )
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text=toggle_label, callback_data="settings:toggle_hw")],
        ]
        if user and user.group_name:
            rows.append([InlineKeyboardButton(text="Отписаться от группы", callback_data="settings:clear_group")])
        rows.extend([
            [InlineKeyboardButton(text="Назад", callback_data="menu:start")],
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def format_admin_panel() -> str:
        return (
            "<b>Админ-панель</b>\n\n"
            "Здесь можно перепарсить сайт, сохранить эталон для сравнения, посмотреть статистику и управлять домашними заданиями."
        )

    def build_admin_users_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="<", callback_data=f"admin:users:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text=">", callback_data=f"admin:users:{page + 1}"))

        rows: list[list[InlineKeyboardButton]] = []
        if nav_row:
            rows.append(nav_row)
        rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin:back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def empty_day_text(label: str) -> str:
        return f"Расписание на {escape(label)}\n\nПар нет."

    def format_snapshot_info(title: str, snapshot_row: dict | None) -> str:
        if snapshot_row is None:
            return f"{title}: <b>еще не было</b>"
        return (
            f"{title}: <b>{escape(snapshot_row['created_at'])}</b>\n"
            f"Сайт отдал данные: <b>{escape(snapshot_row['fetched_at'])}</b>"
        )

    async def format_admin_status() -> str:
        users = await db.list_users()
        active_groups = await db.get_active_groups()
        homework_count = await db.count_homework_entries()
        last_change = await db.get_last_change()
        current_snapshot = await db.get_latest_snapshot("current")
        baseline_snapshot = await db.get_latest_snapshot("daily_baseline")
        editor_count = sum(1 for user in users if user.is_editor)
        last_change_at = escape(last_change["created_at"]) if last_change else "еще не было"
        return (
            "<b>Статус бота</b>\n\n"
            f"Пользователей: <b>{len(users)}</b>\n"
            f"Активных групп: <b>{len(active_groups)}</b>\n"
            f"Редакторов: <b>{editor_count}</b>\n"
            f"Записей ДЗ: <b>{homework_count}</b>\n"
            f"Последнее изменение: <b>{last_change_at}</b>\n\n"
            f"{format_snapshot_info('Последний обычный парс', current_snapshot)}\n\n"
            f"{format_snapshot_info('Последний сохраненный эталон', baseline_snapshot)}"
        )

    def format_group_action_report(title: str, rows: list[tuple[str, str, str]]) -> str:
        lines = [f"<b>{title}</b>", ""]
        if not rows:
            lines.append("Нет записей.")
            return "\n".join(lines)
        for group_name, action_time, action_name in rows:
            lines.append(
                f"{escape(group_name)} | <b>{escape(action_time)}</b> | {escape(action_name)}"
            )
        return "\n".join(lines)

    async def refresh_all_active_groups() -> list[tuple[str, str, str]]:
        groups = await db.get_active_groups()
        if not groups:
            return []

        rows: list[tuple[str, str, str]] = []
        for index, group in enumerate(groups):
            snapshot, snapshot_hash = await parser.parse(group["schedule_id"])
            await db.save_snapshot("current", snapshot_hash, snapshot, group["schedule_id"], group["group_name"])
            rows.append((group["group_name"], snapshot.fetched_at.strftime("%Y-%m-%d %H:%M"), "перепарсено"))
        return rows

    async def save_baseline_for_all_active_groups() -> list[tuple[str, str, str]]:
        groups = await db.get_active_groups()
        if not groups:
            return []

        rows: list[tuple[str, str, str]] = []
        for group in groups:
            snapshot, snapshot_hash = await parser.parse(group["schedule_id"])
            await db.save_snapshot("daily_baseline", snapshot_hash, snapshot, group["schedule_id"], group["group_name"])
            rows.append((group["group_name"], snapshot.fetched_at.strftime("%Y-%m-%d %H:%M"), "эталон сохранен"))
        return rows

    async def replace_context_message(
        bot: Bot,
        chat_id: int,
        context: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        message_ids = context_messages[chat_id].get(context, [])
        if message_ids:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_ids[0],
                    text=text,
                    reply_markup=reply_markup,
                )
                for extra_id in message_ids[1:]:
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=extra_id)
                    except TelegramBadRequest:
                        pass
                context_messages[chat_id][context] = [message_ids[0]]
                return
            except TelegramBadRequest:
                pass
        await clear_context_messages(bot, chat_id, context)
        sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
        context_messages[chat_id][context] = [sent.message_id]

    async def send_new_context_message(
        bot: Bot,
        chat_id: int,
        context: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        await clear_context_messages(bot, chat_id, context)
        sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
        context_messages[chat_id][context] = [sent.message_id]

    async def clear_context_messages(bot: Bot, chat_id: int, context: str) -> None:
        for message_id in context_messages[chat_id].get(context, []):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramBadRequest:
                pass
        context_messages[chat_id][context] = []

    async def try_delete_message(message: Message | None) -> None:
        if message is None:
            return
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

    async def clear_context_messages_except(bot: Bot, chat_id: int, context: str, keep_message_id: int) -> None:
        kept_ids: list[int] = []
        for message_id in context_messages[chat_id].get(context, []):
            if message_id == keep_message_id:
                kept_ids.append(message_id)
                continue
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramBadRequest:
                pass
        context_messages[chat_id][context] = kept_ids

    async def try_edit_source_message(
        source_message: Message | None,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        if source_message is None:
            return False
        try:
            await source_message.edit_text(text, reply_markup=reply_markup)
            context_messages[source_message.chat.id]["homework"] = [source_message.message_id]
            return True
        except TelegramBadRequest:
            return False

    async def safe_callback_answer(callback: CallbackQuery, *args, **kwargs) -> None:
        retries = 3
        for attempt in range(1, retries + 1):
            try:
                await callback.answer(*args, **kwargs)
                return
            except TelegramBadRequest:
                return
            except TelegramNetworkError:
                if attempt >= retries:
                    return
                await asyncio.sleep(0.5 * attempt)

    async def safe_edit_message_text(
        message: Message | None,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        if message is None:
            return False
        retries = 3
        for attempt in range(1, retries + 1):
            try:
                await message.edit_text(text, reply_markup=reply_markup)
                return True
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return True
                return False
            except TelegramNetworkError:
                if attempt >= retries:
                    return False
                await asyncio.sleep(0.5 * attempt)
        return False

    def short_error_text(error: Exception) -> str:
        text = f"{type(error).__name__}: {error}"
        if len(text) > 350:
            text = f"{text[:347]}..."
        return text

    async def notify_user_about_error(bot: Bot, chat_id: int, error: Exception) -> None:
        try:
            await bot.send_message(
                chat_id,
                (
                    "<b>Произошла ошибка при обработке запроса.</b>\n\n"
                    f"Ошибка: <code>{escape(short_error_text(error))}</code>\n\n"
                    f"Напиши мне для решения: {SUPPORT_CONTACT}"
                ),
            )
        except TelegramBadRequest:
            return

    async def notify_admin_about_error(platform: str, user_id: int | None, chat_id: int | None, error: Exception) -> None:
        if broadcaster is None:
            return
        traceback_text = "".join(format_exception(type(error), error, error.__traceback__))
        if len(traceback_text) > 2500:
            traceback_text = f"...{traceback_text[-2500:]}"
        telegram_text = (
            f"<b>Сбой в боте ({escape(platform)})</b>\n\n"
            f"Пользователь: <b>{user_id if user_id is not None else 'неизвестно'}</b>\n"
            f"Чат: <b>{chat_id if chat_id is not None else 'неизвестно'}</b>\n"
            f"Ошибка: <code>{escape(short_error_text(error))}</code>\n\n"
            f"<pre>{escape(traceback_text)}</pre>"
        )
        vk_text = (
            f"Сбой в боте ({platform})\n\n"
            f"Пользователь: {user_id if user_id is not None else 'неизвестно'}\n"
            f"Чат: {chat_id if chat_id is not None else 'неизвестно'}\n"
            f"Ошибка: {short_error_text(error)}\n\n"
            f"{traceback_text}"
        )
        await broadcaster.notify_admins(telegram_text, vk_text)

    def extract_error_context(event: ErrorEvent) -> tuple[int | None, int | None]:
        update = event.update
        if update.message is not None:
            return update.message.from_user.id if update.message.from_user else None, update.message.chat.id
        if update.callback_query is not None:
            callback = update.callback_query
            return callback.from_user.id if callback.from_user else None, callback.message.chat.id if callback.message else None
        return None, None

    async def prompt_group_selection(bot: Bot, chat_id: int, error_text: str | None = None) -> None:
        await send_new_context_message(
            bot,
            chat_id,
            "group_select",
            format_group_prompt(error_text),
        )

    async def ensure_group_selected(bot: Bot, chat_id: int, user_id: int | None) -> bool:
        user = await get_user_record(user_id)
        if user is not None and user.schedule_id is not None and user.group_name:
            return True
        await prompt_group_selection(bot, chat_id)
        return False

    async def prompt_schedule_search(bot: Bot, chat_id: int, user_id: int, error_text: str | None = None) -> None:
        awaiting_schedule_search.add(user_id)
        await replace_context_message(bot, chat_id, "schedule", format_search_prompt(error_text))

    async def perform_schedule_search(bot: Bot, chat_id: int, user_id: int, query: str) -> bool:
        if search_catalog is None:
            await bot.send_message(chat_id, "Поиск временно недоступен.")
            return False
        try:
            target = await search_catalog.find(query)
        except httpx.HTTPError:
            await replace_context_message(
                bot,
                chat_id,
                "schedule",
                format_search_prompt("Сайт расписания временно недоступен. Попробуй еще раз через минуту."),
            )
            return False
        if target is None:
            await bot.send_message(chat_id, "Ничего не найдено. Проверь запрос и попробуй еще раз.")
            return False
        try:
            snapshot_obj, _ = await parser.parse_from_url(target.url)
        except httpx.HTTPError:
            await replace_context_message(
                bot,
                chat_id,
                "schedule",
                format_search_prompt("Сайт расписания временно недоступен. Попробуй еще раз через минуту."),
            )
            return False
        snapshot = {
            "title": target.title,
            "kind": target.kind,
            "content": {
                "group_name": snapshot_obj.group_name,
                "fetched_at": snapshot_obj.fetched_at.isoformat(timespec="seconds"),
                "days": [
                    {
                        "date_label": day.date_label,
                        "date_iso": day.date_iso,
                        "lessons": [
                            {
                                "number": lesson.number,
                                "subject": lesson.subject,
                                "teacher": lesson.teacher,
                                "classroom": lesson.classroom,
                            }
                            for lesson in day.lessons
                        ],
                    }
                    for day in snapshot_obj.days
                ],
            },
        }
        search_results[user_id] = snapshot
        awaiting_schedule_search.discard(user_id)
        await replace_context_message(
            bot,
            chat_id,
            "schedule",
            ScheduleFormatter.format_search_snapshot(target.title, snapshot["content"]),
            reply_markup=build_search_result_keyboard(),
        )
        return True

    async def handle_group_input(bot: Bot, chat_id: int, user_id: int, raw_text: str) -> bool:
        if group_catalog is None:
            await prompt_group_selection(bot, chat_id, "Справочник групп пока недоступен. Попробуй позже.")
            return False
        group = await group_catalog.find_group(raw_text)
        if group is None:
            await prompt_group_selection(bot, chat_id, "Группа не найдена. Проверь написание и попробуй еще раз.")
            return False
        await db.set_user_group("telegram", user_id, group.group_name, group.schedule_id)
        editor = await user_is_editor(user_id)
        await clear_context_messages(bot, chat_id, "group_select")
        await send_new_context_message(
            bot,
            chat_id,
            "menu",
            format_welcome(group.group_name, is_editor=editor),
            reply_markup=START_KEYBOARD,
        )
        return True

    async def send_schedule_menu(bot: Bot, chat_id: int) -> None:
        await send_new_context_message(
            bot,
            chat_id,
            "schedule",
            "Выбери нужный вариант расписания.",
            reply_markup=SCHEDULE_KEYBOARD,
        )

    async def send_homework_subject_picker(bot: Bot, chat_id: int, mode: str) -> None:
        text = (
            "<b>Выбери предмет</b>\n\nПосле выбора я покажу домашние задания."
            if mode == "homework"
            else "<b>Выбери предмет для нового домашнего задания</b>"
        )
        await send_new_context_message(
            bot,
            chat_id,
            "homework" if mode == "homework" else "dz",
            text,
            reply_markup=build_homework_subjects_keyboard(mode),
        )

    async def send_homework_entries(
        bot: Bot,
        chat_id: int,
        subject_key: str,
        source_message: Message | None = None,
    ) -> None:
        subject = get_subject(subject_key)
        if subject is None:
            if source_message is not None:
                await source_message.edit_text("Предмет не найден.", reply_markup=HOMEWORK_BACK_KEYBOARD)
                context_messages[chat_id]["homework"] = [source_message.message_id]
            else:
                await replace_context_message(
                    bot,
                    chat_id,
                    "homework",
                    "Предмет не найден.",
                    reply_markup=HOMEWORK_BACK_KEYBOARD,
                )
            return

        entries = await db.get_homework_for_subject(subject_key)
        if not entries:
            if source_message is not None:
                await clear_context_messages_except(bot, chat_id, "homework", source_message.message_id)
                if await try_edit_source_message(
                    source_message,
                    f"По предмету <b>{escape(subject['subject'])}</b> пока нет домашних заданий.",
                    reply_markup=HOMEWORK_BACK_KEYBOARD,
                ):
                    return
            else:
                await replace_context_message(
                    bot,
                    chat_id,
                    "homework",
                    f"По предмету <b>{escape(subject['subject'])}</b> пока нет домашних заданий.",
                    reply_markup=HOMEWORK_BACK_KEYBOARD,
                )
                return
            await replace_context_message(
                bot,
                chat_id,
                "homework",
                f"По предмету <b>{escape(subject['subject'])}</b> пока нет домашних заданий.",
                reply_markup=HOMEWORK_BACK_KEYBOARD,
            )
            return

        if source_message is not None:
            try:
                await source_message.delete()
            except TelegramBadRequest:
                pass
        await clear_context_messages(bot, chat_id, "homework")

        latest_entry = entries[0]
        context_messages[chat_id]["homework"] = await send_homework_entry_with_attachments(
            bot,
            chat_id,
            latest_entry,
            include_back_button=True,
        )

    async def send_homework_entry_with_attachments(
        bot: Bot,
        chat_id: int,
        entry: dict,
        include_back_button: bool = False,
        title: str | None = None,
    ) -> list[int]:
        message_text = format_homework_message(entry)
        attachments = entry["attachments"]
        sent_ids: list[int] = []
        reply_markup = HOMEWORK_BACK_KEYBOARD if include_back_button else None
        if title:
            message_text = f"{title}\n\n{message_text}"
        if not attachments:
            sent = await bot.send_message(chat_id, message_text, reply_markup=reply_markup)
            sent_ids.append(sent.message_id)
            return sent_ids

        first, *rest = attachments
        first_sent = await send_attachment(bot, chat_id, first, caption=message_text, reply_markup=reply_markup)
        if first_sent is not None:
            sent_ids.append(first_sent.message_id)
        for attachment in rest:
            extra_sent = await send_attachment(
                bot,
                chat_id,
                attachment,
                caption=attachment.get("file_name") or "Вложение",
            )
            if extra_sent is not None:
                sent_ids.append(extra_sent.message_id)
        return sent_ids

    async def send_attachment(
        bot: Bot,
        chat_id: int,
        attachment: dict,
        caption: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ):
        file_type = attachment["file_type"]
        file_id = attachment["file_id"]
        storage_path = attachment.get("storage_path")
        if attachment_storage and storage_path:
            local_path = attachment_storage.resolve_path(storage_path)
            if local_path and local_path.exists():
                input_file = FSInputFile(local_path)
                if file_type == "photo":
                    return await bot.send_photo(chat_id, photo=input_file, caption=caption, reply_markup=reply_markup)
                if file_type == "video":
                    return await bot.send_video(chat_id, video=input_file, caption=caption, reply_markup=reply_markup)
                if file_type == "audio":
                    return await bot.send_audio(chat_id, audio=input_file, caption=caption, reply_markup=reply_markup)
                return await bot.send_document(chat_id, document=input_file, caption=caption, reply_markup=reply_markup)
        if file_type == "vk_attachment":
            return None
        if file_type == "photo":
            return await bot.send_photo(chat_id, photo=file_id, caption=caption, reply_markup=reply_markup)
        if file_type == "video":
            return await bot.send_video(chat_id, video=file_id, caption=caption, reply_markup=reply_markup)
        if file_type == "audio":
            return await bot.send_audio(chat_id, audio=file_id, caption=caption, reply_markup=reply_markup)
        return await bot.send_document(chat_id, document=file_id, caption=caption, reply_markup=reply_markup)

    async def send_draft_preview_message(
        bot: Bot,
        chat_id: int,
        draft: HomeworkDraft,
        author: str,
    ) -> list[int]:
        preview_text = format_homework_preview(
            subject_name=draft.subject_name,
            teacher_name=draft.teacher_name,
            text=draft.text,
            attachments=draft.attachments or [],
            created_by_name=author,
        )
        preview_keyboard = build_homework_preview_keyboard()
        attachments = draft.attachments or []
        if not attachments:
            sent = await bot.send_message(chat_id, preview_text, reply_markup=preview_keyboard)
            return [sent.message_id]

        sent_ids: list[int] = []
        first_attachment = {
            "file_id": attachments[0].file_id,
            "file_type": attachments[0].file_type,
            "file_name": attachments[0].file_name,
            "mime_type": attachments[0].mime_type,
            "storage_path": attachments[0].storage_path,
        }
        first_sent = await send_attachment(
            bot,
            chat_id,
            first_attachment,
            caption=preview_text,
            reply_markup=preview_keyboard,
        )
        if first_sent is not None:
            sent_ids.append(first_sent.message_id)

        for attachment in attachments[1:]:
            extra_sent = await send_attachment(
                bot,
                chat_id,
                {
                    "file_id": attachment.file_id,
                    "file_type": attachment.file_type,
                    "file_name": attachment.file_name,
                    "mime_type": attachment.mime_type,
                    "storage_path": attachment.storage_path,
                },
            )
            if extra_sent is not None:
                sent_ids.append(extra_sent.message_id)

        return sent_ids

    async def send_draft_preview(
        bot: Bot,
        chat_id: int,
        author: str,
        draft: HomeworkDraft,
    ) -> None:
        await clear_context_messages(bot, chat_id, "dz")
        context_messages[chat_id]["dz"] = await send_draft_preview_message(bot, chat_id, draft, author)

    @dispatcher.message(CommandStart())
    async def handle_start(message: Message) -> None:
        await register_message_user(message)
        search_results.pop(message.from_user.id if message.from_user else 0, None)
        if message.from_user:
            awaiting_schedule_search.discard(message.from_user.id)
        user = await get_user_record(message.from_user.id if message.from_user else None)
        if user is None or user.schedule_id is None or not user.group_name:
            await prompt_group_selection(message.bot, message.chat.id)
            return
        editor = await user_is_editor(message.from_user.id if message.from_user else None)
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "menu",
            format_welcome(user.group_name, is_editor=editor),
            reply_markup=START_KEYBOARD,
        )

    @dispatcher.message(Command("settings"))
    async def handle_settings_command(message: Message) -> None:
        await register_message_user(message)
        if message.from_user is None:
            return
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "settings",
            await format_settings_text(message.from_user.id),
            reply_markup=await build_settings_keyboard(message.from_user.id),
        )

    @dispatcher.message(Command("rasp"))
    async def handle_rasp_command(message: Message) -> None:
        await register_message_user(message)
        if not await ensure_group_selected(message.bot, message.chat.id, message.from_user.id if message.from_user else None):
            return
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "schedule",
            "Выбери нужный вариант расписания.",
            reply_markup=SCHEDULE_KEYBOARD,
        )

    @dispatcher.message(Command("homework"))
    async def handle_homework_command(message: Message) -> None:
        await register_message_user(message)
        if not await ensure_group_selected(message.bot, message.chat.id, message.from_user.id if message.from_user else None):
            return
        if not await user_has_homework_access(message.from_user.id if message.from_user else None):
            await send_new_context_message(
                message.bot,
                message.chat.id,
                "homework",
                f"Просмотр ДЗ сейчас доступен только для группы <b>{HOMEWORK_GROUP_NAME}</b>.",
            )
            return
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "homework",
            "<b>Выбери предмет</b>\n\nПосле выбора я покажу домашние задания.",
            reply_markup=build_homework_subjects_keyboard("homework"),
        )

    @dispatcher.message(Command("dz"))
    async def handle_dz_command(message: Message) -> None:
        await register_message_user(message)
        if not await ensure_group_selected(message.bot, message.chat.id, message.from_user.id if message.from_user else None):
            return
        if not await user_has_homework_access(message.from_user.id if message.from_user else None):
            await send_new_context_message(
                message.bot,
                message.chat.id,
                "dz",
                f"Добавление ДЗ сейчас доступно только для группы <b>{HOMEWORK_GROUP_NAME}</b>.",
            )
            return
        if not await user_is_editor(message.from_user.id if message.from_user else None):
            await send_new_context_message(message.bot, message.chat.id, "dz", "Команда доступна только редакторам домашнего задания.")
            return
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "dz",
            "<b>Выбери предмет для нового домашнего задания</b>",
            reply_markup=build_homework_subjects_keyboard("dz"),
        )

    @dispatcher.message(Command("cancel"))
    async def handle_cancel_command(message: Message) -> None:
        await register_message_user(message)
        if message.from_user:
            homework_drafts.pop(message.from_user.id, None)
        await clear_context_messages(message.bot, message.chat.id, "dz")
        search_results.pop(message.from_user.id, None)
        awaiting_schedule_search.discard(message.from_user.id)
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "menu",
            format_welcome(
                (await get_user_record(message.from_user.id if message.from_user else None)).group_name
                if await get_user_record(message.from_user.id if message.from_user else None)
                else None,
                is_editor=await user_is_editor(message.from_user.id if message.from_user else None),
            ),
            reply_markup=START_KEYBOARD,
        )

    @dispatcher.message(Command("admin"))
    async def handle_admin_command(message: Message) -> None:
        await register_message_user(message)
        if not user_is_admin(message.from_user.id if message.from_user else None):
            await send_new_context_message(message.bot, message.chat.id, "admin", "Команда доступна только администратору.")
            return
        await send_new_context_message(message.bot, message.chat.id, "admin", format_admin_panel(), ADMIN_KEYBOARD)

    @dispatcher.callback_query(F.data == "menu:start")
    async def handle_menu_start(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        search_results.pop(callback.from_user.id, None)
        awaiting_schedule_search.discard(callback.from_user.id)
        editor = await user_is_editor(callback.from_user.id)
        user = await get_user_record(callback.from_user.id)
        if callback.message is not None:
            try:
                await callback.message.edit_text(format_welcome(user.group_name if user else None, is_editor=editor), reply_markup=START_KEYBOARD)
                context_messages[callback.message.chat.id]["menu"] = [callback.message.message_id]
            except TelegramBadRequest:
                await replace_context_message(
                    callback.bot,
                    callback.message.chat.id,
                    "menu",
                    format_welcome(user.group_name if user else None, is_editor=editor),
                    reply_markup=START_KEYBOARD,
                )
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data == "menu:settings")
    async def handle_menu_settings(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if callback.message is None:
            await safe_callback_answer(callback)
            return
        settings_text = await format_settings_text(callback.from_user.id)
        settings_keyboard = await build_settings_keyboard(callback.from_user.id)
        if not await safe_edit_message_text(callback.message, settings_text, reply_markup=settings_keyboard):
            await replace_context_message(
                callback.bot,
                callback.message.chat.id,
                "settings",
                settings_text,
                reply_markup=settings_keyboard,
            )
        context_messages[callback.message.chat.id]["settings"] = [callback.message.message_id]
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data == "menu:homework")
    async def handle_menu_homework(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not await ensure_group_selected(callback.bot, callback.from_user.id, callback.from_user.id):
            await safe_callback_answer(callback)
            return
        await send_homework_subject_picker(callback.bot, callback.from_user.id, "homework")
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data == "start:rasp")
    async def handle_start_rasp(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not await ensure_group_selected(callback.bot, callback.from_user.id, callback.from_user.id):
            await safe_callback_answer(callback)
            return
        await send_schedule_menu(callback.bot, callback.from_user.id)
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data == "start:homework")
    async def handle_start_homework(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not await ensure_group_selected(callback.bot, callback.from_user.id, callback.from_user.id):
            await safe_callback_answer(callback)
            return
        if not await user_has_homework_access(callback.from_user.id):
            await safe_callback_answer(callback, f"ДЗ доступно только для {HOMEWORK_GROUP_NAME}.", show_alert=True)
            return
        await send_homework_subject_picker(callback.bot, callback.from_user.id, "homework")
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data.startswith("schedule:"))
    async def handle_schedule_callback(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if callback.message is None:
            await safe_callback_answer(callback)
            return
        action = callback.data.split(":", 1)[1]
        if action == "find":
            await prompt_schedule_search(callback.bot, callback.message.chat.id, callback.from_user.id)
            await safe_callback_answer(callback)
            return
        snapshot_row = await get_saved_snapshot(callback.from_user.id)
        if snapshot_row is None:
            await safe_edit_message_text(callback.message, "Не удалось получить расписание для твоей группы.", reply_markup=SCHEDULE_KEYBOARD)
            await safe_callback_answer(callback)
            return

        if action == "today":
            day = get_day_by_offset_from_content(snapshot_row["content"], 0)
            text = ScheduleFormatter.format_day_card(day, "сегодня") if day else empty_day_text("сегодня")
        elif action == "tomorrow":
            day = get_day_by_offset_from_content(snapshot_row["content"], 1)
            text = ScheduleFormatter.format_day_card(day, "завтра") if day else empty_day_text("завтра")
        else:
            day = get_day_by_offset_from_content(snapshot_row["content"], 2)
            text = ScheduleFormatter.format_day_card(day, "2 дня") if day else empty_day_text("2 дня")

        await safe_edit_message_text(callback.message, text, reply_markup=SCHEDULE_KEYBOARD)
        context_messages[callback.message.chat.id]["schedule"] = [callback.message.message_id]
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data.startswith("homework:view:"))
    async def handle_homework_subject(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not await user_has_homework_access(callback.from_user.id):
            await safe_callback_answer(callback, f"ДЗ доступно только для {HOMEWORK_GROUP_NAME}.", show_alert=True)
            return
        await safe_callback_answer(callback)
        await send_homework_entries(
            callback.bot,
            callback.from_user.id,
            callback.data.split(":")[-1],
            source_message=callback.message,
        )

    @dispatcher.callback_query(F.data.startswith("dz:subject:"))
    async def handle_homework_subject_for_create(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not await user_has_homework_access(callback.from_user.id):
            await safe_callback_answer(callback, f"ДЗ доступно только для {HOMEWORK_GROUP_NAME}.", show_alert=True)
            return
        if not await user_is_editor(callback.from_user.id):
            await safe_callback_answer(callback, "Недостаточно прав.", show_alert=True)
            return
        subject = get_subject(callback.data.split(":")[-1])
        if subject is None:
            await safe_callback_answer(callback, "Предмет не найден.", show_alert=True)
            return
        homework_drafts[callback.from_user.id] = HomeworkDraft(
            subject_key=subject["key"],
            subject_name=subject["subject"],
            teacher_name=subject["teacher"],
        )
        if callback.message is not None:
            await safe_edit_message_text(
                callback.message,
                f"Выбран предмет <b>{escape(subject['subject'])}</b>.\n\nТеперь отправь текст домашнего задания одним сообщением.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="Отменить", callback_data="dz:cancel")]]
                ),
            )
            context_messages[callback.message.chat.id]["dz"] = [callback.message.message_id]
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data == "dz:add_attachments")
    async def handle_add_attachments(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        draft = homework_drafts.get(callback.from_user.id)
        if draft is None:
            await safe_callback_answer(callback, "Черновик не найден.", show_alert=True)
            return
        draft.awaiting_attachments = True
        if callback.message is not None:
            await clear_context_messages(callback.bot, callback.message.chat.id, "dz")
            await replace_context_message(
                callback.bot,
                callback.message.chat.id,
                "dz",
                "Отправь вложения сообщениями: документ, фото, видео или аудио.\n\nПосле каждого файла я обновлю предпросмотр. Когда закончишь, нажми «Опубликовать».",
                reply_markup=build_homework_attachment_keyboard(),
            )
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data == "dz:save")
    async def handle_save_homework(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        draft = homework_drafts.get(callback.from_user.id)
        if draft is None or not draft.text.strip():
            await safe_callback_answer(callback, "Нет готового черновика для сохранения.", show_alert=True)
            return
        author = callback.from_user.full_name or callback.from_user.username or str(callback.from_user.id)
        homework_id = await db.create_homework(
            subject_key=draft.subject_key,
            subject=draft.subject_name,
            teacher=draft.teacher_name,
            text=draft.text,
            created_by_platform="telegram",
            created_by_user_id=callback.from_user.id,
            created_by_name=author,
            attachments=draft.attachments or [],
        )
        homework_drafts.pop(callback.from_user.id, None)
        entry = {
            "id": homework_id,
            "subject_key": draft.subject_key,
            "subject": draft.subject_name,
            "teacher": draft.teacher_name,
            "text": draft.text,
            "created_by_platform": "telegram",
            "created_by_user_id": callback.from_user.id,
            "created_by_name": author,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "attachments": [
                {
                    "file_id": attachment.file_id,
                    "file_type": attachment.file_type,
                    "file_name": attachment.file_name,
                    "mime_type": attachment.mime_type,
                    "storage_path": attachment.storage_path,
                    "source_platform": attachment.source_platform,
                }
                for attachment in (draft.attachments or [])
            ],
        }
        await clear_context_messages(callback.bot, callback.from_user.id, "dz")
        sent_ids = await send_homework_entry_with_attachments(
            callback.bot,
            callback.from_user.id,
            entry,
            title="<b>Домашнее задание успешно создано</b>",
        )
        context_messages[callback.from_user.id]["dz"] = sent_ids
        if broadcaster is not None:
            await broadcaster.broadcast_homework_update(format_homework_notification(entry), schedule_id=HOMEWORK_SCHEDULE_ID)
        await safe_callback_answer(callback, "Опубликовано")

    @dispatcher.callback_query(F.data == "dz:cancel")
    async def handle_cancel_homework(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        homework_drafts.pop(callback.from_user.id, None)
        editor = await user_is_editor(callback.from_user.id)
        user = await get_user_record(callback.from_user.id)
        await replace_context_message(
            callback.bot,
            callback.from_user.id,
            "menu",
            format_welcome(user.group_name if user else None, is_editor=editor),
            reply_markup=START_KEYBOARD,
        )
        await clear_context_messages(callback.bot, callback.from_user.id, "dz")
        await safe_callback_answer(callback, "Отменено")

    @dispatcher.callback_query(F.data.startswith("settings:"))
    async def handle_settings_callback(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if callback.message is None:
            await safe_callback_answer(callback)
            return
        action = callback.data.split(":", 1)[1]
        if action == "toggle_hw":
            user = await db.get_user("telegram", callback.from_user.id)
            enabled = not (user.homework_notifications_enabled if user else True)
            await db.set_homework_notifications("telegram", callback.from_user.id, enabled)
            await safe_edit_message_text(
                callback.message,
                await format_settings_text(callback.from_user.id),
                reply_markup=await build_settings_keyboard(callback.from_user.id),
            )
            await safe_callback_answer(callback, "Настройка обновлена")
            return
        if action == "clear_group":
            await db.clear_user_group("telegram", callback.from_user.id)
            homework_drafts.pop(callback.from_user.id, None)
            await safe_edit_message_text(
                callback.message,
                format_group_prompt("Ты отписался от своей группы. Выбери новую, когда захочешь."),
            )
            context_messages[callback.message.chat.id]["group_select"] = [callback.message.message_id]
            context_messages[callback.message.chat.id]["settings"] = []
            await safe_callback_answer(callback, "Группа сброшена")
            return

    @dispatcher.callback_query(F.data.startswith("admin:"))
    async def handle_admin_callback(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not user_is_admin(callback.from_user.id):
            await safe_callback_answer(callback, "Недостаточно прав.", show_alert=True)
            return
        if callback.message is None:
            await safe_callback_answer(callback)
            return

        action = callback.data.split(":", 1)[1]
        admin_user = await get_user_record(callback.from_user.id)
        if action == "status":
            text = await format_admin_status()
            await safe_edit_message_text(callback.message, text, reply_markup=ADMIN_KEYBOARD)
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            await safe_callback_answer(callback)
            return
        if action == "baseline":
            groups_count, preview = await save_baseline_for_all_active_groups()
            if groups_count == 0:
                await safe_callback_answer(callback, "Нет активных групп для сохранения эталона.", show_alert=True)
                return
            await safe_edit_message_text(
                callback.message,
                f"<b>Эталоны сохранены для активных групп</b>\n\nГрупп обработано: <b>{groups_count}</b>\n\n{preview or 'Предпросмотр недоступен.'}",
                reply_markup=ADMIN_KEYBOARD,
            )
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            await safe_callback_answer(callback, "Эталоны сохранены")
            return
        if action == "homework_delete":
            await safe_edit_message_text(
                callback.message,
                "<b>Удаление домашнего задания</b>\n\nВыбери предмет, чтобы увидеть последние записи.",
                reply_markup=build_admin_homework_subjects_keyboard(),
            )
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            await safe_callback_answer(callback)
            return
        if action.startswith("hw_subject:"):
            subject_key = action.split(":", 1)[1]
            subject = get_subject(subject_key)
            entries = await db.get_homework_for_subject(subject_key)
            if subject is None:
                text = "Предмет не найден."
                reply_markup = build_admin_homework_subjects_keyboard()
            elif not entries:
                text = f"<b>{escape(subject['subject'])}</b>\n\nДля этого предмета пока нет записей."
                reply_markup = build_admin_homework_subjects_keyboard()
            else:
                text = f"<b>{escape(subject['subject'])}</b>\n\nВыбери запись, которую нужно удалить."
                reply_markup = build_admin_homework_entries_keyboard(subject_key, entries)
            await safe_edit_message_text(callback.message, text, reply_markup=reply_markup)
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            await safe_callback_answer(callback)
            return
        if action.startswith("hw_delete:"):
            _, homework_id_text, subject_key = action.split(":", 2)
            homework_id = int(homework_id_text)
            attachments = await db.get_homework_attachments(homework_id)
            deleted = await db.delete_homework(homework_id)
            if deleted and attachment_storage is not None:
                attachment_storage.delete_attachments(attachments)
            subject = get_subject(subject_key)
            entries = await db.get_homework_for_subject(subject_key)
            subject_name = escape(subject["subject"]) if subject else "Предмет"
            if not entries:
                text = (
                    f"<b>{subject_name}</b>\n\n"
                    + ("Запись удалена." if deleted else "Запись не найдена.")
                )
                reply_markup = build_admin_homework_subjects_keyboard()
            else:
                prefix = "Запись удалена.\n\n" if deleted else "Запись не найдена.\n\n"
                text = f"<b>{subject_name}</b>\n\n{prefix}Выбери следующую запись для удаления."
                reply_markup = build_admin_homework_entries_keyboard(subject_key, entries)
            await safe_edit_message_text(callback.message, text, reply_markup=reply_markup)
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            await safe_callback_answer(callback, "Удалено" if deleted else "Не найдено")
            return
        if action == "close":
            editor = await user_is_editor(callback.from_user.id)
            await safe_edit_message_text(callback.message, format_welcome(admin_user.group_name if admin_user else None, is_editor=editor))
            context_messages[callback.message.chat.id]["menu"] = [callback.message.message_id]
            await safe_callback_answer(callback)
            return
        if action == "back":
            await safe_edit_message_text(callback.message, format_admin_panel(), reply_markup=ADMIN_KEYBOARD)
            await safe_callback_answer(callback)
            return

        if action == "status":
            users = await db.list_users()
            last_change = await db.get_last_change()
            last_change_at = escape(last_change["created_at"]) if last_change else "пока не было"
            text = (
                "<b>Статус бота</b>\n\n"
                f"Пользователей: <b>{len(users)}</b>\n"
                f"Последнее изменение: <b>{last_change_at}</b>"
            )
            reply_markup = ADMIN_KEYBOARD
        elif action == "users" or action.startswith("users:"):
            users = await db.list_users()
            if not users:
                text = "<b>Пользователи</b>\n\nПока никто не зарегистрирован."
                reply_markup = build_admin_users_keyboard(page=0, total_pages=1)
            else:
                page = 0
                if action.startswith("users:"):
                    _, page_raw = action.split(":", 1)
                    if page_raw.isdigit():
                        page = int(page_raw)

                user_rows: list[str] = []
                for user in users:
                    platform_label = "tg" if user.platform == "telegram" else user.platform
                    user_label = user.full_name or "Без имени"
                    nick_or_name = (
                        user.full_name
                        if user.platform == "vk"
                        else (f"@{user.username}" if user.username else (user.full_name or "-"))
                    )
                    group_label = user.group_name or "-"
                    role_flags = []
                    if user.is_admin:
                        role_flags.append("админ")
                    if user.is_editor:
                        role_flags.append("редактор")
                    role_suffix = f" ({', '.join(role_flags)})" if role_flags else ""
                    user_rows.append(
                        "- "
                        f"{escape(platform_label)} | "
                        f"{escape(user_label)} | "
                        f"{escape(nick_or_name)} | "
                        f"<b>{user.user_id}</b> | "
                        f"{escape(group_label)}{escape(role_suffix)}"
                    )

                page_size = 20
                total_pages = max(1, (len(user_rows) + page_size - 1) // page_size)
                page = max(0, min(page, total_pages - 1))
                start = page * page_size
                end = start + page_size
                page_rows = user_rows[start:end]

                lines = [
                    "<b>Пользователи</b>",
                    "",
                    "Формат: платформа | юзер | ник/ФИ | айди | группа (роли)",
                    "",
                    f"Страница {page + 1}/{total_pages}",
                    "",
                ]
                lines.extend(page_rows)

                text = "\n".join(lines)
                reply_markup = build_admin_users_keyboard(page=page, total_pages=total_pages)
        elif action == "editors":
            users = [user for user in await db.list_users("telegram")]
            text = "<b>Управление редакторами</b>\n\nНажми на пользователя, чтобы выдать или снять роль редактора."
            reply_markup = build_editors_keyboard(users)
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
            reply_markup = ADMIN_KEYBOARD
        elif action == "refresh":
            groups_count, preview = await refresh_all_active_groups()
            if groups_count == 0:
                await safe_callback_answer(callback, "Нет активных групп для перепарсинга.", show_alert=True)
                return
            text = f"<b>Расписание перепарсено для активных групп</b>\n\nГрупп обработано: <b>{groups_count}</b>\n\n{preview or 'Предпросмотр недоступен.'}"
            reply_markup = ADMIN_KEYBOARD
        else:
            if broadcaster is not None:
                await broadcaster.broadcast_test_message()
            text = "<b>Тестовая рассылка</b>\n\nСообщение отправлено всем зарегистрированным пользователям."
            reply_markup = ADMIN_KEYBOARD

        await safe_edit_message_text(callback.message, text, reply_markup=reply_markup)
        context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data.startswith("editor:toggle:"))
    async def handle_editor_toggle(callback: CallbackQuery) -> None:
        await register_callback_user(callback)
        if not user_is_admin(callback.from_user.id):
            await safe_callback_answer(callback, "Недостаточно прав.", show_alert=True)
            return
        user_id = int(callback.data.split(":")[-1])
        target = await db.get_user("telegram", user_id)
        if target is None:
            await safe_callback_answer(callback, "Пользователь не найден.", show_alert=True)
            return
        await db.set_editor("telegram", user_id, not target.is_editor)
        users = [user for user in await db.list_users("telegram")]
        await safe_edit_message_text(
            callback.message,
            "<b>Управление редакторами</b>\n\nНажми на пользователя, чтобы выдать или снять роль редактора.",
            reply_markup=build_editors_keyboard(users),
        )
        await safe_callback_answer(callback, "Роль обновлена")

    @dispatcher.message(F.document | F.photo | F.video | F.audio)
    async def handle_homework_attachment_message(message: Message) -> None:
        await register_message_user(message)
        if message.from_user is None:
            return
        draft = homework_drafts.get(message.from_user.id)
        if draft is None or not draft.awaiting_attachments:
            return

        attachment: HomeworkAttachment | None = None
        if message.document:
            attachment = (
                await attachment_storage.save_telegram_file(
                    bot=message.bot,
                    file_id=message.document.file_id,
                    file_type="document",
                    file_name=message.document.file_name,
                    mime_type=message.document.mime_type,
                )
                if attachment_storage is not None
                else HomeworkAttachment(
                    file_id=message.document.file_id,
                    file_type="document",
                    file_name=message.document.file_name,
                    mime_type=message.document.mime_type,
                )
            )
        elif message.photo:
            photo: PhotoSize = message.photo[-1]
            attachment = (
                await attachment_storage.save_telegram_file(
                    bot=message.bot,
                    file_id=photo.file_id,
                    file_type="photo",
                    file_name="photo.jpg",
                    mime_type="image/jpeg",
                )
                if attachment_storage is not None
                else HomeworkAttachment(
                    file_id=photo.file_id,
                    file_type="photo",
                    file_name="photo.jpg",
                    mime_type="image/jpeg",
                )
            )
        elif message.video:
            video: Video = message.video
            attachment = (
                await attachment_storage.save_telegram_file(
                    bot=message.bot,
                    file_id=video.file_id,
                    file_type="video",
                    file_name=video.file_name,
                    mime_type=video.mime_type,
                )
                if attachment_storage is not None
                else HomeworkAttachment(
                    file_id=video.file_id,
                    file_type="video",
                    file_name=video.file_name,
                    mime_type=video.mime_type,
                )
            )
        elif message.audio:
            attachment = (
                await attachment_storage.save_telegram_file(
                    bot=message.bot,
                    file_id=message.audio.file_id,
                    file_type="audio",
                    file_name=message.audio.file_name,
                    mime_type=message.audio.mime_type,
                )
                if attachment_storage is not None
                else HomeworkAttachment(
                    file_id=message.audio.file_id,
                    file_type="audio",
                    file_name=message.audio.file_name,
                    mime_type=message.audio.mime_type,
                )
            )

        if attachment is None:
            return

        draft.attachments.append(attachment)
        draft.awaiting_attachments = True
        await try_delete_message(message)
        author = message.from_user.full_name or message.from_user.username or "Неизвестный пользователь"
        await send_draft_preview(message.bot, message.chat.id, author, draft)

    @dispatcher.message(F.text == "Домашние задания")
    async def handle_homework_text_shortcut(message: Message) -> None:
        await register_message_user(message)
        if not await ensure_group_selected(message.bot, message.chat.id, message.from_user.id if message.from_user else None):
            return
        if not await user_has_homework_access(message.from_user.id if message.from_user else None):
            await send_new_context_message(
                message.bot,
                message.chat.id,
                "homework",
                f"Просмотр ДЗ сейчас доступен только для группы <b>{HOMEWORK_GROUP_NAME}</b>.",
            )
            return
        await send_homework_subject_picker(message.bot, message.chat.id, "homework")

    @dispatcher.message(F.text)
    async def handle_text_message(message: Message) -> None:
        await register_message_user(message)
        if message.from_user is None or message.text is None:
            return
        user = await get_user_record(message.from_user.id)
        if user is None or user.schedule_id is None or not user.group_name:
            if message.text.startswith("/"):
                await prompt_group_selection(message.bot, message.chat.id)
                return
            await try_delete_message(message)
            await handle_group_input(message.bot, message.chat.id, message.from_user.id, message.text)
            return
        if message.from_user.id in awaiting_schedule_search:
            await try_delete_message(message)
            await perform_schedule_search(message.bot, message.chat.id, message.from_user.id, message.text)
            return
        draft = homework_drafts.get(message.from_user.id)
        if draft is not None and draft.awaiting_text:
            draft.text = message.text.strip()
            draft.awaiting_text = False
            draft.awaiting_attachments = False
            await try_delete_message(message)
            author = message.from_user.full_name or message.from_user.username or "Неизвестный пользователь"
            await send_draft_preview(message.bot, message.chat.id, author, draft)
            return

        if draft is not None and draft.awaiting_attachments:
            await try_delete_message(message)
            await replace_context_message(
                message.bot,
                message.chat.id,
                "dz",
                "Сейчас я жду вложение. Отправь документ, фото, видео или аудио, либо нажми «Опубликовать».",
                reply_markup=build_homework_attachment_keyboard(),
            )
            return

        await send_new_context_message(
            message.bot,
            message.chat.id,
            "menu",
            format_welcome(user.group_name, is_editor=await user_is_editor(message.from_user.id)),
            reply_markup=START_KEYBOARD,
        )

    @dispatcher.errors()
    async def handle_telegram_errors(event: ErrorEvent, bot: Bot) -> bool:
        user_id, chat_id = extract_error_context(event)
        if chat_id is not None:
            await notify_user_about_error(bot, chat_id, event.exception)
        await notify_admin_about_error("telegram", user_id, chat_id, event.exception)
        return True

    return dispatcher

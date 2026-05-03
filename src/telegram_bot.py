from __future__ import annotations

from collections import defaultdict
import asyncio
import logging
from datetime import datetime
from html import escape
from time import monotonic
from traceback import format_exception

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
from src.lesson_counters import LessonCounterService
from src.notifier import CAMPAIGN_ADMIN_BROADCAST, Broadcaster
from src.parser import ScheduleParser
from src.schedule_search import ScheduleSearchCatalog
from src.schedule_service import ScheduleFormatter, get_day_by_offset, get_day_by_offset_from_content
from src.subscription_utils import make_group_subscription, make_teacher_subscription, subscription_caption


logger = logging.getLogger(__name__)

SUPPORT_CONTACT = "tg: t.me/nekoty или vk: vk.com/nekotyy"
GROUP_CHAT_TYPES = {"group", "supergroup"}


SCHEDULE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Расписание на сегодня", callback_data="schedule:today")],
        [InlineKeyboardButton(text="Расписание на завтра", callback_data="schedule:tomorrow")],
        [InlineKeyboardButton(text="Расписание на 2 дня", callback_data="schedule:day_after")],
        [InlineKeyboardButton(text="Расписание звонков", callback_data="schedule:bells")],
        [InlineKeyboardButton(text="Найти расписание", callback_data="schedule:find")],
        [InlineKeyboardButton(text="Назад", callback_data="menu:start")],
    ]
)

WEEKDAY_BELLS_TEXT = "\n".join(
    [
        "Звонки ОПК СТИ НИТУ МИСИС",
        "",
        "Будни:",
        "",
        "Понедельник(Классный час) 8:30 - 9:20",
        "",
        "1 пара 9:00 - 10:30",
        "",
        "2 пара 10:40 - 12:10",
        "",
        "перерыв 12:10 - 12:40",
        "",
        "3 пара 12:40 - 14:10",
        "",
        "перерыв 14:10 - 14:30",
        "",
        "4 пара 14:30 - 16:00",
        "",
        "5 пара 16:10 - 17:40",
        "",
        "6 пара 17:50 - 19:20",
    ]
)

SATURDAY_BELLS_TEXT = "\n".join(
    [
        "Звонки ОПК СТИ НИТУ МИСИС",
        "",
        "Суббота:",
        "",
        "1 пара 9:00 - 10:30",
        "",
        "2 пара 10:40 - 12:10",
        "",
        "3 пара 12:20 - 13:50",
        "",
        "4 пара 14:00 - 15:30",
        "",
        "5 пара 15:40 - 17:10",
    ]
)

START_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Узнать расписание", callback_data="start:rasp")],
        [InlineKeyboardButton(text="Дополнительно", callback_data="menu:settings")],
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
            InlineKeyboardButton(text="Информация по группам", callback_data="admin:group_info"),
        ],
        [
            InlineKeyboardButton(text="Разослать", callback_data="admin:broadcast"),
            InlineKeyboardButton(text="Тестовая рассылка", callback_data="admin:test"),
        ],
        [
            InlineKeyboardButton(text="Закрыть админку", callback_data="admin:close"),
        ],
    ]
)

ADMIN_BROADCAST_INPUT_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Отменить", callback_data="admin:broadcast_cancel")],
    ]
)

ADMIN_BROADCAST_PREVIEW_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Отправить", callback_data="admin:broadcast_send")],
        [InlineKeyboardButton(text="Отменить", callback_data="admin:broadcast_cancel")],
    ]
)

def build_dispatcher(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster | None = None,
    group_catalog: GroupCatalog | None = None,
    search_catalog: ScheduleSearchCatalog | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()
    context_messages: dict[int, dict[str, list[int]]] = defaultdict(dict)
    search_results: dict[int, dict[str, object]] = {}
    awaiting_schedule_search: set[int] = set()
    awaiting_group_subscription_input: set[int] = set()
    awaiting_admin_broadcast_text: set[int] = set()
    awaiting_admin_user_search: set[int] = set()
    admin_broadcast_drafts: dict[int, str] = {}
    message_rate_limit: dict[int, float] = {}
    callback_rate_limit: dict[int, float] = {}
    message_rate_locks: dict[int, asyncio.Lock] = {}
    callback_rate_locks: dict[int, asyncio.Lock] = {}
    lesson_counter_service = LessonCounterService(db)

    def is_rate_limited(bucket: dict[int, float], key: int, cooldown: float) -> bool:
        now = monotonic()
        last_hit = bucket.get(key)
        if last_hit is not None and now - last_hit < cooldown:
            return True
        bucket[key] = now
        return False

    async def wait_rate_limit_queue(
        bucket: dict[int, float],
        lock_bucket: dict[int, asyncio.Lock],
        user_id: int,
        cooldown: float,
    ) -> None:
        lock = lock_bucket.setdefault(user_id, asyncio.Lock())
        async with lock:
            now = monotonic()
            next_at = bucket.get(user_id, now)
            delay = next_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            bucket[user_id] = monotonic() + cooldown
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
            subscription_type=existing.subscription_type if existing else None,
            subscription_key=existing.subscription_key if existing else None,
            subscription_title=existing.subscription_title if existing else None,
            subscription_url=existing.subscription_url if existing else None,
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
            subscription_type=existing.subscription_type if existing else None,
            subscription_key=existing.subscription_key if existing else None,
            subscription_title=existing.subscription_title if existing else None,
            subscription_url=existing.subscription_url if existing else None,
            group_name=existing.group_name if existing else None,
            schedule_id=existing.schedule_id if existing else None,
            is_admin=user_is_admin(user.id),
            is_editor=existing.is_editor if existing else False,
        )

    async def register_group_chat(message: Message) -> None:
        if message.chat.type not in GROUP_CHAT_TYPES:
            return
        existing = await db.get_user("telegram", message.chat.id)
        title = message.chat.title or str(message.chat.id)
        await db.upsert_user(
            platform="telegram",
            user_id=message.chat.id,
            username=None,
            full_name=f"TG group: {title}",
            subscription_type=existing.subscription_type if existing else None,
            subscription_key=existing.subscription_key if existing else None,
            subscription_title=existing.subscription_title if existing else None,
            subscription_url=existing.subscription_url if existing else None,
            group_name=existing.group_name if existing else None,
            schedule_id=existing.schedule_id if existing else None,
            is_admin=False,
            is_editor=existing.is_editor if existing else False,
        )

    async def user_can_manage_group(message: Message) -> bool:
        if message.from_user is None or message.chat.type not in GROUP_CHAT_TYPES:
            return False
        try:
            member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        except TelegramBadRequest:
            return False
        status = str(getattr(member, "status", ""))
        return status in {"administrator", "creator"}

    async def resolve_subscription_input(raw_text: str) -> tuple[dict | None, str | None]:
        group = await group_catalog.find_group(raw_text) if group_catalog is not None else None
        if group is not None:
            return make_group_subscription(group.group_name, group.schedule_id), None
        if search_catalog is None:
            return None, "Справочник сейчас недоступен. Попробуй позже."
        try:
            target = await search_catalog.find(raw_text)
        except httpx.HTTPError:
            return None, "Сайт расписания временно недоступен. Попробуй еще раз через минуту."
        if target is None or target.kind != "teacher":
            return None, "Ничего не найдено. Проверь написание и попробуй еще раз."
        return make_teacher_subscription(target), None

    def format_group_subscription_status(subscription_type: str | None, subscription_title: str | None) -> str:
        lines = ["<b>Настройка уведомлений для этой группы</b>", ""]
        subscription_line = subscription_caption(subscription_type, subscription_title)
        if subscription_line:
            label, value = subscription_line.split(":", 1)
            value_lines = [part.strip() for part in value.strip().splitlines() if part.strip()]
            if value_lines:
                lines.append(f"{escape(label)}: <b>{escape(value_lines[0])}</b>")
                lines.extend(escape(part) for part in value_lines[1:])
            else:
                lines.append(f"{escape(label)}: <b>-</b>")
        else:
            lines.append("Подписка: <b>не выбрана</b>")
        lines.extend(
            [
                "",
                "Команды:",
                "/group_setup - интерактивная настройка",
                "/group_set ИСП-25-1 - быстрая настройка",
                "/group_clear - очистить подписку",
            ]
        )
        return "\n".join(lines)

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
    async def get_saved_snapshot(user_id: int | None) -> dict | None:
        user = await get_user_record(user_id)
        if user is None or not user.subscription_key or not user.subscription_title:
            return None
        snapshot = await db.get_latest_snapshot("current", schedule_id=user.schedule_id, source_key=user.subscription_key)
        if snapshot is not None:
            return snapshot
        if user.subscription_type == "teacher" and user.subscription_url:
            snapshot_obj, snapshot_hash = await parser.parse_from_url(user.subscription_url)
        elif user.schedule_id is not None:
            snapshot_obj, snapshot_hash = await parser.parse(user.schedule_id)
        else:
            return None
        await db.save_snapshot(
            "current",
            snapshot_hash,
            snapshot_obj,
            user.schedule_id,
            user.group_name,
            source_type=user.subscription_type,
            source_key=user.subscription_key,
            source_title=user.subscription_title,
            source_url=user.subscription_url,
        )
        return await db.get_latest_snapshot("current", schedule_id=user.schedule_id, source_key=user.subscription_key)

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
        class _WelcomeUser:
            subscription_type = "group" if group_name else None
            subscription_title = group_name

        return build_welcome_text(_WelcomeUser if group_name else None, is_editor=is_editor)

    async def format_settings_text(user_id: int) -> str:
        return await format_subscription_settings_text(user_id)

    async def build_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
        return await build_subscription_settings_keyboard(user_id)

    def format_admin_panel() -> str:
        return (
            "<b>Админ-панель</b>\n\n"
            "Здесь можно перепарсить сайт, сохранить эталон для сравнения и посмотреть статистику."
        )

    def format_admin_broadcast_prompt(error_text: str | None = None) -> str:
        lines = [
            "<b>Разослать</b>",
            "",
            "Отправь текст сообщения одним сообщением.",
            "После этого я покажу предпросмотр.",
        ]
        if error_text:
            lines.extend(["", error_text])
        return "\n".join(lines)

    def format_admin_broadcast_preview(text: str) -> str:
        return "\n".join([
            "<b>Предпросмотр рассылки</b>",
            "",
            escape(text),
        ])

    def sort_admin_users(users: list, sort_mode: str) -> list:
        def platform_priority(user: object) -> int:
            return 0 if getattr(user, "platform", None) == "telegram" else 1

        def user_display_label(user: object) -> str:
            return (
                getattr(user, "full_name", None)
                or getattr(user, "username", None)
                or ""
            ).casefold()

        def source_label(user: object) -> str:
            return (
                getattr(user, "subscription_title", None)
                or getattr(user, "group_name", None)
                or ""
            ).casefold()

        if sort_mode == "platform_vk":
            return sorted(
                users,
                key=lambda user: (
                    1 - platform_priority(user),
                    source_label(user),
                    user_display_label(user),
                    user.user_id,
                ),
            )

        if sort_mode == "platform_tg":
            return sorted(
                users,
                key=lambda user: (
                    platform_priority(user),
                    source_label(user),
                    user_display_label(user),
                    user.user_id,
                ),
            )

        def user_kind_priority(user: object) -> int:
            is_teacher = (getattr(user, "subscription_type", None) or "") == "teacher"
            return 1 if is_teacher else 0

        if sort_mode == "kind_teacher":
            return sorted(
                users,
                key=lambda user: (
                    1 - user_kind_priority(user),
                    source_label(user),
                    platform_priority(user),
                    user_display_label(user),
                    user.user_id,
                ),
            )

        return sorted(
            users,
            key=lambda user: (
                user_kind_priority(user),
                source_label(user),
                platform_priority(user),
                user_display_label(user),
                user.user_id,
            ),
        )

    def get_admin_users_toggle_modes(sort_mode: str) -> tuple[str, str]:
        platform_mode = sort_mode if sort_mode in {"platform_tg", "platform_vk"} else "platform_tg"
        kind_mode = sort_mode if sort_mode in {"kind_group", "kind_teacher"} else "kind_group"
        next_platform_mode = "platform_vk" if platform_mode == "platform_tg" else "platform_tg"
        next_kind_mode = "kind_teacher" if kind_mode == "kind_group" else "kind_group"
        return next_kind_mode, next_platform_mode

    def user_search_haystack(user: object) -> str:
        parts = [
            getattr(user, "platform", None),
            getattr(user, "username", None),
            getattr(user, "full_name", None),
            getattr(user, "subscription_title", None),
            getattr(user, "group_name", None),
            str(getattr(user, "user_id", "")),
        ]
        return " ".join(str(part or "").casefold() for part in parts)

    def filter_admin_users(users: list, query: str) -> list:
        normalized = query.strip().casefold()
        if not normalized:
            return users
        return [user for user in users if normalized in user_search_haystack(user)]

    def telegram_profile_link(user: object) -> str:
        username = getattr(user, "username", None)
        if username:
            return f"https://t.me/{username}"
        return f"tg://user?id={getattr(user, 'user_id')}"

    def external_profile_link(user: object) -> str:
        if getattr(user, "platform", None) == "vk":
            return f"https://vk.com/id{getattr(user, 'user_id')}"
        return telegram_profile_link(user)

    def format_admin_user_row(user: object) -> str:
        platform_label = "tg" if getattr(user, "platform", None) == "telegram" else getattr(user, "platform", None)
        profile_link = external_profile_link(user)
        user_label = getattr(user, "full_name", None) or "Без имени"
        username = getattr(user, "username", None)
        nick_or_name = (
            getattr(user, "full_name", None)
            if getattr(user, "platform", None) == "vk"
            else (f"@{username}" if username else (getattr(user, "full_name", None) or "-"))
        )
        nick_link = f"<a href=\"{escape(profile_link, quote=True)}\">{escape(nick_or_name)}</a>" if nick_or_name != "-" else "-"
        id_link = f"<a href=\"{escape(profile_link, quote=True)}\">{getattr(user, 'user_id')}</a>"
        group_label = getattr(user, "subscription_title", None) or getattr(user, "group_name", None) or "-"
        role_flags: list[str] = []
        if getattr(user, "is_admin", False):
            role_flags.append("админ")
        if getattr(user, "is_editor", False):
            role_flags.append("редактор")
        role_suffix = f" ({', '.join(role_flags)})" if role_flags else ""
        return (
            "- "
            f"{escape(str(platform_label or '-'))} | "
            f"{escape(user_label)} | "
            f"{nick_link} | "
            f"{id_link} | "
            f"{escape(group_label)}{escape(role_suffix)}"
        )

    def format_admin_users_list(
        users: list,
        *,
        sort_mode: str,
        page: int,
        title: str,
        summary: str | None = None,
    ) -> tuple[str, InlineKeyboardMarkup]:
        sorted_users = sort_admin_users(users, sort_mode)
        user_rows = [format_admin_user_row(user) for user in sorted_users]
        page_size = 20
        total_pages = max(1, (len(user_rows) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        end = start + page_size
        lines = [
            f"<b>{title}</b>",
            "",
            "Формат: платформа | юзер | ник/ФИ | айди | группа (роли)",
        ]
        if summary:
            lines.extend(["", summary])
        lines.extend(["", f"Страница {page + 1}/{total_pages}", ""])
        lines.extend(user_rows[start:end] or ["Ничего не найдено."])
        return "\n".join(lines), build_admin_users_keyboard(page=page, total_pages=total_pages, sort_mode=sort_mode)

    def build_admin_users_keyboard(page: int, total_pages: int, sort_mode: str) -> InlineKeyboardMarkup:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="<", callback_data=f"admin:users:{sort_mode}:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text=">", callback_data=f"admin:users:{sort_mode}:{page + 1}"))

        next_kind_mode, next_platform_mode = get_admin_users_toggle_modes(sort_mode)
        kind_button_text = "Сначала преподы" if next_kind_mode == "kind_teacher" else "Сначала группы"
        platform_button_text = "Сначала VK" if next_platform_mode == "platform_vk" else "Сначала TG"

        rows: list[list[InlineKeyboardButton]] = []
        if nav_row:
            rows.append(nav_row)
        rows.append([InlineKeyboardButton(text="Поиск", callback_data=f"admin:users_search:{sort_mode}")])
        rows.append([InlineKeyboardButton(text=kind_button_text, callback_data=f"admin:users:{next_kind_mode}:0")])
        rows.append([InlineKeyboardButton(text=platform_button_text, callback_data=f"admin:users:{next_platform_mode}:0")])
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
        active_groups = await db.get_active_sources()
        active_group_count = sum(1 for item in active_groups if item.get("source_type") == "group")
        active_teacher_count = sum(1 for item in active_groups if item.get("source_type") == "teacher")
        last_change = await db.get_last_change()
        current_snapshot = await db.get_latest_snapshot("current")
        baseline_snapshot = await db.get_latest_snapshot("daily_baseline")
        delivery_stats = await db.get_delivery_stats()
        tg_auto_disabled = await db.count_auto_disabled_users("telegram")
        tg_top_errors = await db.get_top_delivery_errors(platform="telegram", hours=24, limit=3)
        vk_users = sum(1 for user in users if user.platform == "vk")
        tg_users = sum(1 for user in users if user.platform == "telegram")
        last_change_at = escape(last_change["created_at"]) if last_change else "еще не было"
        tg_top_error_lines = "\n".join(
            f"- <b>{item['count']}</b>: {escape(str(item['error_text']))}"
            for item in tg_top_errors
        ) or "- нет данных"
        return (
            "<b>Статус бота</b>\n\n"
            f"Пользователей: <b>{len(users)}</b>\n"
            f"Пользователей с VK: <b>{vk_users}</b>\n"
            f"Пользователей с TG: <b>{tg_users}</b>\n"
            f"Активных групп: <b>{active_group_count}</b>\n"
            f"Активных преподавателей: <b>{active_teacher_count}</b>\n"
            f"Последнее изменение: <b>{last_change_at}</b>\n\n"
            "<b>Статистика отправок</b>\n"
            f"Всего событий доставки: <b>{delivery_stats['events_total']}</b>\n"
            f"Успешно / ошибок: <b>{delivery_stats['sent_total']}</b> / <b>{delivery_stats['failed_total']}</b>\n"
            f"За 24 часа (успешно / ошибок): <b>{delivery_stats['sent_last_24h']}</b> / <b>{delivery_stats['failed_last_24h']}</b>\n"
            f"Уведомлений отправлено: <b>{delivery_stats['notifications_sent']}</b>\n"
            f"Админских рассылок отправлено: <b>{delivery_stats['admin_broadcast_sent']}</b>\n"
            f"Служебных уведомлений админу: <b>{delivery_stats['admin_notify_sent']}</b>\n"
            f"Через RabbitMQ / напрямую: <b>{delivery_stats['sent_via_rabbitmq']}</b> / <b>{delivery_stats['sent_direct']}</b>\n"
            f"Ошибок через RabbitMQ / напрямую: <b>{delivery_stats['failed_via_rabbitmq']}</b> / <b>{delivery_stats['failed_direct']}</b>\n"
            f"Доставлено после ретрая: <b>{delivery_stats['sent_after_retry']}</b>\n"
            f"TG (успешно / ошибок): <b>{delivery_stats['tg_sent']}</b> / <b>{delivery_stats['tg_failed']}</b>\n"
            f"TG ошибок через RabbitMQ / напрямую: <b>{delivery_stats['tg_failed_via_rabbitmq']}</b> / <b>{delivery_stats['tg_failed_direct']}</b>\n"
            f"TG ошибок за 24ч: <b>{delivery_stats['tg_failed_last_24h']}</b>\n"
            f"TG перманентных ошибок (всего / 24ч): <b>{delivery_stats['tg_failed_permanent']}</b> / <b>{delivery_stats['tg_failed_permanent_last_24h']}</b>\n"
            f"TG авто-отключено из-за доставки: <b>{tg_auto_disabled}</b>\n"
            f"VK (успешно / ошибок): <b>{delivery_stats['vk_sent']}</b> / <b>{delivery_stats['vk_failed']}</b>\n\n"
            "<b>Топ TG ошибок за 24ч</b>\n"
            f"{tg_top_error_lines}\n\n"
            f"{format_snapshot_info('Последний обычный парс', current_snapshot)}\n\n"
            f"{format_snapshot_info('Последний сохраненный эталон', baseline_snapshot)}"
        )

    def format_group_action_report(title: str, rows: list[tuple[str, str, str]]) -> str:
        lines = [f"<b>{title}</b>", ""]
        if not rows:
            lines.append("Нет записей.")
            return "\n".join(lines)
        lines.append(f"Затронуто источников: <b>{len(rows)}</b>")
        lines.append("")
        for group_name, action_time, action_name in rows:
            lines.append(
                f"{escape(group_name)} | <b>{escape(action_time)}</b> | {escape(action_name)}"
            )
        return "\n".join(lines)

    def format_daily_change_report(title: str, rows: list[dict]) -> str:
        lines = [f"<b>{title}</b>", ""]
        if not rows:
            lines.append("За сегодня изменений пока не было.")
            return "\n".join(lines)
        lines.append(f"Затронуто источников: <b>{len(rows)}</b>")
        lines.append("")
        for row in rows:
            lines.append(f"{escape(row['group_name'])} | <b>{escape(row['created_at'])}</b>")
        return "\n".join(lines)

    def format_group_user_stats(rows: list[dict[str, int | str]]) -> str:
        lines = ["<b>Информация по группам</b>", ""]
        if not rows:
            lines.append("Пока нет пользователей с выбранной учебной группой.")
            return "\n".join(lines)
        total_users = sum(int(row["users_count"]) for row in rows)
        lines.append(f"Групп найдено: <b>{len(rows)}</b>")
        lines.append(f"Всего пользователей с группой: <b>{total_users}</b>")
        lines.append("")
        lines.append("№ | группа | кол-во юзеров")
        lines.append("")
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"{index} | {escape(str(row['group_name']))} | <b>{int(row['users_count'])}</b>"
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

    async def refresh_all_active_sources() -> list[tuple[str, str, str]]:
        sources = await db.get_active_sources()
        if not sources:
            return []

        rows: list[tuple[str, str, str]] = []
        for source in sources:
            if source["source_type"] == "teacher":
                snapshot, snapshot_hash = await parser.parse_from_url(source["source_url"])
            else:
                snapshot, snapshot_hash = await parser.parse(source["schedule_id"])
            await db.save_snapshot(
                "current",
                snapshot_hash,
                snapshot,
                source["schedule_id"],
                source["group_name"],
                source_type=source["source_type"],
                source_key=source["source_key"],
                source_title=source["source_title"],
                source_url=source["source_url"],
            )
            rows.append((source["source_title"], snapshot.fetched_at.strftime("%Y-%m-%d %H:%M"), "перепарсено"))
        return rows

    async def save_baseline_for_all_active_sources() -> list[tuple[str, str, str]]:
        sources = await db.get_active_sources()
        if not sources:
            return []

        rows: list[tuple[str, str, str]] = []
        for source in sources:
            if source["source_type"] == "teacher":
                snapshot, snapshot_hash = await parser.parse_from_url(source["source_url"])
            else:
                snapshot, snapshot_hash = await parser.parse(source["schedule_id"])
            await db.save_snapshot(
                "daily_baseline",
                snapshot_hash,
                snapshot,
                source["schedule_id"],
                source["group_name"],
                source_type=source["source_type"],
                source_key=source["source_key"],
                source_title=source["source_title"],
                source_url=source["source_url"],
            )
            rows.append((source["source_title"], snapshot.fetched_at.strftime("%Y-%m-%d %H:%M"), "эталон сохранен"))
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
                    await safe_delete_message(bot, chat_id, extra_id)
                context_messages[chat_id][context] = [message_ids[0]]
                return
            except (TelegramBadRequest, TelegramNetworkError):
                pass
        sent = await safe_send_message(bot, chat_id, text, reply_markup=reply_markup)
        context_messages[chat_id][context] = [sent.message_id] if sent is not None else message_ids

    async def send_new_context_message(
        bot: Bot,
        chat_id: int,
        context: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        await clear_context_messages(bot, chat_id, context)
        sent = await safe_send_message(bot, chat_id, text, reply_markup=reply_markup)
        context_messages[chat_id][context] = [sent.message_id] if sent is not None else []

    async def safe_send_message(
        bot: Bot,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        retries = 3
        for attempt in range(1, retries + 1):
            try:
                return await bot.send_message(chat_id, text, reply_markup=reply_markup)
            except TelegramBadRequest:
                return None
            except TelegramNetworkError as exc:
                if attempt >= retries:
                    logger.warning("Telegram send_message failed for chat %s: %s", chat_id, exc)
                    return None
                await asyncio.sleep(0.5 * attempt)
        return None

    async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
        retries = 3
        for attempt in range(1, retries + 1):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
                return True
            except TelegramBadRequest:
                return False
            except TelegramNetworkError as exc:
                if attempt >= retries:
                    logger.warning(
                        "Telegram delete_message failed for chat %s message %s: %s",
                        chat_id,
                        message_id,
                        exc,
                    )
                    return False
                await asyncio.sleep(0.5 * attempt)
        return False

    async def clear_context_messages(bot: Bot, chat_id: int, context: str) -> None:
        for message_id in context_messages[chat_id].get(context, []):
            await safe_delete_message(bot, chat_id, message_id)
        context_messages[chat_id][context] = []

    async def try_delete_message(message: Message | None) -> None:
        if message is None:
            return
        retries = 3
        for attempt in range(1, retries + 1):
            try:
                await message.delete()
                return
            except TelegramBadRequest:
                return
            except TelegramNetworkError:
                if attempt >= retries:
                    return
                await asyncio.sleep(0.5 * attempt)

    async def clear_context_messages_except(bot: Bot, chat_id: int, context: str, keep_message_id: int) -> None:
        kept_ids: list[int] = []
        for message_id in context_messages[chat_id].get(context, []):
            if message_id == keep_message_id:
                kept_ids.append(message_id)
                continue
            await safe_delete_message(bot, chat_id, message_id)
        context_messages[chat_id][context] = kept_ids
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

    async def callback_is_rate_limited(callback: CallbackQuery, cooldown: float = 0.8) -> bool:
        user_id = callback.from_user.id
        if user_is_admin(user_id):
            return False
        await wait_rate_limit_queue(callback_rate_limit, callback_rate_locks, user_id, cooldown)
        return False

    async def wait_message_rate_limit(user_id: int, cooldown: float = 0.8) -> None:
        if user_is_admin(user_id):
            return
        await wait_rate_limit_queue(message_rate_limit, message_rate_locks, user_id, cooldown)

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
                    f"Напишите мне для решения: {SUPPORT_CONTACT}"
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
            "\n".join(
                [
                    "<b>Укажи свою группу</b>",
                    "",
                    "Напиши группу в таком же формате, как и на сайте.",
                    "Например: <b>ИСП-25-1</b> или <b>МТО-25</b>",
                    "",
                    "Регистр не важен.",
                    *([ "", error_text ] if error_text else []),
                ]
            ),
        )

    async def ensure_group_selected(bot: Bot, chat_id: int, user_id: int | None) -> bool:
        user = await get_user_record(user_id)
        if user is not None and user.subscription_key and user.subscription_title:
            return True
        await prompt_group_selection(bot, chat_id)
        return False

    async def prompt_schedule_search(bot: Bot, chat_id: int, user_id: int, error_text: str | None = None) -> None:
        awaiting_schedule_search.add(user_id)
        await send_new_context_message(bot, chat_id, "schedule", format_search_prompt(error_text))

    async def perform_schedule_search(bot: Bot, chat_id: int, user_id: int, query: str) -> bool:
        if search_catalog is None:
            await bot.send_message(chat_id, "Поиск временно недоступен.")
            return False
        try:
            target = await search_catalog.find(query)
        except httpx.HTTPError:
            await send_new_context_message(
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
            await send_new_context_message(
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
        await send_new_context_message(
            bot,
            chat_id,
            "schedule",
            ScheduleFormatter.format_search_snapshot(target.title, snapshot["content"]),
            reply_markup=build_search_result_keyboard(),
        )
        return True

    async def handle_group_input(bot: Bot, chat_id: int, user_id: int, raw_text: str) -> bool:
        return await handle_subscription_input(bot, chat_id, user_id, raw_text)

    def build_welcome_text(user, is_editor: bool = False) -> str:
        lines = ["<b>Привет! Я бот расписания колледжа</b>"]
        subscription_line = subscription_caption(
            user.subscription_type if user else None,
            user.subscription_title if user else None,
        )
        if subscription_line:
            label, value = subscription_line.split(":", 1)
            value_lines = [part.strip() for part in value.strip().splitlines() if part.strip()]
            if value_lines:
                lines.extend(["", f"{escape(label)}: <b>{escape(value_lines[0])}</b>"])
                lines.extend(escape(part) for part in value_lines[1:])
            else:
                lines.extend(["", f"{escape(label)}: <b>-</b>"])
        lines.extend(["", "/rasp — посмотреть расписание", "/settings — дополнительно"])
        return "\n".join(lines)

    async def format_subscription_settings_text(user_id: int) -> str:
        user = await db.get_user("telegram", user_id)
        notifications_enabled = user.homework_notifications_enabled if user else True
        lines = ["<b>Дополнительно</b>", ""]
        subscription_line = subscription_caption(
            user.subscription_type if user else None,
            user.subscription_title if user else None,
        )
        if subscription_line:
            label, value = subscription_line.split(":", 1)
            value_lines = [part.strip() for part in value.strip().splitlines() if part.strip()]
            if value_lines:
                lines.append(f"{escape(label)}: <b>{escape(value_lines[0])}</b>")
                lines.extend(escape(part) for part in value_lines[1:])
            else:
                lines.append(f"{escape(label)}: <b>-</b>")
        else:
            lines.append("Подписка: <b>не выбрана</b>")
        lines.append(f"Уведомления: <b>{'включены' if notifications_enabled else 'выключены'}</b>")
        return "\n".join(lines)

    async def build_subscription_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
        user = await db.get_user("telegram", user_id)
        notifications_enabled = user.homework_notifications_enabled if user else True
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="Пройденные пары", callback_data="settings:lesson_counters")],
            [
                InlineKeyboardButton(
                    text="Отключить уведомления" if notifications_enabled else "Включить уведомления",
                    callback_data="settings:toggle_notifications",
                )
            ]
        ]
        if user and user.subscription_key:
            rows.append([InlineKeyboardButton(text="Отписаться", callback_data="settings:clear_group")])
        rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:start")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def format_lesson_counters_text(user_id: int) -> str:
        if not settings.lesson_counters_enabled:
            return "Сейчас данный функционал глобально выключен."
        user = await db.get_user("telegram", user_id)
        if not user or user.subscription_type != "group" or user.schedule_id is None:
            return "Счетчики пар доступны после выбора группы."
        return await lesson_counter_service.format_counters_text(user.schedule_id, html=True)

    async def handle_subscription_input(bot: Bot, chat_id: int, user_id: int, raw_text: str) -> bool:
        subscription_data, error_text = await resolve_subscription_input(raw_text)
        if subscription_data is None:
            await prompt_group_selection(bot, chat_id, error_text)
            return False
        await db.set_user_subscription("telegram", user_id, **subscription_data)
        editor = await user_is_editor(user_id)
        user = await get_user_record(user_id)
        await clear_context_messages(bot, chat_id, "group_select")
        await send_new_context_message(
            bot,
            chat_id,
            "menu",
            build_welcome_text(user, is_editor=editor),
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

    async def send_bells_schedule(bot: Bot, chat_id: int) -> None:
        await clear_context_messages(bot, chat_id, "schedule")
        message_ids: list[int] = []

        weekday_message = await safe_send_message(bot, chat_id, WEEKDAY_BELLS_TEXT)
        if weekday_message is not None:
            message_ids.append(weekday_message.message_id)

        saturday_message = await safe_send_message(bot, chat_id, SATURDAY_BELLS_TEXT)
        if saturday_message is not None:
            message_ids.append(saturday_message.message_id)

        prompt_message = await safe_send_message(
            bot,
            chat_id,
            "Выбери нужный вариант расписания.",
            reply_markup=SCHEDULE_KEYBOARD,
        )
        if prompt_message is not None:
            message_ids.append(prompt_message.message_id)

        context_messages[chat_id]["schedule"] = message_ids

    async def send_schedule_response(bot: Bot, chat_id: int, user_id: int, action: str) -> None:
        if action == "find":
            await prompt_schedule_search(bot, chat_id, user_id)
            return

        if action == "bells":
            await send_bells_schedule(bot, chat_id)
            return

        snapshot_row = await get_saved_snapshot(user_id)
        if snapshot_row is None:
            await send_new_context_message(
                bot,
                chat_id,
                "schedule",
                "Не удалось получить расписание для твоей группы.",
                reply_markup=SCHEDULE_KEYBOARD,
            )
            return

        if action == "today":
            day = get_day_by_offset_from_content(snapshot_row["content"], 0)
            text = ScheduleFormatter.format_day_card(day, "сегодня") if day else empty_day_text("сегодня")
        elif action == "tomorrow":
            day = get_day_by_offset_from_content(snapshot_row["content"], 1)
            text = ScheduleFormatter.format_day_card(day, "завтра") if day else empty_day_text("завтра")
        elif action == "day_after":
            day = get_day_by_offset_from_content(snapshot_row["content"], 2)
            text = ScheduleFormatter.format_day_card(day, "2 дня") if day else empty_day_text("2 дня")
        else:
            await send_schedule_menu(bot, chat_id)
            return

        await send_new_context_message(
            bot,
            chat_id,
            "schedule",
            text,
            reply_markup=SCHEDULE_KEYBOARD,
        )
    @dispatcher.message(CommandStart())
    async def handle_start(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type in GROUP_CHAT_TYPES:
            await register_group_chat(message)
            await message.bot.send_message(
                message.chat.id,
                "Бот добавлен в группу.\n\n"
                "Для настройки уведомлений используй:\n"
                "/group_setup\n"
                "или\n"
                "/group_set ИСП-25-1\n\n"
                "Текущий статус: /group_status",
            )
            return
        search_results.pop(message.from_user.id if message.from_user else 0, None)
        if message.from_user:
            awaiting_schedule_search.discard(message.from_user.id)
        user = await get_user_record(message.from_user.id if message.from_user else None)
        if user is None or not user.subscription_key or not user.subscription_title:
            await prompt_group_selection(message.bot, message.chat.id)
            return
        editor = await user_is_editor(message.from_user.id if message.from_user else None)
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "menu",
            build_welcome_text(user, is_editor=editor),
            reply_markup=START_KEYBOARD,
        )

    @dispatcher.message(Command("settings"))
    async def handle_settings_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type != "private":
            return
        if message.from_user is None:
            return
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "settings",
            await format_subscription_settings_text(message.from_user.id),
            reply_markup=await build_subscription_settings_keyboard(message.from_user.id),
        )

    @dispatcher.message(Command("rasp"))
    async def handle_rasp_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type != "private":
            return
        if not await ensure_group_selected(message.bot, message.chat.id, message.from_user.id if message.from_user else None):
            return
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "schedule",
            "Выбери нужный вариант расписания.",
            reply_markup=SCHEDULE_KEYBOARD,
        )
    @dispatcher.message(Command("cancel"))
    async def handle_cancel_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type != "private":
            return
        if message.from_user:
            awaiting_admin_broadcast_text.discard(message.from_user.id)
            admin_broadcast_drafts.pop(message.from_user.id, None)
        await clear_context_messages(message.bot, message.chat.id, "dz")
        await clear_context_messages(message.bot, message.chat.id, "admin_broadcast")
        search_results.pop(message.from_user.id, None)
        awaiting_schedule_search.discard(message.from_user.id)
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "menu",
            build_welcome_text(
                await get_user_record(message.from_user.id if message.from_user else None),
                is_editor=await user_is_editor(message.from_user.id if message.from_user else None),
            ),
            reply_markup=START_KEYBOARD,
        )

    @dispatcher.message(Command("admin"))
    async def handle_admin_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type != "private":
            return
        if not user_is_admin(message.from_user.id if message.from_user else None):
            await send_new_context_message(message.bot, message.chat.id, "admin", "Команда доступна только администратору.")
            return
        if message.from_user is not None:
            awaiting_admin_broadcast_text.discard(message.from_user.id)
            admin_broadcast_drafts.pop(message.from_user.id, None)
        await clear_context_messages(message.bot, message.chat.id, "admin_broadcast")
        await send_new_context_message(message.bot, message.chat.id, "admin", format_admin_panel(), ADMIN_KEYBOARD)

    @dispatcher.message(Command("group_setup"))
    async def handle_group_setup_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type not in GROUP_CHAT_TYPES:
            return
        await register_group_chat(message)
        if not await user_can_manage_group(message):
            await message.bot.send_message(message.chat.id, "Настройку может выполнять только администратор группы.")
            return
        awaiting_group_subscription_input.add(message.chat.id)
        await message.bot.send_message(
            message.chat.id,
            "<b>Настройка уведомлений группы</b>\n\n"
            "Отправь название группы (например, <b>ИСП-25-1</b>) "
            "или фамилию преподавателя.\n\n"
            "Либо используй команду: <b>/group_set ИСП-25-1</b>",
        )

    @dispatcher.message(Command("group_set"))
    async def handle_group_set_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type not in GROUP_CHAT_TYPES:
            return
        await register_group_chat(message)
        if not await user_can_manage_group(message):
            await message.bot.send_message(message.chat.id, "Настройку может выполнять только администратор группы.")
            return
        raw_text = ""
        if message.text:
            parts = message.text.split(maxsplit=1)
            raw_text = parts[1].strip() if len(parts) > 1 else ""
        if not raw_text:
            await message.bot.send_message(message.chat.id, "Использование: /group_set ИСП-25-1")
            return
        subscription_data, error_text = await resolve_subscription_input(raw_text)
        if subscription_data is None:
            awaiting_group_subscription_input.add(message.chat.id)
            await message.bot.send_message(message.chat.id, error_text)
            return
        await db.set_user_subscription("telegram", message.chat.id, **subscription_data)
        awaiting_group_subscription_input.discard(message.chat.id)
        await message.bot.send_message(
            message.chat.id,
            "Подписка группы обновлена.\n\n"
            + format_group_subscription_status(
                subscription_data.get("subscription_type"),
                subscription_data.get("subscription_title"),
            ),
        )

    @dispatcher.message(Command("group_status"))
    async def handle_group_status_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type not in GROUP_CHAT_TYPES:
            return
        await register_group_chat(message)
        group_record = await db.get_user("telegram", message.chat.id)
        await message.bot.send_message(
            message.chat.id,
            format_group_subscription_status(
                group_record.subscription_type if group_record else None,
                group_record.subscription_title if group_record else None,
            ),
        )

    @dispatcher.message(Command("group_clear"))
    async def handle_group_clear_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type not in GROUP_CHAT_TYPES:
            return
        await register_group_chat(message)
        if not await user_can_manage_group(message):
            await message.bot.send_message(message.chat.id, "Отключить уведомления может только администратор группы.")
            return
        awaiting_group_subscription_input.discard(message.chat.id)
        await db.clear_user_subscription("telegram", message.chat.id)
        await message.bot.send_message(
            message.chat.id,
            "Подписка группы очищена. Уведомления об изменениях расписания в этот чат больше не отправляются.",
        )

    @dispatcher.callback_query(F.data == "menu:start")
    async def handle_menu_start(callback: CallbackQuery) -> None:
        if await callback_is_rate_limited(callback):
            return
        await register_callback_user(callback)
        search_results.pop(callback.from_user.id, None)
        awaiting_schedule_search.discard(callback.from_user.id)
        awaiting_admin_broadcast_text.discard(callback.from_user.id)
        admin_broadcast_drafts.pop(callback.from_user.id, None)
        editor = await user_is_editor(callback.from_user.id)
        user = await get_user_record(callback.from_user.id)
        if callback.message is not None:
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_broadcast")
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "menu",
                build_welcome_text(user, is_editor=editor),
                reply_markup=START_KEYBOARD,
            )
        await safe_callback_answer(callback)

    @dispatcher.callback_query(F.data == "menu:settings")
    async def handle_menu_settings(callback: CallbackQuery) -> None:
        if await callback_is_rate_limited(callback):
            return
        await register_callback_user(callback)
        if callback.message is None:
            await safe_callback_answer(callback)
            return
        settings_text = await format_subscription_settings_text(callback.from_user.id)
        settings_keyboard = await build_subscription_settings_keyboard(callback.from_user.id)
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
    @dispatcher.callback_query(F.data == "start:rasp")
    async def handle_start_rasp(callback: CallbackQuery) -> None:
        if await callback_is_rate_limited(callback):
            return
        await register_callback_user(callback)
        if not await ensure_group_selected(callback.bot, callback.from_user.id, callback.from_user.id):
            await safe_callback_answer(callback)
            return
        await send_schedule_menu(callback.bot, callback.from_user.id)
        await safe_callback_answer(callback)
    @dispatcher.callback_query(F.data.startswith("schedule:"))
    async def handle_schedule_callback(callback: CallbackQuery) -> None:
        if await callback_is_rate_limited(callback):
            return
        await register_callback_user(callback)
        if callback.message is None:
            await safe_callback_answer(callback)
            return
        action = callback.data.split(":", 1)[1]
        await send_schedule_response(
            callback.bot,
            callback.message.chat.id,
            callback.from_user.id,
            action,
        )
        await safe_callback_answer(callback)
    @dispatcher.callback_query(F.data.startswith("settings:"))
    async def handle_settings_callback(callback: CallbackQuery) -> None:
        if await callback_is_rate_limited(callback):
            return
        await register_callback_user(callback)
        if callback.message is None:
            await safe_callback_answer(callback)
            return
        action = callback.data.split(":", 1)[1]
        if action == "lesson_counters":
            await safe_edit_message_text(
                callback.message,
                await format_lesson_counters_text(callback.from_user.id),
                reply_markup=await build_subscription_settings_keyboard(callback.from_user.id),
            )
            await safe_callback_answer(callback)
            return
        if action == "toggle_notifications":
            user = await db.get_user("telegram", callback.from_user.id)
            enabled = not (user.homework_notifications_enabled if user else True)
            await db.set_notifications_enabled("telegram", callback.from_user.id, enabled)
            await safe_edit_message_text(
                callback.message,
                await format_subscription_settings_text(callback.from_user.id),
                reply_markup=await build_subscription_settings_keyboard(callback.from_user.id),
            )
            await safe_callback_answer(callback, "Уведомления обновлены")
            return
        if action == "clear_group":
            await db.clear_user_subscription("telegram", callback.from_user.id)
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
        if await callback_is_rate_limited(callback, cooldown=1.2):
            return
        await register_callback_user(callback)
        if not user_is_admin(callback.from_user.id):
            await safe_callback_answer(callback, "Недостаточно прав.", show_alert=True)
            return
        if callback.message is None:
            await safe_callback_answer(callback)
            return

        action = callback.data.split(":", 1)[1]
        admin_user = await get_user_record(callback.from_user.id)
        if action == "broadcast":
            awaiting_admin_broadcast_text.add(callback.from_user.id)
            admin_broadcast_drafts.pop(callback.from_user.id, None)
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin_broadcast",
                format_admin_broadcast_prompt(),
                reply_markup=ADMIN_BROADCAST_INPUT_KEYBOARD,
            )
            await safe_callback_answer(callback)
            return
        if action == "broadcast_cancel":
            awaiting_admin_broadcast_text.discard(callback.from_user.id)
            admin_broadcast_drafts.pop(callback.from_user.id, None)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_broadcast")
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin",
                format_admin_panel(),
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Рассылка отменена")
            return
        if action == "broadcast_send":
            draft_text = admin_broadcast_drafts.get(callback.from_user.id)
            if not draft_text:
                await safe_callback_answer(callback, "Сначала пришли текст рассылки.", show_alert=True)
                return
            if broadcaster is None:
                await safe_callback_answer(callback, "Сервис рассылки сейчас недоступен.", show_alert=True)
                return
            await broadcaster.broadcast(
                draft_text,
                telegram_message=escape(draft_text),
                vk_message=draft_text,
                campaign_type=CAMPAIGN_ADMIN_BROADCAST,
            )
            awaiting_admin_broadcast_text.discard(callback.from_user.id)
            admin_broadcast_drafts.pop(callback.from_user.id, None)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_broadcast")
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin",
                "<b>Рассылка отправлена.</b>\n\nСообщение поставлено в очередь доставки.",
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Отправлено")
            return
        if action == "status":
            text = await format_admin_status()
            await safe_edit_message_text(callback.message, text, reply_markup=ADMIN_KEYBOARD)
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            await safe_callback_answer(callback)
            return
        if action == "baseline":
            await safe_edit_message_text(
                callback.message,
                "<b>Сохранение эталонов...</b>\n\nПарсю активные источники и записываю новый эталон. Это может занять до минуты.",
            )
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            await safe_callback_answer(callback, "Сохраняю эталоны...")
            report_rows = await save_baseline_for_all_active_sources()
            if not report_rows:
                await safe_edit_message_text(
                    callback.message,
                    "Нет активных групп для сохранения эталона.",
                    reply_markup=ADMIN_KEYBOARD,
                )
                context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
                return
            await safe_edit_message_text(
                callback.message,
                format_group_action_report("Эталоны для активных групп", report_rows),
                reply_markup=ADMIN_KEYBOARD,
            )
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            return
        if action == "close":
            editor = await user_is_editor(callback.from_user.id)
            awaiting_admin_user_search.discard(callback.from_user.id)
            awaiting_admin_broadcast_text.discard(callback.from_user.id)
            admin_broadcast_drafts.pop(callback.from_user.id, None)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_broadcast")
            await safe_edit_message_text(callback.message, build_welcome_text(admin_user, is_editor=editor))
            context_messages[callback.message.chat.id]["menu"] = [callback.message.message_id]
            await safe_callback_answer(callback)
            return
        if action == "back":
            awaiting_admin_user_search.discard(callback.from_user.id)
            awaiting_admin_broadcast_text.discard(callback.from_user.id)
            admin_broadcast_drafts.pop(callback.from_user.id, None)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_broadcast")
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
            awaiting_admin_user_search.discard(callback.from_user.id)
            users = await db.list_users()
            sort_mode = "kind_group"
            if not users:
                text = "<b>Пользователи</b>\n\nПока никто не зарегистрирован."
                reply_markup = build_admin_users_keyboard(page=0, total_pages=1, sort_mode=sort_mode)
            else:
                page = 0
                if action.startswith("users:"):
                    action_parts = action.split(":")
                    if len(action_parts) >= 2 and action_parts[1] in {"kind_group", "kind_teacher", "platform_tg", "platform_vk"}:
                        sort_mode = action_parts[1]
                        if len(action_parts) >= 3 and action_parts[2].isdigit():
                            page = int(action_parts[2])
                    elif len(action_parts) >= 2 and action_parts[1].isdigit():
                        page = int(action_parts[1])

                users = sort_admin_users(users, sort_mode)

                user_rows: list[str] = []
                for user in users:
                    user_rows.append(format_admin_user_row(user))

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
                reply_markup = build_admin_users_keyboard(page=page, total_pages=total_pages, sort_mode=sort_mode)
        elif action.startswith("users_search:"):
            sort_mode = action.split(":", 1)[1] if ":" in action else "kind_group"
            awaiting_admin_user_search.add(callback.from_user.id)
            text = (
                "<b>Поиск пользователя</b>\n\n"
                "Пришли запрос одним сообщением.\n"
                "Поддерживается поиск по айди, @username, имени, фамилии и названию группы."
            )
            reply_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Назад к списку", callback_data=f"admin:users:{sort_mode}:0")],
                    [InlineKeyboardButton(text="Назад в админку", callback_data="admin:back")],
                ]
            )
        elif action == "editors":
            users = [user for user in await db.list_users("telegram")]
            text = "<b>Управление редакторами</b>\n\nНажми на пользователя, чтобы выдать или снять роль редактора."
            reply_markup = build_editors_keyboard(users)
        elif action == "last_change":
            today_prefix = datetime.now().date().isoformat()
            daily_changes = await db.get_daily_change_groups(today_prefix)
            text = format_daily_change_report("Последние изменения за сегодня", daily_changes)
            reply_markup = ADMIN_KEYBOARD
        elif action == "group_info":
            text = format_group_user_stats(await db.get_group_user_stats())
            reply_markup = ADMIN_KEYBOARD
        elif action == "refresh":
            await safe_edit_message_text(
                callback.message,
                "<b>Перепарсинг...</b>\n\nПарсю активные источники и обновляю текущие слепки. Это может занять до минуты.",
            )
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            await safe_callback_answer(callback, "Перепарсинг запущен...")
            report_rows = await refresh_all_active_sources()
            if not report_rows:
                await safe_edit_message_text(
                    callback.message,
                    "Нет активных групп для перепарсинга.",
                    reply_markup=ADMIN_KEYBOARD,
                )
                context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
                return
            await safe_edit_message_text(
                callback.message,
                format_group_action_report("Перепарсинг активных групп", report_rows),
                reply_markup=ADMIN_KEYBOARD,
            )
            context_messages[callback.message.chat.id]["admin"] = [callback.message.message_id]
            return
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
        if await callback_is_rate_limited(callback, cooldown=1.2):
            return
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
    @dispatcher.message(F.text)
    async def handle_text_message(message: Message) -> None:
        await register_message_user(message)
        if message.from_user is None or message.text is None:
            return
        await wait_message_rate_limit(message.from_user.id)

        if message.chat.type in GROUP_CHAT_TYPES:
            await register_group_chat(message)
            if message.chat.id not in awaiting_group_subscription_input:
                return
            if message.text.startswith("/"):
                return
            if not await user_can_manage_group(message):
                return
            subscription_data, error_text = await resolve_subscription_input(message.text.strip())
            if subscription_data is None:
                await message.bot.send_message(message.chat.id, error_text)
                return
            await db.set_user_subscription("telegram", message.chat.id, **subscription_data)
            awaiting_group_subscription_input.discard(message.chat.id)
            await message.bot.send_message(
                message.chat.id,
                "Подписка группы обновлена.\n\n"
                + format_group_subscription_status(
                    subscription_data.get("subscription_type"),
                    subscription_data.get("subscription_title"),
                ),
            )
            return

        text_normalized = message.text.strip().casefold()

        if text_normalized in {"узнать расписание", "расписание"}:
            if not await ensure_group_selected(message.bot, message.chat.id, message.from_user.id):
                return
            await send_schedule_menu(message.bot, message.chat.id)
            return

        if text_normalized in {
            "расписание на сегодня",
            "расписание на завтра",
            "расписание на 2 дня",
            "расписание звонков",
            "найти расписание",
        }:
            if not await ensure_group_selected(message.bot, message.chat.id, message.from_user.id):
                return
            action_map = {
                "расписание на сегодня": "today",
                "расписание на завтра": "tomorrow",
                "расписание на 2 дня": "day_after",
                "расписание звонков": "bells",
                "найти расписание": "find",
            }
            await send_schedule_response(
                message.bot,
                message.chat.id,
                message.from_user.id,
                action_map[text_normalized],
            )
            return

        if (
            user_is_admin(message.from_user.id)
            and message.from_user.id in awaiting_admin_broadcast_text
        ):
            draft_text = message.text.strip()
            if not draft_text:
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_broadcast",
                    format_admin_broadcast_prompt("Текст не должен быть пустым."),
                    reply_markup=ADMIN_BROADCAST_INPUT_KEYBOARD,
                )
                return
            awaiting_admin_broadcast_text.discard(message.from_user.id)
            admin_broadcast_drafts[message.from_user.id] = draft_text
            await send_new_context_message(
                message.bot,
                message.chat.id,
                "admin_broadcast",
                format_admin_broadcast_preview(draft_text),
                reply_markup=ADMIN_BROADCAST_PREVIEW_KEYBOARD,
            )
            return
        if user_is_admin(message.from_user.id) and message.from_user.id in awaiting_admin_user_search:
            query = message.text.strip()
            users = await db.list_users()
            matches = filter_admin_users(users, query)
            awaiting_admin_user_search.discard(message.from_user.id)
            if not matches:
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin",
                    (
                        "<b>Поиск пользователя</b>\n\n"
                        f"По запросу <b>{escape(query)}</b> ничего не найдено."
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="Искать снова", callback_data="admin:users_search:kind_group")],
                            [InlineKeyboardButton(text="Все пользователи", callback_data="admin:users:kind_group:0")],
                        ]
                    ),
                )
                return
            text, reply_markup = format_admin_users_list(
                matches,
                sort_mode="kind_group",
                page=0,
                title=f"Результаты поиска: {escape(query)}",
                summary=f"Найдено пользователей: {len(matches)}",
            )
            await send_new_context_message(
                message.bot,
                message.chat.id,
                "admin",
                text,
                reply_markup=reply_markup,
            )
            return
        user = await get_user_record(message.from_user.id)
        if user is None or not user.subscription_key or not user.subscription_title:
            if message.text.startswith("/"):
                await prompt_group_selection(message.bot, message.chat.id)
                return
            await try_delete_message(message)
            await handle_subscription_input(message.bot, message.chat.id, message.from_user.id, message.text)
            return
        if message.from_user.id in awaiting_schedule_search:
            await try_delete_message(message)
            await perform_schedule_search(message.bot, message.chat.id, message.from_user.id, message.text)
            return
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "menu",
            build_welcome_text(user, is_editor=await user_is_editor(message.from_user.id)),
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

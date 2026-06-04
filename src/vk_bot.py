from __future__ import annotations

import asyncio
from collections import defaultdict
import logging
from datetime import datetime
from html import escape
from pathlib import Path
from time import monotonic
from traceback import format_exception

import httpx
from aiohttp import ClientError, TCPConnector
from vkbottle import API, Keyboard, Text
from vkbottle.bot import Bot, Message
from vkbottle.exception_factory import ErrorHandler
from vkbottle.exception_factory.base_exceptions import VKAPIError
from vkbottle.http import AiohttpClient
from vkbottle.tools import DocUploader

from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
from src.lesson_counters import LessonCounterService, normalize_lesson_text, subject_matches, teacher_matches
from src.notifier import CAMPAIGN_ADMIN_BROADCAST, Broadcaster
from src.parser import ScheduleParser
from src.schedule_search import ScheduleSearchCatalog
from src.schedule_service import ScheduleFormatter, get_day_by_offset, get_day_by_offset_from_content
from src.subscription_utils import (
    make_audience_subscription,
    make_group_subscription,
    make_teacher_subscription,
    subscription_caption,
)
from web_configurator.lesson_editor import load_lesson_config, save_lesson_config, upsert_lesson_subject, validate_lesson_config

PAGE_SIZE = 6
SUPPORT_CONTACT = "tg: t.me/nekoty или vk: vk.com/nekotyy"
SEARCH_NOT_FOUND_TEXT = (
    "Ничего не найдено.\n\n"
    "Что я пробовал найти:\n"
    "- группу, например: ИСП-25-1;\n"
    "- преподавателя по фамилии;\n"
    "- кабинет, например: 101.\n\n"
    "Проверь раскладку, дефисы и пробелы.\n"
    "Если группа введена точно, но не находится, значит проблема, скорее всего, в каталоге групп на стороне сайта."
)

logger = logging.getLogger(__name__)

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


def build_vk_bot(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster | None = None,
    group_catalog: GroupCatalog | None = None,
    search_catalog: ScheduleSearchCatalog | None = None,
) -> Bot | None:
    if not settings.vk_bot_token:
        return None

    api = None
    if settings.vk_disable_ssl_verify:
        api = API(
            settings.vk_bot_token,
            http_client=AiohttpClient(connector=TCPConnector(ssl=False)),
        )

    error_handler = ErrorHandler(redirect_arguments=True)
    bot = Bot(token=settings.vk_bot_token, api=api, error_handler=error_handler)
    search_results: dict[int, dict[str, object]] = {}
    peer_modes: dict[int, str] = {}
    peer_pages: dict[int, dict[str, int]] = defaultdict(dict)
    editor_option_map: dict[int, dict[str, int]] = defaultdict(dict)
    admin_broadcast_drafts: dict[int, str] = {}
    admin_lesson_drafts: dict[int, dict[str, object]] = {}
    admin_lesson_delete_drafts: dict[int, dict[str, object]] = {}
    admin_lesson_delete_one_drafts: dict[int, dict[str, object]] = {}
    message_rate_limit: dict[int, float] = {}
    message_rate_locks: dict[int, asyncio.Lock] = {}
    lesson_counter_service = LessonCounterService(db)

    def make_keyboard(rows: list[list[str]]) -> str:
        keyboard = Keyboard(one_time=False, inline=False)
        for row_index, row in enumerate(rows):
            if row_index:
                keyboard.row()
            for label in row:
                keyboard.add(Text(label))
        return keyboard.get_json()

    def paged_rows(items: list[str], page: int) -> tuple[list[list[str]], int]:
        total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        rows = [[item] for item in chunk]
        nav: list[str] = []
        if page > 0:
            nav.append("Предыдущая страница")
        if page < total_pages - 1:
            nav.append("Следующая страница")
        if nav:
            rows.append(nav)
        return rows, page

    def shorten_button_label(text: str, limit: int = 40) -> str:
        clean = " ".join(text.split())
        if len(clean) <= limit:
            return clean
        return f"{clean[: limit - 3].rstrip()}..."

    def short_error_text(error: Exception) -> str:
        text = f"{type(error).__name__}: {error}"
        if len(text) > 350:
            text = f"{text[:347]}..."
        return text

    def is_rate_limited(bucket: dict[int, float], key: int, cooldown: float) -> bool:
        now = monotonic()
        last_hit = bucket.get(key)
        if last_hit is not None and now - last_hit < cooldown:
            return True
        bucket[key] = now
        return False

    async def wait_rate_limit_queue(user_id: int, cooldown: float = 0.8) -> None:
        if user_is_admin(user_id):
            return
        lock = message_rate_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            now = monotonic()
            next_at = message_rate_limit.get(user_id, now)
            delay = next_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            message_rate_limit[user_id] = monotonic() + cooldown

    async def notify_user_about_error(peer_id: int, error: Exception) -> None:
        try:
            await bot.api.messages.send(
                peer_ids=[peer_id],
                message=(
                    "Произошла ошибка при обработке запроса.\n\n"
                    f"Ошибка: {short_error_text(error)}\n\n"
                    f"Напишите мне для решения: {SUPPORT_CONTACT}"
                ),
                random_id=0,
            )
        except Exception:
            return

    async def notify_admin_about_error(user_id: int | None, peer_id: int | None, error: Exception) -> None:
        if broadcaster is None:
            return
        traceback_text = "".join(format_exception(type(error), error, error.__traceback__))
        if len(traceback_text) > 2500:
            traceback_text = f"...{traceback_text[-2500:]}"
        telegram_text = (
            "<b>Сбой в боте (vk)</b>\n\n"
            f"Пользователь: <b>{user_id if user_id is not None else 'неизвестно'}</b>\n"
            f"Чат: <b>{peer_id if peer_id is not None else 'неизвестно'}</b>\n"
            f"Ошибка: <code>{escape(short_error_text(error))}</code>\n\n"
            f"<pre>{escape(traceback_text)}</pre>"
        )
        vk_text = (
            "Сбой в боте (vk)\n\n"
            f"Пользователь: {user_id if user_id is not None else 'неизвестно'}\n"
            f"Чат: {peer_id if peer_id is not None else 'неизвестно'}\n"
            f"Ошибка: {short_error_text(error)}\n\n"
            f"{traceback_text}"
        )
        await broadcaster.notify_admins(telegram_text, vk_text)

    def user_is_admin(user_id: int | None) -> bool:
        return bool(user_id and settings.admin_vk_id and user_id == settings.admin_vk_id)

    async def user_is_editor(user_id: int | None) -> bool:
        if user_id is None:
            return False
        user = await db.get_user("vk", user_id)
        return bool(user and user.is_editor)
    async def fetch_vk_names(user_ids: list[int]) -> dict[int, str]:
        unique_ids = sorted({user_id for user_id in user_ids if user_id > 0})
        if not unique_ids:
            return {}
        try:
            profiles = await bot.api.users.get(user_ids=unique_ids)
        except (ClientError, OSError) as exc:
            logger.warning("Failed to fetch VK names for %s users: %s", len(unique_ids), exc)
            return {}
        result: dict[int, str] = {}
        for profile in profiles:
            full_name = " ".join(part for part in [profile.first_name, profile.last_name] if part).strip()
            if full_name:
                result[profile.id] = full_name
        return result

    async def sync_vk_user_names(user_ids: list[int]) -> dict[int, str]:
        names = await fetch_vk_names(user_ids)
        for user_id, full_name in names.items():
            existing = await db.get_user("vk", user_id)
            if existing is None:
                continue
            await db.upsert_user(
                platform="vk",
                user_id=user_id,
                username=existing.username,
                full_name=full_name,
                is_admin=existing.is_admin,
                is_editor=existing.is_editor,
            )
        return names

    async def register_user(message: Message) -> None:
        if message.from_id is None:
            return
        names = await fetch_vk_names([message.from_id])
        existing = await db.get_user("vk", message.from_id)
        full_name = names.get(message.from_id) or (existing.full_name if existing else None)
        await db.upsert_user(
            platform="vk",
            user_id=message.from_id,
            username=None,
            full_name=full_name,
            subscription_type=existing.subscription_type if existing else None,
            subscription_key=existing.subscription_key if existing else None,
            subscription_title=existing.subscription_title if existing else None,
            subscription_url=existing.subscription_url if existing else None,
            group_name=existing.group_name if existing else None,
            schedule_id=existing.schedule_id if existing else None,
            is_admin=user_is_admin(message.from_id),
            is_editor=existing.is_editor if existing else False,
        )

    async def send_vk_message(
        peer_id: int,
        message: str,
        *,
        keyboard: str | None = None,
        attachment: str | None = None,
        max_attempts: int = 3,
    ) -> None:
        delay_seconds = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                await bot.api.messages.send(
                    peer_ids=[peer_id],
                    message=message,
                    keyboard=keyboard,
                    attachment=attachment,
                    random_id=0,
                )
                return
            except VKAPIError as exc:
                if getattr(exc, "code", None) != 10 or attempt >= max_attempts:
                    raise
                logger.warning(
                    "VK messages.send temporary error for peer %s on attempt %s/%s: %s",
                    peer_id,
                    attempt,
                    max_attempts,
                    exc,
                )
            except (ClientError, OSError) as exc:
                if attempt >= max_attempts:
                    raise
                logger.warning(
                    "VK messages.send network error for peer %s on attempt %s/%s: %s",
                    peer_id,
                    attempt,
                    max_attempts,
                    exc,
                )
            await asyncio.sleep(delay_seconds)
            delay_seconds *= 2

    async def show_screen(peer_id: int, text: str, keyboard: str | None = None, attachment: str | None = None) -> None:
        await send_vk_message(peer_id=peer_id, message=text, keyboard=keyboard, attachment=attachment)
    def menu_keyboard(user, is_editor: bool, is_admin: bool) -> str:
        rows = [["Расписание"], ["Дополнительно"]]
        if user and user.subscription_type == "teacher":
            rows.insert(1, ["Изменить кабинет" if user.audience_subscription_key else "Подписаться на кабинет"])
        if is_admin:
            rows.append(["Админка"])
        return make_keyboard(rows)

    def group_prompt_text(error_text: str | None = None) -> str:
        lines = [
            "Укажи свою группу",
            "",
            "Напиши ее в формате, как на сайте колледжа.",
            "Например: ИСП-25-1",
            "",
            "Регистр не важен.",
        ]
        if error_text:
            lines.extend(["", error_text])
        return "\n".join(lines)

    def schedule_search_prompt_text(error_text: str | None = None) -> str:
        lines = [
            "Поиск расписания",
            "",
            "Поиск осуществляется по группам, преподавателям, и аудиториям!",
            "",
            "Напиши группу, фамилию преподавателя или аудиторию.",
        ]
        if error_text:
            lines.extend(["", error_text])
        return "\n".join(lines)

    def admin_broadcast_prompt_text(error_text: str | None = None) -> str:
        lines = [
            "Разослать",
            "",
            "Отправь текст сообщения одним сообщением.",
            "После этого покажу предпросмотр.",
        ]
        if error_text:
            lines.extend(["", error_text])
        return "\n".join(lines)

    def admin_broadcast_preview_text(text: str) -> str:
        return "\n".join([
            "Предпросмотр рассылки",
            "",
            text,
        ])

    def schedule_keyboard() -> str:
        return make_keyboard(
            [
                ["Расписание на сегодня"],
                ["Расписание на завтра"],
                ["Расписание на 2 дня"],
                ["Расписание звонков"],
                ["Найти расписание"],
                ["Назад в меню"],
            ]
        )

    async def show_bells_schedule(peer_id: int) -> None:
        await show_screen(peer_id, WEEKDAY_BELLS_TEXT)
        await show_screen(peer_id, SATURDAY_BELLS_TEXT)
        peer_modes[peer_id] = "schedule_menu"
        await show_screen(peer_id, "Выбери нужный вариант расписания.", keyboard=schedule_keyboard())

    def search_result_keyboard() -> str:
        return make_keyboard(
            [
                ["Найти расписание"],
                ["Назад в меню"],
            ]
        )
    def settings_keyboard(notifications_enabled: bool, has_group: bool) -> str:
        rows: list[list[str]] = [
            ["Пройденные пары"],
            ["О проекте"],
            ["Отключить уведомления" if notifications_enabled else "Включить уведомления"]
        ]
        if has_group:
            rows.append(["Отписаться от группы"])
        rows.append(["Назад в меню"])
        return make_keyboard(rows)

    def admin_keyboard() -> str:
        return make_keyboard(
            [
                ["Статус", "Перепарсить"],
                ["Сохранить эталон", "Последнее изменение"],
                ["Скачать БД", "Скачать пары"],
                ['Добавить пару', 'Изменить пару'],
                ['Удалить пару', 'Удалить пары'],
                ["Пользователи", "Информация по группам"],
                ["Разослать", "Тестовая рассылка"],
                ["Закрыть админку"],
            ]
        )

    def admin_user_profile_link(user) -> str:
        if user.platform == "vk":
            return f"https://vk.com/id{user.user_id}"
        if user.username:
            return f"https://t.me/{user.username}"
        return f"tg://user?id={user.user_id}"

    def admin_user_search_haystack(user) -> str:
        parts = [
            user.platform,
            user.username,
            user.full_name,
            user.subscription_title,
            user.group_name,
            str(user.user_id),
        ]
        return " ".join(str(part or "").casefold() for part in parts)

    def filter_admin_users(users: list, query: str) -> list:
        normalized = query.strip().casefold()
        if not normalized:
            return users
        return [user for user in users if normalized in admin_user_search_haystack(user)]

    def admin_broadcast_preview_keyboard() -> str:
        return make_keyboard([["Отправить"], ["Отменить"]])

    def welcome_text(group_name: str | None, is_editor: bool, is_admin: bool) -> str:
        lines = [
            "Бот расписания колледжа",
            "",
            f"Твоя группа: {group_name}" if group_name else "Группа пока не выбрана.",
            "",
            "Используй кнопки ниже для расписания.",
        ]
        if is_admin:
            lines.append("Кнопка «Админка» доступна тебе как администратору.")
        return "\n".join(lines)

    async def settings_text(user_id: int, extra: str | None = None) -> str:
        user = await db.get_user("vk", user_id)
        notifications_enabled = user.homework_notifications_enabled if user else True
        lines = [
            "Дополнительно",
            "",
            f"Группа: {user.group_name if user and user.group_name else 'не выбрана'}",
            f"Уведомления: {'включены' if notifications_enabled else 'выключены'}",
        ]
        if extra:
            lines.extend(["", extra])
        return "\n".join(lines)

    def build_welcome_text(user, is_admin: bool) -> str:
        lines = ["Бот расписания колледжа", ""]
        subscription_line = subscription_caption(
            user.subscription_type if user else None,
            user.subscription_title if user else None,
            user.audience_subscription_title if user else None,
        )
        lines.append(subscription_line if subscription_line else "Подписка пока не выбрана.")
        lines.extend(["", "Используй кнопки ниже для расписания."])
        if is_admin:
            lines.append("Кнопка «Админка» доступна тебе как администратору.")
        return "\n".join(lines)

    async def build_settings_text(user_id: int, extra: str | None = None) -> str:
        user = await db.get_user("vk", user_id)
        notifications_enabled = user.homework_notifications_enabled if user else True
        lines = ["Дополнительно", ""]
        subscription_line = subscription_caption(
            user.subscription_type if user else None,
            user.subscription_title if user else None,
            user.audience_subscription_title if user else None,
        )
        lines.append(subscription_line if subscription_line else "Подписка: не выбрана")
        lines.append(f"Уведомления: {'включены' if notifications_enabled else 'выключены'}")
        if extra:
            lines.extend(["", extra])
        return "\n".join(lines)

    def build_project_about_text() -> str:
        return "\n".join(
            [
                "О проекте",
                "",
                "Бот сделан студентом ОИТ, группы ИСП-25-1, в качестве альтернативы официальному боту, который давно не работает. Я не сотрудник колледжа, а просто энтузиаст, который хочет помочь всем получать актуальную информацию о расписании и изменениях. Я не несу никакой ответственности за точность данных, так как получаю их с официального сайта, и не имею возможности оперативно исправлять ошибки в расписании. Если ты заметил неточности, пожалуйста, сообщи об этом администрации колледжа, чтобы они могли исправить информацию на сайте.",
                "",
                "Профиль: [https://github.com/nekotyy|тык]",
                "Проект: [https://github.com/nekotyy/misis-rasp-bot|тык]",
                "",
                "Если понравилось, поставь звездочку на GitHub ⭐",
            ]
        )

    def format_admin_lesson_prompt(step: str, draft: dict[str, object] | None = None, error_text: str | None = None) -> str:
        header = 'Изменить пару' if draft and draft.get("mode") == "edit" else 'Добавление пары'
        prompt_map = {
            "group": "Шаг 1/5. Укажи группу или schedule_id.",
            "subject": "Шаг 2/5. Укажи дисциплину.",
            "teacher": "Шаг 3/5. Укажи преподавателя.",
            "passed": "Шаг 4/5. Сколько пар уже прошло? (число)",
            "total": "Шаг 5/5. Сколько пар всего? (число)",
        }
        lines = [header, "", prompt_map.get(step, 'Продолжай ввод.')]
        if draft and draft.get("group_name"):
            lines.extend(["", f"Группа: {draft['group_name']}"])
        if error_text:
            lines.extend(["", f"Ошибка: {error_text}"])
        lines.append("\nОтмена: Отменить")
        return "\n".join(lines)

    def format_admin_lesson_preview(draft: dict[str, object]) -> str:
        return "\n".join(
            [
                "Проверь данные",
                "",
                f"Группа: {draft.get('group_name', '')}",
                f"schedule_id: {draft.get('schedule_id', '')}",
                f"Дисциплина: {draft.get('subject', '')}",
                f"Преподаватель: {draft.get('teacher', '')}",
                f"Прошло: {draft.get('passed', 0)}",
                f"Всего: {draft.get('total', 0)}",
                "",
                "Подтвердить добавление пары?",
            ]
        )

    def format_admin_lesson_delete_prompt(
        step: str,
        draft: dict[str, object] | None = None,
        error_text: str | None = None,
    ) -> str:
        prompt_map = {
            "group": "Шаг 1/2. Укажи группу или schedule_id.",
            "confirm": "Шаг 2/2. Подтверди удаление всех пар у группы.",
        }
        lines = ["Удаление пар", "", prompt_map.get(step, "Продолжай ввод.")]
        if draft and draft.get("group_name"):
            lines.extend(["", f"Группа: {draft['group_name']}"])
        if error_text:
            lines.extend(["", f"Ошибка: {error_text}"])
        lines.append("\nОтмена: Отменить")
        return "\n".join(lines)

    def format_admin_lesson_delete_one_prompt(
        step: str,
        draft: dict[str, object] | None = None,
        error_text: str | None = None,
    ) -> str:
        prompt_map = {
            "group": "Шаг 1/4. Укажи группу или schedule_id.",
            "subject": "Шаг 2/4. Укажи дисциплину.",
            "teacher": "Шаг 3/4. Укажи преподавателя.",
            "confirm": "Шаг 4/4. Подтверди удаление пары.",
        }
        lines = ["Удаление пары", "", prompt_map.get(step, "Продолжай ввод.")]
        if draft and draft.get("group_name"):
            lines.extend(["", f"Группа: {draft['group_name']}"])
        if draft and draft.get("subject"):
            lines.append(f"Дисциплина: {draft['subject']}")
        if draft and draft.get("teacher"):
            lines.append(f"Преподаватель: {draft['teacher']}")
        if error_text:
            lines.extend(["", f"Ошибка: {error_text}"])
        lines.append("\nОтмена: Отменить")
        return "\n".join(lines)

    async def send_admin_document(peer_id: int, path: Path, title: str) -> None:
        if not path.exists():
            await show_screen(peer_id, f"{title} не найден.", keyboard=admin_keyboard())
            return
        uploader = DocUploader(bot.api)
        doc = await uploader.upload(path, peer_id=peer_id, title=title)
        await bot.api.messages.send(
            peer_ids=[peer_id],
            message=title,
            attachment=doc,
            random_id=0,
        )

    async def sync_lesson_counters_from_file() -> None:
        try:
            active_catalog = group_catalog or GroupCatalog(settings.schedule_url)
            await active_catalog.ensure_loaded()
            counters = await lesson_counter_service.load_config_file(settings.lesson_counters_path, active_catalog)
            await lesson_counter_service.sync_config(counters)
        except Exception:
            logger.exception("Lesson counters sync failed after admin update.")

    def build_subscription_settings_keyboard(user) -> str:
        notifications_enabled = user.homework_notifications_enabled if user else True
        has_subscription = bool(user and user.subscription_key)
        rows: list[list[str]] = [
            ["Пройденные пары"],
            ["О проекте"],
            ["Отключить уведомления" if notifications_enabled else "Включить уведомления"],
        ]
        if user and user.subscription_type == "teacher":
            rows.append(["Изменить кабинет" if user.audience_subscription_key else "Подписаться на кабинет"])
            if user.audience_subscription_key:
                rows.append(["Убрать кабинет"])
        if has_subscription:
            rows.append(["Отписаться от группы"])
        rows.append(["Назад в меню"])
        return make_keyboard(rows)

    async def lesson_counters_text(user_id: int) -> str:
        if not settings.lesson_counters_enabled:
            return "Сейчас данный функционал глобально выключен."
        user = await db.get_user("vk", user_id)
        if not user or user.subscription_type != "group" or user.schedule_id is None:
            return "Счетчики пар доступны после выбора группы."
        return await lesson_counter_service.format_counters_text(
            user.schedule_id,
            group_name=user.group_name,
        )

    def format_group_action_report(title: str, rows: list[tuple[str, str, str]]) -> str:
        lines = [title, ""]
        if not rows:
            lines.append("Нет записей.")
            return "\n".join(lines)
        lines.append(f"Затронуто групп: {len(rows)}")
        lines.append("")
        for group_name, action_time, action_name in rows:
            lines.append(f"{group_name} | {action_time} | {action_name}")
        return "\n".join(lines)

    def format_daily_change_report(title: str, rows: list[dict]) -> str:
        lines = [title, ""]
        if not rows:
            lines.append("За сегодня изменений пока не было.")
            return "\n".join(lines)
        lines.append(f"Затронуто групп: {len(rows)}")
        lines.append("")
        for row in rows:
            lines.append(f"{row['group_name']} | {row['created_at']}")
        return "\n".join(lines)

    def format_group_user_stats(rows: list[dict[str, int | str]]) -> str:
        lines = ["Информация по группам", ""]
        if not rows:
            lines.append("Пока нет пользователей с выбранной учебной группой.")
            return "\n".join(lines)
        total_users = sum(int(row["users_count"]) for row in rows)
        lines.append(f"Групп найдено: {len(rows)}")
        lines.append(f"Всего пользователей с группой: {total_users}")
        lines.append("")
        lines.append("№ | группа | кол-во юзеров")
        lines.append("")
        for index, row in enumerate(rows, start=1):
            lines.append(f"{index} | {row['group_name']} | {int(row['users_count'])}")
        return "\n".join(lines)

    async def refresh_all_active_groups() -> list[tuple[str, str, str]]:
        groups = await db.get_active_groups()
        if not groups:
            return []

        rows: list[tuple[str, str, str]] = []
        for group in groups:
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

    def schedule_text(day, fallback: str) -> str:
        if day is None or not day.lessons:
            label = fallback if day is None else day.date_label
            return f"Расписание на {label}\n\nПар нет."
        lines = [f"Расписание на {day.date_label}", ""]
        for lesson in day.lessons:
            lines.append(f"{lesson.number}. в {lesson.classroom} по {lesson.subject} у {lesson.teacher}")
        return "\n".join(lines)
    def snapshot_line(title: str, snapshot: dict | None) -> str:
        if snapshot is None:
            return f"{title}: еще не было"
        return f"{title}: {snapshot['created_at']}\nСайт отдал данные: {snapshot['fetched_at']}"

    async def admin_status_text() -> str:
        users = await db.list_users()
        active_groups = await db.get_active_sources()
        active_group_count = sum(1 for item in active_groups if item.get("source_type") == "group")
        active_teacher_count = sum(1 for item in active_groups if item.get("source_type") == "teacher")
        current_snapshot = await db.get_latest_snapshot("current")
        baseline_snapshot = await db.get_latest_snapshot("daily_baseline")
        last_change = await db.get_last_change()
        delivery_stats = await db.get_delivery_stats()
        tg_auto_disabled = await db.count_auto_disabled_users("telegram")
        tg_top_errors = await db.get_top_delivery_errors(platform="telegram", hours=24, limit=3)
        vk_users = sum(1 for user in users if user.platform == "vk")
        tg_users = sum(1 for user in users if user.platform == "telegram")
        last_change_at = last_change["created_at"] if last_change else "еще не было"
        tg_top_error_lines = "\n".join(
            f"- {item['count']}: {item['error_text']}"
            for item in tg_top_errors
        ) or "- нет данных"
        return (
            "Статус бота\n\n"
            f"Пользователей: {len(users)}\n"
            f"Пользователей с VK: {vk_users}\n"
            f"Пользователей с TG: {tg_users}\n"
            f"Активных групп: {active_group_count}\n"
            f"Активных преподавателей: {active_teacher_count}\n"
            f"Последнее изменение: {last_change_at}\n\n"
            "Статистика отправок\n"
            f"Всего событий доставки: {delivery_stats['events_total']}\n"
            f"Успешно / ошибок: {delivery_stats['sent_total']} / {delivery_stats['failed_total']}\n"
            f"За 24 часа (успешно / ошибок): {delivery_stats['sent_last_24h']} / {delivery_stats['failed_last_24h']}\n"
            f"Уведомлений отправлено: {delivery_stats['notifications_sent']}\n"
            f"Админских рассылок отправлено: {delivery_stats['admin_broadcast_sent']}\n"
            f"Служебных уведомлений админу: {delivery_stats['admin_notify_sent']}\n"
            f"Через RabbitMQ / напрямую: {delivery_stats['sent_via_rabbitmq']} / {delivery_stats['sent_direct']}\n"
            f"Ошибок через RabbitMQ / напрямую: {delivery_stats['failed_via_rabbitmq']} / {delivery_stats['failed_direct']}\n"
            f"Доставлено после ретрая: {delivery_stats['sent_after_retry']}\n"
            f"TG (успешно / ошибок): {delivery_stats['tg_sent']} / {delivery_stats['tg_failed']}\n"
            f"TG ошибок через RabbitMQ / напрямую: {delivery_stats['tg_failed_via_rabbitmq']} / {delivery_stats['tg_failed_direct']}\n"
            f"TG ошибок за 24ч: {delivery_stats['tg_failed_last_24h']}\n"
            f"TG перманентных ошибок (всего / 24ч): {delivery_stats['tg_failed_permanent']} / {delivery_stats['tg_failed_permanent_last_24h']}\n"
            f"TG авто-отключено из-за доставки: {tg_auto_disabled}\n"
            f"VK (успешно / ошибок): {delivery_stats['vk_sent']} / {delivery_stats['vk_failed']}\n\n"
            "Топ TG ошибок за 24ч\n"
            f"{tg_top_error_lines}\n\n"
            f"{snapshot_line('Последний обычный парс', current_snapshot)}\n\n"
            f"{snapshot_line('Последний сохраненный эталон', baseline_snapshot)}"
        )

    async def show_main_menu(peer_id: int, user_id: int) -> None:
        peer_modes[peer_id] = "main_menu"
        user = await db.get_user("vk", user_id)
        is_editor = await user_is_editor(user_id)
        is_admin = user_is_admin(user_id)
        await show_screen(
            peer_id,
            build_welcome_text(user, is_admin),
            keyboard=menu_keyboard(user, is_editor, is_admin),
        )

    async def prompt_group_selection(peer_id: int, error_text: str | None = None) -> None:
        peer_modes[peer_id] = "group_select"
        lines = [
            "Укажи свою группу",
            "",
            "Напиши группу в таком же формате, как и на сайте.",
            "Например: ИСП-25-1 или МТО-25",
            "",
            "Регистр не важен.",
        ]
        if error_text:
            lines.extend(["", error_text])
        await show_screen(peer_id, "\n".join(lines))

    async def prompt_audience_selection(peer_id: int, error_text: str | None = None) -> None:
        peer_modes[peer_id] = "audience_select"
        lines = [
            "Укажи кабинет",
            "",
            "Напиши кабинет точно в таком же формате, как на сайте расписания.",
            "Например: 312, 305/2, 508/2М или с-з.",
            "",
            "Эта подписка работает вместе с преподавателем и помогает быстрее замечать изменения по кабинету.",
        ]
        if error_text:
            lines.extend(["", error_text])
        await show_screen(peer_id, "\n".join(lines))

    async def ensure_group_selected(peer_id: int, user_id: int) -> bool:
        user = await db.get_user("vk", user_id)
        if user is not None and user.schedule_id is not None and user.group_name:
            return True
        await prompt_group_selection(peer_id)
        return False

    async def handle_group_input(peer_id: int, user_id: int, text: str) -> bool:
        if group_catalog is None:
            await prompt_group_selection(peer_id, "Справочник групп пока недоступен. Попробуй позже.")
            return False
        group = await group_catalog.find_group(text)
        if group is None:
            await prompt_group_selection(peer_id, "Группа не найдена. Проверь написание и попробуй еще раз.")
            return False
        await db.set_user_group("vk", user_id, group.group_name, group.schedule_id)
        await show_main_menu(peer_id, user_id)
        return True

    async def get_or_fetch_snapshot(user_id: int) -> dict | None:
        user = await db.get_user("vk", user_id)
        if user is None or user.schedule_id is None:
            return None
        snapshot = await db.get_latest_snapshot("current", user.schedule_id)
        if snapshot is not None:
            return snapshot
        snapshot_obj, snapshot_hash = await parser.parse(user.schedule_id)
        await db.save_snapshot("current", snapshot_hash, snapshot_obj, user.schedule_id, user.group_name or snapshot_obj.group_name)
        return await db.get_latest_snapshot("current", user.schedule_id)

    async def ensure_subscription_selected(peer_id: int, user_id: int) -> bool:
        user = await db.get_user("vk", user_id)
        if user is not None and user.subscription_key and user.subscription_title:
            return True
        await prompt_group_selection(peer_id)
        return False

    async def handle_subscription_input(peer_id: int, user_id: int, text: str) -> bool:
        existing_user = await db.get_user("vk", user_id)
        group = await group_catalog.find_group(text) if group_catalog is not None else None
        if group is not None:
            await db.set_user_subscription("vk", user_id, **make_group_subscription(group.group_name, group.schedule_id))
            await db.clear_user_audience_subscription("vk", user_id)
        else:
            if search_catalog is None:
                await prompt_group_selection(peer_id, "Справочник сейчас недоступен. Попробуй позже.")
                return False
            target = await search_catalog.find(text)
            if target is None or target.kind != "teacher":
                await prompt_group_selection(peer_id, SEARCH_NOT_FOUND_TEXT)
                return False
            subscription_data = make_teacher_subscription(target)
            await db.set_user_subscription("vk", user_id, **subscription_data)
            if (
                existing_user is None
                or existing_user.subscription_type != "teacher"
                or existing_user.subscription_key != subscription_data["subscription_key"]
            ):
                await db.clear_user_audience_subscription("vk", user_id)
        await show_main_menu(peer_id, user_id)
        return True

    async def handle_audience_input(peer_id: int, user_id: int, text: str) -> bool:
        if search_catalog is None:
            await prompt_audience_selection(peer_id, "Справочник сейчас недоступен. Попробуй позже.")
            return False
        target = await search_catalog.find(text)
        if target is None or target.kind != "audience":
            await prompt_audience_selection(peer_id, "Кабинет не найден. Проверь написание и попробуй еще раз.")
            return False
        await db.set_user_audience_subscription("vk", user_id, **make_audience_subscription(target))
        await show_main_menu(peer_id, user_id)
        return True

    async def get_or_fetch_subscription_snapshot(user_id: int) -> dict | None:
        user = await db.get_user("vk", user_id)
        if user is None or not user.subscription_key or not user.subscription_title:
            return None
        snapshot = await db.get_latest_snapshot("current", schedule_id=user.schedule_id, source_key=user.subscription_key)
        if snapshot is not None:
            return snapshot
        if user.subscription_type in {"teacher", "audience"} and user.subscription_url:
            snapshot_obj, snapshot_hash = await parser.parse_from_url(user.subscription_url)
        elif user.schedule_id is not None:
            snapshot_obj, snapshot_hash = await parser.parse(user.schedule_id)
        else:
            return None
        return {
            "source_type": user.subscription_type,
            "source_key": user.subscription_key,
            "source_title": user.subscription_title,
            "source_url": user.subscription_url,
            "group_name": user.group_name or snapshot_obj.group_name,
            "schedule_id": user.schedule_id,
            "snapshot_hash": snapshot_hash,
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
            "fetched_at": snapshot_obj.fetched_at.isoformat(timespec="seconds"),
            "created_at": snapshot_obj.fetched_at.isoformat(timespec="seconds"),
        }

    async def perform_schedule_search(peer_id: int, query: str) -> bool:
        if search_catalog is None:
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text("Поиск временно недоступен."))
            return False
        try:
            target = await search_catalog.find(query)
        except httpx.HTTPError:
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text("Сайт расписания временно недоступен. Попробуй еще раз через минуту."))
            return False
        if target is None:
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text(SEARCH_NOT_FOUND_TEXT))
            return False
        try:
            snapshot_obj, _ = await parser.parse_from_url(target.url)
        except httpx.HTTPError:
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text("Сайт расписания временно недоступен. Попробуй еще раз через минуту."))
            return False
        snapshot = {
            "title": target.title,
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
        search_results[peer_id] = snapshot
        peer_modes[peer_id] = "schedule_search_result"
        await show_screen(
            peer_id,
            ScheduleFormatter.format_search_snapshot(target.title, snapshot["content"]),
            keyboard=search_result_keyboard(),
        )
        return True

    @error_handler.register_undefined_error_handler
    async def handle_vk_errors(*args: object, **kwargs: object) -> None:
        error_obj = kwargs.get("error")
        message_obj = kwargs.get("message")

        for arg in args:
            if isinstance(arg, Exception) and error_obj is None:
                error_obj = arg
            elif isinstance(arg, Message) and message_obj is None:
                message_obj = arg

        if not isinstance(error_obj, Exception):
            return

        message = message_obj if isinstance(message_obj, Message) else None
        peer_id = message.peer_id if message is not None else None
        user_id = message.from_id if message is not None else None
        if peer_id is not None:
            await notify_user_about_error(peer_id, error_obj)
        await notify_admin_about_error(user_id, peer_id, error_obj)

    async def show_settings(peer_id: int, user_id: int, extra: str | None = None) -> None:
        user = await db.get_user("vk", user_id)
        peer_modes[peer_id] = "settings"
        await show_screen(
            peer_id,
            await build_settings_text(user_id, extra=extra),
            keyboard=build_subscription_settings_keyboard(user),
        )
    async def refresh_all_active_sources() -> list[tuple[str, str, str]]:
        sources = await db.get_active_sources()
        if not sources:
            return []

        rows: list[tuple[str, str, str]] = []
        for source in sources:
            if source["source_type"] in {"teacher", "audience"}:
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
            if source["source_type"] in {"teacher", "audience"}:
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

    async def show_admin_users(peer_id: int, page: int = 0) -> None:
        users = await db.list_users()
        await sync_vk_user_names([user.user_id for user in users if user.platform == "vk"])
        users = await db.list_users()

        if not users:
            peer_modes[peer_id] = "admin_users"
            await show_screen(peer_id, "Пользователи\n\nПока никто не зарегистрирован.", keyboard=make_keyboard([["Назад в админку"]]))
            return

        user_rows: list[str] = []
        for user in users:
            platform_label = "tg" if user.platform == "telegram" else user.platform
            user_label = user.full_name or "Без имени"
            nick_or_name = user.full_name if user.platform == "vk" else (f"@{user.username}" if user.username else (user.full_name or "-"))
            group_label = user.subscription_title or user.group_name or "-"
            profile_link = admin_user_profile_link(user)
            role_flags: list[str] = []
            if user.is_admin:
                role_flags.append("админ")
            if user.is_editor:
                role_flags.append("редактор")
            role_suffix = f" ({', '.join(role_flags)})" if role_flags else ""
            user_rows.append(f"- {platform_label} | {user_label} | {nick_or_name} | {user.user_id} | {group_label}{role_suffix}\n  {profile_link}")

        page_size = 20
        total_pages = max(1, (len(user_rows) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        peer_pages[peer_id]["admin_users"] = page
        peer_modes[peer_id] = "admin_users"

        start = page * page_size
        end = start + page_size
        lines = [
            "Пользователи",
            "",
            "Формат: платформа | юзер | ник/ФИ | айди | группа (роли)",
            "",
            f"Страница {page + 1}/{total_pages}",
            "",
            *user_rows[start:end],
        ]

        rows: list[list[str]] = []
        nav: list[str] = []
        if page > 0:
            nav.append("Предыдущая страница")
        if page < total_pages - 1:
            nav.append("Следующая страница")
        if nav:
            rows.append(nav)
        rows.append(["Поиск пользователя"])
        rows.append(["Назад в админку"])

        await show_screen(peer_id, "\n".join(lines), keyboard=make_keyboard(rows))
    def build_editor_keyboard(peer_id: int, users: list, page: int) -> str:
        labels: dict[str, int] = {}
        button_texts: list[str] = []
        for user in users:
            if user.platform != "vk":
                continue
            display = user.full_name or user.username or str(user.user_id)
            prefix = "Снять ред." if user.is_editor else "Выдать ред."
            label = shorten_button_label(f"{prefix}: {display} ({user.user_id})")
            labels[label] = user.user_id
            button_texts.append(label)
        rows, actual_page = paged_rows(button_texts, page)
        peer_pages[peer_id]["editors"] = actual_page
        rows.append(["Назад в админку"])
        editor_option_map[peer_id] = labels
        return make_keyboard(rows)
    @bot.on.message()
    async def all_messages_handler(message: Message) -> None:
        await register_user(message)
        if message.peer_id is None or message.from_id is None:
            return

        peer_id = message.peer_id
        user_id = message.from_id
        text = (message.text or "").strip()
        normalized = text.casefold()
        mode = peer_modes.get(peer_id, "main_menu")

        has_attachments = bool(getattr(message, "attachments", None))
        if text and not has_attachments:
            await wait_rate_limit_queue(user_id, 0.8)

        if normalized in {"/start", "start", "начать"}:
            admin_broadcast_drafts.pop(peer_id, None)
            user = await db.get_user("vk", user_id)
            if user is None or not user.subscription_key or not user.subscription_title:
                await prompt_group_selection(peer_id)
            else:
                await show_main_menu(peer_id, user_id)
            return

        if user_is_admin(user_id) and mode in {"admin_broadcast_input", "admin_broadcast_preview"}:
            if text == "Отменить":
                admin_broadcast_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери нужное действие.", keyboard=admin_keyboard())
                return
            if mode == "admin_broadcast_input":
                if not text:
                    await show_screen(peer_id, admin_broadcast_prompt_text("Текст не должен быть пустым."), keyboard=make_keyboard([["Отменить"]]))
                    return
                admin_broadcast_drafts[peer_id] = text
                peer_modes[peer_id] = "admin_broadcast_preview"
                await show_screen(
                    peer_id,
                    admin_broadcast_preview_text(text),
                    keyboard=admin_broadcast_preview_keyboard(),
                )
                return
            draft_text = admin_broadcast_drafts.get(peer_id)
            if text == "Отправить":
                if not draft_text:
                    peer_modes[peer_id] = "admin_broadcast_input"
                    await show_screen(peer_id, admin_broadcast_prompt_text("Сначала отправь текст рассылки."), keyboard=make_keyboard([["Отменить"]]))
                    return
                if broadcaster is None:
                    await show_screen(peer_id, "Сервис рассылки сейчас недоступен.", keyboard=admin_broadcast_preview_keyboard())
                    return
                await broadcaster.broadcast(
                    draft_text,
                    telegram_message=escape(draft_text),
                    vk_message=draft_text,
                    campaign_type=CAMPAIGN_ADMIN_BROADCAST,
                )
                admin_broadcast_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Рассылка отправлена.\n\nСообщение поставлено в очередь доставки.", keyboard=admin_keyboard())
                return
            if text:
                admin_broadcast_drafts[peer_id] = text
            draft_text = admin_broadcast_drafts.get(peer_id)
            if draft_text is None:
                peer_modes[peer_id] = "admin_broadcast_input"
                await show_screen(peer_id, admin_broadcast_prompt_text("Сначала отправь текст рассылки."), keyboard=make_keyboard([["Отменить"]]))
                return
            await show_screen(
                peer_id,
                admin_broadcast_preview_text(draft_text),
                keyboard=admin_broadcast_preview_keyboard(),
            )
            return

        if user_is_admin(user_id) and mode == "admin_lesson_add":
            if text == "Отменить":
                admin_lesson_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери нужное действие.", keyboard=admin_keyboard())
                return
            draft = admin_lesson_drafts.get(peer_id, {"step": "group"})
            step = str(draft.get("step") or "group")
            if step == "group":
                active_catalog = group_catalog or GroupCatalog(settings.schedule_url)
                await active_catalog.ensure_loaded()
                if text.isdigit():
                    schedule_id = int(text)
                    group = await active_catalog.get_by_schedule_id(schedule_id)
                    draft.update(
                        {
                            "schedule_id": schedule_id,
                            "group_name": group.group_name if group else str(schedule_id),
                            "step": "subject",
                        }
                    )
                    admin_lesson_drafts[peer_id] = draft
                    await show_screen(peer_id, format_admin_lesson_prompt("subject", draft), keyboard=make_keyboard([["Отменить"]]))
                    return
                group = await active_catalog.find_group(text)
                if group is None:
                    await show_screen(peer_id, format_admin_lesson_prompt("group", draft, "Группа не найдена."), keyboard=make_keyboard([["Отменить"]]))
                    return
                draft.update({"schedule_id": group.schedule_id, "group_name": group.group_name, "step": "subject"})
                admin_lesson_drafts[peer_id] = draft
                await show_screen(peer_id, format_admin_lesson_prompt("subject", draft), keyboard=make_keyboard([["Отменить"]]))
                return
            if step == "subject":
                if not text:
                    await show_screen(peer_id, format_admin_lesson_prompt("subject", draft, "Дисциплина не может быть пустой."), keyboard=make_keyboard([["Отменить"]]))
                    return
                draft.update({"subject": text, "step": "teacher"})
                admin_lesson_drafts[peer_id] = draft
                await show_screen(peer_id, format_admin_lesson_prompt("teacher", draft), keyboard=make_keyboard([["Отменить"]]))
                return
            if step == "teacher":
                if not text:
                    await show_screen(peer_id, format_admin_lesson_prompt("teacher", draft, "Преподаватель не может быть пустым."), keyboard=make_keyboard([["Отменить"]]))
                    return
                draft.update({"teacher": text, "step": "passed"})
                admin_lesson_drafts[peer_id] = draft
                await show_screen(peer_id, format_admin_lesson_prompt("passed", draft), keyboard=make_keyboard([['Пропустить'], ['Отменить']]))
                return
            if step == "passed":
                if text == 'Пропустить':
                    draft.update({"passed": 0, "step": "total"})
                    admin_lesson_drafts[peer_id] = draft
                    await show_screen(peer_id, format_admin_lesson_prompt("total", draft), keyboard=make_keyboard([['Отменить']]))
                    return
                if not text.isdigit():
                    await show_screen(peer_id, format_admin_lesson_prompt("passed", draft, "Нужно число."), keyboard=make_keyboard([["Отменить"]]))
                    return
                draft.update({"passed": int(text), "step": "total"})
                admin_lesson_drafts[peer_id] = draft
                await show_screen(peer_id, format_admin_lesson_prompt("total", draft), keyboard=make_keyboard([["Отменить"]]))
                return
            if step == "total":
                if not text.isdigit():
                    await show_screen(peer_id, format_admin_lesson_prompt("total", draft, "Нужно число."), keyboard=make_keyboard([["Отменить"]]))
                    return
                draft.update({"total": int(text), "step": "confirm"})
                admin_lesson_drafts[peer_id] = draft
                await show_screen(peer_id, format_admin_lesson_preview(draft), keyboard=make_keyboard([["Подтвердить"], ["Отменить"]]))
                return
            if step == "confirm":
                if text != "Подтвердить":
                    await show_screen(peer_id, "Подтверди или отмени добавление.", keyboard=make_keyboard([["Подтвердить"], ["Отменить"]]))
                    return
                payload = load_lesson_config(settings.lesson_counters_path)
                schedule_id = int(draft.get("schedule_id") or 0)
                group_name = str(draft.get("group_name") or schedule_id)
                subject = str(draft.get("subject") or "").strip()
                teacher = str(draft.get("teacher") or "").strip()
                passed = int(draft.get("passed") or 0)
                total = int(draft.get("total") or 0)
                replaced = upsert_lesson_subject(
                    payload,
                    schedule_id=schedule_id,
                    group_name=group_name,
                    subject=subject,
                    teacher=teacher,
                    passed=passed,
                    total=total,
                )

                active_catalog = group_catalog or GroupCatalog(settings.schedule_url)
                await active_catalog.ensure_loaded()
                normalized, problems = await validate_lesson_config(
                    payload,
                    group_catalog=active_catalog,
                    parser=parser,
                )
                has_errors = any(problem.get("level") == "error" for problem in problems)
                if has_errors:
                    errors = "\n".join(f"- {problem['message']}" for problem in problems)
                    admin_lesson_drafts.pop(peer_id, None)
                    peer_modes[peer_id] = "admin_menu"
                    await show_screen(peer_id, "Ошибка валидации:\n\n" + errors, keyboard=admin_keyboard())
                    return
                save_lesson_config(settings.lesson_counters_path, normalized)
                await sync_lesson_counters_from_file()
                admin_lesson_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, 'Пара изменена.' if replaced or draft.get("mode") == "edit" else 'Пара добавлена.', keyboard=admin_keyboard())
                return

        if user_is_admin(user_id) and mode == "admin_lesson_delete":
            if text == "Отменить":
                admin_lesson_delete_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери нужное действие.", keyboard=admin_keyboard())
                return
            draft = admin_lesson_delete_drafts.get(peer_id, {"step": "group"})
            step = str(draft.get("step") or "group")
            if step == "group":
                active_catalog = group_catalog or GroupCatalog(settings.schedule_url)
                await active_catalog.ensure_loaded()
                if text.isdigit():
                    schedule_id = int(text)
                    group = await active_catalog.get_by_schedule_id(schedule_id)
                    draft.update(
                        {
                            "schedule_id": schedule_id,
                            "group_name": group.group_name if group else str(schedule_id),
                            "step": "confirm",
                        }
                    )
                    admin_lesson_delete_drafts[peer_id] = draft
                    await show_screen(
                        peer_id,
                        format_admin_lesson_delete_prompt("confirm", draft),
                        keyboard=make_keyboard([["Подтвердить"], ["Отменить"]]),
                    )
                    return
                group = await active_catalog.find_group(text)
                if group is None:
                    await show_screen(
                        peer_id,
                        format_admin_lesson_delete_prompt("group", draft, "Группа не найдена."),
                        keyboard=make_keyboard([["Отменить"]]),
                    )
                    return
                draft.update({"schedule_id": group.schedule_id, "group_name": group.group_name, "step": "confirm"})
                admin_lesson_delete_drafts[peer_id] = draft
                await show_screen(
                    peer_id,
                    format_admin_lesson_delete_prompt("confirm", draft),
                    keyboard=make_keyboard([["Подтвердить"], ["Отменить"]]),
                )
                return
            if step == "confirm":
                if text != "Подтвердить":
                    await show_screen(
                        peer_id,
                        "Подтверди или отмени удаление.",
                        keyboard=make_keyboard([["Подтвердить"], ["Отменить"]]),
                    )
                    return
                payload = load_lesson_config(settings.lesson_counters_path)
                schedule_id = int(draft.get("schedule_id") or 0)
                groups = payload.setdefault("groups", [])
                before_count = len(groups)
                groups[:] = [
                    item
                    for item in groups
                    if not (isinstance(item, dict) and int(item.get("schedule_id") or 0) == schedule_id)
                ]
                if len(groups) == before_count:
                    admin_lesson_delete_drafts.pop(peer_id, None)
                    peer_modes[peer_id] = "admin_menu"
                    await show_screen(peer_id, "Группа не найдена в конфиге.", keyboard=admin_keyboard())
                    return
                save_lesson_config(settings.lesson_counters_path, payload)
                await sync_lesson_counters_from_file()
                admin_lesson_delete_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Пары удалены.", keyboard=admin_keyboard())
                return

        if user_is_admin(user_id) and mode == "admin_lesson_delete_one":
            if text == "Отменить":
                admin_lesson_delete_one_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери нужное действие.", keyboard=admin_keyboard())
                return
            draft = admin_lesson_delete_one_drafts.get(peer_id, {"step": "group"})
            step = str(draft.get("step") or "group")
            if step == "group":
                active_catalog = group_catalog or GroupCatalog(settings.schedule_url)
                await active_catalog.ensure_loaded()
                if text.isdigit():
                    schedule_id = int(text)
                    group = await active_catalog.get_by_schedule_id(schedule_id)
                    draft.update(
                        {
                            "schedule_id": schedule_id,
                            "group_name": group.group_name if group else str(schedule_id),
                            "step": "subject",
                        }
                    )
                    admin_lesson_delete_one_drafts[peer_id] = draft
                    await show_screen(peer_id, format_admin_lesson_delete_one_prompt("subject", draft), keyboard=make_keyboard([["Отменить"]]))
                    return
                group = await active_catalog.find_group(text)
                if group is None:
                    await show_screen(
                        peer_id,
                        format_admin_lesson_delete_one_prompt("group", draft, "Группа не найдена."),
                        keyboard=make_keyboard([["Отменить"]]),
                    )
                    return
                draft.update({"schedule_id": group.schedule_id, "group_name": group.group_name, "step": "subject"})
                admin_lesson_delete_one_drafts[peer_id] = draft
                await show_screen(peer_id, format_admin_lesson_delete_one_prompt("subject", draft), keyboard=make_keyboard([["Отменить"]]))
                return
            if step == "subject":
                if not text:
                    await show_screen(
                        peer_id,
                        format_admin_lesson_delete_one_prompt("subject", draft, "Дисциплина не может быть пустой."),
                        keyboard=make_keyboard([["Отменить"]]),
                    )
                    return
                draft.update({"subject": text, "step": "teacher"})
                admin_lesson_delete_one_drafts[peer_id] = draft
                await show_screen(peer_id, format_admin_lesson_delete_one_prompt("teacher", draft), keyboard=make_keyboard([["Отменить"]]))
                return
            if step == "teacher":
                if not text:
                    await show_screen(
                        peer_id,
                        format_admin_lesson_delete_one_prompt("teacher", draft, "Преподаватель не может быть пустым."),
                        keyboard=make_keyboard([["Отменить"]]),
                    )
                    return
                draft.update({"teacher": text, "step": "confirm"})
                admin_lesson_delete_one_drafts[peer_id] = draft
                await show_screen(
                    peer_id,
                    format_admin_lesson_delete_one_prompt("confirm", draft),
                    keyboard=make_keyboard([["Подтвердить"], ["Отменить"]]),
                )
                return
            if step == "confirm":
                if text != "Подтвердить":
                    await show_screen(
                        peer_id,
                        "Подтверди или отмени удаление.",
                        keyboard=make_keyboard([["Подтвердить"], ["Отменить"]]),
                    )
                    return
                payload = load_lesson_config(settings.lesson_counters_path)
                schedule_id = int(draft.get("schedule_id") or 0)
                subject_input = str(draft.get("subject") or "").strip()
                teacher_input = str(draft.get("teacher") or "").strip()
                groups = payload.setdefault("groups", [])
                group = next(
                    (
                        item
                        for item in groups
                        if isinstance(item, dict) and int(item.get("schedule_id") or 0) == schedule_id
                    ),
                    None,
                )
                if group is None:
                    admin_lesson_delete_one_drafts.pop(peer_id, None)
                    peer_modes[peer_id] = "admin_menu"
                    await show_screen(peer_id, "Группа не найдена в конфиге.", keyboard=admin_keyboard())
                    return
                subjects = group.get("subjects", [])
                if not isinstance(subjects, list):
                    admin_lesson_delete_one_drafts.pop(peer_id, None)
                    peer_modes[peer_id] = "admin_menu"
                    await show_screen(peer_id, "Некорректная структура subjects.", keyboard=admin_keyboard())
                    return
                subject_norm = normalize_lesson_text(subject_input)
                teacher_norm = normalize_lesson_text(teacher_input)
                kept: list[dict[str, object]] = []
                removed = 0
                for item in subjects:
                    if not isinstance(item, dict):
                        kept.append(item)
                        continue
                    item_subject = str(item.get("subject") or "")
                    item_teacher = str(item.get("teacher") or "")
                    if subject_matches(subject_norm, item_subject) and teacher_matches(teacher_norm, item_teacher):
                        removed += 1
                        continue
                    kept.append(item)
                if removed == 0:
                    admin_lesson_delete_one_drafts.pop(peer_id, None)
                    peer_modes[peer_id] = "admin_menu"
                    await show_screen(peer_id, "Пара не найдена в конфиге.", keyboard=admin_keyboard())
                    return
                group["subjects"] = kept
                save_lesson_config(settings.lesson_counters_path, payload)
                await sync_lesson_counters_from_file()
                admin_lesson_delete_one_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Пара удалена.", keyboard=admin_keyboard())
                return

        user = await db.get_user("vk", user_id)
        if user is None or not user.subscription_key or not user.subscription_title:
            if text in {"/admin", "Админка"}:
                pass
            elif text.startswith("/") or text in {"Дополнительно", "Настройки", "Расписание"}:
                await prompt_group_selection(peer_id)
                return
            else:
                await handle_subscription_input(peer_id, user_id, text)
                return

        if text in {"Назад в меню", "Закрыть админку"}:
            search_results.pop(peer_id, None)
            admin_broadcast_drafts.pop(peer_id, None)
            admin_lesson_drafts.pop(peer_id, None)
            admin_lesson_delete_drafts.pop(peer_id, None)
            admin_lesson_delete_one_drafts.pop(peer_id, None)
            await show_main_menu(peer_id, user_id)
            return

        if text in {"Дополнительно", "Настройки"}:
            await show_settings(peer_id, user_id)
            return

        if text == "Пройденные пары":
            user = await db.get_user("vk", user_id)
            await show_screen(
                peer_id,
                await lesson_counters_text(user_id),
                keyboard=build_subscription_settings_keyboard(user),
            )
            return

        if text == "О проекте":
            user = await db.get_user("vk", user_id)
            await show_screen(
                peer_id,
                build_project_about_text(),
                keyboard=build_subscription_settings_keyboard(user),
            )
            return

        if text == "Отключить уведомления":
            await db.set_notifications_enabled("vk", user_id, False)
            await show_settings(peer_id, user_id, extra="Уведомления отключены.")
            return

        if text == "Включить уведомления":
            await db.set_notifications_enabled("vk", user_id, True)
            await show_settings(peer_id, user_id, extra="Уведомления включены.")
            return

        if text in {"Подписаться на кабинет", "Изменить кабинет"}:
            user = await db.get_user("vk", user_id)
            if not user or user.subscription_type != "teacher":
                await show_settings(peer_id, user_id, extra="Сначала выбери преподавателя.")
                return
            await prompt_audience_selection(peer_id)
            return

        if text == "Убрать кабинет":
            await db.clear_user_audience_subscription("vk", user_id)
            await show_settings(peer_id, user_id, extra="Кабинет отвязан.")
            return

        if text == "Отписаться от группы":
            await db.clear_user_subscription("vk", user_id)
            await prompt_group_selection(peer_id, "Ты отписался от своей группы. Выбери новую, когда захочешь.")
            return

        if text in {"/rasp", "Расписание"}:
            if not await ensure_subscription_selected(peer_id, user_id):
                return
            peer_modes[peer_id] = "schedule_menu"
            await show_screen(peer_id, "Выбери нужный вариант расписания.", keyboard=schedule_keyboard())
            return

        if text == "Найти расписание":
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text())
            return

        if mode == "audience_select":
            await handle_audience_input(peer_id, user_id, text)
            return

        if mode == "schedule_menu":
            if text == "Расписание звонков":
                await show_bells_schedule(peer_id)
                return
            snapshot = await get_or_fetch_subscription_snapshot(user_id)
            if snapshot is None:
                await show_screen(peer_id, "Не удалось получить расписание для твоей группы.", keyboard=schedule_keyboard())
                return
            if text == "Расписание на сегодня":
                await show_screen(peer_id, schedule_text(get_day_by_offset_from_content(snapshot["content"], 0), "сегодня"), keyboard=schedule_keyboard())
                return
            if text == "Расписание на завтра":
                await show_screen(peer_id, schedule_text(get_day_by_offset_from_content(snapshot["content"], 1), "завтра"), keyboard=schedule_keyboard())
                return
            if text == "Расписание на 2 дня":
                await show_screen(peer_id, schedule_text(get_day_by_offset_from_content(snapshot["content"], 2), "2 дня"), keyboard=schedule_keyboard())
                return

        if mode == "schedule_search":
            await perform_schedule_search(peer_id, text)
            return

        if mode == "schedule_search_result":
            snapshot = search_results.get(peer_id)
            if snapshot is None:
                peer_modes[peer_id] = "schedule_search"
                await show_screen(peer_id, schedule_search_prompt_text("Сначала найди расписание."))
                return
            if text not in {"Найти расписание", "Назад в меню"}:
                await show_screen(
                    peer_id,
                    "Поиск отдает расписание сразу по всем дням. Нажми «Найти расписание», чтобы ввести новый запрос.",
                    keyboard=search_result_keyboard(),
                )
                return


        if text in {"/admin", "Админка"}:
            if not user_is_admin(user_id):
                await show_screen(
                    peer_id,
                    "Эта кнопка доступна только администратору.",
                    keyboard=menu_keyboard(await db.get_user("vk", user_id), await user_is_editor(user_id), user_is_admin(user_id)),
                )
                return
            admin_broadcast_drafts.pop(peer_id, None)
            peer_modes[peer_id] = "admin_menu"
            await show_screen(peer_id, "Админ-панель\n\nВыбери нужное действие.", keyboard=admin_keyboard())
            return

        if user_is_admin(user_id):
            if text == "Разослать":
                admin_broadcast_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_broadcast_input"
                await show_screen(peer_id, admin_broadcast_prompt_text(), keyboard=make_keyboard([["Отменить"]]))
                return
            if text == "Скачать БД":
                await send_admin_document(peer_id, settings.database_path, "bot.db")
                return
            if text == "Скачать пары":
                await send_admin_document(peer_id, settings.lesson_counters_path, "lesson_counters.json")
                return
            if text == "Добавить пару":
                admin_lesson_drafts[peer_id] = {"step": "group"}
                peer_modes[peer_id] = "admin_lesson_add"
                await show_screen(peer_id, format_admin_lesson_prompt("group"), keyboard=make_keyboard([["Отменить"]]))
                return
            if text == 'Изменить пару':
                draft = {"step": "group", "mode": "edit"}
                admin_lesson_drafts[peer_id] = draft
                peer_modes[peer_id] = "admin_lesson_add"
                await show_screen(peer_id, format_admin_lesson_prompt("group", draft), keyboard=make_keyboard([['Отменить']]))
                return
            if text == "Удалить пары":
                admin_lesson_delete_drafts[peer_id] = {"step": "group"}
                peer_modes[peer_id] = "admin_lesson_delete"
                await show_screen(peer_id, format_admin_lesson_delete_prompt("group"), keyboard=make_keyboard([["Отменить"]]))
                return
            if text == "Удалить пару":
                admin_lesson_delete_one_drafts[peer_id] = {"step": "group"}
                peer_modes[peer_id] = "admin_lesson_delete_one"
                await show_screen(peer_id, format_admin_lesson_delete_one_prompt("group"), keyboard=make_keyboard([["Отменить"]]))
                return
            if text == "Статус":
                await show_screen(peer_id, await admin_status_text(), keyboard=admin_keyboard())
                return
            if text == "Перепарсить":
                await show_screen(
                    peer_id,
                    "Перепарсинг запущен...\n\nПарсю активные источники и обновляю текущие слепки. Это может занять до минуты.",
                )
                report_rows = await refresh_all_active_sources()
                if not report_rows:
                    await show_screen(peer_id, "Нет активных групп для перепарсинга.", keyboard=admin_keyboard())
                    return
                await show_screen(
                    peer_id,
                    format_group_action_report("Перепарсинг активных групп", report_rows),
                    keyboard=admin_keyboard(),
                )
                return
            if text == "Сохранить эталон":
                await show_screen(
                    peer_id,
                    "Сохранение эталонов запущено...\n\nПарсю активные источники и записываю новый эталон. Это может занять до минуты.",
                )
                report_rows = await save_baseline_for_all_active_sources()
                if not report_rows:
                    await show_screen(peer_id, "Нет активных групп для сохранения эталона.", keyboard=admin_keyboard())
                    return
                await show_screen(
                    peer_id,
                    format_group_action_report("Эталоны для активных групп", report_rows),
                    keyboard=admin_keyboard(),
                )
                return
            if text == "Последнее изменение":
                today_prefix = datetime.now().date().isoformat()
                daily_changes = await db.get_daily_change_groups(today_prefix)
                response = format_daily_change_report("Последние изменения за сегодня", daily_changes)
                await show_screen(peer_id, response, keyboard=admin_keyboard())
                return
            if text == "Пользователи":
                await show_admin_users(peer_id, 0)
                return
            if text == "Поиск пользователя":
                peer_modes[peer_id] = "admin_user_search"
                await show_screen(
                    peer_id,
                    "Поиск пользователя\n\nНапиши запрос одним сообщением. Поддерживается поиск по айди, @username, имени, фамилии и названию группы.",
                    keyboard=make_keyboard([["Все пользователи"], ["Назад в админку"]]),
                )
                return
            if text == "Информация по группам":
                await show_screen(peer_id, format_group_user_stats(await db.get_group_user_stats()), keyboard=admin_keyboard())
                return
            if mode == "admin_users":
                if text == "Следующая страница":
                    await show_admin_users(peer_id, peer_pages[peer_id].get("admin_users", 0) + 1)
                    return
                if text == "Предыдущая страница":
                    await show_admin_users(peer_id, peer_pages[peer_id].get("admin_users", 0) - 1)
                    return
                if text == "Поиск пользователя":
                    peer_modes[peer_id] = "admin_user_search"
                    await show_screen(
                        peer_id,
                        "Поиск пользователя\n\nНапиши запрос одним сообщением. Поддерживается поиск по айди, @username, имени, фамилии и названию группы.",
                        keyboard=make_keyboard([["Все пользователи"], ["Назад в админку"]]),
                    )
                    return
            if mode == "admin_user_search":
                if text == "Искать снова":
                    await show_screen(
                        peer_id,
                        "Поиск пользователя\n\nНапиши запрос одним сообщением. Поддерживается поиск по айди, @username, имени, фамилии и названию группы.",
                        keyboard=make_keyboard([["Все пользователи"], ["Назад в админку"]]),
                    )
                    return
                if text == "Все пользователи":
                    await show_admin_users(peer_id, 0)
                    return
                users = await db.list_users()
                await sync_vk_user_names([user.user_id for user in users if user.platform == "vk"])
                users = await db.list_users()
                matches = filter_admin_users(users, text)
                peer_modes[peer_id] = "admin_user_search"
                if not matches:
                    await show_screen(
                        peer_id,
                        f"Поиск пользователя\n\nПо запросу «{text}» ничего не найдено.",
                        keyboard=make_keyboard([["Искать снова"], ["Все пользователи"], ["Назад в админку"]]),
                    )
                    return
                lines = [
                    "Результаты поиска",
                    "",
                    f"Запрос: {text}",
                    f"Найдено: {len(matches)}",
                    "",
                    "Формат: платформа | юзер | ник/ФИ | айди | группа (роли)",
                    "",
                ]
                for user in matches[:20]:
                    platform_label = "tg" if user.platform == "telegram" else user.platform
                    user_label = user.full_name or "Без имени"
                    nick_or_name = user.full_name if user.platform == "vk" else (f"@{user.username}" if user.username else (user.full_name or "-"))
                    group_label = user.subscription_title or user.group_name or "-"
                    role_flags: list[str] = []
                    if user.is_admin:
                        role_flags.append("админ")
                    if user.is_editor:
                        role_flags.append("редактор")
                    role_suffix = f" ({', '.join(role_flags)})" if role_flags else ""
                    lines.append(f"- {platform_label} | {user_label} | {nick_or_name} | {user.user_id} | {group_label}{role_suffix}")
                    lines.append(f"  {admin_user_profile_link(user)}")
                if len(matches) > 20:
                    lines.extend(["", f"Показаны первые 20 из {len(matches)}."])
                await show_screen(
                    peer_id,
                    "\n".join(lines),
                    keyboard=make_keyboard([["Искать снова"], ["Все пользователи"], ["Назад в админку"]]),
                )
                return
            if text == "Редакторы":
                users = await db.list_users("vk")
                await sync_vk_user_names([user.user_id for user in users])
                users = await db.list_users("vk")
                peer_modes[peer_id] = "admin_editors"
                await show_screen(peer_id, "Управление редакторами\n\nВыбери пользователя, чтобы выдать или снять роль редактора.", keyboard=build_editor_keyboard(peer_id, users, 0))
                return
            if mode == "admin_editors":
                users = await db.list_users("vk")
                await sync_vk_user_names([user.user_id for user in users])
                users = await db.list_users("vk")
                if text == "Следующая страница":
                    await show_screen(peer_id, "Управление редакторами\n\nВыбери пользователя, чтобы выдать или снять роль редактора.", keyboard=build_editor_keyboard(peer_id, users, peer_pages[peer_id].get("editors", 0) + 1))
                    return
                if text == "Предыдущая страница":
                    await show_screen(peer_id, "Управление редакторами\n\nВыбери пользователя, чтобы выдать или снять роль редактора.", keyboard=build_editor_keyboard(peer_id, users, peer_pages[peer_id].get("editors", 0) - 1))
                    return
                if text in editor_option_map.get(peer_id, {}):
                    target_id = editor_option_map[peer_id][text]
                    target = await db.get_user("vk", target_id)
                    if target is not None:
                        await db.set_editor("vk", target_id, not target.is_editor)
                    users = await db.list_users("vk")
                    await show_screen(peer_id, "Управление редакторами\n\nРоль обновлена. Выбери пользователя, чтобы продолжить.", keyboard=build_editor_keyboard(peer_id, users, peer_pages[peer_id].get("editors", 0)))
                    return
            if text == "Тестовая рассылка":
                users = await db.get_users_for_platform("vk")
                for user in users:
                    try:
                        await bot.api.messages.send(peer_ids=[user.user_id], message="Тестовое уведомление: бот активен и рассылка работает.", random_id=0)
                    except Exception:
                        continue
                await show_screen(peer_id, "Тестовая рассылка\n\nСообщение отправлено всем зарегистрированным пользователям VK.", keyboard=admin_keyboard())
                return

        await show_main_menu(peer_id, user_id)

    return bot


from __future__ import annotations

import asyncio
import logging
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from time import monotonic
from traceback import format_exception
from typing import Any

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
from src.lesson_counters import LessonCounterService, normalize_lesson_text, subject_matches, teacher_matches
from src.notifier import CAMPAIGN_ADMIN_BROADCAST, Broadcaster, BroadcastProgress
from src.parser import ScheduleParser
from src.schedule_search import ScheduleSearchCatalog
from src.schedule_service import ScheduleFormatter, get_day_by_offset_from_content
from src.subscription_utils import (
    make_audience_subscription,
    make_group_subscription,
    make_teacher_subscription,
    subscription_caption,
)
from web_configurator.lesson_editor import (
    apply_imported_lessons_config,
    format_import_preview,
    load_lesson_config,
    parse_imported_json_payload,
    save_lesson_config,
    upsert_lesson_subject,
    validate_lesson_config,
)

logger = logging.getLogger(__name__)

def generate_progress_bar(percent: int, length: int = 10) -> str:
    percent = max(0, min(100, percent))
    filled_length = int(round(length * percent / 100))
    bar = "█" * filled_length + "░" * (length - filled_length)
    return f"[{bar}] {percent}%"


def format_broadcast_progress_status(
    text: str,
    progress: BroadcastProgress,
    html: bool = True,
) -> str:
    platform_map = {
        "all": "Глобально (везде)",
        "telegram": "Telegram",
        "vk": "ВКонтакте",
    }
    audience_map = {
        "all": "Всем",
        "students": "Студентам",
        "teachers": "Преподавателям",
    }
    platform_str = platform_map.get(progress.target_platform, "Глобально (везде)")
    audience_str = audience_map.get(progress.target_audience, "Всем")

    total = progress.total_users
    processed = progress.processed_count
    percent = int(round(processed / total * 100)) if total > 0 else 100
    bar_str = generate_progress_bar(percent)

    if html:
        escaped_text = escape(text)
        if not progress.is_finished:
            return "\n".join([
                "<b>Рассылка выполняется...</b>",
                "",
                "ℹ️ <b>Служебная информация:</b>",
                f"• <b>Платформа:</b> {platform_str}",
                f"• <b>Кому:</b> {audience_str}",
                f"• <b>Время начала:</b> {progress.started_at}",
                "",
                "<b>Текст рассылки:</b>",
                escaped_text,
                "",
                "🔄 <b>Прогресс рассылки:</b>",
                "• <b>Состояние:</b> В процессе...",
                f"• <b>Отправлено:</b> {processed} / {total} ({percent}%)",
                f"• <b>Прогресс:</b> <code>{bar_str}</code>",
            ])
        else:
            success_pct = int(round(progress.success_count / total * 100)) if total > 0 else 100
            return "\n".join([
                "<b>Рассылка завершена</b>",
                "",
                "ℹ️ <b>Служебная информация:</b>",
                f"• <b>Платформа:</b> {platform_str}",
                f"• <b>Кому:</b> {audience_str}",
                f"• <b>Время начала:</b> {progress.started_at}",
                f"• <b>Время окончания:</b> {progress.finished_at or '-'}",
                "",
                "<b>Текст рассылки:</b>",
                escaped_text,
                "",
                "📊 <b>Итоги рассылки:</b>",
                f"• <b>Всего получателей:</b> {total}",
                f"• <b>Успешно доставлено:</b> {progress.success_count}",
                f"• <b>Ошибки доставки:</b> {progress.failed_count}",
                f"• <b>Процент успеха:</b> {success_pct}%",
                f"• <b>Прогресс:</b> <code>{generate_progress_bar(100)}</code>",
            ])
    else:
        if not progress.is_finished:
            return "\n".join([
                "Рассылка выполняется...",
                "",
                "Служебная информация:",
                f"• Платформа: {platform_str}",
                f"• Кому: {audience_str}",
                f"• Время начала: {progress.started_at}",
                "",
                "Текст рассылки:",
                text,
                "",
                "Прогресс рассылки:",
                "• Состояние: В процессе...",
                f"• Отправлено: {processed} / {total} ({percent}%)",
                f"• Прогресс: {bar_str}",
            ])
        else:
            success_pct = int(round(progress.success_count / total * 100)) if total > 0 else 100
            return "\n".join([
                "Рассылка завершена",
                "",
                "Служебная информация:",
                f"• Платформа: {platform_str}",
                f"• Кому: {audience_str}",
                f"• Время начала: {progress.started_at}",
                f"• Время окончания: {progress.finished_at or '-'}",
                "",
                "Текст рассылки:",
                text,
                "",
                "Итоги рассылки:",
                f"• Всего получателей: {total}",
                f"• Успешно доставлено: {progress.success_count}",
                f"• Ошибки доставки: {progress.failed_count}",
                f"• Процент успеха: {success_pct}%",
                f"• Прогресс: {generate_progress_bar(100)}",
            ])


async def format_personalization_settings_text(user_id: int, db: Database) -> str:
    user = await db.get_user("telegram", user_id)
    has_sticker = bool(user and user.custom_sticker_file_id)
    sticker_status = "<b>Прикреплен</b>" if has_sticker else "<i>Не установлен</i>"

    return "\n".join([
        "<b>Персонализация уведомлений</b>",
        "",
        f"• Ваш стикер: {sticker_status}",
        "",
        "Вы можете установить свой стикер. Он будет отправляться перед уведомлениями об изменениях и при вызове меню расписания.",
    ])


async def build_personalization_keyboard(user_id: int, db: Database) -> InlineKeyboardMarkup:
    user = await db.get_user("telegram", user_id)
    has_sticker = bool(user and user.custom_sticker_file_id)

    rows = [
        [InlineKeyboardButton(text="Установить стикер", callback_data="settings:pers_set_sticker")],
    ]
    if has_sticker:
        rows.append([InlineKeyboardButton(text="Предпросмотр стикера", callback_data="settings:pers_preview_sticker")])
        rows.append([InlineKeyboardButton(text="Сбросить стикер", callback_data="settings:pers_clear_sticker")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


SUPPORT_CONTACT = "tg: t.me/nekoty или vk: vk.com/nekotyy"
STAR_ICON = '<tg-emoji emoji-id="5465453857578888257">⭐</tg-emoji>'
GROUP_CHAT_TYPES = {"group", "supergroup"}
SEARCH_NOT_FOUND_TEXT = (
    "Ничего не найдено.\n\n"
    "Что я пробовал найти:\n"
    "- группу, например: ИСП-25-1;\n"
    "- преподавателя по фамилии;\n"
    "- кабинет, например: 101.\n\n"
    "Проверь раскладку, дефисы и пробелы.\n"
    "Если группа введена точно, но не находится, значит проблема, скорее всего, в каталоге групп на стороне сайта."
)


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
            InlineKeyboardButton(text="Скачать БД", callback_data="admin:download_db"),
            InlineKeyboardButton(text="Скачать пары", callback_data="admin:download_counters"),
        ],
        [
            InlineKeyboardButton(text="Добавить пару", callback_data="admin:lesson_add"),
            InlineKeyboardButton(text="Изменить пару", callback_data="admin:lesson_edit"),
        ],
        [
            InlineKeyboardButton(text="Импорт пар из JSON", callback_data="admin:import_lessons"),
        ],
        [
            InlineKeyboardButton(text="Удалить пару", callback_data="admin:lesson_delete_one"),
            InlineKeyboardButton(text="Удалить пары", callback_data="admin:lesson_delete"),
        ],
        [
            InlineKeyboardButton(text="Очистить БД", callback_data="admin:cleandb"),
            InlineKeyboardButton(text="Закрыть админку", callback_data="admin:close"),
        ],
    ]
)

ADMIN_KEYBOARD_LIMITED = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Статус", callback_data="admin:status"),
            InlineKeyboardButton(text="Перепарсить", callback_data="admin:refresh"),
        ],
        [
            InlineKeyboardButton(text="Пользователи", callback_data="admin:users"),
            InlineKeyboardButton(text="Информация по группам", callback_data="admin:group_info"),
        ],
        [
            InlineKeyboardButton(text="Последнее изменение", callback_data="admin:last_change"),
            InlineKeyboardButton(text="Скачать пары", callback_data="admin:download_counters"),
        ],
        [
            InlineKeyboardButton(text="Закрыть админку", callback_data="admin:close"),
        ],
    ]
)


def format_help_main_text() -> str:
    return "\n".join([
        "<b>Справочное руководство (Wiki)</b>",
        "",
        "Выберите раздел документации для получения подробной информации:",
    ])


def build_help_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. Настройка групп и бесед", callback_data="help:group_setup")],
        [InlineKeyboardButton(text="2. Поиск и подписки", callback_data="help:search_setup")],
        [InlineKeyboardButton(text="3. Уведомления и расписание", callback_data="help:notifications_setup")],
        [InlineKeyboardButton(text="4. Персонализация", callback_data="help:personalization_setup")],
        [InlineKeyboardButton(text="5. Список команд", callback_data="help:commands_list")],
        [InlineKeyboardButton(text="Назад в настройки", callback_data="menu:settings")],
    ])


def format_help_group_setup_text() -> str:
    return "\n".join([
        "<b>Справочник: Настройка групп и бесед</b>",
        "",
        "<b>1. Telegram (Групповые чаты):</b>",
        "• Добавьте бота в ваш групповой чат Telegram.",
        "• Назначьте бота администратором чата (с правом отправки сообщений).",
        "• В чате отправьте команду <code>/startgroup</code> (или <code>/group</code>).",
        "• Укажите название вашей учебной группы (например: <code>ИСП-25-1</code>).",
        "<i>Примечание: настраивать группу могут администраторы чата или пользователь, добавивший бота.</i>",
        "",
        "<b>2. ВКонтакте (Беседы):</b>",
        "• Зайдите в сообщество бота ВКонтакте и нажмите кнопку <b>«Добавить в беседу»</b> (под обложкой или в меню действия) либо перейдите по ссылке <code>vk.ru/app6441755_-237526231</code>.",
        "• Выберите нужную беседу и подтвердите добавление.",
        "• В настройках беседы разрешите боту доступ к переписке (или назначьте администратором).",
        "• Отправьте в беседу команду <code>/startgroup</code> или фразы <code>Настройка группы</code> / <code>Группа</code>.",
        "• Укажите название вашей учебной группы (например: <code>ИСП-25-1</code>).",
        "",
        "<b>3. Изменение и сброс группы в чате:</b>",
        "• Чтобы привязать другую группу, администратор чата может повторно отправить <code>/startgroup</code> и указать новое название.",
        "• После настройки бот автоматически присылает ежедневно расписание и изменения пар.",
    ])


def format_help_personal_setup_text() -> str:
    return "\n".join([
        "<b>Справочник: Поиск расписания и подписки</b>",
        "",
        "<b>1. Поиск учебной группы:</b>",
        "• Введите название группы в формате сайта колледжа (например: <code>ИСП-25-1</code>). Регистр букв не имеет значения.",
        "",
        "<b>2. Поиск преподавателя:</b>",
        "• Введите фамилию преподавателя (например: <code>Иванов</code>). Бот найдет личное расписание преподавателя.",
        "",
        "<b>3. Поиск аудитории / кабинета:</b>",
        "• Введите номер кабинета (например: <code>101</code>). Бот покажет расписание занятий в этом кабинете.",
        "",
        "<b>4. Подписка преподавателя на кабинет:</b>",
        "• Преподаватели могут привязать основной кабинет в меню <b>Дополнительно</b>, чтобы отслеживать замену аудиторий отдельно.",
    ])


def format_help_notifications_text() -> str:
    return "\n".join([
        "<b>Справочник: Уведомления и расписание</b>",
        "",
        "<b>1. Автоматические уведомления об изменениях:</b>",
        "• Бот автоматически отслеживает изменения на сайте колледжа. При публикации замен подписчики получают мгновенное сообщение.",
        "",
        "<b>2. Управление рассылкой:</b>",
        "• В меню <b>Дополнительно</b> вы можете в любой момент временно отключить или снова включить рассылку уведомлений.",
        "",
        "<b>3. Статистика пройденных пар:</b>",
        "• В меню <b>Дополнительно -> Пройденные пары</b> доступна подробная статистика проведённых и оставшихся занятий.",
    ])


def format_help_personalization_text() -> str:
    return "\n".join([
        "<b>Справочник: Персонализация сообщений</b>",
        "",
        "<b>1. Кастомный стикер:</b>",
        "• В меню <b>Дополнительно -> Персонализация</b> вы можете прикрепить собственный стикер.",
        "• Установленный стикер будет присылаться перед уведомлениями об изменениях и сообщениями расписания.",
        "",
        "<b>2. Сброс стикера:</b>",
        "• Отвязать стикер можно в том же меню персонализации в любой момент.",
    ])


def format_help_commands_text() -> str:
    return "\n".join([
        "<b>Справочник: Полный список команд</b>",
        "",
        "<b>Основные команды:</b>",
        "• <code>/start</code> — запуск бота и вывод главного меню",
        "• <code>/rasp</code> — посмотреть расписание",
        "• <code>/settings</code> — открыть меню Дополнительно",
        "• <code>/startgroup</code> — мастер настройки бота в групповом чате или беседе",
        "• <code>/group</code> — быстрый вызов настройки группы",
    ])


def is_group_setup_command(text: str | None) -> bool:
    if not text:
        return False
    raw = text.strip().casefold()
    return (
        raw.startswith("/startgroup")
        or raw.startswith("/group")
        or raw in {"/startgroup", "/group", "startgroup", "group"}
    )


async def resolve_subscription_input(
    raw_text: str,
    target_group_catalog: GroupCatalog | None = None,
    target_search_catalog: ScheduleSearchCatalog | None = None,
) -> tuple[dict | None, str | None]:
    g_cat = target_group_catalog
    s_cat = target_search_catalog
    group = None
    if g_cat is not None:
        try:
            group = await g_cat.find_group(raw_text)
        except Exception as exc:
            logger.warning("Error finding group in GroupCatalog: %s", exc)
            return None, "Сайт расписания колледжа сейчас недоступен (ошибка подключения к серверу). Попробуйте еще раз через несколько минут."

    if group is not None:
        return make_group_subscription(group.group_name, group.schedule_id), None

    if g_cat is not None and getattr(g_cat, "last_error", None) is not None and not getattr(g_cat, "_groups_by_name", {}):
        return None, "Сайт расписания колледжа сейчас недоступен. Не удалось загрузить данные с официального сайта. Попробуйте еще раз через несколько минут."

    if s_cat is not None:
        try:
            target = await s_cat.find(raw_text)
            if target is not None and target.kind == "teacher":
                return make_teacher_subscription(target), None
        except Exception as exc:
            logger.warning("Error in search_catalog.find: %s", exc)
            return None, "Сайт расписания колледжа сейчас временно недоступен. Попробуйте еще раз через несколько минут."

    return None, SEARCH_NOT_FOUND_TEXT

ADMIN_FILE_MAX_BYTES = 49 * 1024 * 1024

ADMIN_BROADCAST_INPUT_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Отменить", callback_data="admin:broadcast_cancel")],
    ]
)

def build_admin_broadcast_preview_keyboard(
    target_platform: str = "all",
    target_audience: str = "all",
) -> InlineKeyboardMarkup:
    plat_all = "✅ Везде" if target_platform == "all" else "Везде"
    plat_tg = "✅ В ТГ" if target_platform == "telegram" else "В ТГ"
    plat_vk = "✅ В ВК" if target_platform == "vk" else "В ВК"

    aud_all = "✅ Всем" if target_audience == "all" else "Всем"
    aud_students = "✅ Студентам" if target_audience == "students" else "Студентам"
    aud_teachers = "✅ Преподавателям" if target_audience == "teachers" else "Преподавателям"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=plat_all, callback_data="admin:broadcast_plat:all"),
                InlineKeyboardButton(text=plat_tg, callback_data="admin:broadcast_plat:telegram"),
                InlineKeyboardButton(text=plat_vk, callback_data="admin:broadcast_plat:vk"),
            ],
            [
                InlineKeyboardButton(text=aud_all, callback_data="admin:broadcast_aud:all"),
                InlineKeyboardButton(text=aud_students, callback_data="admin:broadcast_aud:students"),
                InlineKeyboardButton(text=aud_teachers, callback_data="admin:broadcast_aud:teachers"),
            ],
            [InlineKeyboardButton(text="Подтвердить", callback_data="admin:broadcast_confirm")],
            [InlineKeyboardButton(text="Отменить", callback_data="admin:broadcast_cancel")],
        ]
    )


ADMIN_BROADCAST_PREVIEW_KEYBOARD = build_admin_broadcast_preview_keyboard("all", "all")

ADMIN_IMPORT_LESSONS_INPUT_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Отменить", callback_data="admin:import_lessons_cancel")],
    ]
)

ADMIN_IMPORT_LESSONS_PREVIEW_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить импорт", callback_data="admin:import_lessons_confirm")],
        [InlineKeyboardButton(text="Отменить", callback_data="admin:import_lessons_cancel")],
    ]
)

DONATE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ 15", callback_data="donate:stars:15"),
            InlineKeyboardButton(text="⭐ 25", callback_data="donate:stars:25"),
        ],
        [
            InlineKeyboardButton(text="⭐ 50", callback_data="donate:stars:50"),
            InlineKeyboardButton(text="⭐ 100", callback_data="donate:stars:100"),
        ],
        [
            InlineKeyboardButton(text="Отправить свое количество", callback_data="donate:stars:custom"),
        ],
    ]
)

DONATE_CUSTOM_CANCEL_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Отменить", callback_data="donate:cancel")],
    ]
)


def format_user_profile_link(platform: str, user_id: int | None, username: str | None = None, html: bool = True) -> str:
    if user_id is None:
        return "неизвестно"

    plat = platform.lower()
    clean_username = username.lstrip("@").strip() if username else None

    if plat == "telegram":
        if clean_username:
            link_text = f"t.me/{clean_username}"
            if html:
                return f'<a href="https://{link_text}">{link_text}</a> (ID: <code>{user_id}</code>)'
            return f"{link_text} (ID: {user_id})"
        else:
            if html:
                return f'<code>{user_id}</code> (<a href="tg://user?id={user_id}">профиль</a>)'
            return f"ID: {user_id}"
    else:  # vk
        link_text = f"vk.ru/{clean_username}" if clean_username else f"vk.ru/id{user_id}"
        if html:
            return f'<a href="https://{link_text}">{link_text}</a> (ID: <code>{user_id}</code>)'
        return f"{link_text} (ID: {user_id})"


def build_dispatcher(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster | None = None,
    group_catalog: GroupCatalog | None = None,
    search_catalog: ScheduleSearchCatalog | None = None,
    schedule_jobs: Any | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()
    context_messages: dict[int, dict[str, list[int]]] = defaultdict(dict)
    search_results: dict[int, dict[str, object]] = {}
    awaiting_schedule_search: set[int] = set()
    awaiting_group_subscription_input: set[int] = set()
    awaiting_audience_subscription_input: set[int] = set()
    awaiting_admin_broadcast_text: set[int] = set()
    awaiting_admin_user_search: set[int] = set()
    admin_user_search_state: dict[int, dict[str, str]] = {}
    admin_broadcast_drafts: dict[int, dict] = {}
    admin_lesson_drafts: dict[int, dict[str, object]] = {}
    awaiting_admin_lesson_input: set[int] = set()
    admin_lesson_delete_drafts: dict[int, dict[str, object]] = {}
    awaiting_admin_lesson_delete_input: set[int] = set()
    admin_lesson_delete_one_drafts: dict[int, dict[str, object]] = {}
    awaiting_admin_lesson_delete_one_input: set[int] = set()
    awaiting_admin_import_lessons: set[int] = set()
    admin_import_lessons_drafts: dict[int, dict] = {}
    awaiting_custom_donate_stars: set[int] = set()
    awaiting_custom_sticker: set[int] = set()
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
        is_admin = bool(
            user.id in settings.admin_telegram_ids or user.id in settings.limited_admin_telegram_ids
        )
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
        g_cat = group_catalog
        s_cat = search_catalog
        group = None
        if g_cat is not None:
            try:
                group = await g_cat.find_group(raw_text)
            except Exception as exc:
                logger.warning("Error finding group in GroupCatalog: %s", exc)
                return None, "Сайт расписания колледжа сейчас недоступен (ошибка подключения к серверу). Попробуйте еще раз через несколько минут."

        if group is not None:
            return make_group_subscription(group.group_name, group.schedule_id), None

        if g_cat is not None and getattr(g_cat, "last_error", None) is not None and not getattr(g_cat, "_groups_by_name", {}):
            return None, "Сайт расписания колледжа сейчас недоступен. Не удалось загрузить данные с официального сайта. Попробуйте еще раз через несколько минут."

        if s_cat is not None:
            try:
                target = await s_cat.find(raw_text)
                if target is not None and target.kind == "teacher":
                    return make_teacher_subscription(target), None
            except Exception as exc:
                logger.warning("Error in search_catalog.find: %s", exc)
                return None, "Сайт расписания колледжа сейчас временно недоступен. Попробуйте еще раз через несколько минут."

        return None, SEARCH_NOT_FOUND_TEXT

    async def resolve_audience_input(raw_text: str) -> tuple[dict | None, str | None]:
        if search_catalog is None:
            return None, "Справочник сейчас недоступен. Попробуй позже."
        try:
            target = await search_catalog.find(raw_text)
        except httpx.HTTPError:
            return None, "Сайт расписания временно недоступен. Попробуй еще раз через минуту."
        if target is None or target.kind != "audience":
            return None, "Кабинет не найден. Проверь написание и попробуй еще раз."
        return make_audience_subscription(target), None

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
        if user_id is None:
            return False
        return user_id in settings.admin_telegram_ids or user_id in settings.limited_admin_telegram_ids

    def user_is_full_admin(user_id: int | None) -> bool:
        if user_id is None:
            return False
        return user_id in settings.admin_telegram_ids

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
        try:
            if user.subscription_type == "teacher" and user.subscription_url:
                snapshot_obj, snapshot_hash = await parser.parse_from_url(user.subscription_url)
            elif user.schedule_id is not None:
                snapshot_obj, snapshot_hash = await parser.parse(user.schedule_id)
            else:
                return None
        except httpx.HTTPError as exc:
            logger.warning(
                "Failed to fetch Telegram subscription snapshot for user %s (%s): %s",
                user_id,
                user.subscription_key,
                exc,
            )
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

    async def build_start_keyboard(user_id: int | None) -> InlineKeyboardMarkup:
        user = await get_user_record(user_id)
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="Узнать расписание", callback_data="start:rasp")],
            [InlineKeyboardButton(text="Дополнительно", callback_data="menu:settings")],
        ]
        if user and user.subscription_type == "teacher":
            rows.insert(
                1,
                [
                    InlineKeyboardButton(
                        text="Изменить кабинет" if user.audience_subscription_key else "Подписаться на кабинет",
                        callback_data="settings:audience_setup",
                    )
                ],
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

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



    def format_admin_broadcast_preview(
        text: str,
        target_platform: str = "all",
        target_audience: str = "all",
    ) -> str:
        platform_map = {
            "all": "Глобально (везде)",
            "telegram": "Telegram",
            "vk": "ВКонтакте",
        }
        audience_map = {
            "all": "Всем",
            "students": "Студентам",
            "teachers": "Преподавателям",
        }
        platform_str = platform_map.get(target_platform, "Глобально (везде)")
        audience_str = audience_map.get(target_audience, "Всем")

        return "\n".join([
            "<b>Предпросмотр рассылки</b>",
            "",
            "ℹ️ <b>Служебная информация:</b>",
            f"• <b>Платформа:</b> {platform_str}",
            f"• <b>Кому:</b> {audience_str}",
            "",
            "<b>Текст сообщения:</b>",
            escape(text),
        ])

    def format_admin_lesson_prompt(step: str, draft: dict[str, object] | None = None, error_text: str | None = None) -> str:
        header = "<b>Изменение пары</b>" if draft and draft.get("mode") == "edit" else "<b>Добавление пары</b>"
        prompt_map = {
            "group": "Шаг 1/5. Укажи группу или schedule_id.",
            "subject": "Шаг 2/5. Укажи дисциплину.",
            "teacher": "Шаг 3/5. Укажи преподавателя.",
            "passed": "Шаг 4/5. Сколько пар уже прошло? (число)",
            "total": "Шаг 5/5. Сколько пар всего? (число)",
        }
        lines = [header, "", prompt_map.get(step, "Продолжай ввод.")]
        if draft and draft.get("group_name"):
            lines.extend(["", f"Группа: <b>{escape(str(draft['group_name']))}</b>"])
        if error_text:
            lines.extend(["", f"<b>Ошибка:</b> {escape(error_text)}"])
        lines.append("\nОтмена: /cancel")
        return "\n".join(lines)

    def format_admin_lesson_preview(draft: dict[str, object]) -> str:
        return "\n".join(
            [
                "<b>Проверь данные</b>",
                "",
                f"Группа: <b>{escape(str(draft.get('group_name', ''))) }</b>",
                f"schedule_id: <b>{escape(str(draft.get('schedule_id', ''))) }</b>",
                f"Дисциплина: <b>{escape(str(draft.get('subject', ''))) }</b>",
                f"Преподаватель: <b>{escape(str(draft.get('teacher', ''))) }</b>",
                f"Прошло: <b>{escape(str(draft.get('passed', 0)))}</b>",
                f"Всего: <b>{escape(str(draft.get('total', 0)))}</b>",
                "",
                "Подтвердить добавление пары?",
            ]
        )

    def admin_lesson_input_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Пропустить", callback_data="admin:lesson_skip_passed")],
                [InlineKeyboardButton(text="Отменить", callback_data="admin:lesson_cancel")],
            ]
        )

    def admin_lesson_confirm_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Подтвердить", callback_data="admin:lesson_confirm")],
                [InlineKeyboardButton(text="Отменить", callback_data="admin:lesson_cancel")],
            ]
        )

    def admin_lesson_force_confirm_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Сохранить несмотря на ошибки", callback_data="admin:lesson_confirm_force")],
                [InlineKeyboardButton(text="Отменить", callback_data="admin:lesson_cancel")],
            ]
        )

    def format_admin_lesson_delete_prompt(
        step: str,
        draft: dict[str, object] | None = None,
        error_text: str | None = None,
    ) -> str:
        header = "<b>Удаление пар</b>"
        prompt_map = {
            "group": "Шаг 1/2. Укажи группу или schedule_id.",
            "confirm": "Шаг 2/2. Подтверди удаление всех пар у группы.",
        }
        lines = [header, "", prompt_map.get(step, "Продолжай ввод.")]
        if draft and draft.get("group_name"):
            lines.extend(["", f"Группа: <b>{escape(str(draft['group_name']))}</b>"])
        if error_text:
            lines.extend(["", f"<b>Ошибка:</b> {escape(error_text)}"])
        lines.append("\nОтмена: /cancel")
        return "\n".join(lines)

    def admin_lesson_delete_confirm_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Подтвердить", callback_data="admin:lesson_delete_confirm")],
                [InlineKeyboardButton(text="Отменить", callback_data="admin:lesson_delete_cancel")],
            ]
        )

    def format_admin_lesson_delete_one_prompt(
        step: str,
        draft: dict[str, object] | None = None,
        error_text: str | None = None,
    ) -> str:
        header = "<b>Удаление пары</b>"
        prompt_map = {
            "group": "Шаг 1/4. Укажи группу или schedule_id.",
            "subject": "Шаг 2/4. Укажи дисциплину.",
            "teacher": "Шаг 3/4. Укажи преподавателя.",
            "confirm": "Шаг 4/4. Подтверди удаление пары.",
        }
        lines = [header, "", prompt_map.get(step, "Продолжай ввод.")]
        if draft and draft.get("group_name"):
            lines.extend(["", f"Группа: <b>{escape(str(draft['group_name']))}</b>"])
        if draft and draft.get("subject"):
            lines.extend([f"Дисциплина: <b>{escape(str(draft['subject']))}</b>"])
        if draft and draft.get("teacher"):
            lines.extend([f"Преподаватель: <b>{escape(str(draft['teacher']))}</b>"])
        if error_text:
            lines.extend(["", f"<b>Ошибка:</b> {escape(error_text)}"])
        lines.append("\nОтмена: /cancel")
        return "\n".join(lines)

    def admin_lesson_delete_one_confirm_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Подтвердить", callback_data="admin:lesson_delete_one_confirm")],
                [InlineKeyboardButton(text="Отменить", callback_data="admin:lesson_delete_one_cancel")],
            ]
        )

    async def send_admin_document(
        bot: Bot,
        chat_id: int,
        file_path: Path,
        caption: str,
    ) -> bool:
        if not file_path.exists():
            return False
        size = file_path.stat().st_size
        if size <= ADMIN_FILE_MAX_BYTES:
            await bot.send_document(chat_id, document=FSInputFile(file_path), caption=caption)
            return True

        with tempfile.TemporaryDirectory(prefix="tg-admin-") as tmp_dir:
            zip_path = Path(tmp_dir) / f"{caption}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(file_path, arcname=caption)
            if zip_path.stat().st_size > ADMIN_FILE_MAX_BYTES:
                return False
            try:
                await bot.send_document(chat_id, document=FSInputFile(zip_path), caption=zip_path.name)
                return True
            except TelegramEntityTooLarge:
                return False

    async def sync_lesson_counters_from_file() -> None:
        try:
            active_catalog = group_catalog or GroupCatalog(settings.schedule_url)
            await active_catalog.ensure_loaded()
            counters = await lesson_counter_service.load_config_file(settings.lesson_counters_path, active_catalog)
            await lesson_counter_service.sync_config(counters)
        except Exception:
            logger.exception("Lesson counters sync failed after admin update.")

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
        return f"tg://user?id={user.user_id}"

    def external_profile_link(user: object) -> str:
        if getattr(user, "platform", None) == "vk":
            return f"https://vk.com/id{user.user_id}"
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
        id_link = f"<a href=\"{escape(profile_link, quote=True)}\">{user.user_id}</a>"
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
        search_results_mode: bool = False,
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
        return "\n".join(lines), build_admin_users_keyboard(
            page=page,
            total_pages=total_pages,
            sort_mode=sort_mode,
            search_results_mode=search_results_mode,
        )

    def build_admin_users_keyboard(
        page: int,
        total_pages: int,
        sort_mode: str,
        *,
        search_results_mode: bool = False,
    ) -> InlineKeyboardMarkup:
        nav_row: list[InlineKeyboardButton] = []
        callback_prefix = "admin:users_found" if search_results_mode else "admin:users"
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="<", callback_data=f"{callback_prefix}:{sort_mode}:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text=">", callback_data=f"{callback_prefix}:{sort_mode}:{page + 1}"))

        next_kind_mode, next_platform_mode = get_admin_users_toggle_modes(sort_mode)
        kind_button_text = "Сначала преподы" if next_kind_mode == "kind_teacher" else "Сначала группы"
        platform_button_text = "Сначала VK" if next_platform_mode == "platform_vk" else "Сначала TG"

        rows: list[list[InlineKeyboardButton]] = []
        if nav_row:
            rows.append(nav_row)
        rows.append([InlineKeyboardButton(text="Поиск", callback_data=f"admin:users_search:{sort_mode}")])
        rows.append([InlineKeyboardButton(text=kind_button_text, callback_data=f"{callback_prefix}:{next_kind_mode}:0")])
        rows.append([InlineKeyboardButton(text=platform_button_text, callback_data=f"{callback_prefix}:{next_platform_mode}:0")])
        if search_results_mode:
            rows.append([InlineKeyboardButton(text="\u0412\u0441\u0435 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0438", callback_data="admin:users:kind_group:0")])
        rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin:back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def format_admin_search_results(
        user_id: int,
        *,
        page: int,
        sort_mode: str | None = None,
    ) -> tuple[str, InlineKeyboardMarkup] | None:
        state = admin_user_search_state.get(user_id)
        if state is None:
            return None

        users = await db.list_users()
        matches = filter_admin_users(users, state["query"])
        resolved_sort_mode = sort_mode or state.get("sort_mode") or "kind_group"
        if not matches:
            admin_user_search_state.pop(user_id, None)
            return (
                (
                    "<b>\u041f\u043e\u0438\u0441\u043a \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f</b>\n\n"
                    f"\u041f\u043e \u0437\u0430\u043f\u0440\u043e\u0441\u0443 <b>{escape(state['query'])}</b> \u043d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e."
                ),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="\u0418\u0441\u043a\u0430\u0442\u044c \u0441\u043d\u043e\u0432\u0430", callback_data=f"admin:users_search:{resolved_sort_mode}")],
                        [InlineKeyboardButton(text="\u0412\u0441\u0435 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0438", callback_data="admin:users:kind_group:0")],
                    ]
                ),
            )

        state["sort_mode"] = resolved_sort_mode
        text, reply_markup = format_admin_users_list(
            matches,
            sort_mode=resolved_sort_mode,
            page=page,
            title=f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b \u043f\u043e\u0438\u0441\u043a\u0430: {escape(state['query'])}",
            summary=f"\u041d\u0430\u0439\u0434\u0435\u043d\u043e \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u0439: {len(matches)}",
            search_results_mode=True,
        )
        return text, reply_markup

    def build_editors_keyboard(users: list) -> InlineKeyboardMarkup:
        def sort_key(user: object) -> tuple[int, str]:
            label = getattr(user, "full_name", None) or (f"@{user.username}" if user.username else str(user.user_id))
            return (0 if getattr(user, "is_editor", False) else 1, label.casefold())

        rows: list[list[InlineKeyboardButton]] = []
        for user in sorted(users, key=sort_key):
            label = getattr(user, "full_name", None) or (f"@{user.username}" if user.username else str(user.user_id))
            if len(label) > 32:
                label = f"{label[:29]}..."
            prefix = "✅" if getattr(user, "is_editor", False) else "➕"
            rows.append([InlineKeyboardButton(text=f"{prefix} {label}", callback_data=f"editor:toggle:{user.user_id}")])
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

    async def refresh_all_active_sources() -> list[tuple[str, str, str]]:
        sources = await db.get_active_sources()
        if not sources:
            return []

        rows: list[tuple[str, str, str]] = []
        for source in sources:
            if source["source_type"] == "audience":
                snapshot, snapshot_hash = await parser.parse_from_url(source["source_url"])
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
                rows.append((source["source_title"], snapshot.fetched_at.strftime("%Y-%m-%d %H:%M"), "РїРµСЂРµРїР°СЂСЃРµРЅРѕ"))
                continue
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
            if source["source_type"] == "audience":
                snapshot, snapshot_hash = await parser.parse_from_url(source["source_url"])
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
                rows.append((source["source_title"], snapshot.fetched_at.strftime("%Y-%m-%d %H:%M"), "СЌС‚Р°Р»РѕРЅ СЃРѕС…СЂР°РЅРµРЅ"))
                continue
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
                return "message is not modified" in str(exc).lower()
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
        db_user = await db.get_user(platform, user_id) if user_id is not None else None
        username = db_user.username if db_user else None
        user_info_tg = format_user_profile_link(platform, user_id, username, html=True)
        user_info_vk = format_user_profile_link(platform, user_id, username, html=False)

        traceback_text = "".join(format_exception(type(error), error, error.__traceback__))
        if len(traceback_text) > 2500:
            traceback_text = f"...{traceback_text[-2500:]}"
        telegram_text = (
            f"<b>Сбой в боте ({escape(platform)})</b>\n\n"
            f"Пользователь: {user_info_tg}\n"
            f"Чат: <b>{chat_id if chat_id is not None else 'неизвестно'}</b>\n"
            f"Ошибка: <code>{escape(short_error_text(error))}</code>\n\n"
            f"<pre>{escape(traceback_text)}</pre>"
        )
        vk_text = (
            f"Сбой в боте ({platform})\n\n"
            f"Пользователь: {user_info_vk}\n"
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

    async def prompt_audience_selection(bot: Bot, chat_id: int, user_id: int, error_text: str | None = None) -> None:
        awaiting_audience_subscription_input.add(user_id)
        lines = [
            "<b>Укажи кабинет</b>",
            "",
            "Напиши кабинет точно в таком же формате, как на сайте расписания.",
            "Например: <b>312</b>, <b>305/2</b>, <b>508/2М</b> или <b>с-з</b>.",
            "",
            "Эта подписка работает вместе с преподавателем и помогает быстрее замечать изменения по кабинету.",
        ]
        if error_text:
            lines.extend(["", error_text])
        await send_new_context_message(bot, chat_id, "settings", "\n".join(lines))

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
            await bot.send_message(chat_id, SEARCH_NOT_FOUND_TEXT)
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
            user.audience_subscription_title if user else None,
        )
        if subscription_line:
            label, value = subscription_line.split(":", 1)
            value_lines = [part.strip() for part in value.strip().splitlines() if part.strip()]
            if value_lines:
                lines.extend(["", f"{escape(label)}: <b>{escape(value_lines[0])}</b>"])
                lines.extend(escape(part) for part in value_lines[1:])
            else:
                lines.extend(["", f"{escape(label)}: <b>-</b>"])
        if user and user.subscription_type == "teacher" and not user.audience_subscription_key:
            lines.extend(["", "Ниже можно быстро привязать кабинет, чтобы следить за обновлениями аудитории отдельно."])
        lines.extend(["", "/rasp — посмотреть расписание", "/settings — дополнительно"])
        return "\n".join(lines)

    async def format_subscription_settings_text(user_id: int) -> str:
        user = await db.get_user("telegram", user_id)
        notifications_enabled = user.homework_notifications_enabled if user else True
        lines = ["<b>Дополнительно</b>", ""]
        subscription_line = subscription_caption(
            user.subscription_type if user else None,
            user.subscription_title if user else None,
            user.audience_subscription_title if user else None,
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

    def format_project_about_text() -> str:
        return "\n".join(
            [
                "<b>О проекте</b>",
                "",
                "Бот сделан студентом ОИТ, группы ИСП-25-1, в качестве альтернативы официальному боту, который давно не работает. Я не сотрудник колледжа, а просто энтузиаст, который хочет помочь всем получать актуальную информацию о расписании и изменениях. Я не несу никакой ответственности за точность данных, так как получаю их с официального сайта, и не имею возможности оперативно исправлять ошибки в расписании. Если ты заметил неточности, пожалуйста, сообщи об этом администрации колледжа, чтобы они могли исправить информацию на сайте.",
                "",
                "Профиль: <a href=\"https://github.com/nekotyy\">тык</a>",
                "Проект: <a href=\"https://github.com/nekotyy/misis-rasp-bot\">тык</a>",
                "",
                "Если понравилось, поставь звездочку на GitHub ⭐",
            ]
        )

    async def build_subscription_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
        user = await db.get_user("telegram", user_id)
        notifications_enabled = user.homework_notifications_enabled if user else True
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="Пройденные пары", callback_data="settings:lesson_counters")],
            [InlineKeyboardButton(text="О проекте", callback_data="settings:about")],
            [
                InlineKeyboardButton(
                    text="Отключить уведомления" if notifications_enabled else "Включить уведомления",
                    callback_data="settings:toggle_notifications",
                )
            ]
        ]
        if user and user.subscription_type == "teacher":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Изменить кабинет" if user.audience_subscription_key else "Подписаться на кабинет",
                        callback_data="settings:audience_setup",
                    )
                ]
            )
            if user.audience_subscription_key:
                rows.append([InlineKeyboardButton(text="Убрать кабинет", callback_data="settings:audience_clear")])
        if user and user.subscription_key:
            rows.append([InlineKeyboardButton(text="Отписаться", callback_data="settings:clear_group")])
        rows.append([InlineKeyboardButton(text="Персонализация", callback_data="settings:personalization")])
        rows.append([InlineKeyboardButton(text="Помощь", callback_data="settings:help")])
        rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:start")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def format_lesson_counters_text(user_id: int) -> str:
        if not settings.lesson_counters_enabled:
            return "Сейчас данный функционал глобально выключен."
        user = await db.get_user("telegram", user_id)
        if not user or user.subscription_type != "group" or user.schedule_id is None:
            return "Счетчики пар доступны после выбора группы."
        return await lesson_counter_service.format_counters_text(
            user.schedule_id,
            group_name=user.group_name,
            html=True,
        )

    async def handle_subscription_input(bot: Bot, chat_id: int, user_id: int, raw_text: str) -> bool:
        subscription_data, error_text = await resolve_subscription_input(raw_text)
        if subscription_data is None:
            await prompt_group_selection(bot, chat_id, error_text)
            return False
        existing_user = await db.get_user("telegram", user_id)
        await db.set_user_subscription("telegram", user_id, **subscription_data)
        should_clear_audience = subscription_data.get("subscription_type") != "teacher"
        if (
            not should_clear_audience
            and existing_user is not None
            and (
                existing_user.subscription_type != "teacher"
                or existing_user.subscription_key != subscription_data.get("subscription_key")
            )
        ):
            should_clear_audience = True
        if should_clear_audience:
            await db.clear_user_audience_subscription("telegram", user_id)
        editor = await user_is_editor(user_id)
        user = await get_user_record(user_id)
        await clear_context_messages(bot, chat_id, "group_select")
        await send_new_context_message(
            bot,
            chat_id,
            "menu",
            build_welcome_text(user, is_editor=editor),
            reply_markup=await build_start_keyboard(user_id),
        )
        return True

    async def send_schedule_menu(bot: Bot, chat_id: int) -> None:
        try:
            user = await db.get_user("telegram", chat_id)
            if user and user.custom_sticker_file_id:
                try:
                    await bot.send_sticker(chat_id=chat_id, sticker=user.custom_sticker_file_id)
                except Exception as exc:
                    logger.warning("Failed to send schedule menu custom sticker to %s: %s", chat_id, exc)
        except Exception as exc:
            logger.warning("Failed to fetch user for schedule menu sticker: %s", exc)

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
            awaiting_audience_subscription_input.discard(message.from_user.id)
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
            reply_markup=await build_start_keyboard(message.from_user.id if message.from_user else None),
        )

    async def send_reply(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> Message | None:
        return await safe_send_message(message.bot, message.chat.id, text, reply_markup=reply_markup)

    async def send_stars_invoice(bot_inst: Bot, chat_id: int, user_id: int, stars: int) -> None:
        prices = [LabeledPrice(label="Пожертвование", amount=stars)]
        await bot_inst.send_invoice(
            chat_id=chat_id,
            title="Поддержка проекта",
            description=f"Пожертвование {stars} ⭐ на развитие и поддержку работы бота.",
            payload=f"star_donate:{user_id}:{stars}",
            provider_token="",
            currency="XTR",
            prices=prices,
        )

    @dispatcher.message(F.sticker)
    async def handle_sticker_message(message: Message) -> None:
        if message.from_user is None or message.sticker is None:
            return
        if message.from_user.id in awaiting_custom_sticker:
            awaiting_custom_sticker.discard(message.from_user.id)
            sticker_id = message.sticker.file_id
            await db.set_user_custom_sticker("telegram", message.from_user.id, sticker_id)
            await send_new_context_message(
                message.bot,
                message.chat.id,
                "settings",
                await format_personalization_settings_text(message.from_user.id, db),
                reply_markup=await build_personalization_keyboard(message.from_user.id, db),
            )

    @dispatcher.message(Command("donate"))
    async def handle_donate_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type in GROUP_CHAT_TYPES:
            await send_reply(message, "Команда /donate доступна только в личных сообщениях с ботом.")
            return
        if message.from_user:
            awaiting_custom_donate_stars.discard(message.from_user.id)
        prompt_text = (
            f"{STAR_ICON} <b>Развитие бота расписания</b>\n\n"
            "Бот работает 24/7, ежедневно обрабатывает тысячи запросов и мгновенно оповещает об изменениях в парах.\n"
            "Поддерживая проект Telegram Звёздами (Stars), ты помогаешь оплачивать хостинг и ускорять разработку новых возможностей.\n"
            "💡 Любая поддержка помогает проекту расти и оставаться бесплатным для всех студентов!\n\n"
            "Выбери количество звёзд ниже или отправь своё число в чат:"
        )
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "donate",
            prompt_text,
            reply_markup=DONATE_KEYBOARD,
        )

    @dispatcher.message(Command("cleandb"))
    async def handle_cleandb_command(message: Message) -> None:
        await register_message_user(message)
        if message.from_user is None or not user_is_admin(message.from_user.id):
            return

        await send_reply(
            message,
            "⚡ <b>Запущена принудительная очистка базы данных через RabbitMQ...</b>\n\n"
            "После завершения очистки служебный отчёт будет выслан администраторам.",
        )
        if schedule_jobs is not None:
            await schedule_jobs.enqueue_or_run_db_cleanup()

    @dispatcher.message(Command("dnremove"))
    async def handle_dnremove_command(message: Message) -> None:
        await register_message_user(message)
        if message.from_user is None or not user_is_admin(message.from_user.id):
            return

        args = (message.text or "").strip().split()
        if len(args) < 2:
            await send_reply(
                message,
                "Использование:\n"
                "<code>/dnremove &lt;id_операции_или_charge_id&gt;</code>\n\n"
                "Пример: <code>/dnremove 1</code> или <code>/dnremove stx1gY...</code>",
            )
            return

        query = args[1].strip()
        donation = await db.get_star_donation(query)

        if donation:
            if donation["refunded"]:
                await send_reply(
                    message,
                    f"⚠️ Пожертвование <b>#{donation['id']}</b> ({donation['stars']} {STAR_ICON}) уже было возвращено ранее.",
                )
                return

            try:
                await message.bot.refund_star_payment(
                    user_id=donation["user_id"],
                    telegram_payment_charge_id=donation["charge_id"],
                )
            except Exception as exc:
                logger.error("Failed to refund star payment %s: %s", donation["id"], exc)
                await send_reply(message, f"❌ Ошибка при возврате средств в Telegram API:\n<code>{escape(str(exc))}</code>")
                return

            await db.refund_star_donation(donation["id"])

            user_id = donation["user_id"]
            stars = donation["stars"]
            await send_reply(
                message,
                f"✅ <b>Возврат выполнен!</b>\n\n"
                f"Средства за пожертвование <b>#{donation['id']}</b> ({stars} {STAR_ICON}) успешно возвращены пользователю <code>{user_id}</code>.",
            )

            try:
                user_notify_msg = (
                    f"⭐️ <b>Возврат средств</b>\n\n"
                    f"Средства за пожертвование #{donation['id']} ({stars} ⭐) были возвращены администратором на ваш баланс Telegram Stars."
                )
                await message.bot.send_message(user_id, user_notify_msg)
            except Exception as exc:
                logger.warning("Failed to notify user about star refund: %s", exc)
            return

        # Direct refund fallback if user provided charge_id and user_id explicitly
        if len(args) >= 3 and args[2].isdigit():
            target_user_id = int(args[2])
            charge_id = query
            try:
                await message.bot.refund_star_payment(
                    user_id=target_user_id,
                    telegram_payment_charge_id=charge_id,
                )
                await send_reply(
                    message,
                    f"✅ <b>Прямой возврат выполнен!</b>\n\n"
                    f"Запрос на возврат для Charge ID <code>{charge_id}</code> пользователю <code>{target_user_id}</code> отправлен.",
                )
            except Exception as exc:
                await send_reply(message, f"❌ Ошибка прямого возврата:\n<code>{escape(str(exc))}</code>")
            return

        await send_reply(
            message,
            f"❌ Пожертвование с ID или Charge ID <code>{escape(query)}</code> не найдено в базе.\n\n"
            "Если нужно сделать прямой возврат вне базы, укажи User ID вторым параметром:\n"
            "<code>/dnremove &lt;charge_id&gt; &lt;user_id&gt;</code>",
        )

    @dispatcher.pre_checkout_query()
    async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
        await pre_checkout_query.answer(ok=True)

    @dispatcher.message(F.successful_payment)
    async def process_successful_payment(message: Message) -> None:
        if message.successful_payment is None or message.from_user is None:
            return
        payment = message.successful_payment
        stars = payment.total_amount
        charge_id = payment.telegram_payment_charge_id
        user_id = message.from_user.id
        username = message.from_user.username
        full_name = message.from_user.full_name or ""

        donation_id = await db.record_star_donation(
            user_id=user_id,
            username=username,
            full_name=full_name,
            stars=stars,
            charge_id=charge_id,
        )

        thank_you_msg = (
            f"❤️ <b>Спасибо за вашу поддержку!</b>\n\n"
            f"Вы пожертвовали <b>{stars} {STAR_ICON}</b>. Благодаря вашей помощи проект становится лучше и продолжает работать 24/7! 💘"
        )
        await send_reply(message, thank_you_msg)

        if settings.admin_telegram_id:
            user_info = format_user_profile_link("telegram", user_id, username, html=True)
            now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            admin_msg = (
                f"{STAR_ICON} <b>Новое пожертвование (Stars)!</b>\n\n"
                f"<b>Отправитель:</b> {user_info}\n"
                f"<b>Количество:</b> {stars} {STAR_ICON}\n"
                f"<b>Дата и время:</b> {now_str}\n"
                f"<b>ID операции:</b> <code>#{donation_id}</code>\n"
                f"<b>Telegram Charge ID:</b> <code>{charge_id}</code>\n\n"
                f"Для возврата средств скопируй команду:\n"
                f"<code>/dnremove {donation_id}</code>\n"
                f"или по Charge ID:\n"
                f"<code>/dnremove {charge_id}</code>"
            )
            try:
                await message.bot.send_message(settings.admin_telegram_id, admin_msg)
            except Exception as exc:
                logger.warning("Failed to send admin donation notification: %s", exc)

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
            awaiting_audience_subscription_input.discard(message.from_user.id)
        await clear_context_messages(message.bot, message.chat.id, "dz")
        await clear_context_messages(message.bot, message.chat.id, "admin_broadcast")
        await clear_context_messages(message.bot, message.chat.id, "admin_lesson")
        search_results.pop(message.from_user.id, None)
        awaiting_schedule_search.discard(message.from_user.id)
        awaiting_admin_lesson_input.discard(message.from_user.id)
        if message.from_user:
            awaiting_custom_donate_stars.discard(message.from_user.id)
        await clear_context_messages(message.bot, message.chat.id, "donate")
        admin_lesson_drafts.pop(message.from_user.id, None)
        await send_new_context_message(
            message.bot,
            message.chat.id,
            "menu",
            build_welcome_text(
                await get_user_record(message.from_user.id if message.from_user else None),
                is_editor=await user_is_editor(message.from_user.id if message.from_user else None),
            ),
            reply_markup=await build_start_keyboard(message.from_user.id if message.from_user else None),
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
        keyboard = ADMIN_KEYBOARD if user_is_full_admin(message.from_user.id) else ADMIN_KEYBOARD_LIMITED
        await send_new_context_message(message.bot, message.chat.id, "admin", format_admin_panel(), keyboard)

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
        awaiting_admin_lesson_input.discard(callback.from_user.id)
        awaiting_audience_subscription_input.discard(callback.from_user.id)
        admin_broadcast_drafts.pop(callback.from_user.id, None)
        admin_lesson_drafts.pop(callback.from_user.id, None)
        editor = await user_is_editor(callback.from_user.id)
        user = await get_user_record(callback.from_user.id)
        if callback.message is not None:
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_broadcast")
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "menu",
                build_welcome_text(user, is_editor=editor),
                reply_markup=await build_start_keyboard(callback.from_user.id),
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
        if action == "about":
            await safe_edit_message_text(
                callback.message,
                format_project_about_text(),
                reply_markup=await build_subscription_settings_keyboard(callback.from_user.id),
            )
            await safe_callback_answer(callback)
            return
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
        if action == "audience_setup":
            user = await db.get_user("telegram", callback.from_user.id)
            if not user or user.subscription_type != "teacher":
                await safe_callback_answer(callback, "Эта настройка доступна после выбора преподавателя.", show_alert=True)
                return
            await prompt_audience_selection(callback.bot, callback.message.chat.id, callback.from_user.id)
            await safe_callback_answer(callback)
            return
        if action == "help":
            await safe_edit_message_text(
                callback.message,
                format_help_main_text(),
                reply_markup=build_help_main_keyboard(),
            )
            await safe_callback_answer(callback)
            return
        if action == "personalization":
            awaiting_custom_sticker.discard(callback.from_user.id)
            await safe_edit_message_text(
                callback.message,
                await format_personalization_settings_text(callback.from_user.id, db),
                reply_markup=await build_personalization_keyboard(callback.from_user.id, db),
            )
            await safe_callback_answer(callback)
            return

        if action == "pers_preview_sticker":
            user = await db.get_user("telegram", callback.from_user.id)
            if user and user.custom_sticker_file_id:
                try:
                    await callback.bot.send_sticker(chat_id=callback.message.chat.id, sticker=user.custom_sticker_file_id)
                except Exception as exc:
                    logger.warning("Failed to preview sticker for %s: %s", callback.from_user.id, exc)
                    await safe_callback_answer(callback, "Не удалось отправить стикер.", show_alert=True)
                    return
                await clear_context_messages(callback.bot, callback.message.chat.id, "settings")
                await send_new_context_message(
                    callback.bot,
                    callback.message.chat.id,
                    "settings",
                    await format_personalization_settings_text(callback.from_user.id, db),
                    reply_markup=await build_personalization_keyboard(callback.from_user.id, db),
                )
            await safe_callback_answer(callback)
            return

        if action == "pers_set_sticker":
            awaiting_custom_sticker.add(callback.from_user.id)
            await safe_edit_message_text(
                callback.message,
                "Пришли стикер, который будет высылаться перед уведомлениями и при вызове меню расписания:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="settings:personalization")]]),
            )
            await safe_callback_answer(callback)
            return

        if action == "pers_clear_sticker":
            await db.clear_user_custom_sticker("telegram", callback.from_user.id)
            await safe_edit_message_text(
                callback.message,
                await format_personalization_settings_text(callback.from_user.id, db),
                reply_markup=await build_personalization_keyboard(callback.from_user.id, db),
            )
            await safe_callback_answer(callback, "Стикер сброшен")
            return

        if action == "audience_clear":
            await db.clear_user_audience_subscription("telegram", callback.from_user.id)
            await safe_edit_message_text(
                callback.message,
                await format_subscription_settings_text(callback.from_user.id),
                reply_markup=await build_subscription_settings_keyboard(callback.from_user.id),
            )
            await safe_callback_answer(callback, "Кабинет отвязан")
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

    @dispatcher.callback_query(F.data.startswith("help:"))
    async def handle_help_query(callback: CallbackQuery) -> None:
        if await callback_is_rate_limited(callback):
            return
        await register_callback_user(callback)
        if callback.message is None:
            await safe_callback_answer(callback)
            return
        action = callback.data.split(":", 1)[1]
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад в Wiki", callback_data="settings:help")]
        ])
        if action == "group_setup":
            await safe_edit_message_text(callback.message, format_help_group_setup_text(), reply_markup=back_kb)
        elif action in {"personal_setup", "search_setup"}:
            await safe_edit_message_text(callback.message, format_help_personal_setup_text(), reply_markup=back_kb)
        elif action == "notifications_setup":
            await safe_edit_message_text(callback.message, format_help_notifications_text(), reply_markup=back_kb)
        elif action == "personalization_setup":
            await safe_edit_message_text(callback.message, format_help_personalization_text(), reply_markup=back_kb)
        elif action == "commands_list":
            await safe_edit_message_text(callback.message, format_help_commands_text(), reply_markup=back_kb)
        await safe_callback_answer(callback)

    @dispatcher.my_chat_member()
    async def handle_bot_added_to_chat(event: ChatMemberUpdated) -> None:
        if (
            event.new_chat_member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
            and event.old_chat_member.status not in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
        ):
            welcome_msg = (
                "<b>Инструкция по настройке бота в групповом чате</b>\n\n"
                "Бот успешно добавлен в ваш чат.\n\n"
                "Пошаговая настройка:\n"
                "1. Назначьте бота администратором чата (с правом отправки сообщений).\n"
                "2. Отправьте в этот чат команду <code>/startgroup</code> (или <code>/group</code>).\n"
                "3. Напишите название вашей учебной группы (например: <code>ИСП-25-1</code>).\n\n"
                "После выполнения настройки бот привяжет расписание и будет автоматически отправлять ежедневные обновления и изменения пар."
            )
            try:
                await event.bot.send_message(event.chat.id, welcome_msg)
            except Exception as exc:
                logger.warning("Failed to send welcome message to chat %s: %s", event.chat.id, exc)

    @dispatcher.message(Command("startgroup", "group"))
    async def handle_startgroup_command(message: Message) -> None:
        await register_message_user(message)
        if message.chat.type == "private":
            me = await message.bot.get_me()
            bot_username = me.username or "bot"
            add_url = f"https://t.me/{bot_username}?startgroup=true"
            msg_text = (
                "<b>Настройка бота в групповом чате Telegram</b>\n\n"
                "Чтобы получать расписание и уведомления об изменениях в вашем чате:\n"
                "1. Нажмите кнопку ниже и добавьте бота в ваш групповой чат.\n"
                "2. Назначьте бота администратором чата (с правом отправки сообщений).\n"
                "3. В чате отправьте команду <code>/startgroup</code> (или <code>/group</code>).\n"
                "4. Напишите название вашей учебной группы (например: <code>ИСП-25-1</code>)."
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Добавить бота в группу", url=add_url)]
            ])
            await send_new_context_message(message.bot, message.chat.id, "menu", msg_text, reply_markup=keyboard)
            return

        # Executed in group / supergroup
        if message.from_user is not None and not await user_can_manage_group(message):
            await send_reply(
                message,
                "<b>Настройка группы доступна только администраторам чата или пользователю, добавившему бота.</b>"
            )
            return

        awaiting_group_subscription_input.add(message.chat.id)
        await send_reply(
            message,
            "<b>Быстрая настройка группового чата</b>\n\n"
            "Пришлите название вашей учебной группы одним сообщением (например: <code>ИСП-25-1</code> или <code>МТО-25</code>):"
        )

    @dispatcher.callback_query(F.data.startswith("donate:"))
    async def handle_donate_callback(callback: CallbackQuery) -> None:
        if await callback_is_rate_limited(callback, cooldown=0.5):
            return
        await register_callback_user(callback)
        if callback.message is None:
            await safe_callback_answer(callback)
            return

        if callback.data == "donate:cancel":
            awaiting_custom_donate_stars.discard(callback.from_user.id)
            await clear_context_messages(callback.bot, callback.message.chat.id, "donate")
            await safe_callback_answer(callback, "Отменено")
            return

        if callback.data and callback.data.startswith("donate:stars:"):
            val = callback.data.split(":", 2)[2]
            if val == "custom":
                awaiting_custom_donate_stars.add(callback.from_user.id)
                await send_new_context_message(
                    callback.bot,
                    callback.message.chat.id,
                    "donate",
                    "✏️ <b>Своё количество звёзд</b>\n\nОтправь числом в чат, сколько звёзд ты хочешь пожертвовать (от 15 до 2000):",
                    reply_markup=DONATE_CUSTOM_CANCEL_KEYBOARD,
                )
                await safe_callback_answer(callback)
                return

            try:
                stars = int(val)
            except ValueError:
                await safe_callback_answer(callback, "Некорректное значение", show_alert=True)
                return

            await send_stars_invoice(callback.bot, callback.message.chat.id, callback.from_user.id, stars)
            await clear_context_messages(callback.bot, callback.message.chat.id, "donate")
            await safe_callback_answer(callback)
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
        is_full_admin = user_is_full_admin(callback.from_user.id)
        if not is_full_admin and action in {
            "broadcast", "broadcast_send", "broadcast_send_all", "broadcast_send_tg", "broadcast_send_vk", "baseline", "editors", "test",
            "download_db", "lesson_add", "lesson_edit",
            "lesson_delete", "lesson_delete_one",
            "lesson_confirm", "lesson_confirm_force",
            "lesson_delete_confirm", "lesson_delete_one_confirm", "import_lessons", "import_lessons_confirm", "import_lessons_cancel", "cleandb",
        }:
            await safe_callback_answer(callback, "Доступно только полному администратору.", show_alert=True)
            return
        admin_user = await get_user_record(callback.from_user.id)
        if action == "cleandb":
            await safe_callback_answer(callback, "Запущена принудительная очистка БД...")
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin",
                "⚡ <b>Запущена принудительная очистка базы данных через RabbitMQ...</b>\n\n"
                "После завершения очистки служебный отчёт будет выслан администраторам.",
                reply_markup=ADMIN_KEYBOARD,
            )
            if schedule_jobs is not None:
                await schedule_jobs.enqueue_or_run_db_cleanup()
            return
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
        if action.startswith("broadcast_plat:"):
            plat = action.split(":", 1)[1]
            draft = admin_broadcast_drafts.get(callback.from_user.id)
            if not draft:
                await safe_callback_answer(callback, "Сначала пришли текст рассылки.", show_alert=True)
                return
            if plat in {"all", "telegram", "vk"}:
                draft["target_platform"] = plat
                text = draft.get("text", "")
                target_aud = draft.get("target_audience", "all")
                await safe_edit_message_text(
                    callback.message,
                    format_admin_broadcast_preview(text, target_platform=plat, target_audience=target_aud),
                    reply_markup=build_admin_broadcast_preview_keyboard(plat, target_aud),
                )
            await safe_callback_answer(callback)
            return

        if action.startswith("broadcast_aud:"):
            aud = action.split(":", 1)[1]
            draft = admin_broadcast_drafts.get(callback.from_user.id)
            if not draft:
                await safe_callback_answer(callback, "Сначала пришли текст рассылки.", show_alert=True)
                return
            if aud in {"all", "students", "teachers"}:
                draft["target_audience"] = aud
                text = draft.get("text", "")
                target_plat = draft.get("target_platform", "all")
                await safe_edit_message_text(
                    callback.message,
                    format_admin_broadcast_preview(text, target_platform=target_plat, target_audience=aud),
                    reply_markup=build_admin_broadcast_preview_keyboard(target_plat, aud),
                )
            await safe_callback_answer(callback)
            return

        if action in {"broadcast_confirm", "broadcast_send", "broadcast_send_all", "broadcast_send_tg", "broadcast_send_vk"}:
            draft = admin_broadcast_drafts.get(callback.from_user.id)
            if not draft or not draft.get("text"):
                await safe_callback_answer(callback, "Сначала пришли текст рассылки.", show_alert=True)
                return
            if broadcaster is None:
                await safe_callback_answer(callback, "Сервис рассылки сейчас недоступен.", show_alert=True)
                return

            draft_text = draft["text"]
            target_audience = draft.get("target_audience", "all")
            target_platform = draft.get("target_platform", "all")

            awaiting_admin_broadcast_text.discard(callback.from_user.id)
            admin_broadcast_drafts.pop(callback.from_user.id, None)
            await safe_callback_answer(callback, "Рассылка запущена")

            status_msg = callback.message

            async def on_broadcast_progress(prog: BroadcastProgress) -> None:
                report = format_broadcast_progress_status(draft_text, prog, html=True)
                await safe_edit_message_text(
                    status_msg,
                    report,
                    reply_markup=None,
                )

            await broadcaster.broadcast(
                draft_text,
                telegram_message=escape(draft_text),
                vk_message=draft_text,
                campaign_type=CAMPAIGN_ADMIN_BROADCAST,
                target_platform=target_platform,
                target_audience=target_audience,
                progress_callback=on_broadcast_progress,
            )
            return
        if action == "download_db":
            file_path = settings.database_path
            if not file_path.exists():
                await safe_callback_answer(callback, "Файл базы не найден.", show_alert=True)
                return
            sent = await send_admin_document(callback.message.bot, callback.message.chat.id, file_path, "bot.db")
            if not sent:
                await safe_callback_answer(
                    callback,
                    "Файл слишком большой для Telegram. Сожми базу или скачай с сервера вручную.",
                    show_alert=True,
                )
                return
            await safe_callback_answer(callback, "База отправлена")
            return
        if action == "download_counters":
            file_path = settings.lesson_counters_path
            if not file_path.exists():
                await safe_callback_answer(callback, "Файл счетчиков не найден.", show_alert=True)
                return
            sent = await send_admin_document(
                callback.message.bot,
                callback.message.chat.id,
                file_path,
                "lesson_counters.json",
            )
            if not sent:
                await safe_callback_answer(
                    callback,
                    "Файл слишком большой для Telegram. Сожми JSON или скачай с сервера вручную.",
                    show_alert=True,
                )
                return
            await safe_callback_answer(callback, "Файл отправлен")
            return
        if action == "import_lessons":
            awaiting_admin_import_lessons.add(callback.from_user.id)
            admin_import_lessons_drafts.pop(callback.from_user.id, None)
            prompt_text = (
                "<b>Импорт пар из JSON</b>\n\n"
                "Отправь <b>JSON-файл</b> документа или пришли JSON-текст сообщением.\n\n"
                "Пример формата JSON:\n"
                "<code>{\n"
                '  "groups": [\n'
                '    {\n'
                '      "group_name": "ИСП-25-1",\n'
                '      "subjects": [\n'
                '        {"subject": "Литература", "teacher": "Волошина Н. В.", "passed": 10, "total": 62}\n'
                "      ]\n"
                "    }\n"
                "  ]\n"
                "}</code>"
            )
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin_import_lessons",
                prompt_text,
                reply_markup=ADMIN_IMPORT_LESSONS_INPUT_KEYBOARD,
            )
            await safe_callback_answer(callback)
            return
        if action == "import_lessons_cancel":
            awaiting_admin_import_lessons.discard(callback.from_user.id)
            admin_import_lessons_drafts.pop(callback.from_user.id, None)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_import_lessons")
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin",
                format_admin_panel(),
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Импорт отменен")
            return
        if action == "import_lessons_confirm":
            parsed_data = admin_import_lessons_drafts.get(callback.from_user.id)
            if not parsed_data:
                await safe_callback_answer(callback, "Данные для импорта устарели. Начни заново.", show_alert=True)
                return

            active_catalog = search_catalog or group_catalog
            current_payload = load_lesson_config(settings.lesson_counters_path)
            updated_payload, total_groups, total_subjects = await apply_imported_lessons_config(
                parsed_data, current_payload, active_catalog
            )
            save_lesson_config(settings.lesson_counters_path, updated_payload)

            lesson_counter_config = await lesson_counter_service.load_config_file(settings.lesson_counters_path, active_catalog)
            await lesson_counter_service.sync_config(lesson_counter_config)

            awaiting_admin_import_lessons.discard(callback.from_user.id)
            admin_import_lessons_drafts.pop(callback.from_user.id, None)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_import_lessons")

            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin",
                f"<b>Импорт пар успешно завершен!</b>\n\nИмпортировано/обновлено: <b>{total_groups}</b> групп, <b>{total_subjects}</b> пар.",
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Импорт завершен")
            return
        if action == "lesson_add":
            admin_lesson_drafts[callback.from_user.id] = {"step": "group"}
            awaiting_admin_lesson_input.add(callback.from_user.id)
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin_lesson",
                format_admin_lesson_prompt("group"),
            )
            await safe_callback_answer(callback)
            return
        if action == "lesson_edit":
            draft = {"step": "group", "mode": "edit"}
            admin_lesson_drafts[callback.from_user.id] = draft
            awaiting_admin_lesson_input.add(callback.from_user.id)
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin_lesson",
                format_admin_lesson_prompt("group", draft),
            )
            await safe_callback_answer(callback)
            return
        if action == "lesson_skip_passed":
            draft = admin_lesson_drafts.get(callback.from_user.id)
            if not draft or draft.get("step") != "passed":
                await safe_callback_answer(callback, 'Пропустить можно только на шаге прошедших пар.', show_alert=True)
                return
            draft.update({"passed": 0, "step": "total"})
            admin_lesson_drafts[callback.from_user.id] = draft
            await safe_edit_message_text(callback.message, format_admin_lesson_prompt("total", draft))
            await safe_callback_answer(callback, 'Пропущено')
            return
        if action == "lesson_cancel":
            admin_lesson_drafts.pop(callback.from_user.id, None)
            awaiting_admin_lesson_input.discard(callback.from_user.id)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_lesson")
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin",
                format_admin_panel(),
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Отменено")
            return
        if action == "lesson_delete":
            admin_lesson_delete_drafts[callback.from_user.id] = {"step": "group"}
            awaiting_admin_lesson_delete_input.add(callback.from_user.id)
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin_lesson_delete",
                format_admin_lesson_delete_prompt("group"),
            )
            await safe_callback_answer(callback)
            return
        if action == "lesson_delete_one":
            admin_lesson_delete_one_drafts[callback.from_user.id] = {"step": "group"}
            awaiting_admin_lesson_delete_one_input.add(callback.from_user.id)
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin_lesson_delete_one",
                format_admin_lesson_delete_one_prompt("group"),
            )
            await safe_callback_answer(callback)
            return
        if action == "lesson_delete_cancel":
            admin_lesson_delete_drafts.pop(callback.from_user.id, None)
            awaiting_admin_lesson_delete_input.discard(callback.from_user.id)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_lesson_delete")
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin",
                format_admin_panel(),
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Отменено")
            return
        if action == "lesson_delete_one_cancel":
            admin_lesson_delete_one_drafts.pop(callback.from_user.id, None)
            awaiting_admin_lesson_delete_one_input.discard(callback.from_user.id)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_lesson_delete_one")
            await send_new_context_message(
                callback.bot,
                callback.message.chat.id,
                "admin",
                format_admin_panel(),
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Отменено")
            return
        if action == "lesson_delete_confirm":
            draft = admin_lesson_delete_drafts.get(callback.from_user.id)
            if not draft:
                await safe_callback_answer(callback, "Черновик не найден.", show_alert=True)
                return
            schedule_id = int(draft.get("schedule_id") or 0)
            payload = load_lesson_config(settings.lesson_counters_path)
            groups = payload.setdefault("groups", [])
            before_count = len(groups)
            groups[:] = [
                item
                for item in groups
                if not (isinstance(item, dict) and int(item.get("schedule_id") or 0) == schedule_id)
            ]
            if len(groups) == before_count:
                await safe_callback_answer(callback, "Группа не найдена в конфиге.", show_alert=True)
                return
            save_lesson_config(settings.lesson_counters_path, payload)
            await sync_lesson_counters_from_file()
            admin_lesson_delete_drafts.pop(callback.from_user.id, None)
            awaiting_admin_lesson_delete_input.discard(callback.from_user.id)
            await safe_edit_message_text(
                callback.message,
                "<b>Пары удалены.</b>",
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Удалено")
            return
        if action == "lesson_delete_one_confirm":
            draft = admin_lesson_delete_one_drafts.get(callback.from_user.id)
            if not draft:
                await safe_callback_answer(callback, "Черновик не найден.", show_alert=True)
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
                await safe_callback_answer(callback, "Группа не найдена в конфиге.", show_alert=True)
                return
            subjects = group.get("subjects", [])
            if not isinstance(subjects, list):
                await safe_callback_answer(callback, "Некорректная структура subjects.", show_alert=True)
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
                await safe_callback_answer(callback, "Пара не найдена в конфиге.", show_alert=True)
                return
            group["subjects"] = kept
            save_lesson_config(settings.lesson_counters_path, payload)
            await sync_lesson_counters_from_file()
            admin_lesson_delete_one_drafts.pop(callback.from_user.id, None)
            awaiting_admin_lesson_delete_one_input.discard(callback.from_user.id)
            await safe_edit_message_text(
                callback.message,
                "<b>Пара удалена.</b>",
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Удалено")
            return
        if action == "lesson_confirm":
            draft = admin_lesson_drafts.get(callback.from_user.id)
            if not draft:
                await safe_callback_answer(callback, "Черновик не найден.", show_alert=True)
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
                await safe_edit_message_text(
                    callback.message,
                    "<b>Ошибка валидации</b>\n\n" + escape(errors),
                    reply_markup=admin_lesson_force_confirm_keyboard(),
                )
                return
            save_lesson_config(settings.lesson_counters_path, normalized)
            await sync_lesson_counters_from_file()
            admin_lesson_drafts.pop(callback.from_user.id, None)
            awaiting_admin_lesson_input.discard(callback.from_user.id)
            await safe_edit_message_text(
                callback.message,
                "<b>Пара изменена.</b>" if replaced or draft.get("mode") == "edit" else "<b>Пара добавлена.</b>",
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Сохранено")
            return
        if action == "lesson_confirm_force":
            draft = admin_lesson_drafts.get(callback.from_user.id)
            if not draft:
                await safe_callback_answer(callback, "Черновик не найден.", show_alert=True)
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

            save_lesson_config(settings.lesson_counters_path, payload)
            await sync_lesson_counters_from_file()
            admin_lesson_drafts.pop(callback.from_user.id, None)
            awaiting_admin_lesson_input.discard(callback.from_user.id)
            await safe_edit_message_text(
                callback.message,
                "<b>Пара изменена (без строгой валидации).</b>" if replaced or draft.get("mode") == "edit" else "<b>Пара добавлена (без строгой валидации).</b>",
                reply_markup=ADMIN_KEYBOARD,
            )
            await safe_callback_answer(callback, "Сохранено")
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
            admin_user_search_state.pop(callback.from_user.id, None)
            awaiting_admin_broadcast_text.discard(callback.from_user.id)
            awaiting_admin_lesson_input.discard(callback.from_user.id)
            admin_broadcast_drafts.pop(callback.from_user.id, None)
            admin_lesson_drafts.pop(callback.from_user.id, None)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_broadcast")
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_lesson")
            await safe_edit_message_text(callback.message, build_welcome_text(admin_user, is_editor=editor))
            context_messages[callback.message.chat.id]["menu"] = [callback.message.message_id]
            await safe_callback_answer(callback)
            return
        if action == "back":
            awaiting_admin_user_search.discard(callback.from_user.id)
            admin_user_search_state.pop(callback.from_user.id, None)
            awaiting_admin_broadcast_text.discard(callback.from_user.id)
            awaiting_admin_lesson_input.discard(callback.from_user.id)
            admin_broadcast_drafts.pop(callback.from_user.id, None)
            admin_lesson_drafts.pop(callback.from_user.id, None)
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_broadcast")
            await clear_context_messages(callback.bot, callback.message.chat.id, "admin_lesson")
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
            admin_user_search_state.pop(callback.from_user.id, None)
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
        elif action.startswith("users_found:"):
            awaiting_admin_user_search.discard(callback.from_user.id)
            action_parts = action.split(":")
            sort_mode = "kind_group"
            page = 0
            if len(action_parts) >= 2 and action_parts[1] in {"kind_group", "kind_teacher", "platform_tg", "platform_vk"}:
                sort_mode = action_parts[1]
                if len(action_parts) >= 3 and action_parts[2].isdigit():
                    page = int(action_parts[2])
            search_result = await format_admin_search_results(callback.from_user.id, page=page, sort_mode=sort_mode)
            if search_result is None:
                text = "<b>РџРѕРёСЃРє РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ</b>\n\nРџРѕРёСЃРє СѓР¶Рµ РЅРµР°РєС‚СѓР°Р»РµРЅ. Р—Р°РїСѓСЃС‚Рё РµРіРѕ РµС‰Рµ СЂР°Р·."
                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="РџРѕРёСЃРє", callback_data="admin:users_search:kind_group")],
                        [InlineKeyboardButton(text="Р’СЃРµ РїРѕР»СЊР·РѕРІР°С‚РµР»Рё", callback_data="admin:users:kind_group:0")],
                    ]
                )
            else:
                text, reply_markup = search_result
        elif action.startswith("users_search:"):
            sort_mode = action.split(":", 1)[1] if ":" in action else "kind_group"
            awaiting_admin_user_search.add(callback.from_user.id)
            admin_user_search_state.pop(callback.from_user.id, None)
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
        if not user_is_full_admin(callback.from_user.id):
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
                await send_reply(
                    message,
                    "Настройка группы доступна только администраторам чата или пользователю, добавившему бота."
                )
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

        if message.from_user.id in awaiting_audience_subscription_input:
            if message.text.startswith("/"):
                return
            audience_data, error_text = await resolve_audience_input(message.text.strip())
            if audience_data is None:
                await prompt_audience_selection(message.bot, message.chat.id, message.from_user.id, error_text)
                return
            await db.set_user_audience_subscription("telegram", message.from_user.id, **audience_data)
            awaiting_audience_subscription_input.discard(message.from_user.id)
            await send_new_context_message(
                message.bot,
                message.chat.id,
                "menu",
                build_welcome_text(await get_user_record(message.from_user.id), is_editor=await user_is_editor(message.from_user.id)),
                reply_markup=await build_start_keyboard(message.from_user.id),
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

        if user_is_admin(message.from_user.id) and message.from_user.id in awaiting_admin_lesson_input:
            draft = admin_lesson_drafts.get(message.from_user.id, {"step": "group"})
            step = str(draft.get("step") or "group")
            text = message.text.strip()
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
                    admin_lesson_drafts[message.from_user.id] = draft
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson",
                        format_admin_lesson_prompt("subject", draft),
                    )
                    return
                group = await active_catalog.find_group(text)
                if group is None:
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson",
                        format_admin_lesson_prompt("group", draft, "Группа не найдена."),
                    )
                    return
                draft.update({"schedule_id": group.schedule_id, "group_name": group.group_name, "step": "subject"})
                admin_lesson_drafts[message.from_user.id] = draft
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_lesson",
                    format_admin_lesson_prompt("subject", draft),
                )
                return
            if step == "subject":
                if not text:
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson",
                        format_admin_lesson_prompt("subject", draft, "Дисциплина не может быть пустой."),
                    )
                    return
                draft.update({"subject": text, "step": "teacher"})
                admin_lesson_drafts[message.from_user.id] = draft
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_lesson",
                    format_admin_lesson_prompt("teacher", draft),
                )
                return
            if step == "teacher":
                if not text:
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson",
                        format_admin_lesson_prompt("teacher", draft, "Преподаватель не может быть пустым."),
                    )
                    return
                draft.update({"teacher": text, "step": "passed"})
                admin_lesson_drafts[message.from_user.id] = draft
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_lesson",
                    format_admin_lesson_prompt("passed", draft),
                    reply_markup=admin_lesson_input_keyboard(),
                )
                return
            if step == "passed":
                if not text.isdigit():
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson",
                        format_admin_lesson_prompt("passed", draft, "Нужно число."),
                        reply_markup=admin_lesson_input_keyboard(),
                    )
                    return
                draft.update({"passed": int(text), "step": "total"})
                admin_lesson_drafts[message.from_user.id] = draft
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_lesson",
                    format_admin_lesson_prompt("total", draft),
                )
                return
            if step == "total":
                if not text.isdigit():
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson",
                        format_admin_lesson_prompt("total", draft, "Нужно число."),
                    )
                    return
                draft.update({"total": int(text), "step": "confirm"})
                admin_lesson_drafts[message.from_user.id] = draft
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_lesson",
                    format_admin_lesson_preview(draft),
                    reply_markup=admin_lesson_confirm_keyboard(),
                )
                return

        if user_is_admin(message.from_user.id) and message.from_user.id in awaiting_admin_lesson_delete_input:
            draft = admin_lesson_delete_drafts.get(message.from_user.id, {"step": "group"})
            step = str(draft.get("step") or "group")
            text = message.text.strip()
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
                    admin_lesson_delete_drafts[message.from_user.id] = draft
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson_delete",
                        format_admin_lesson_delete_prompt("confirm", draft),
                        reply_markup=admin_lesson_delete_confirm_keyboard(),
                    )
                    return
                group = await active_catalog.find_group(text)
                if group is None:
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson_delete",
                        format_admin_lesson_delete_prompt("group", draft, "Группа не найдена."),
                    )
                    return
                draft.update({"schedule_id": group.schedule_id, "group_name": group.group_name, "step": "confirm"})
                admin_lesson_delete_drafts[message.from_user.id] = draft
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_lesson_delete",
                    format_admin_lesson_delete_prompt("confirm", draft),
                    reply_markup=admin_lesson_delete_confirm_keyboard(),
                )
                return

        if user_is_admin(message.from_user.id) and message.from_user.id in awaiting_admin_lesson_delete_one_input:
            draft = admin_lesson_delete_one_drafts.get(message.from_user.id, {"step": "group"})
            step = str(draft.get("step") or "group")
            text = message.text.strip()
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
                    admin_lesson_delete_one_drafts[message.from_user.id] = draft
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson_delete_one",
                        format_admin_lesson_delete_one_prompt("subject", draft),
                    )
                    return
                group = await active_catalog.find_group(text)
                if group is None:
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson_delete_one",
                        format_admin_lesson_delete_one_prompt("group", draft, "Группа не найдена."),
                    )
                    return
                draft.update({"schedule_id": group.schedule_id, "group_name": group.group_name, "step": "subject"})
                admin_lesson_delete_one_drafts[message.from_user.id] = draft
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_lesson_delete_one",
                    format_admin_lesson_delete_one_prompt("subject", draft),
                )
                return
            if step == "subject":
                if not text:
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson_delete_one",
                        format_admin_lesson_delete_one_prompt("subject", draft, "Дисциплина не может быть пустой."),
                    )
                    return
                draft.update({"subject": text, "step": "teacher"})
                admin_lesson_delete_one_drafts[message.from_user.id] = draft
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_lesson_delete_one",
                    format_admin_lesson_delete_one_prompt("teacher", draft),
                )
                return
            if step == "teacher":
                if not text:
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_lesson_delete_one",
                        format_admin_lesson_delete_one_prompt("teacher", draft, "Преподаватель не может быть пустым."),
                    )
                    return
                draft.update({"teacher": text, "step": "confirm"})
                admin_lesson_delete_one_drafts[message.from_user.id] = draft
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_lesson_delete_one",
                    format_admin_lesson_delete_one_prompt("confirm", draft),
                    reply_markup=admin_lesson_delete_one_confirm_keyboard(),
                )
                return

        if message.from_user and message.from_user.id in awaiting_custom_donate_stars:
            text = (message.text or "").strip()
            try:
                stars = int(text)
                if not (15 <= stars <= 2000):
                    raise ValueError("out of range")
            except ValueError:
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "donate",
                    "Пожалуйста, введи целое число от 15 до 2000 звёзд. Попробуй ещё раз:",
                    reply_markup=DONATE_CUSTOM_CANCEL_KEYBOARD,
                )
                return

            awaiting_custom_donate_stars.discard(message.from_user.id)
            await clear_context_messages(message.bot, message.chat.id, "donate")
            await send_stars_invoice(message.bot, message.chat.id, message.from_user.id, stars)
            return

        if user_is_admin(message.from_user.id) and message.from_user.id in awaiting_admin_import_lessons:
            raw_data = ""
            if message.document:
                try:
                    file_info = await message.bot.get_file(message.document.file_id)
                    file_bytes = await message.bot.download_file(file_info.file_path)
                    raw_data = file_bytes.read().decode("utf-8")
                except Exception as exc:
                    await send_new_context_message(
                        message.bot,
                        message.chat.id,
                        "admin_import_lessons",
                        f"Не удалось прочитать документ: {exc}\nПришли JSON-текст сообщением.",
                        reply_markup=ADMIN_IMPORT_LESSONS_INPUT_KEYBOARD,
                    )
                    return
            elif message.text:
                raw_data = message.text.strip()

            if not raw_data:
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_import_lessons",
                    "Пришли JSON-файл или отправь JSON-текст сообщением.",
                    reply_markup=ADMIN_IMPORT_LESSONS_INPUT_KEYBOARD,
                )
                return

            parsed_data, error_msg = parse_imported_json_payload(raw_data)
            if error_msg or not parsed_data:
                await send_new_context_message(
                    message.bot,
                    message.chat.id,
                    "admin_import_lessons",
                    f"<b>Ошибка обработки JSON:</b>\n{error_msg}\n\nПроверь формат и отправь повторно.",
                    reply_markup=ADMIN_IMPORT_LESSONS_INPUT_KEYBOARD,
                )
                return

            admin_import_lessons_drafts[message.from_user.id] = parsed_data
            awaiting_admin_import_lessons.discard(message.from_user.id)

            active_catalog = search_catalog or group_catalog
            preview_text, _, _ = await format_import_preview(parsed_data, active_catalog, html=True)

            await send_new_context_message(
                message.bot,
                message.chat.id,
                "admin_import_lessons",
                preview_text,
                reply_markup=ADMIN_IMPORT_LESSONS_PREVIEW_KEYBOARD,
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
            admin_broadcast_drafts[message.from_user.id] = {
                "text": draft_text,
                "target_audience": "all",
            }
            await send_new_context_message(
                message.bot,
                message.chat.id,
                "admin_broadcast",
                format_admin_broadcast_preview(draft_text, target_audience="all"),
                reply_markup=build_admin_broadcast_preview_keyboard("all"),
            )
            return
        if user_is_admin(message.from_user.id) and message.from_user.id in awaiting_admin_user_search:
            query = message.text.strip()
            users = await db.list_users()
            matches = filter_admin_users(users, query)
            awaiting_admin_user_search.discard(message.from_user.id)
            admin_user_search_state.pop(message.from_user.id, None)
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
            admin_user_search_state[message.from_user.id] = {"query": query, "sort_mode": "kind_group"}
            text, reply_markup = format_admin_users_list(
                matches,
                sort_mode="kind_group",
                page=0,
                title=f"Результаты поиска: {escape(query)}",
                summary=f"Найдено пользователей: {len(matches)}",
                search_results_mode=True,
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
            reply_markup=await build_start_keyboard(message.from_user.id),
        )

    @dispatcher.errors()
    async def handle_telegram_errors(event: ErrorEvent, bot: Bot) -> bool:
        user_id, chat_id = extract_error_context(event)
        if chat_id is not None:
            await notify_user_about_error(bot, chat_id, event.exception)
        await notify_admin_about_error("telegram", user_id, chat_id, event.exception)
        return True

    return dispatcher

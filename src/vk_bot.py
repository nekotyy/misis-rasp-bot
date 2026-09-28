from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
from vkbottle import Keyboard, Text
from vkbottle.bot import Bot, Message
from vkbottle.exception_factory import ErrorHandler
from vkbottle.tools import DocMessagesUploader

from src.config import Settings
from src.db import Database
from src.error_reporting import AdminErrorReporter
from src.group_catalog import GroupCatalog
from src.lesson_counters import (
    LessonCounterService,
    build_teacher_schedule_snapshot,
    format_counter_sync_report,
    normalize_lesson_text,
    resolve_audience_preview_content,
    resolve_group_preview_content,
    snapshot_to_search_content,
    subject_matches,
    sync_lesson_counters_for_date,
    teacher_matches,
)
from src.notifier import CAMPAIGN_ADMIN_BROADCAST, Broadcaster, BroadcastProgress
from src.ocr_import import (
    OCR_STAGE_UPLOAD,
    OcrScheduleImporter,
    build_ocr_importer,
    format_admin_gemini_status,
    format_ocr_preview,
    format_ocr_summary_preview,
    format_progress_bar,
)
from src.ocr_schedule import (
    MAX_OCR_IMAGES,
    SUMMARY_RECOGNITION_PROMPT,
    OcrEngineError,
    compress_image_for_ocr,
)
from src.parser import MANUAL_REFRESH_PAUSE_SECONDS, ScheduleParser, compute_snapshot_hash
from src.schedule_search import ScheduleSearchCatalog
from src.schedule_service import ScheduleFormatter, get_day_by_offset_from_content
from src.subscription_utils import (
    make_audience_subscription,
    make_group_subscription,
    make_teacher_subscription,
    subscription_caption,
)
from src.system_status import (
    BOT_VERSION,
    STARTED_AT,
    check_database_status,
    check_schedule_site,
    format_daily_errors_report,
    format_uptime,
    get_memory_usage_mb,
)
from src.telegram_bot import format_broadcast_progress_status, format_user_profile_link
from src.vk_runtime import (
    ResilientBotPolling,
    build_vk_api,
    vk_call_with_retry,
    vk_edit_message,
    vk_send_message,
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

PAGE_SIZE = 6
SUPPORT_CONTACT = "tg: t.me/nekoty или vk: vk.com/nekotyy"
MAX_OCR_IMAGE_BYTES = 20 * 1024 * 1024
VK_MESSAGE_LIMIT = 4096
# VK выдаёт peer_id бесед (в отличие от личных диалогов) начиная с этого значения.
VK_CHAT_PEER_ID_THRESHOLD = 2_000_000_000
ADMIN_USERS_PAGE_SIZE = 20
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

USER_ERROR_TEXT = (
    "Что-то пошло не так при обработке запроса. Администратор уже получил отчёт об ошибке.\n\n"
    "Попробуй ещё раз через минуту. Если повторится — напиши: " + SUPPORT_CONTACT
)
USER_TIMEOUT_TEXT = (
    "Запрос обрабатывался слишком долго и был прерван — скорее всего, сайт расписания "
    "сейчас тормозит. Попробуй ещё раз через пару минут."
)

# Имя пользователя VK подтягиваем не на каждое сообщение (это лишний запрос к API
# и лишняя точка отказа), а раз в сутки.
VK_NAME_REFRESH_SECONDS = 24 * 3600

# В беседах VK подставляет упоминание бота перед текстом кнопки или команды:
# "[club123|@bot] Расписание" — без очистки такие нажатия не распознаются.
_VK_MENTION_RE = re.compile(r"^\s*(?:\[(?:club|public)\d+\|[^\]]*\]|@(?:club|public)\d+)[\s,:]*", re.IGNORECASE)


def strip_vk_bot_mention(text: str | None) -> str:
    if not text:
        return ""
    return _VK_MENTION_RE.sub("", text, count=1).strip()


def vk_handler_timeout(settings: Settings) -> float:
    """Потолок обработки одного сообщения: OCR ждёт дольше остальных сценариев."""
    ocr_timeout = float(getattr(settings, "ocr_timeout_seconds", 180.0) or 180.0)
    return max(300.0, ocr_timeout + 120.0)


# Лимиты обычной (не inline) клавиатуры VK. Превышение любого из них — ошибка
# API 911 "Keyboard format is invalid", то есть отвалившийся экран у пользователя.
VK_KEYBOARD_MAX_ROWS = 10
VK_KEYBOARD_MAX_BUTTONS_PER_ROW = 5
VK_KEYBOARD_MAX_BUTTONS = 40


def vk_admin_keyboard_rows() -> list[list[str]]:
    """Раскладка админ-панели VK.

    Вынесена на уровень модуля, чтобы тесты держали её в рамках лимитов VK:
    лишний ряд роняет весь экран админки ошибкой 911.
    """
    return [
        list(VK_ADMIN_SECTION_LABELS[:2]),
        list(VK_ADMIN_SECTION_LABELS[2:4]),
        [VK_ADMIN_SECTION_LABELS[4]],
        ["Закрыть админку"],
    ]


VK_ADMIN_SECTION_LABELS = ("Мониторинг", "Расписание и OCR", "Счётчики пар", "Пользователи и рассылки", "Служебное")

# "Назад в меню" в VK общий обработчик отправляет в главное меню пользователя, а не в админку,
# поэтому из раздела возвращаемся отдельной кнопкой.
VK_ADMIN_SECTION_BACK = "Назад в админку"

VK_ADMIN_SECTIONS: dict[str, dict] = {
    "Мониторинг": {
        "text": "Мониторинг\n\nСтатус бота, ошибки, последние изменения и группы.",
        "rows": [
            ["Статус", "Ошибки за день"],
            ["Последнее изменение", "Информация по группам"],
            [VK_ADMIN_SECTION_BACK],
        ],
    },
    "Расписание и OCR": {
        "text": "Расписание и OCR\n\nПерепарсинг с сайта, эталоны и загрузка расписания по фото или JSON.",
        "rows": [
            ["Перепарсить", "Сохранить эталон"],
            ["Расписание с фото", "Сводное расписание"],
            ["Импорт OCR JSON", "Управление Gemini"],
            [VK_ADMIN_SECTION_BACK],
        ],
    },
    "Счётчики пар": {
        "text": "Счётчики пар\n\nРучной подсчёт и правка счётчиков по группам.",
        "rows": [
            ["Ручной подсчёт"],
            ["Добавить пару", "Изменить пару"],
            ["Удалить пару", "Удалить пары"],
            ["Импорт пар из JSON", "Скачать пары"],
            [VK_ADMIN_SECTION_BACK],
        ],
    },
    "Пользователи и рассылки": {
        "text": "Пользователи и рассылки\n\nПоиск пользователей и рассылки сообщений.",
        "rows": [
            ["Пользователи", "Разослать"],
            ["Тестовая рассылка"],
            [VK_ADMIN_SECTION_BACK],
        ],
    },
    "Служебное": {
        "text": "Служебное\n\nВыгрузка и очистка базы данных.",
        "rows": [
            ["Скачать БД"],
            ["Очистить БД"],
            [VK_ADMIN_SECTION_BACK],
        ],
    },
}

VK_ADMIN_COUNTER_SYNC_ROWS = [["Подсчёт за сегодня", "Подсчёт за вчера"], [VK_ADMIN_SECTION_BACK]]

VK_ADMIN_COUNTER_SYNC_TEXT = (
    "Ручной подсчёт пар\n\n"
    "Считает пары выбранного дня по расписанию для всех групп — так же, как автоподсчёт в 23:20. "
    "Уже учтённые группы пропускаются, ничего не задвоится.\n\n"
    "Лучше запускать после последней пары дня: учтённая группа больше не пересчитывается, "
    "даже если расписание потом изменится."
)


def make_vk_keyboard(rows: list[list[str]]) -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    for row_index, row in enumerate(rows):
        if row_index:
            keyboard.row()
        for label in row:
            keyboard.add(Text(label))
    return keyboard.get_json()


def vk_help_main_keyboard() -> str:
    return make_vk_keyboard([
        ["1. Настройка групп и бесед"],
        ["2. Поиск и подписки"],
        ["3. Уведомления и расписание"],
        ["4. Персонализация"],
        ["5. Список команд"],
        ["Назад в меню"],
    ])


def build_vk_subscription_settings_keyboard(user=None) -> str:
    notifications_enabled = getattr(user, "homework_notifications_enabled", True) if user else True
    has_subscription = bool(user and getattr(user, "subscription_key", None))
    rows: list[list[str]] = [
        ["Пройденные пары"],
        ["Персонализация"],
        ["О проекте"],
        ["Помощь"],
        ["Отключить уведомления" if notifications_enabled else "Включить уведомления"],
    ]
    if user and getattr(user, "subscription_type", None) == "teacher":
        rows.append(["Изменить кабинет" if getattr(user, "audience_subscription_key", None) else "Подписаться на кабинет"])
        if getattr(user, "audience_subscription_key", None):
            rows.append(["Убрать кабинет"])
    if has_subscription:
        rows.append(["Отписаться от группы"])
    rows.append(["Назад в меню"])
    return make_vk_keyboard(rows)


def vk_help_group_setup_text() -> str:
    return "\n".join([
        "Справочник: Настройка групп и бесед",
        "",
        "1. ВКонтакте (Беседы):",
        "- Зайдите в сообщество бота ВКонтакте и нажмите кнопку «Добавить в беседу» (под обложкой или в меню действий) либо перейдите по ссылке vk.ru/app6441755_-237526231",
        "- Выберите нужную беседу и подтвердите добавление.",
        "- В настройках беседы разрешите боту доступ к переписке (или назначьте администратором).",
        "- В беседе отправьте команду /startgroup или фразы Настройка группы / Группа.",
        "- Укажите название вашей учебной группы (например: ИСП-25-1).",
        "",
        "2. Telegram (Групповые чаты):",
        "- Добавьте бота в ваш групповой чат Telegram.",
        "- Назначьте администратором с правом отправки сообщений.",
        "- В чате отправьте /startgroup и напишите название группы.",
        "",
        "3. Изменение и сброс группы в беседе:",
        "- Повторно отправьте /startgroup в беседу для смены учебной группы.",
        "- После привязки бот автоматически рассылает расписание и замены пар.",
    ])


def vk_help_personal_setup_text() -> str:
    return "\n".join([
        "Справочник: Поиск расписания и подписки",
        "",
        "1. Поиск учебной группы:",
        "- Напишите название группы в формате сайта колледжа (например: ИСП-25-1).",
        "",
        "2. Поиск преподавателя:",
        "- Напишите фамилию преподавателя (например: Иванов). Бот найдет личное расписание преподавателя.",
        "",
        "3. Поиск аудитории / кабинета:",
        "- Напишите номер кабинета (например: 101). Бот покажет расписание занятий в этом кабинете.",
    ])


def vk_help_notifications_text() -> str:
    return "\n".join([
        "Справочник: Уведомления и расписание",
        "",
        "1. Автоматические уведомления об изменениях:",
        "- Бот отслеживает публикации замен на сайте колледжа и автоматически высылает обновления.",
        "",
        "2. Включение и отключение уведомлений:",
        "- В меню Дополнительно можно выключить или включить получение уведомлений.",
    ])


def vk_help_personalization_text() -> str:
    return "\n".join([
        "Справочник: Персонализация сообщений",
        "",
        "1. Кастомный стикер:",
        "- В меню Дополнительно -> Персонализация вы можете прикрепить стикер.",
        "- Прикрепленный стикер отправляется перед сообщениями расписания и уведомлений.",
    ])


def is_group_setup_command(text: str | None) -> bool:
    if not text:
        return False
    raw = " ".join(text.strip().casefold().split())
    if not raw:
        return False
    exact_matches = {
        "/startgroup",
        "/group",
        "startgroup",
        "group",
        "настройка группы",
        "настройки группы",
        "настроить группу",
        "группа",
    }
    if raw in exact_matches:
        return True
    prefixes = (
        "/startgroup",
        "/group",
        "startgroup ",
        "group ",
        "настройка группы",
        "настройки группы",
        "настроить группу",
        "группа ",
        "группа:",
        "группа -",
        "группа-",
    )
    return any(raw.startswith(prefix) for prefix in prefixes)


def vk_help_commands_text() -> str:
    return "\n".join([
        "Справочник: Полный список команд",
        "",
        "Основные команды:",
        "- /start — запуск бота и переход в главное меню",
        "- /rasp — посмотреть расписание",
        "- /settings — открыть меню Дополнительно",
        "- /startgroup — мастер настройки бота в беседe или групповом чате",
        "- /group — быстрый вызов настройки группы",
    ])

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


async def build_vk_admin_status_text(
    db: Database,
    settings: Settings | None = None,
    ocr_importer=None,
) -> str:
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
    daily_errors_summary = await db.get_daily_errors_summary()
    first_created_at = await db.get_db_first_created_at()

    schedule_url = settings.schedule_url if settings else "http://asu.sf-misis.ru/rasp/600"
    rabbitmq_url = settings.rabbitmq_url if settings else ""
    site_status = await check_schedule_site(ScheduleParser(schedule_url).site_root_url, timeout=3.0)
    db_status = await check_database_status(db)

    vk_users = sum(1 for user in users if user.platform == "vk")
    tg_users = sum(1 for user in users if user.platform == "telegram")
    tg_personal = sum(1 for u in users if u.platform == "telegram" and u.user_id > 0)
    tg_chats = sum(1 for u in users if u.platform == "telegram" and u.user_id < 0)
    vk_personal = sum(1 for u in users if u.platform == "vk" and u.user_id < VK_CHAT_PEER_ID_THRESHOLD)
    vk_chats = sum(1 for u in users if u.platform == "vk" and u.user_id >= VK_CHAT_PEER_ID_THRESHOLD)
    total_personal = tg_personal + vk_personal
    total_chats = tg_chats + vk_chats

    chat_groups_map: dict[str, dict[str, int]] = {}
    for u in users:
        is_chat = (u.platform == "telegram" and u.user_id < 0) or (u.platform == "vk" and u.user_id >= VK_CHAT_PEER_ID_THRESHOLD)
        if not is_chat:
            continue
        g_title = u.subscription_title or u.group_name or "Без группы"
        if g_title not in chat_groups_map:
            chat_groups_map[g_title] = {"total": 0, "tg": 0, "vk": 0}
        chat_groups_map[g_title]["total"] += 1
        if u.platform == "telegram":
            chat_groups_map[g_title]["tg"] += 1
        else:
            chat_groups_map[g_title]["vk"] += 1

    sorted_chat_groups = sorted(chat_groups_map.items(), key=lambda x: x[1]["total"], reverse=True)
    if sorted_chat_groups:
        chat_groups_lines = [
            f"  • {g}: {stats['total']} (TG: {stats['tg']}, VK: {stats['vk']})."
            for g, stats in sorted_chat_groups[:15]
        ]
        if len(sorted_chat_groups) > 15:
            chat_groups_lines.append(f"  • ... и ещё {len(sorted_chat_groups) - 15} групп.")
    else:
        chat_groups_lines = ["  • Нет настроенных пользовательских групп."]

    last_change_at = last_change["created_at"] if last_change else "еще не было"
    tg_top_error_lines = "\n".join(
        f"• {item['count']}: {item['error_text']}."
        for item in tg_top_errors
    ) or "• Нет зарегистрированных ошибок."

    def snapshot_line(title: str, snapshot: dict | None) -> str:
        if snapshot is None:
            return f"{title}: еще не было"
        return f"{title}: {snapshot['created_at']}\n  Сайт отдал данные: {snapshot['fetched_at']}"

    uptime_str = format_uptime()
    started_str = STARTED_AT.strftime("%d.%m.%Y %H:%M:%S")
    installed_str = first_created_at or "Не определено"
    if "T" in installed_str:
        installed_str = installed_str.replace("T", " ")
    ram_mb = get_memory_usage_mb()
    db_size_str = db_status.get("size_formatted", "0 Б")

    site_status_label = f"🟢 Доступен ({site_status.get('status_code', 200)} OK, {site_status.get('latency_ms', 0)} мс)" if site_status["ok"] else f"🔴 Недоступен ({str(site_status.get('error') or 'Ошибка')})"
    rmq_label = "🟢 Подключен" if rabbitmq_url else "🟡 Direct Fallback (не настроен)"

    return "\n".join([
        "Статус бота",
        "───────────────────────────",
        "Системная информация:",
        f"• Версия бота: v{BOT_VERSION}.",
        f"• Аптайм: {uptime_str} (старт: {started_str}).",
        f"• Поставлен на сервер: {installed_str}.",
        f"• Память процесса (RAM): {ram_mb} МБ.",
        f"• Размер базы данных: {db_size_str}.",
        "───────────────────────────",
        "Сайт расписания и службы:",
        f"• Сайт МИСИС: {site_status_label}.",
        f"• RabbitMQ: {rmq_label}.",
        "• Telegram Bot API: 🟢 Работает.",
        "• VK Bot API: 🟢 Работает.",
        f"• База данных SQLite: 🟢 OK ({db_size_str}).",
        "• Фоновый планировщик: 🟢 Активен.",
        "• " + (ocr_importer.status_line(html=False) if ocr_importer is not None else "Распознавание с фото: не настроено") + ".",
        f"• Ошибок за сегодня: {daily_errors_summary['total_errors']} (Службы: {daily_errors_summary['system_errors_total']}, Доставка: {daily_errors_summary['delivery_errors_total']}).",
        "───────────────────────────",
        "Пользователи и источники:",
        f"• Пользователей: {len(users)}.",
        f"• Пользователей с VK: {vk_users}.",
        f"• Пользователей с TG: {tg_users}.",
        f"• Личных пользователей: {total_personal} (TG: {tg_personal}, VK: {vk_personal}).",
        f"• Активных групп: {active_group_count}.",
        f"• Активных преподавателей: {active_teacher_count}.",
        "",
        "Активные пользовательские группы:",
        f"• Всего бесед и групп: {total_chats}.",
        f"• Групповых чатов в Telegram: {tg_chats}.",
        f"• Бесед ВКонтакте: {vk_chats}.",
        "• Где настроен бот:",
        *chat_groups_lines,
        "───────────────────────────",
        "Состояние расписания:",
        f"• Последнее изменение: {last_change_at}.",
        f"• {snapshot_line('Последний обычный парс', current_snapshot)}.",
        f"• {snapshot_line('Последний сохраненный эталон', baseline_snapshot)}.",
        "───────────────────────────",
        "Статистика отправок:",
        f"• Всего событий доставки: {delivery_stats['events_total']}.",
        f"• Успешно / ошибок: {delivery_stats['sent_total']} / {delivery_stats['failed_total']}.",
        f"• За 24 часа (успешно / ошибок): {delivery_stats['sent_last_24h']} / {delivery_stats['failed_last_24h']}.",
        f"• Уведомлений отправлено: {delivery_stats['notifications_sent']}.",
        f"• Админских рассылок отправлено: {delivery_stats['admin_broadcast_sent']}.",
        f"• Служебных уведомлений админу: {delivery_stats['admin_notify_sent']}.",
        f"• Через RabbitMQ / напрямую: {delivery_stats['sent_via_rabbitmq']} / {delivery_stats['sent_direct']} .",
        f"• Ошибок через RabbitMQ / напрямую: {delivery_stats['failed_via_rabbitmq']} / {delivery_stats['failed_direct']} .",
        f"• Доставлено после ретрая: {delivery_stats['sent_after_retry']} .",
        f"• TG (успешно / ошибок): {delivery_stats['tg_sent']} / {delivery_stats['tg_failed']}.",
        f"  - Ошибок через RabbitMQ / напрямую: {delivery_stats['tg_failed_via_rabbitmq']} / {delivery_stats['tg_failed_direct']}.",
        f"  - Ошибок за 24ч: {delivery_stats['tg_failed_last_24h']} .",
        f"  - Перманентных ошибок (всего / 24ч): {delivery_stats['tg_failed_permanent']} / {delivery_stats['tg_failed_permanent_last_24h']}.",
        f"  - TG авто-отключено из-за доставки: {tg_auto_disabled}.",
        f"• VK (успешно / ошибок): {delivery_stats['vk_sent']} / {delivery_stats['vk_failed']}.",
        "───────────────────────────",
        "Топ TG ошибок за 24ч:",
        tg_top_error_lines,
    ])


def format_vk_ocr_prompt(error: str = "") -> str:
    lines = ["Импорт расписания с фото", ""]
    if error:
        lines.extend([error, ""])
    lines.extend(
        [
            "Пришли фото или картинку-документ со страницей расписания.",
            "",
            "Чтобы распознавание сработало хорошо:",
            "- в кадр должны попасть название группы и даты;",
            "- снимай ровно, без наклона и бликов;",
            "- лучше один день или неделя целиком, но крупно и в фокусе.",
            "",
            "После распознавания покажу, что получилось, и спрошу подтверждение — "
            "ничего не сохранится и не разошлётся без него.",
        ]
    )
    return "\n".join(lines)


def format_vk_ocr_summary_prompt(error: str = "") -> str:
    lines = ["Импорт сводного расписания", ""]
    if error:
        lines.extend([error, ""])
    lines.extend(
        [
            "Этот режим — для листа с расписанием на один день сразу для нескольких групп "
            "(таблица со столбцами Группа, №, Дисциплина, Преподаватель, Аудитория).",
            "Для расписания одной группы на несколько дней используй обычное «Расписание с фото».",
            "",
            "Пришли фото или несколько фото листа (можно все вложениями в одном сообщении).",
            "",
            "После распознавания покажу, для скольких групп нашёлся источник, и спрошу "
            "подтверждение — ничего не сохранится и не разошлётся без него.",
        ]
    )
    return "\n".join(lines)


def format_vk_ocr_summary_add_more_prompt(queued_count: int) -> str:
    lines = ["Добавляю фото к сводному расписанию", ""]
    if queued_count:
        lines.append(f"Уже загружено фото: {queued_count}.")
    lines.append("Пришли ещё фото листа — распознаю их вместе с уже загруженными.")
    return "\n".join(lines)


def format_vk_ocr_json_prompt(error: str = "") -> str:
    lines = ["Импорт готового JSON распознавания", ""]
    if error:
        lines.extend([error, ""])
    lines.extend(
        [
            "Если встроенное распознавание (Gemini) недоступно, перегружено или временно "
            "заблокировано — можно распознать фото любой другой нейросетью самому и прислать "
            "сюда уже готовый результат.",
            "",
            "1. Сфотографируй лист(ы) сводного расписания (один день, много групп).",
            "2. Пришли фото и этот промт любой нейросети с поддержкой картинок:",
            "",
            SUMMARY_RECOGNITION_PROMPT,
            "",
            "3. Скопируй её JSON-ответ и пришли его сюда — текстом или .json-файлом.",
            "",
            "После разбора покажу, для скольких групп нашёлся источник, и спрошу подтверждение — "
            "ничего не сохранится и не разошлётся без него.",
        ]
    )
    return "\n".join(lines)


def _best_vk_photo_url(photo) -> str:
    sizes = getattr(photo, "sizes", None) or []
    best_url = ""
    best_area = -1
    for size in sizes:
        url = getattr(size, "url", "") or ""
        area = (getattr(size, "width", 0) or 0) * (getattr(size, "height", 0) or 0)
        if url and area > best_area:
            best_area = area
            best_url = url
    return best_url


def _has_image_attachment(message: Message) -> bool:
    for attachment in getattr(message, "attachments", None) or []:
        if getattr(attachment, "photo", None) is not None:
            return True
        doc = getattr(attachment, "doc", None)
        if doc is not None and (getattr(doc, "ext", "") or "").lower() in {"jpg", "jpeg", "png", "bmp", "webp"}:
            return True
    return False


def _collect_vk_image_urls(message: Message) -> list[str]:
    """Все картинки во вложениях сообщения — в VK, в отличие от Telegram, альбом это одно сообщение с несколькими attachments."""
    urls: list[str] = []
    for attachment in getattr(message, "attachments", None) or []:
        photo = getattr(attachment, "photo", None)
        if photo is not None:
            url = _best_vk_photo_url(photo)
            if url:
                urls.append(url)
                continue
        doc = getattr(attachment, "doc", None)
        if doc is not None and (getattr(doc, "ext", "") or "").lower() in {"jpg", "jpeg", "png", "bmp", "webp"}:
            url = getattr(doc, "url", "") or ""
            if url:
                urls.append(url)
    return urls


async def _download_vk_url(client: httpx.AsyncClient, url: str) -> tuple[bytes | None, str]:
    try:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
    except httpx.HTTPError as exc:
        logger.warning("Не удалось скачать изображение для OCR (VK): %s", exc)
        return None, f"Не удалось скачать изображение: {exc}"

    if len(content) > MAX_OCR_IMAGE_BYTES:
        return None, "Файл слишком большой. Пришли фото поменьше (до 20 МБ)."
    content = await asyncio.to_thread(compress_image_for_ocr, content)
    return content, ""


async def download_vk_images(message: Message) -> tuple[list[bytes] | None, str]:
    """Достаёт все изображения из вложений сообщения VK — там альбом это одно сообщение.

    Если хоть одно вложение не скачалось — прерывает всё целиком: частичный
    импорт хуже честной ошибки, админ должен понимать, что именно распозналось.
    """
    urls = _collect_vk_image_urls(message)
    if not urls:
        return None, "Не вижу изображения. Пришли фото расписания."
    if len(urls) > MAX_OCR_IMAGES:
        return None, f"Слишком много фото за раз (максимум {MAX_OCR_IMAGES}). Пришли частями."

    images: list[bytes] = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for index, url in enumerate(urls, start=1):
            image_bytes, error = await _download_vk_url(client, url)
            if image_bytes is None:
                prefix = f"Фото {index}/{len(urls)}: " if len(urls) > 1 else ""
                return None, f"{prefix}{error}"
            images.append(image_bytes)
    return images, ""


def build_vk_bot(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster | None = None,
    group_catalog: GroupCatalog | None = None,
    search_catalog: ScheduleSearchCatalog | None = None,
    schedule_jobs: Any | None = None,
    ocr_importer: OcrScheduleImporter | None = None,
    error_reporter: AdminErrorReporter | None = None,
) -> Bot | None:
    if not settings.vk_bot_token:
        return None

    reporter = error_reporter or AdminErrorReporter(broadcaster.notify_admins if broadcaster is not None else None)
    api = build_vk_api(settings.vk_bot_token, verify_ssl=not settings.vk_disable_ssl_verify)

    async def on_polling_error(error: BaseException, failures: int) -> None:
        # Одиночные сбои сети не шлём: long poll сам переподключится. Шлём, если
        # связь не восстанавливается — это уже похоже на реальную проблему.
        if failures in {5, 30} or (failures > 30 and failures % 120 == 0):
            await reporter.report_exception(
                "VK long poll",
                error,
                summary=f"Нет связи с VK уже {failures} попыток подряд: {type(error).__name__}: {error}",
                details=[("Что происходит", "бот не получает сообщения VK, переподключается")],
            )

    async def on_polling_recovered(failures: int, outage_seconds: float) -> None:
        if failures >= 5 and broadcaster is not None:
            text = f"VK long poll снова работает (было {failures} ошибок, ~{outage_seconds:.0f} с без связи)."
            await broadcaster.notify_admins(text, text)

    polling = ResilientBotPolling(api, on_error=on_polling_error, on_recovered=on_polling_recovered)
    error_handler = ErrorHandler(redirect_arguments=True)
    bot = Bot(api=api, polling=polling, error_handler=error_handler)
    bot.resilient_polling = polling  # type: ignore[attr-defined]
    bot.error_reporter = reporter  # type: ignore[attr-defined]
    search_results: dict[int, dict[str, object]] = {}
    peer_modes: dict[int, str] = {}
    peer_pages: dict[int, dict[str, int]] = defaultdict(dict)
    admin_user_search_state: dict[int, dict[str, str]] = {}
    editor_option_map: dict[int, dict[str, int]] = defaultdict(dict)
    admin_broadcast_drafts: dict[int, dict] = {}
    admin_lesson_drafts: dict[int, dict[str, object]] = {}
    admin_lesson_delete_drafts: dict[int, dict[str, object]] = {}
    admin_lesson_delete_one_drafts: dict[int, dict[str, object]] = {}
    admin_import_lessons_drafts: dict[int, dict] = {}
    admin_ocr_drafts: dict[int, Any] = {}
    admin_ocr_summary_drafts: dict[int, Any] = {}
    # Фото, накопленные для текущего сводного распознавания — «Добавить ещё
    # фото» дозаписывает сюда новые страницы листа вместо замены прежних.
    admin_ocr_summary_images: dict[int, list[bytes]] = {}
    # Защита от повторного тапа "Подтвердить": между чтением черновика и его
    # удалением есть await (сам apply/apply_summary), и без этой блокировки
    # два быстрых подряд тапа могли применить один и тот же черновик дважды.
    admin_ocr_apply_locks: set[int] = set()
    message_rate_limit: dict[int, float] = {}
    message_rate_locks: dict[int, asyncio.Lock] = {}
    name_refreshed_at: dict[int, float] = {}
    # Долгие админские операции (рассылка, перепарсинг, подсчёт) идут в фоне:
    # сообщения одного диалога обрабатываются по очереди, и иначе админ минутами
    # не мог бы даже нажать «Назад».
    admin_background_tasks: set[asyncio.Task] = set()
    admin_running_jobs: set[str] = set()
    lesson_counter_service = LessonCounterService(db, settings.lesson_counters_path)
    admin_counter_sync_lock = asyncio.Lock()
    ocr_service = ocr_importer or build_ocr_importer(settings, db, schedule_jobs, group_catalog)

    make_keyboard = make_vk_keyboard

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

    async def notify_user_about_error(peer_id: int, error: BaseException) -> None:
        text = USER_TIMEOUT_TEXT if isinstance(error, TimeoutError) else USER_ERROR_TEXT
        try:
            await vk_send_message(bot.api, peer_id, text, max_attempts=2)
        except Exception as exc:
            logger.warning("Failed to notify VK user %s about their error: %s", peer_id, exc)

    async def notify_admin_about_error(
        user_id: int | None,
        peer_id: int | None,
        error: BaseException,
        text: str | None = None,
        mode: str | None = None,
    ) -> None:
        user_info = format_user_profile_link("vk", user_id, None, html=False)
        details = [
            ("Пользователь", user_info),
            ("Чат", str(peer_id) if peer_id is not None else "неизвестно"),
        ]
        if mode:
            details.append(("Режим", mode))
        if text:
            details.append(("Сообщение", text[:200]))
        await reporter.report_exception("VK-бот", error, details=details)

    async def report_update_failure(update: dict, error: BaseException) -> None:
        """Сбой вне обработчика (таймаут, ошибка роутера) — сообщаем и пользователю, и админу."""
        obj = update.get("object") if isinstance(update, dict) else None
        message_obj = obj.get("message") if isinstance(obj, dict) else None
        peer_id = message_obj.get("peer_id") if isinstance(message_obj, dict) else None
        from_id = message_obj.get("from_id") if isinstance(message_obj, dict) else None
        text = message_obj.get("text") if isinstance(message_obj, dict) else None
        if isinstance(peer_id, int):
            await notify_user_about_error(peer_id, error)
        await notify_admin_about_error(
            from_id if isinstance(from_id, int) else None,
            peer_id if isinstance(peer_id, int) else None,
            error,
            text=text,
            mode=peer_modes.get(peer_id) if isinstance(peer_id, int) else None,
        )

    bot.report_update_failure = report_update_failure  # type: ignore[attr-defined]

    async def wait_admin_jobs(timeout: float = 30.0) -> None:
        """Ждёт фоновые админские операции — для тестов и корректной остановки."""
        if admin_background_tasks:
            await asyncio.wait(set(admin_background_tasks), timeout=timeout)

    bot.wait_admin_jobs = wait_admin_jobs  # type: ignore[attr-defined]

    def spawn_admin_job(peer_id: int, job_key: str, coro: Awaitable[None]) -> bool:
        """Запускает долгую админскую операцию в фоне. False — такая уже идёт."""
        if job_key in admin_running_jobs:
            coro.close()  # type: ignore[attr-defined]
            return False
        admin_running_jobs.add(job_key)

        async def runner() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Фоновая админская операция %s упала: %s", job_key, exc)
                await notify_admin_about_error(settings.admin_vk_id, peer_id, exc, mode=f"фон: {job_key}")
                try:
                    await show_screen(peer_id, f"Операция «{job_key}» завершилась ошибкой: {short_error_text(exc)}", keyboard=admin_keyboard())
                except Exception:
                    logger.warning("Не удалось сообщить админу о сбое операции %s.", job_key)
            finally:
                admin_running_jobs.discard(job_key)

        task = asyncio.create_task(runner(), name=f"vk-admin-{job_key}")
        admin_background_tasks.add(task)
        task.add_done_callback(admin_background_tasks.discard)
        return True

    def user_is_admin(user_id: int | None) -> bool:
        return bool(user_id and settings.admin_vk_id and user_id == settings.admin_vk_id)

    async def user_can_manage_group(peer_id: int, user_id: int) -> bool:
        if peer_id < VK_CHAT_PEER_ID_THRESHOLD:
            return True
        if user_is_admin(user_id):
            return True
        try:
            members_resp = await bot.api.messages.get_conversation_members(peer_id=peer_id)
            items = getattr(members_resp, "items", []) or []
            for item in items:
                m_id = getattr(item, "member_id", None)
                if m_id == user_id:
                    if getattr(item, "is_admin", False) or getattr(item, "is_owner", False):
                        return True
                    join_by = getattr(item, "invited_by", None)
                    return join_by == user_id
        except Exception as exc:
            logger.warning("Failed to check conversation members for peer %s: %s", peer_id, exc)
            return False
        return False

    async def user_is_editor(user_id: int | None) -> bool:
        if user_id is None:
            return False
        user = await db.get_user("vk", user_id)
        return bool(user and user.is_editor)
    async def fetch_vk_names(user_ids: list[int]) -> dict[int, str]:
        unique_ids = sorted({user_id for user_id in user_ids if user_id > 0})
        result: dict[int, str] = {}
        # users.get принимает до 1000 id за раз.
        for start in range(0, len(unique_ids), 1000):
            batch = unique_ids[start : start + 1000]
            try:
                response = await vk_call_with_retry(
                    bot.api, "users.get", {"user_ids": ",".join(str(user_id) for user_id in batch)}, max_attempts=3
                )
            except Exception as exc:
                logger.warning("Failed to fetch VK names for %s users: %s", len(batch), exc)
                continue
            profiles = response.get("response") if isinstance(response, dict) else None
            for profile in profiles or []:
                if not isinstance(profile, dict) or not isinstance(profile.get("id"), int):
                    continue
                full_name = " ".join(part for part in [profile.get("first_name"), profile.get("last_name")] if part).strip()
                if full_name:
                    result[profile["id"]] = full_name
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
        if message.from_id is None or message.from_id <= 0:
            return
        existing = await db.get_user("vk", message.from_id)
        full_name = existing.full_name if existing else None
        last_refresh = name_refreshed_at.get(message.from_id)
        if not full_name or last_refresh is None or monotonic() - last_refresh > VK_NAME_REFRESH_SECONDS:
            names = await fetch_vk_names([message.from_id])
            name_refreshed_at[message.from_id] = monotonic()
            full_name = names.get(message.from_id) or full_name
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

    last_pers_menu_message_id: dict[int, int] = {}

    async def send_vk_message(
        peer_id: int,
        message: str,
        *,
        keyboard: str | None = None,
        attachment: str | None = None,
        max_attempts: int = 4,
    ) -> int | None:
        return await vk_send_message(
            bot.api, peer_id, message, keyboard=keyboard, attachment=attachment, max_attempts=max_attempts
        )

    async def show_screen(peer_id: int, text: str, keyboard: str | None = None, attachment: str | None = None) -> None:
        await send_vk_message(peer_id=peer_id, message=text, keyboard=keyboard, attachment=attachment)

    class ProgressMessage:
        """Одно сообщение о ходе долгой операции, которое обновляется на месте.

        Раньше каждый шаг распознавания и каждые 0.8 с рассылки приходили новым
        сообщением — на рассылке по сотням пользователей это был поток спама админу.
        """

        def __init__(self, peer_id: int, min_interval: float = 3.0) -> None:
            self.peer_id = peer_id
            self.min_interval = min_interval
            self.message_id: int | None = None
            self._last_update = 0.0

        async def update(self, text: str, *, force: bool = False, keyboard: str | None = None) -> None:
            now = monotonic()
            if not force and self.message_id is not None and now - self._last_update < self.min_interval:
                return
            self._last_update = now
            if self.message_id is not None and keyboard is None and await vk_edit_message(bot.api, self.peer_id, self.message_id, text):
                return
            self.message_id = await send_vk_message(self.peer_id, text, keyboard=keyboard)
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
            "Массовая рассылка",
            "───────────────────────────",
            "Отправьте текст сообщения для рассылки одним сообщением.",
            "После отправки текста откроется окно настройки параметров и предпросмотра.",
            "",
            "Для отмены используйте кнопку «Отменить».",
        ]
        if error_text:
            lines.extend(["", f"Ошибка: {error_text}."])
        return "\n".join(lines)

    def admin_broadcast_preview_text(
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
            "all": "Всем пользователям",
            "students": "Студентам",
            "teachers": "Преподавателям",
        }
        platform_str = platform_map.get(target_platform, "Глобально (везде)")
        audience_str = audience_map.get(target_audience, "Всем пользователям")

        return "\n".join([
            "Предпросмотр рассылки",
            "───────────────────────────",
            "Параметры отправки:",
            f"• Платформа: {platform_str}.",
            f"• Аудитория: {audience_str}.",
            "───────────────────────────",
            "Текст сообщения:",
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

    def search_prompt_keyboard() -> str:
        return make_keyboard([["Назад в меню"]])

    def search_result_keyboard() -> str:
        return make_keyboard(
            [
                ["Найти расписание"],
                ["Назад в меню"],
            ]
        )
    async def format_vk_personalization_text(peer_id: int) -> str:
        user = await db.get_user("vk", peer_id)
        has_sticker = bool(user and user.custom_sticker_file_id)
        sticker_status = "Прикреплен" if has_sticker else "Не установлен"

        return "\n".join([
            "Персонализация уведомлений",
            "",
            f"• Ваш стикер: {sticker_status}",
            "",
            "Вы можете установить свой стикер. Он будет отправляться перед автоматическими уведомлениями и при вызове меню расписания.",
        ])


    async def vk_personalization_keyboard(peer_id: int) -> str:
        user = await db.get_user("vk", peer_id)
        has_sticker = bool(user and user.custom_sticker_file_id)

        rows = [["Установить стикер"]]
        if has_sticker:
            rows.append(["Предпросмотр стикера", "Сбросить стикер"])
        rows.append(["Назад к настройкам"])
        return make_keyboard(rows)

    async def show_pers_screen(peer_id: int, text: str, keyboard: str | None = None) -> None:
        msg_id = await send_vk_message(peer_id=peer_id, message=text, keyboard=keyboard)
        if msg_id:
            last_pers_menu_message_id[peer_id] = msg_id

    def admin_keyboard() -> str:
        return make_keyboard(vk_admin_keyboard_rows())

    # Экраны внутри админки возвращают в админку, а не в пользовательское меню:
    # раньше «Назад в меню» после статуса или перепарсинга выкидывало из админки целиком.
    def admin_back_keyboard() -> str:
        return make_keyboard([[VK_ADMIN_SECTION_BACK]])

    def admin_status_keyboard() -> str:
        return make_keyboard([["Ошибки за день"], ["Обновить статус", VK_ADMIN_SECTION_BACK]])

    def admin_daily_errors_keyboard() -> str:
        return make_keyboard([["Статус", VK_ADMIN_SECTION_BACK]])

    def admin_gemini_keyboard() -> str:
        return make_keyboard([["Обновить", VK_ADMIN_SECTION_BACK]])

    def admin_user_profile_link(user) -> str:
        if user.platform == "vk":
            if user.username:
                clean = user.username.lstrip("@").strip()
                return f"https://vk.ru/{clean}"
            return f"https://vk.ru/id{user.user_id}"
        if user.username:
            clean = user.username.lstrip("@").strip()
            return f"https://t.me/{clean}"
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

    def format_admin_user_row(user) -> str:
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
        return f"• [{platform_label}] {user_label} | {nick_or_name} | {user.user_id} | {group_label}{role_suffix}\n  {admin_user_profile_link(user)}"

    def paginate(total_items: int, page: int, page_size: int) -> tuple[int, int]:
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        return max(0, min(page, total_pages - 1)), total_pages

    def admin_broadcast_preview_keyboard(
        target_platform: str = "all",
        target_audience: str = "all",
    ) -> str:
        plat_all = "Платформа: ✅ Везде" if target_platform == "all" else "Платформа: Везде"
        plat_tg = "Платформа: ✅ В ТГ" if target_platform == "telegram" else "Платформа: В ТГ"
        plat_vk = "Платформа: ✅ В ВК" if target_platform == "vk" else "Платформа: В ВК"

        aud_all = "Кому: ✅ Всем" if target_audience == "all" else "Кому: Всем"
        aud_students = "Кому: ✅ Студентам" if target_audience == "students" else "Кому: Студентам"
        aud_teachers = "Кому: ✅ Преподавателям" if target_audience == "teachers" else "Кому: Преподавателям"

        return make_keyboard([
            [plat_all, plat_tg, plat_vk],
            [aud_all, aud_students, aud_teachers],
            ["Подтвердить рассылку"],
            ["Отменить"],
        ])

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
                "Профиль: github.com/nekotyy",
                "Проект: github.com/nekotyy/misis-rasp-bot",
                "",
                "Если понравилось, поставь звездочку на GitHub ⭐",
            ]
        )

    def format_admin_lesson_prompt(step: str, draft: dict[str, object] | None = None, error_text: str | None = None) -> str:
        header = 'Редактирование пары' if draft and draft.get("mode") == "edit" else 'Добавление пары'
        prompt_map = {
            "group": "Шаг 1/5. Укажите группу или schedule_id.",
            "subject": "Шаг 2/5. Укажите название дисциплины.",
            "teacher": "Шаг 3/5. Укажите ФИО преподавателя.",
            "passed": "Шаг 4/5. Сколько пар уже прошло (число)?",
            "total": "Шаг 5/5. Сколько пар запланировано всего (число)?",
        }
        lines = [header, "───────────────────────────", prompt_map.get(step, 'Продолжайте ввод.')]
        if draft and draft.get("group_name"):
            lines.extend(["", f"• Группа: {draft['group_name']}."])
        if error_text:
            lines.extend(["", f"Ошибка: {error_text}."])
        lines.extend(["───────────────────────────", "Для отмены нажмите кнопку «Отменить»."])
        return "\n".join(lines)

    def format_admin_lesson_preview(draft: dict[str, object]) -> str:
        return "\n".join([
            "Проверка данных пары",
            "───────────────────────────",
            f"• Группа: {draft.get('group_name', '')}.",
            f"• Schedule ID: {draft.get('schedule_id', '')}.",
            f"• Дисциплина: {draft.get('subject', '')}.",
            f"• Преподаватель: {draft.get('teacher', '')}.",
            f"• Пройдено пар: {draft.get('passed', 0)}.",
            f"• Всего пар: {draft.get('total', 0)}.",
            "───────────────────────────",
            "Подтвердить сохранение пары?",
        ])

    def format_admin_lesson_delete_prompt(
        step: str,
        draft: dict[str, object] | None = None,
        error_text: str | None = None,
    ) -> str:
        prompt_map = {
            "group": "Шаг 1/2. Укажите группу или schedule_id.",
            "confirm": "Шаг 2/2. Подтвердите удаление всех пар у выбранной группы.",
        }
        lines = ["Удаление всех пар группы", "───────────────────────────", prompt_map.get(step, "Продолжайте ввод.")]
        if draft and draft.get("group_name"):
            lines.extend(["", f"• Группа: {draft['group_name']}."])
        if error_text:
            lines.extend(["", f"Ошибка: {error_text}."])
        lines.extend(["───────────────────────────", "Для отмены нажмите кнопку «Отменить»."])
        return "\n".join(lines)

    def format_admin_lesson_delete_one_prompt(
        step: str,
        draft: dict[str, object] | None = None,
        error_text: str | None = None,
    ) -> str:
        prompt_map = {
            "group": "Шаг 1/4. Укажите группу или schedule_id.",
            "subject": "Шаг 2/4. Укажите дисциплину.",
            "teacher": "Шаг 3/4. Укажите преподавателя.",
            "confirm": "Шаг 4/4. Подтвердите удаление пары.",
        }
        lines = ["Удаление пары", "───────────────────────────", prompt_map.get(step, "Продолжайте ввод.")]
        if draft and draft.get("group_name"):
            lines.extend(["", f"• Группа: {draft['group_name']}."])
        if draft and draft.get("subject"):
            lines.append(f"• Дисциплина: {draft['subject']}.")
        if draft and draft.get("teacher"):
            lines.append(f"• Преподаватель: {draft['teacher']}.")
        if error_text:
            lines.extend(["", f"Ошибка: {error_text}."])
        lines.extend(["───────────────────────────", "Для отмены нажмите кнопку «Отменить»."])
        return "\n".join(lines)

    async def send_admin_document(peer_id: int, path: Path, title: str) -> None:
        if not path.exists():
            await show_screen(peer_id, f"{title} не найден.", keyboard=admin_keyboard())
            return
        await show_screen(peer_id, f"Загружаю {title}...")
        uploader = DocMessagesUploader(bot.api)
        doc = await uploader.upload(path, peer_id=peer_id, title=title)
        await send_vk_message(peer_id, title, attachment=doc, keyboard=admin_keyboard())

    async def sync_lesson_counters_from_file() -> None:
        try:
            active_catalog = group_catalog or GroupCatalog(settings.schedule_url, db=db)
            await active_catalog.ensure_loaded()
            counters = await lesson_counter_service.load_config_file(settings.lesson_counters_path, active_catalog)
            await lesson_counter_service.sync_config(counters)
        except Exception:
            logger.exception("Lesson counters sync failed after admin update.")

    build_subscription_settings_keyboard = build_vk_subscription_settings_keyboard

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
        lines = [title, "───────────────────────────"]
        if not rows:
            lines.append("Нет записей.")
            return "\n".join(lines)
        lines.append(f"Затронуто источников: {len(rows)}.")
        lines.append("───────────────────────────")
        for index, (group_name, action_time, action_name) in enumerate(rows, start=1):
            lines.append(f"{index}. {group_name} — {action_time} ({action_name}).")
        return "\n".join(lines)

    def format_daily_change_report(title: str, rows: list[dict]) -> str:
        lines = [title, "───────────────────────────"]
        if not rows:
            lines.append("Сегодня изменений расписания пока не было.")
            return "\n".join(lines)
        lines.append(f"Всего изменений: {len(rows)}.")
        lines.append("───────────────────────────")
        for index, row in enumerate(rows, start=1):
            name = row.get("group_name") or row.get("subscription_title") or "Без названия"
            t = row.get("created_at") or ""
            lines.append(f"{index}. {name} — {t}.")
        return "\n".join(lines)

    def format_group_user_stats(rows: list[dict[str, int | str]]) -> str:
        lines = [
            "Информация по учебным группам",
            "───────────────────────────",
        ]
        if not rows:
            lines.append("Пока нет пользователей с выбранной учебной группой.")
            return "\n".join(lines)
        total_users = sum(int(row["users_count"]) for row in rows)
        total_personal = sum(int(row.get("personal_count", 0)) for row in rows)
        total_chats = sum(int(row.get("chat_count", 0)) for row in rows)
        lines.append(f"• Всего групп с подписчиками: {len(rows)}.")
        lines.append(f"• Всего подписчиков на группы: {total_users} (Личных: {total_personal}, Бесед/чатов: {total_chats}).")
        lines.append("───────────────────────────")
        lines.append("Список групп:")
        for index, row in enumerate(rows, start=1):
            g_name = str(row["group_name"])
            u_count = int(row["users_count"])
            p_count = int(row.get("personal_count", 0))
            c_count = int(row.get("chat_count", 0))
            tg_c = int(row.get("tg_chat_count", 0))
            vk_c = int(row.get("vk_chat_count", 0))
            chat_info = f", Бесед: {c_count} [TG: {tg_c}, VK: {vk_c}]" if c_count > 0 else ""
            lines.append(f"{index}. {g_name} — {u_count} подп. (Личных: {p_count}{chat_info}).")
        return "\n".join(lines)

    def schedule_text(day, fallback: str) -> str:
        if day is None:
            return f"Расписание на {fallback}\n\nПар нет."
        return ScheduleFormatter.format_day_plain(day)

    async def admin_status_text() -> str:
        return await build_vk_admin_status_text(db, settings, ocr_service)

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
        await prompt_group_selection_screen(peer_id, error_text)

    async def prompt_group_selection_screen(peer_id: int, error_text: str | None = None) -> None:
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

    async def ensure_subscription_selected(peer_id: int, user_id: int) -> bool:
        user = await db.get_user("vk", user_id)
        if user is not None and user.subscription_key and user.subscription_title:
            return True
        await prompt_group_selection(peer_id)
        return False

    async def handle_subscription_input(peer_id: int, user_id: int, text: str) -> bool:
        is_chat = peer_id >= VK_CHAT_PEER_ID_THRESHOLD and user_id == peer_id
        existing_user = await db.get_user("vk", user_id)
        if existing_user is None and is_chat:
            # У беседы нет своей строки в users (регистрируются только люди), а
            # set_user_subscription — это UPDATE: без строки подписка беседы молча
            # не сохранялась, и настройка бота в беседах VK не работала вообще.
            await db.upsert_user(platform="vk", user_id=peer_id, username=None, full_name=f"VK беседа {peer_id - VK_CHAT_PEER_ID_THRESHOLD}")

        async def prompt_group_selection(target_peer_id: int, error_text: str | None = None) -> None:
            if is_chat:
                peer_modes[target_peer_id] = "awaiting_group_selection"
                retry_text = "Пришлите название группы ещё раз одним сообщением."
                await show_screen(target_peer_id, f"{error_text}\n\n{retry_text}" if error_text else retry_text)
                return
            await prompt_group_selection_screen(target_peer_id, error_text)

        group = None
        if group_catalog is not None:
            try:
                group = await group_catalog.find_group(text)
            except Exception as exc:
                logger.warning("VK error finding group in GroupCatalog: %s", exc)
                await prompt_group_selection(peer_id, "Не получилось проверить группу — временная ошибка связи с сайтом расписания. Попробуйте еще раз через несколько минут.")
                return False

        if group is not None:
            await db.set_user_subscription("vk", user_id, **make_group_subscription(group.group_name, group.schedule_id))
            await db.clear_user_audience_subscription("vk", user_id)
        else:
            if group_catalog is not None and getattr(group_catalog, "last_error", None) is not None and not getattr(group_catalog, "_groups_by_name", {}):
                await prompt_group_selection(peer_id, "Такая группа не найдена. Сайт расписания сейчас недоступен, а среди ранее сохранённых групп её тоже нет — если группа новая, попробуйте еще раз, когда сайт заработает.")
                return False

            if search_catalog is None:
                await prompt_group_selection(peer_id, "Справочник сейчас недоступен. Попробуйте позже.")
                return False
            try:
                target = await search_catalog.find(text)
            except Exception as exc:
                logger.warning("VK error in search_catalog.find: %s", exc)
                await prompt_group_selection(peer_id, "Сайт расписания колледжа сейчас временно недоступен. Попробуйте еще раз через несколько минут.")
                return False
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
        if is_chat:
            updated = await db.get_user("vk", peer_id)
            title = updated.subscription_title if updated else text
            peer_modes[peer_id] = "main_menu"
            await show_screen(
                peer_id,
                f"Беседа подписана: {title}.\n\n"
                "Сюда будут приходить уведомления об изменениях расписания. "
                "Сменить группу — снова /startgroup.",
            )
            return True
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
        try:
            if user.subscription_type == "teacher" and user.subscription_title:
                snapshot_obj = await build_teacher_schedule_snapshot(db, user.subscription_title)
                snapshot_hash = compute_snapshot_hash(snapshot_obj)
            elif user.subscription_type == "audience" and user.subscription_url:
                snapshot_obj, snapshot_hash = await parser.parse_from_url(user.subscription_url)
            elif user.schedule_id is not None:
                snapshot_obj, snapshot_hash = await parser.parse(user.schedule_id)
            else:
                return None
        except httpx.HTTPError as exc:
            logger.warning(
                "Failed to fetch VK subscription snapshot for user %s (%s): %s",
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

    async def perform_schedule_search(peer_id: int, query: str) -> bool:
        if search_catalog is None:
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text("Поиск временно недоступен."), keyboard=search_prompt_keyboard())
            return False
        try:
            target = await search_catalog.find(query)
        except httpx.HTTPError:
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text("Сайт расписания временно недоступен. Попробуй еще раз через минуту."), keyboard=search_prompt_keyboard())
            return False
        if target is None:
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text(SEARCH_NOT_FOUND_TEXT), keyboard=search_prompt_keyboard())
            return False
        try:
            if target.kind == "teacher":
                content = snapshot_to_search_content(await build_teacher_schedule_snapshot(db, target.title))
            elif target.kind == "group":
                content = await resolve_group_preview_content(db, parser, target.url)
            else:
                content = await resolve_audience_preview_content(db, parser, target.url)
        except httpx.HTTPError:
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text("Сайт расписания временно недоступен. Попробуй еще раз через минуту."), keyboard=search_prompt_keyboard())
            return False
        snapshot = {
            "title": target.title,
            "content": content,
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
        logger.error(
            "VK handler failed for peer %s: %s",
            peer_id,
            error_obj,
            exc_info=error_obj,
            extra={"skip_admin_report": True},
        )
        if peer_id is not None:
            await notify_user_about_error(peer_id, error_obj)
        await notify_admin_about_error(
            user_id,
            peer_id,
            error_obj,
            text=getattr(message, "text", None),
            mode=peer_modes.get(peer_id) if peer_id is not None else None,
        )

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
        for index, source in enumerate(sources):
            if index and source["source_type"] != "teacher":
                await asyncio.sleep(MANUAL_REFRESH_PAUSE_SECONDS)
            try:
                if source["source_type"] == "teacher":
                    snapshot = await build_teacher_schedule_snapshot(db, str(source.get("source_title") or ""))
                    snapshot_hash = compute_snapshot_hash(snapshot)
                elif source["source_type"] == "audience":
                    snapshot, snapshot_hash = await parser.parse_from_url(source["source_url"])
                else:
                    snapshot, snapshot_hash = await parser.parse(source["schedule_id"])
            except Exception as exc:
                logger.warning("Admin refresh failed for source %s: %s", source["source_title"], exc)
                rows.append((source["source_title"], "-", f"ошибка: {exc}"))
                continue
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
        for index, source in enumerate(sources):
            if index and source["source_type"] != "teacher":
                await asyncio.sleep(MANUAL_REFRESH_PAUSE_SECONDS)
            try:
                if source["source_type"] == "teacher":
                    snapshot = await build_teacher_schedule_snapshot(db, str(source.get("source_title") or ""))
                    snapshot_hash = compute_snapshot_hash(snapshot)
                elif source["source_type"] == "audience":
                    snapshot, snapshot_hash = await parser.parse_from_url(source["source_url"])
                else:
                    snapshot, snapshot_hash = await parser.parse(source["schedule_id"])
            except Exception as exc:
                logger.warning("Admin baseline save failed for source %s: %s", source["source_title"], exc)
                rows.append((source["source_title"], "-", f"ошибка: {exc}"))
                continue
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
            await show_screen(peer_id, "Пользователи бота\n───────────────────────────\nПока никто не зарегистрирован.", keyboard=make_keyboard([["Назад в админку"]]))
            return

        user_rows = [format_admin_user_row(user) for user in users]

        page, total_pages = paginate(len(user_rows), page, ADMIN_USERS_PAGE_SIZE)
        peer_pages[peer_id]["admin_users"] = page
        peer_modes[peer_id] = "admin_users"

        start = page * ADMIN_USERS_PAGE_SIZE
        end = start + ADMIN_USERS_PAGE_SIZE
        lines = [
            "Пользователи бота",
            "───────────────────────────",
            "Формат: [платформа] Имя | Ник/ФИ | ID | Группа (роли)",
            f"Страница {page + 1}/{total_pages} (всего: {len(user_rows)}).",
            "───────────────────────────",
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

    async def show_admin_user_search_results(peer_id: int, page: int = 0) -> bool:
        state = admin_user_search_state.get(peer_id)
        if state is None:
            return False

        users = await db.list_users()
        await sync_vk_user_names([user.user_id for user in users if user.platform == "vk"])
        users = await db.list_users()
        matches = filter_admin_users(users, state["query"])
        if not matches:
            admin_user_search_state.pop(peer_id, None)
            peer_pages[peer_id].pop("admin_user_search", None)
            peer_modes[peer_id] = "admin_user_search"
            await show_screen(
                peer_id,
                f"Поиск пользователя\n───────────────────────────\nПо запросу «{state['query']}» ничего не найдено.",
                keyboard=make_keyboard([["Искать снова"], ["Все пользователи"], ["Назад в админку"]]),
            )
            return True

        user_rows = [format_admin_user_row(user) for user in matches]

        page, total_pages = paginate(len(user_rows), page, ADMIN_USERS_PAGE_SIZE)
        peer_pages[peer_id]["admin_user_search"] = page
        peer_modes[peer_id] = "admin_user_search_results"

        start = page * ADMIN_USERS_PAGE_SIZE
        end = start + ADMIN_USERS_PAGE_SIZE
        lines = [
            "Результаты поиска",
            "───────────────────────────",
            f"Запрос: {state['query']}.",
            f"Найдено пользователей: {len(matches)}.",
            f"Страница: {page + 1}/{total_pages}.",
            "───────────────────────────",
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
        rows.append(["Искать снова"])
        rows.append(["Все пользователи"])
        rows.append(["Назад в админку"])

        await show_screen(peer_id, "\n".join(lines), keyboard=make_keyboard(rows))
        return True

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
        if message.peer_id is None or message.from_id is None:
            return
        # Сообщения других сообществ и ботов в беседе не обрабатываем.
        if message.from_id < 0:
            return
        try:
            await register_user(message)
        except Exception as exc:
            # Регистрация — вспомогательный шаг, из-за неё нельзя терять ответ пользователю.
            logger.warning("Не удалось обновить профиль VK-пользователя %s: %s", message.from_id, exc)

        peer_id = message.peer_id
        user_id = message.from_id
        text = strip_vk_bot_mention(message.text)
        normalized = text.casefold()
        mode = peer_modes.get(peer_id, "main_menu")

        has_attachments = bool(getattr(message, "attachments", None))
        if text and not has_attachments:
            await wait_rate_limit_queue(user_id, 0.8)

        # Инструкция — только когда в беседу добавили самого бота (сообщество: отрицательный member_id),
        # а не при каждом приглашённом участнике.
        if (
            message.action
            and getattr(message.action, "type", None) in {"chat_invite_user", "chat_invite_user_by_link"}
            and (getattr(message.action, "member_id", None) or 0) < 0
        ):
            welcome_msg = (
                "Инструкция по настройке бота в беседе\n\n"
                "Бот успешно добавлен в вашу беседу.\n\n"
                "Пошаговая настройка:\n"
                "1. Предоставьте боту доступ к переписке в настройках беседы (или назначьте администратором).\n"
                "2. Отправьте в беседу команду /startgroup.\n"
                "3. Укажите название вашей учебной группы (например: ИСП-25-1).\n\n"
                "После этого беседе будет доступно расписание и автоматические уведомления об изменениях."
            )
            await show_screen(peer_id, welcome_msg)
            return

        if mode == "awaiting_group_selection":
            if text.startswith("/"):
                return
            if not await user_can_manage_group(peer_id, user_id):
                await show_screen(peer_id, "Настройка беседы доступна только администраторам беседы или пользователю, добавившему бота.")
                return
            success = await handle_subscription_input(peer_id, peer_id if peer_id >= VK_CHAT_PEER_ID_THRESHOLD else user_id, text)
            if success:
                peer_modes[peer_id] = "main_menu"
            return

        if peer_id >= VK_CHAT_PEER_ID_THRESHOLD and not is_group_setup_command(text):
            # В беседе бот реагирует только на настройку — как и в группах Telegram.
            # Иначе каждое сообщение участников без личной подписки уходило бы в
            # поиск группы, и бот отвечал бы «Ничего не найдено» на всю переписку.
            return

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
                await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
                return
            if mode == "admin_broadcast_input":
                if not text:
                    await show_screen(peer_id, admin_broadcast_prompt_text("Текст не должен быть пустым."), keyboard=make_keyboard([["Отменить"]]))
                    return
                admin_broadcast_drafts[peer_id] = {
                    "text": text,
                    "target_audience": "all",
                }
                peer_modes[peer_id] = "admin_broadcast_preview"
                await show_screen(
                    peer_id,
                    admin_broadcast_preview_text(text, target_audience="all"),
                    keyboard=admin_broadcast_preview_keyboard("all"),
                )
                return

            draft = admin_broadcast_drafts.get(peer_id)
            if not isinstance(draft, dict):
                draft = {"text": str(draft or ""), "target_platform": "all", "target_audience": "all"}
                admin_broadcast_drafts[peer_id] = draft

            if text in {"Платформа: Везде", "Платформа: ✅ Везде"}:
                draft["target_platform"] = "all"
                target_aud = draft.get("target_audience", "all")
                await show_screen(
                    peer_id,
                    admin_broadcast_preview_text(draft["text"], target_platform="all", target_audience=target_aud),
                    keyboard=admin_broadcast_preview_keyboard("all", target_aud),
                )
                return

            if text in {"Платформа: В ТГ", "Платформа: ✅ В ТГ"}:
                draft["target_platform"] = "telegram"
                target_aud = draft.get("target_audience", "all")
                await show_screen(
                    peer_id,
                    admin_broadcast_preview_text(draft["text"], target_platform="telegram", target_audience=target_aud),
                    keyboard=admin_broadcast_preview_keyboard("telegram", target_aud),
                )
                return

            if text in {"Платформа: В ВК", "Платформа: ✅ В ВК"}:
                draft["target_platform"] = "vk"
                target_aud = draft.get("target_audience", "all")
                await show_screen(
                    peer_id,
                    admin_broadcast_preview_text(draft["text"], target_platform="vk", target_audience=target_aud),
                    keyboard=admin_broadcast_preview_keyboard("vk", target_aud),
                )
                return

            if text in {"Кому: Всем", "Кому: ✅ Всем", "Всем"}:
                draft["target_audience"] = "all"
                target_plat = draft.get("target_platform", "all")
                await show_screen(
                    peer_id,
                    admin_broadcast_preview_text(draft["text"], target_platform=target_plat, target_audience="all"),
                    keyboard=admin_broadcast_preview_keyboard(target_plat, "all"),
                )
                return

            if text in {"Кому: Студентам", "Кому: ✅ Студентам", "Студентам"}:
                draft["target_audience"] = "students"
                target_plat = draft.get("target_platform", "all")
                await show_screen(
                    peer_id,
                    admin_broadcast_preview_text(draft["text"], target_platform=target_plat, target_audience="students"),
                    keyboard=admin_broadcast_preview_keyboard(target_plat, "students"),
                )
                return

            if text in {"Кому: Преподавателям", "Кому: ✅ Преподавателям", "Преподавателям"}:
                draft["target_audience"] = "teachers"
                target_plat = draft.get("target_platform", "all")
                await show_screen(
                    peer_id,
                    admin_broadcast_preview_text(draft["text"], target_platform=target_plat, target_audience="teachers"),
                    keyboard=admin_broadcast_preview_keyboard(target_plat, "teachers"),
                )
                return

            if text in {"Подтвердить", "Подтвердить рассылку", "Отправить", "Отправить везде"}:
                draft_text = draft.get("text")
                if not draft_text:
                    peer_modes[peer_id] = "admin_broadcast_input"
                    await show_screen(peer_id, admin_broadcast_prompt_text("Сначала отправь текст рассылки."), keyboard=make_keyboard([["Отменить"]]))
                    return
                if broadcaster is None:
                    await show_screen(peer_id, "Сервис рассылки сейчас недоступен.", keyboard=admin_broadcast_preview_keyboard(draft.get("target_platform", "all"), draft.get("target_audience", "all")))
                    return

                target_audience = draft.get("target_audience", "all")
                target_platform = draft.get("target_platform", "all")

                admin_broadcast_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                progress_message = ProgressMessage(peer_id, min_interval=5.0)

                async def on_vk_progress(prog: BroadcastProgress) -> None:
                    report = format_broadcast_progress_status(draft_text, prog, html=False)
                    if prog.is_finished:
                        await progress_message.update(report, force=True)
                        await show_screen(peer_id, "Рассылка завершена.", keyboard=admin_keyboard())
                        return
                    await progress_message.update(report)

                started = spawn_admin_job(
                    peer_id,
                    "рассылка",
                    broadcaster.broadcast(
                        draft_text,
                        telegram_message=escape(draft_text),
                        vk_message=draft_text,
                        campaign_type=CAMPAIGN_ADMIN_BROADCAST,
                        target_platform=target_platform,
                        target_audience=target_audience,
                        progress_callback=on_vk_progress,
                    ),
                )
                if not started:
                    await show_screen(peer_id, "Предыдущая рассылка ещё идёт — дождись её отчёта.", keyboard=admin_keyboard())
                return

            if text:
                draft["text"] = text

            target_aud = draft.get("target_audience", "all")
            await show_screen(
                peer_id,
                admin_broadcast_preview_text(draft.get("text", ""), target_audience=target_aud),
                keyboard=admin_broadcast_preview_keyboard(target_aud),
            )
            return

        # Админ прислал картинку в личку — значит, хочет импорт расписания.
        # Кнопка в админке не обязательна: раньше без неё бот просто молчал.
        if (
            user_is_admin(user_id)
            and peer_id < VK_CHAT_PEER_ID_THRESHOLD
            and mode
            not in {
                "admin_ocr_input",
                "admin_ocr_preview",
                "admin_ocr_summary_input",
                "admin_ocr_summary_preview",
                "admin_ocr_json_input",
                "admin_ocr_json_preview",
            }
            and _has_image_attachment(message)
        ):
            available, availability_message = ocr_service.availability()
            if not available:
                await show_screen(peer_id, availability_message, keyboard=admin_keyboard())
                return
            admin_ocr_drafts.pop(peer_id, None)
            peer_modes[peer_id] = "admin_ocr_input"
            mode = "admin_ocr_input"

        if user_is_admin(user_id) and mode in {"admin_ocr_input", "admin_ocr_preview"}:
            if text == "Отменить":
                admin_ocr_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
                return

            if mode == "admin_ocr_preview" and text in {"Подтвердить и разослать", "Сохранить без рассылки"}:
                if peer_id in admin_ocr_apply_locks:
                    await show_screen(peer_id, "Уже сохраняю расписание, подожди...")
                    return
                draft = admin_ocr_drafts.get(peer_id)
                if draft is None:
                    peer_modes[peer_id] = "admin_ocr_input"
                    await show_screen(
                        peer_id,
                        "Данные распознавания устарели. Пришли фото заново.",
                        keyboard=make_keyboard([["Отменить"]]),
                    )
                    return
                admin_ocr_apply_locks.add(peer_id)
                try:
                    applied, report = await ocr_service.apply(draft, notify=text == "Подтвердить и разослать")
                finally:
                    admin_ocr_apply_locks.discard(peer_id)
                if applied:
                    admin_ocr_drafts.pop(peer_id, None)
                    peer_modes[peer_id] = "admin_menu"
                    await show_screen(
                        peer_id,
                        f"Расписание с фото импортировано.\n\n{report}",
                        keyboard=admin_keyboard(),
                    )
                    return
                await show_screen(
                    peer_id,
                    f"Импорт не выполнен.\n\n{report}",
                    keyboard=make_keyboard([["Отменить"]]),
                )
                return

            images, download_error = await download_vk_images(message)
            if images is None:
                await show_screen(
                    peer_id,
                    format_vk_ocr_prompt(download_error),
                    keyboard=make_keyboard([["Отменить"]]),
                )
                return

            upload_label = OCR_STAGE_UPLOAD if len(images) == 1 else f"{OCR_STAGE_UPLOAD} ({len(images)} фото)"
            ocr_progress = ProgressMessage(peer_id, min_interval=2.0)
            await ocr_progress.update(format_progress_bar(upload_label, 10), force=True)

            async def report_progress(stage: str, percent: int) -> None:
                await ocr_progress.update(format_progress_bar(stage, percent), force=percent >= 100)

            try:
                draft = await asyncio.wait_for(
                    ocr_service.build_draft(images, progress=report_progress),
                    timeout=ocr_service.recognize_timeout,
                )
            except TimeoutError:
                logger.warning("Распознавание фото не уложилось в %s с (VK).", ocr_service.recognize_timeout)
                await show_screen(
                    peer_id,
                    format_vk_ocr_prompt(
                        f"Распознавание не уложилось в {ocr_service.recognize_timeout:.0f} с и было прервано. "
                        "Пришли фото поменьше или увеличь OCR_TIMEOUT_SECONDS."
                    ),
                    keyboard=make_keyboard([["Отменить"]]),
                )
                return
            except OcrEngineError as exc:
                await show_screen(
                    peer_id,
                    format_vk_ocr_prompt(f"Не удалось распознать фото: {exc}"),
                    keyboard=make_keyboard([["Отменить"]]),
                )
                return
            except Exception as exc:
                logger.exception("Ошибка распознавания расписания с фото (VK).")
                await show_screen(
                    peer_id,
                    format_vk_ocr_prompt(f"Внутренняя ошибка: {type(exc).__name__}: {exc}"),
                    keyboard=make_keyboard([["Отменить"]]),
                )
                return

            admin_ocr_drafts[peer_id] = draft
            peer_modes[peer_id] = "admin_ocr_preview"
            keyboard = (
                make_keyboard([["Подтвердить и разослать"], ["Сохранить без рассылки"], ["Отменить"]])
                if draft.can_apply
                else make_keyboard([["Отменить"]])
            )
            await show_screen(
                peer_id,
                format_ocr_preview(draft, html=False, max_length=VK_MESSAGE_LIMIT),
                keyboard=keyboard,
            )
            return

        if user_is_admin(user_id) and mode in {"admin_ocr_summary_input", "admin_ocr_summary_preview"}:
            if text == "Отменить":
                admin_ocr_summary_drafts.pop(peer_id, None)
                admin_ocr_summary_images.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
                return

            if mode == "admin_ocr_summary_preview":
                if text in {"Подтвердить и разослать", "Сохранить без рассылки"}:
                    if peer_id in admin_ocr_apply_locks:
                        await show_screen(peer_id, "Уже сохраняю расписание, подожди...")
                        return
                    draft = admin_ocr_summary_drafts.get(peer_id)
                    if draft is None:
                        peer_modes[peer_id] = "admin_ocr_summary_input"
                        await show_screen(
                            peer_id,
                            "Данные распознавания устарели. Пришли фото заново.",
                            keyboard=make_keyboard([["Отменить"]]),
                        )
                        return
                    admin_ocr_apply_locks.add(peer_id)
                    try:
                        applied, report = await ocr_service.apply_summary(draft, notify=text == "Подтвердить и разослать")
                    finally:
                        admin_ocr_apply_locks.discard(peer_id)
                    if applied:
                        admin_ocr_summary_drafts.pop(peer_id, None)
                        admin_ocr_summary_images.pop(peer_id, None)
                        peer_modes[peer_id] = "admin_menu"
                        await show_screen(
                            peer_id,
                            f"Сводное расписание импортировано.\n\n{report}",
                            keyboard=admin_keyboard(),
                        )
                        return
                    await show_screen(
                        peer_id,
                        f"Импорт не выполнен.\n\n{report}",
                        keyboard=make_keyboard([["Отменить"]]),
                    )
                    return

                if text == "Добавить ещё фото":
                    queued = len(admin_ocr_summary_images.get(peer_id, []))
                    await show_screen(
                        peer_id,
                        format_vk_ocr_summary_add_more_prompt(queued),
                        keyboard=make_keyboard([["Отменить"]]),
                    )
                    return

            previous_images = admin_ocr_summary_images.get(peer_id, [])
            has_existing_draft = peer_id in admin_ocr_summary_drafts
            retry_keyboard = (
                make_keyboard([["Подтвердить и разослать"], ["Сохранить без рассылки"], ["Добавить ещё фото"], ["Отменить"]])
                if has_existing_draft
                else make_keyboard([["Отменить"]])
            )

            new_images, download_error = await download_vk_images(message)
            if new_images is None:
                await show_screen(
                    peer_id,
                    format_vk_ocr_summary_prompt(download_error),
                    keyboard=retry_keyboard,
                )
                return

            if len(previous_images) + len(new_images) > MAX_OCR_IMAGES:
                await show_screen(
                    peer_id,
                    format_vk_ocr_summary_prompt(
                        f"Слишком много фото (уже загружено {len(previous_images)}, максимум {MAX_OCR_IMAGES} всего). "
                        "Подтверди текущий черновик или отмени и начни заново."
                        if previous_images
                        else f"Слишком много фото за раз (максимум {MAX_OCR_IMAGES}). Пришли частями."
                    ),
                    keyboard=retry_keyboard,
                )
                return

            images = previous_images + new_images
            admin_ocr_summary_images[peer_id] = images

            upload_label = OCR_STAGE_UPLOAD if len(images) == 1 else f"{OCR_STAGE_UPLOAD} ({len(images)} фото)"
            ocr_progress = ProgressMessage(peer_id, min_interval=2.0)
            await ocr_progress.update(format_progress_bar(upload_label, 10), force=True)

            async def report_progress(stage: str, percent: int) -> None:
                await ocr_progress.update(format_progress_bar(stage, percent), force=percent >= 100)

            try:
                draft = await asyncio.wait_for(
                    ocr_service.build_summary_draft(images, progress=report_progress),
                    timeout=ocr_service.recognize_timeout,
                )
            except TimeoutError:
                logger.warning("Распознавание сводного фото не уложилось в %s с (VK).", ocr_service.recognize_timeout)
                await show_screen(
                    peer_id,
                    format_vk_ocr_summary_prompt(
                        f"Распознавание не уложилось в {ocr_service.recognize_timeout:.0f} с и было прервано. "
                        "Пришли фото поменьше или увеличь OCR_TIMEOUT_SECONDS."
                    ),
                    keyboard=retry_keyboard,
                )
                return
            except OcrEngineError as exc:
                await show_screen(
                    peer_id,
                    format_vk_ocr_summary_prompt(f"Не удалось распознать фото: {exc}"),
                    keyboard=retry_keyboard,
                )
                return
            except Exception as exc:
                logger.exception("Ошибка распознавания сводного расписания с фото (VK).")
                await show_screen(
                    peer_id,
                    format_vk_ocr_summary_prompt(f"Внутренняя ошибка: {type(exc).__name__}: {exc}"),
                    keyboard=retry_keyboard,
                )
                return

            admin_ocr_summary_drafts[peer_id] = draft
            peer_modes[peer_id] = "admin_ocr_summary_preview"
            keyboard = (
                make_keyboard([["Подтвердить и разослать"], ["Сохранить без рассылки"], ["Добавить ещё фото"], ["Отменить"]])
                if draft.can_apply
                else make_keyboard([["Отменить"]])
            )
            await show_screen(
                peer_id,
                format_ocr_summary_preview(draft, html=False, max_length=VK_MESSAGE_LIMIT),
                keyboard=keyboard,
            )
            return

        if user_is_admin(user_id) and mode in {"admin_ocr_json_input", "admin_ocr_json_preview"}:
            if text == "Отменить":
                admin_ocr_summary_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
                return

            if mode == "admin_ocr_json_preview" and text in {"Подтвердить и разослать", "Сохранить без рассылки"}:
                if peer_id in admin_ocr_apply_locks:
                    await show_screen(peer_id, "Уже сохраняю расписание, подожди...")
                    return
                draft = admin_ocr_summary_drafts.get(peer_id)
                if draft is None:
                    peer_modes[peer_id] = "admin_ocr_json_input"
                    await show_screen(
                        peer_id,
                        "Данные распознавания устарели. Пришли JSON заново.",
                        keyboard=make_keyboard([["Отменить"]]),
                    )
                    return
                admin_ocr_apply_locks.add(peer_id)
                try:
                    applied, report = await ocr_service.apply_summary(draft, notify=text == "Подтвердить и разослать")
                finally:
                    admin_ocr_apply_locks.discard(peer_id)
                if applied:
                    admin_ocr_summary_drafts.pop(peer_id, None)
                    peer_modes[peer_id] = "admin_menu"
                    await show_screen(
                        peer_id,
                        f"Сводное расписание импортировано.\n\n{report}",
                        keyboard=admin_keyboard(),
                    )
                    return
                await show_screen(
                    peer_id,
                    f"Импорт не выполнен.\n\n{report}",
                    keyboard=make_keyboard([["Отменить"]]),
                )
                return

            # Не зависит от Gemini: JSON уже распознан вручную другой нейросетью,
            # весь смысл этого режима — работать даже когда OCR полностью недоступен.
            raw_data = ""
            if message.attachments:
                doc = next((att.doc for att in message.attachments if att.doc), None)
                if doc and doc.url:
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            resp = await client.get(doc.url)
                            raw_data = resp.text
                    except Exception as exc:
                        await show_screen(
                            peer_id,
                            format_vk_ocr_json_prompt(f"Не удалось прочитать документ: {exc}\nПришли JSON-текст сообщением."),
                            keyboard=make_keyboard([["Отменить"]]),
                        )
                        return
            if not raw_data and text:
                raw_data = text

            if not raw_data:
                await show_screen(
                    peer_id,
                    format_vk_ocr_json_prompt("Пришли JSON-файл или JSON-текст сообщением."),
                    keyboard=make_keyboard([["Отменить"]]),
                )
                return

            try:
                draft = await ocr_service.build_summary_draft_from_json(raw_data)
            except OcrEngineError as exc:
                await show_screen(
                    peer_id,
                    format_vk_ocr_json_prompt(f"Не удалось разобрать: {exc}"),
                    keyboard=make_keyboard([["Отменить"]]),
                )
                return
            except Exception as exc:
                logger.exception("Импорт готового JSON расписания упал (VK).")
                await show_screen(
                    peer_id,
                    format_vk_ocr_json_prompt(f"Внутренняя ошибка: {type(exc).__name__}: {exc}"),
                    keyboard=make_keyboard([["Отменить"]]),
                )
                return

            admin_ocr_summary_drafts[peer_id] = draft
            peer_modes[peer_id] = "admin_ocr_json_preview"
            keyboard = (
                make_keyboard([["Подтвердить и разослать"], ["Сохранить без рассылки"], ["Отменить"]])
                if draft.can_apply
                else make_keyboard([["Отменить"]])
            )
            await show_screen(
                peer_id,
                format_ocr_summary_preview(draft, html=False, max_length=VK_MESSAGE_LIMIT),
                keyboard=keyboard,
            )
            return

        if user_is_admin(user_id) and mode in {"admin_import_lessons_input", "admin_import_lessons_preview"}:
            if text == "Отменить":
                admin_import_lessons_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
                return

            if mode == "admin_import_lessons_input":
                raw_data = ""
                if message.attachments:
                    doc = next((att.doc for att in message.attachments if att.doc), None)
                    if doc and doc.url:
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                resp = await client.get(doc.url)
                                raw_data = resp.text
                        except Exception as exc:
                            await show_screen(peer_id, f"Не удалось прочитать документ: {exc}\nПришли JSON-текст сообщением.", keyboard=make_keyboard([["Отменить"]]))
                            return
                if not raw_data and text:
                    raw_data = text

                if not raw_data:
                    await show_screen(peer_id, "Отправь JSON-файл или пришли JSON-текст сообщением.", keyboard=make_keyboard([["Отменить"]]))
                    return

                parsed_data, error_msg = parse_imported_json_payload(raw_data)
                if error_msg or not parsed_data:
                    await show_screen(peer_id, f"Ошибка обработки JSON:\n{error_msg}\n\nПроверь формат и отправь повторно.", keyboard=make_keyboard([["Отменить"]]))
                    return

                admin_import_lessons_drafts[peer_id] = parsed_data
                peer_modes[peer_id] = "admin_import_lessons_preview"

                active_catalog = search_catalog or group_catalog
                preview_text, _, _ = await format_import_preview(parsed_data, active_catalog, html=False)

                await show_screen(peer_id, preview_text, keyboard=make_keyboard([["Подтвердить импорт"], ["Отменить"]]))
                return

            if mode == "admin_import_lessons_preview":
                parsed_data = admin_import_lessons_drafts.get(peer_id)
                if text == "Подтвердить импорт":
                    if not parsed_data:
                        peer_modes[peer_id] = "admin_import_lessons_input"
                        await show_screen(peer_id, "Данные импорта устарели. Отправь JSON заново.", keyboard=make_keyboard([["Отменить"]]))
                        return

                    active_catalog = search_catalog or group_catalog
                    current_payload = load_lesson_config(settings.lesson_counters_path)
                    updated_payload, total_groups, total_subjects = await apply_imported_lessons_config(
                        parsed_data, current_payload, active_catalog
                    )
                    save_lesson_config(settings.lesson_counters_path, updated_payload)

                    lesson_counter_config = await lesson_counter_service.load_config_file(settings.lesson_counters_path, active_catalog)
                    await lesson_counter_service.sync_config(lesson_counter_config)

                    admin_import_lessons_drafts.pop(peer_id, None)
                    peer_modes[peer_id] = "admin_menu"

                    msg = f"Импорт пар успешно завершен!\n\nИмпортировано/обновлено: {total_groups} групп, {total_subjects} пар."
                    await show_screen(peer_id, msg, keyboard=admin_keyboard())
                    return

        if user_is_admin(user_id) and mode == "admin_lesson_add":
            if text == "Отменить":
                admin_lesson_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_menu"
                await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
                return
            draft = admin_lesson_drafts.get(peer_id, {"step": "group"})
            step = str(draft.get("step") or "group")
            if step == "group":
                active_catalog = group_catalog or GroupCatalog(settings.schedule_url, db=db)
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

                active_catalog = group_catalog or GroupCatalog(settings.schedule_url, db=db)
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
                await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
                return
            draft = admin_lesson_delete_drafts.get(peer_id, {"step": "group"})
            step = str(draft.get("step") or "group")
            if step == "group":
                active_catalog = group_catalog or GroupCatalog(settings.schedule_url, db=db)
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
                await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
                return
            draft = admin_lesson_delete_one_drafts.get(peer_id, {"step": "group"})
            step = str(draft.get("step") or "group")
            if step == "group":
                active_catalog = group_catalog or GroupCatalog(settings.schedule_url, db=db)
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

        if is_group_setup_command(text):
            if peer_id < VK_CHAT_PEER_ID_THRESHOLD:
                msg_text = (
                    "Настройка бота в беседах ВКонтакте\n\n"
                    "Чтобы получать расписание и уведомления об изменениях в вашей беседe:\n"
                    "1. Добавьте бота в беседу из сообщества.\n"
                    "2. Дайте боту доступ к переписке (или сделайте администратором беседы).\n"
                    "3. В беседе отправьте команду /startgroup или фразы Настройка группы / Группа.\n"
                    "4. Укажите название вашей учебной группы (например: ИСП-25-1 или МТО-25)."
                )
                await show_screen(peer_id, msg_text)
                return

            if not await user_can_manage_group(peer_id, user_id):
                await show_screen(peer_id, "Настройка беседы доступна только администраторам беседы или пользователю, добавившему бота.")
                return

            peer_modes[peer_id] = "awaiting_group_selection"
            await show_screen(
                peer_id,
                "Быстрая настройка беседы\n\nПришлите название вашей учебной группы одним сообщением (например: ИСП-25-1 или МТО-25):"
            )
            return

        user = await db.get_user("vk", user_id)
        if user is None or not user.subscription_key or not user.subscription_title:
            if user_is_admin(user_id) and (
                text in {"/admin", "Админка", VK_ADMIN_SECTION_BACK} or text in VK_ADMIN_SECTIONS or mode.startswith("admin")
            ):
                pass
            elif text.startswith("/") or text in {"Дополнительно", "Настройки", "Расписание"}:
                await prompt_group_selection(peer_id)
                return
            else:
                await handle_subscription_input(peer_id, user_id, text)
                return

        if text in {"Назад в меню", "Закрыть админку"}:
            search_results.pop(peer_id, None)
            admin_user_search_state.pop(peer_id, None)
            peer_pages[peer_id].pop("admin_user_search", None)
            admin_broadcast_drafts.pop(peer_id, None)
            admin_lesson_drafts.pop(peer_id, None)
            admin_lesson_delete_drafts.pop(peer_id, None)
            admin_lesson_delete_one_drafts.pop(peer_id, None)
            admin_import_lessons_drafts.pop(peer_id, None)
            admin_ocr_drafts.pop(peer_id, None)
            admin_ocr_summary_drafts.pop(peer_id, None)
            admin_ocr_summary_images.pop(peer_id, None)
            await show_main_menu(peer_id, user_id)
            return

        if text in {"Дополнительно", "Настройки"}:
            await show_settings(peer_id, user_id)
            return

        if text in {"Помощь", "Помощь / Инструкция"}:
            await show_screen(peer_id, "Справочное руководство (Wiki)\n\nВыберите раздел документации для получения подробной информации:", keyboard=vk_help_main_keyboard())
            return

        if text in {"1. Настройка групп и бесед", "1. Настройка группы"}:
            await show_screen(peer_id, vk_help_group_setup_text(), keyboard=vk_help_main_keyboard())
            return

        if text in {"2. Поиск и подписки", "2. Личные подписки"}:
            await show_screen(peer_id, vk_help_personal_setup_text(), keyboard=vk_help_main_keyboard())
            return

        if text == "3. Уведомления и расписание":
            await show_screen(peer_id, vk_help_notifications_text(), keyboard=vk_help_main_keyboard())
            return

        if text == "4. Персонализация":
            await show_screen(peer_id, vk_help_personalization_text(), keyboard=vk_help_main_keyboard())
            return

        if text == "5. Список команд":
            await show_screen(peer_id, vk_help_commands_text(), keyboard=vk_help_main_keyboard())
            return

        if mode == "awaiting_custom_sticker":
            if text == "Отменить":
                peer_modes[peer_id] = "personalization_menu"
                await show_pers_screen(peer_id, await format_vk_personalization_text(peer_id), keyboard=await vk_personalization_keyboard(peer_id))
                return
            sticker_id = None
            if message.attachments:
                for att in message.attachments:
                    if getattr(att, "sticker", None) and getattr(att.sticker, "sticker_id", None):
                        sticker_id = att.sticker.sticker_id
                        break
            if sticker_id:
                await db.set_user_custom_sticker("vk", user_id, str(sticker_id))
                peer_modes[peer_id] = "personalization_menu"
                text_out = "Стикер установлен.\n\n" + await format_vk_personalization_text(peer_id)
                await show_pers_screen(peer_id, text_out, keyboard=await vk_personalization_keyboard(peer_id))
                return
            await show_screen(peer_id, "Пришли стикер. Если передумал, нажми Отменить.", keyboard=make_keyboard([["Отменить"]]))
            return

        if text in {"Персонализация", "Персонализация уведомлений"}:
            peer_modes[peer_id] = "personalization_menu"
            await show_pers_screen(peer_id, await format_vk_personalization_text(peer_id), keyboard=await vk_personalization_keyboard(peer_id))
            return

        if text == "Установить стикер":
            peer_modes[peer_id] = "awaiting_custom_sticker"
            await show_screen(peer_id, "Пришли стикер, который будет высылаться перед уведомлениями и при вызове меню расписания:", keyboard=make_keyboard([["Отменить"]]))
            return

        if text == "Предпросмотр стикера":
            user = await db.get_user("vk", user_id)
            if user and user.custom_sticker_file_id:
                try:
                    await vk_send_message(bot.api, peer_id, sticker_id=int(user.custom_sticker_file_id), max_attempts=2)
                except Exception as exc:
                    logger.warning("Failed to preview VK sticker for %s: %s", user_id, exc)

                old_msg_id = last_pers_menu_message_id.get(peer_id)
                if old_msg_id:
                    try:
                        await bot.api.messages.delete(message_ids=[old_msg_id], delete_for_all=True)
                    except Exception as del_exc:
                        logger.warning("Failed to delete previous VK pers menu message for %s: %s", peer_id, del_exc)
                await show_pers_screen(peer_id, await format_vk_personalization_text(peer_id), keyboard=await vk_personalization_keyboard(peer_id))
            return

        if text == "Сбросить стикер":
            await db.clear_user_custom_sticker("vk", user_id)
            text_out = "Стикер сброшен.\n\n" + await format_vk_personalization_text(peer_id)
            await show_pers_screen(peer_id, text_out, keyboard=await vk_personalization_keyboard(peer_id))
            return

        if text in {"Назад к настройкам", "Назад в меню"} and mode in {"personalization_menu", "awaiting_custom_sticker"}:
            peer_modes.pop(peer_id, None)
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
            user = await db.get_user("vk", user_id)
            if user and user.custom_sticker_file_id:
                try:
                    await vk_send_message(bot.api, peer_id, sticker_id=int(user.custom_sticker_file_id), max_attempts=2)
                except Exception as exc:
                    logger.warning("Failed to send VK sticker on schedule menu for %s: %s", user_id, exc)
            peer_modes[peer_id] = "schedule_menu"
            await show_screen(peer_id, "Выбери нужный вариант расписания.", keyboard=schedule_keyboard())
            return

        if text == "Расписание звонков":
            await show_bells_schedule(peer_id)
            return

        if text in {"Расписание на сегодня", "Расписание на завтра", "Расписание на 2 дня"}:
            # Кнопки клавиатуры расписания раньше работали только при mode ==
            # "schedule_menu", а peer_modes живёт лишь в памяти процесса — после
            # перезапуска бота старая клавиатура у пользователя переставала
            # работать (нажатие тихо уходило в главное меню без единой ошибки).
            snapshot = await get_or_fetch_subscription_snapshot(user_id)
            if snapshot is None:
                await show_screen(peer_id, "Не удалось получить расписание для твоей группы.", keyboard=schedule_keyboard())
                return
            offset, label = {
                "Расписание на сегодня": (0, "сегодня"),
                "Расписание на завтра": (1, "завтра"),
                "Расписание на 2 дня": (2, "2 дня"),
            }[text]
            peer_modes[peer_id] = "schedule_menu"
            await show_screen(
                peer_id,
                schedule_text(get_day_by_offset_from_content(snapshot["content"], offset), label),
                keyboard=schedule_keyboard(),
            )
            return

        if text == "Найти расписание":
            peer_modes[peer_id] = "schedule_search"
            await show_screen(peer_id, schedule_search_prompt_text(), keyboard=search_prompt_keyboard())
            return

        # Вход в админку и навигация по ней — раньше режимов свободного ввода:
        # иначе в режиме поиска нажатие «Админка» уходило в поиск расписания.
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
            await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
            return

        if user_is_admin(user_id) and text == VK_ADMIN_SECTION_BACK:
            admin_broadcast_drafts.pop(peer_id, None)
            admin_lesson_drafts.pop(peer_id, None)
            admin_lesson_delete_drafts.pop(peer_id, None)
            admin_lesson_delete_one_drafts.pop(peer_id, None)
            admin_user_search_state.pop(peer_id, None)
            peer_modes[peer_id] = "admin_menu"
            await show_screen(peer_id, "Админ-панель\n\nВыбери раздел.", keyboard=admin_keyboard())
            return

        if user_is_admin(user_id) and text in VK_ADMIN_SECTIONS:
            section = VK_ADMIN_SECTIONS[text]
            peer_modes[peer_id] = "admin_menu"
            await show_screen(peer_id, section["text"], keyboard=make_keyboard(section["rows"]))
            return

        if mode == "audience_select":
            await handle_audience_input(peer_id, user_id, text)
            return

        if mode == "schedule_search":
            await perform_schedule_search(peer_id, text)
            return

        if mode == "schedule_search_result":
            snapshot = search_results.get(peer_id)
            if snapshot is None:
                peer_modes[peer_id] = "schedule_search"
                await show_screen(peer_id, schedule_search_prompt_text("Сначала найди расписание."), keyboard=search_prompt_keyboard())
                return
            if text not in {"Найти расписание", "Назад в меню"}:
                await show_screen(
                    peer_id,
                    "Поиск отдает расписание сразу по всем дням. Нажми «Найти расписание», чтобы ввести новый запрос.",
                    keyboard=search_result_keyboard(),
                )
                return


        if user_is_admin(user_id):

            if text == "Ручной подсчёт":
                peer_modes[peer_id] = "admin_menu"
                if not settings.lesson_counters_enabled:
                    await show_screen(peer_id, "Счётчики пар выключены в настройках.", keyboard=admin_keyboard())
                    return
                await show_screen(
                    peer_id, VK_ADMIN_COUNTER_SYNC_TEXT, keyboard=make_keyboard(VK_ADMIN_COUNTER_SYNC_ROWS)
                )
                return

            if text in {"Подсчёт за сегодня", "Подсчёт за вчера"}:
                peer_modes[peer_id] = "admin_menu"
                if not settings.lesson_counters_enabled:
                    await show_screen(peer_id, "Счётчики пар выключены в настройках.", keyboard=admin_keyboard())
                    return
                if admin_counter_sync_lock.locked():
                    await show_screen(peer_id, "Подсчёт уже идёт, подожди отчёта.", keyboard=make_keyboard(VK_ADMIN_COUNTER_SYNC_ROWS))
                    return
                target_date = datetime.now().date()
                if text.endswith("вчера"):
                    target_date -= timedelta(days=1)
                target_date_iso = target_date.isoformat()
                counters_section_keyboard = make_keyboard(VK_ADMIN_SECTIONS["Счётчики пар"]["rows"])

                async def run_counter_sync() -> None:
                    async with admin_counter_sync_lock:
                        try:
                            sync_result = await sync_lesson_counters_for_date(db, parser, lesson_counter_service, target_date_iso)
                            report_text = format_counter_sync_report(sync_result, target_date_iso)
                        except Exception:
                            logger.exception("Ручной подсчёт пар за %s не удался", target_date_iso)
                            report_text = "Не удалось выполнить подсчёт. Подробности уже отправлены администратору."
                        await show_screen(peer_id, report_text, keyboard=counters_section_keyboard)

                await show_screen(
                    peer_id,
                    "Считаю пары...\n\nОпрашиваю сайт по всем группам. Если он тормозит, это может занять несколько минут. "
                    "Отчёт пришлю отдельным сообщением, админкой можно пользоваться.",
                    keyboard=counters_section_keyboard,
                )
                spawn_admin_job(peer_id, "подсчёт пар", run_counter_sync())
                return

            if text in {"/cleandb", "cleandb", "Очистить БД", "Очистить бд"} or text.startswith("/cleandb"):
                await show_screen(
                    peer_id,
                    "Запущена принудительная очистка базы данных через RabbitMQ...\n\n"
                    "После завершения очистки служебный отчёт будет выслан администраторам.",
                    keyboard=admin_back_keyboard(),
                )
                if schedule_jobs is not None:
                    await schedule_jobs.enqueue_or_run_db_cleanup()
                return

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
            if text == "Расписание с фото":
                available, availability_message = ocr_service.availability()
                if not available:
                    await show_screen(peer_id, availability_message, keyboard=admin_keyboard())
                    return
                admin_ocr_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_ocr_input"
                await show_screen(peer_id, format_vk_ocr_prompt(), keyboard=make_keyboard([["Отменить"]]))
                return
            if text == "Сводное расписание":
                available, availability_message = ocr_service.availability()
                if not available:
                    await show_screen(peer_id, availability_message, keyboard=admin_keyboard())
                    return
                admin_ocr_summary_drafts.pop(peer_id, None)
                admin_ocr_summary_images.pop(peer_id, None)
                peer_modes[peer_id] = "admin_ocr_summary_input"
                await show_screen(peer_id, format_vk_ocr_summary_prompt(), keyboard=make_keyboard([["Отменить"]]))
                return
            if text == "Импорт OCR JSON":
                # В отличие от фото-режимов, не проверяем ocr_service.availability(): весь
                # смысл этого режима — работать даже когда Gemini недоступен или забанен.
                admin_ocr_summary_drafts.pop(peer_id, None)
                admin_ocr_summary_images.pop(peer_id, None)
                peer_modes[peer_id] = "admin_ocr_json_input"
                await show_screen(peer_id, format_vk_ocr_json_prompt(), keyboard=make_keyboard([["Отменить"]]))
                return
            if text == "Импорт пар из JSON":
                admin_import_lessons_drafts.pop(peer_id, None)
                peer_modes[peer_id] = "admin_import_lessons_input"
                prompt_text = (
                    "Импорт пар из JSON\n\n"
                    "Отправь JSON-файл документа или пришли JSON-текст сообщением.\n\n"
                    "Пример формата:\n"
                    "{\n"
                    '  "groups": [\n'
                    '    {\n'
                    '      "group_name": "ИСП-25-1",\n'
                    '      "subjects": [\n'
                    '        {"subject": "Литература", "teacher": "Волошина Н. В.", "passed": 10, "total": 62}\n'
                    "      ]\n"
                    "    }\n"
                    "  ]\n"
                    "}"
                )
                await show_screen(peer_id, prompt_text, keyboard=make_keyboard([["Отменить"]]))
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
            if text in {"Статус", "Обновить статус"}:
                peer_modes[peer_id] = "admin_status"
                await show_screen(peer_id, await admin_status_text(), keyboard=admin_status_keyboard())
                return
            if text == "Ошибки за день":
                peer_modes[peer_id] = "admin_daily_errors"
                await show_screen(peer_id, await format_daily_errors_report(db, html=False), keyboard=admin_daily_errors_keyboard())
                return
            if text == "Управление Gemini" or (text == "Обновить" and mode == "admin_gemini_status"):
                peer_modes[peer_id] = "admin_gemini_status"
                await show_screen(peer_id, format_admin_gemini_status(ocr_service, html=False), keyboard=admin_gemini_keyboard())
                return
            if text in {"Перепарсить", "Сохранить эталон"}:
                is_refresh = text == "Перепарсить"
                job_name = "перепарсинг" if is_refresh else "сохранение эталона"

                async def run_sources_job() -> None:
                    rows = await (refresh_all_active_sources() if is_refresh else save_baseline_for_all_active_sources())
                    if not rows:
                        await show_screen(peer_id, "Нет активных групп для этой операции.", keyboard=admin_back_keyboard())
                        return
                    title = "Перепарсинг активных групп" if is_refresh else "Эталоны для активных групп"
                    await show_screen(peer_id, format_group_action_report(title, rows), keyboard=admin_back_keyboard())

                if not spawn_admin_job(peer_id, job_name, run_sources_job()):
                    await show_screen(peer_id, f"Операция «{job_name}» уже идёт — дождись отчёта.", keyboard=admin_back_keyboard())
                    return
                await show_screen(
                    peer_id,
                    ("Перепарсинг запущен..." if is_refresh else "Сохранение эталонов запущено...")
                    + "\n\nПарсю активные источники, это может занять несколько минут. Отчёт пришлю отдельным сообщением.",
                    keyboard=admin_back_keyboard(),
                )
                return
            if text == "Последнее изменение":
                today_prefix = datetime.now().date().isoformat()
                daily_changes = await db.get_daily_change_groups(today_prefix)
                response = format_daily_change_report("Последние изменения за сегодня", daily_changes)
                await show_screen(peer_id, response, keyboard=admin_back_keyboard())
                return
            if text == "Пользователи":
                admin_user_search_state.pop(peer_id, None)
                peer_pages[peer_id].pop("admin_user_search", None)
                peer_modes[peer_id] = "admin_users"
                await show_admin_users(peer_id, 0)
                return
            if text == "Поиск пользователя":
                admin_user_search_state.pop(peer_id, None)
                peer_pages[peer_id].pop("admin_user_search", None)
                peer_modes[peer_id] = "admin_user_search"
                await show_screen(
                    peer_id,
                    "Поиск пользователя\n\nНапиши запрос одним сообщением. Поддерживается поиск по айди, @username, имени, фамилии и названию группы.",
                    keyboard=make_keyboard([["Все пользователи"], [VK_ADMIN_SECTION_BACK]]),
                )
                return
            if text == "Информация по группам":
                await show_screen(peer_id, format_group_user_stats(await db.get_group_user_stats()), keyboard=admin_back_keyboard())
                return
            if text == "Тестовая рассылка":
                if broadcaster is None:
                    await show_screen(peer_id, "Сервис рассылки сейчас недоступен.", keyboard=admin_back_keyboard())
                    return

                async def run_test_broadcast() -> None:
                    progress = await broadcaster.broadcast_test_message()
                    summary = (
                        f"Тестовая рассылка завершена.\n\nУспешно: {progress.success_count}, ошибок: {progress.failed_count}."
                        if progress is not None
                        else "Тестовая рассылка завершена."
                    )
                    await show_screen(peer_id, summary, keyboard=admin_back_keyboard())

                if not spawn_admin_job(peer_id, "рассылка", run_test_broadcast()):
                    await show_screen(peer_id, "Другая рассылка ещё идёт — дождись её отчёта.", keyboard=admin_back_keyboard())
                    return
                await show_screen(peer_id, "Тестовая рассылка запущена. Отчёт пришлю, когда закончится.", keyboard=admin_back_keyboard())
                return
            if mode == "admin_users":
                if text == "Следующая страница":
                    await show_admin_users(peer_id, peer_pages[peer_id].get("admin_users", 0) + 1)
                    return
                if text == "Предыдущая страница":
                    await show_admin_users(peer_id, peer_pages[peer_id].get("admin_users", 0) - 1)
                    return
                if text == "Поиск пользователя":
                    admin_user_search_state.pop(peer_id, None)
                    peer_pages[peer_id].pop("admin_user_search", None)
                    peer_modes[peer_id] = "admin_user_search"
                    await show_screen(
                        peer_id,
                        "Поиск пользователя\n\nНапиши запрос одним сообщением. Поддерживается поиск по айди, @username, имени, фамилии и названию группы.",
                        keyboard=make_keyboard([["Все пользователи"], ["Назад в админку"]]),
                    )
                    return
            if mode == "admin_user_search_results":
                if text == "Следующая страница" and await show_admin_user_search_results(peer_id, peer_pages[peer_id].get("admin_user_search", 0) + 1):
                    return
                if text == "Предыдущая страница" and await show_admin_user_search_results(peer_id, peer_pages[peer_id].get("admin_user_search", 0) - 1):
                    return
                if text == "Искать снова":
                    peer_modes[peer_id] = "admin_user_search"
                    await show_screen(
                        peer_id,
                        "Поиск пользователя\n\nНапиши запрос одним сообщением. Поддерживается поиск по айди, @username, имени, фамилии и названию группы.",
                        keyboard=make_keyboard([["Все пользователи"], ["Назад в админку"]]),
                    )
                    return
                if text == "Все пользователи":
                    admin_user_search_state.pop(peer_id, None)
                    peer_pages[peer_id].pop("admin_user_search", None)
                    await show_admin_users(peer_id, 0)
                    return
            if mode == "admin_user_search":
                if text == "Искать снова":
                    admin_user_search_state.pop(peer_id, None)
                    peer_pages[peer_id].pop("admin_user_search", None)
                    await show_screen(
                        peer_id,
                        "Поиск пользователя\n\nНапиши запрос одним сообщением. Поддерживается поиск по айди, @username, имени, фамилии и названию группы.",
                        keyboard=make_keyboard([["Все пользователи"], ["Назад в админку"]]),
                    )
                    return
                if text == "Все пользователи":
                    admin_user_search_state.pop(peer_id, None)
                    peer_pages[peer_id].pop("admin_user_search", None)
                    await show_admin_users(peer_id, 0)
                    return
                users = await db.list_users()
                await sync_vk_user_names([user.user_id for user in users if user.platform == "vk"])
                users = await db.list_users()
                matches = filter_admin_users(users, text)
                peer_modes[peer_id] = "admin_user_search"
                admin_user_search_state.pop(peer_id, None)
                peer_pages[peer_id].pop("admin_user_search", None)
                if not matches:
                    await show_screen(
                        peer_id,
                        f"Поиск пользователя\n\nПо запросу «{text}» ничего не найдено.",
                        keyboard=make_keyboard([["Искать снова"], ["Все пользователи"], ["Назад в админку"]]),
                    )
                    return
                admin_user_search_state[peer_id] = {"query": text}
                await show_admin_user_search_results(peer_id, 0)
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

        await show_main_menu(peer_id, user_id)

    return bot


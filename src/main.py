from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable
from html import escape
from pathlib import Path
from time import monotonic, time

import aio_pika
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.methods import GetUpdates

from src.config import Settings
from src.db import Database
from src.db_migrations import apply_migrations
from src.error_reporting import AdminErrorReporter, install_error_reporting
from src.group_catalog import GroupCatalog
from src.lesson_counters import LessonCounterService
from src.message_broker import (
    AutoDailyLessonCounterJobBroker,
    DatabaseCleanupJobBroker,
    RabbitMQBroker,
)
from src.notifier import Broadcaster
from src.ocr_import import OcrScheduleImporter, build_ocr_importer
from src.parser import ScheduleParser
from src.schedule_search import ScheduleSearchCatalog
from src.scheduler import ScheduleJobs
from src.system_status import SystemAlertManager
from src.telegram_bot import build_dispatcher
from src.vk_bot import build_vk_bot, vk_handler_timeout
from src.vk_runtime import VkUpdateDispatcher

# Если long poll VK / getUpdates Telegram не отвечали дольше этого, мониторинг считает приём сообщений упавшим.
VK_POLLING_STALE_SECONDS = 300.0
TELEGRAM_POLLING_STALE_SECONDS = 300.0

_background_tasks: set[asyncio.Task] = set()

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def restore_logging() -> None:
    """Возвращает наше логирование после alembic.

    `migrations/env.py` вызывает `fileConfig()`, а тот по умолчанию отключает
    все уже созданные логгеры и ставит корневому уровень WARN из alembic.ini.
    В результате бот работал молча: в логах оставались только строки alembic,
    и понять, что происходит с распознаванием, было невозможно.
    """
    for logger_name in ("src", "__main__", ""):
        logging.getLogger(logger_name).disabled = False
    # `fileConfig()` помечает disabled каждый уже созданный логгер отдельно —
    # не только наши `src.*`, но и aiogram, aio_pika, asyncio. Без этого ошибки
    # поллинга Telegram и «Task exception was never retrieved» молча пропадали
    # и не доходили ни до лога, ни до админа.
    for logger_object in logging.root.manager.loggerDict.values():
        if isinstance(logger_object, logging.Logger):
            logger_object.disabled = False
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)


def log_memory(stage: str) -> None:
    """Печатает потребление памяти процессом — чтобы видеть подходы к OOM."""
    try:
        with open("/proc/self/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    logging.info("Память процесса (%s): %s", stage, line.split(":", 1)[1].strip())
                    return
    except OSError:
        return


def start_background_task(name: str, coro) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    # Строгая ссылка: asyncio держит на задачу только слабую, и без неё фоновая
    # задача может быть собрана сборщиком мусора посреди работы.
    _background_tasks.add(task)

    def _log_result(done_task: asyncio.Task) -> None:
        _background_tasks.discard(done_task)
        try:
            done_task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logging.exception("%s stopped unexpectedly.", name)

    task.add_done_callback(_log_result)
    return task


class TrackingAiohttpSession(AiohttpSession):
    """Сессия aiogram, которая помнит время последнего успешного getUpdates — для мониторинга."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started_at = monotonic()
        self.last_updates_ok_at: float | None = None

    async def make_request(self, bot, method, timeout=None):
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, GetUpdates):
            self.last_updates_ok_at = monotonic()
        return result


def telegram_polling_probe(session: TrackingAiohttpSession) -> Callable[[], tuple[bool, str]]:
    def probe() -> tuple[bool, str]:
        reference = session.last_updates_ok_at if session.last_updates_ok_at is not None else session.started_at
        age = monotonic() - reference
        return age < TELEGRAM_POLLING_STALE_SECONDS, f"последний успешный getUpdates {age:.0f} с назад"

    return probe


def build_telegram_bot(settings: Settings) -> Bot:
    session = TrackingAiohttpSession(proxy=settings.telegram_proxy or None)
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )


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
    ocr_importer: OcrScheduleImporter | None = None,
    error_reporter: AdminErrorReporter | None = None,
) -> Bot | None:
    if not settings.telegram_bot_token:
        logging.warning("TELEGRAM_BOT_TOKEN не задан. Telegram-бот не будет запущен.")
        return None

    # Бот и диспетчер создаются один раз. Раньше при каждом перезапуске поллинга
    # старая сессия закрывалась, а рассылка продолжала слать через неё
    # ("Connector is closed") — уведомления терялись.
    bot = build_telegram_bot(settings)
    broadcaster.telegram_bot = bot
    dispatcher = build_dispatcher(
        settings,
        db,
        parser,
        broadcaster,
        group_catalog,
        search_catalog,
        schedule_jobs,
        ocr_importer,
        error_reporter=error_reporter,
    )

    async def _run_once() -> None:
        # Сигналы обрабатывает main(): иначе aiogram перехватывал SIGTERM, останавливал
        # только поллинг, а супервизор тут же поднимал его снова.
        await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)

    start_background_task("telegram-supervisor", run_forever("Telegram bot", _run_once))
    return bot


def start_vk_polling(
    settings: Settings,
    db: Database,
    parser: ScheduleParser,
    broadcaster: Broadcaster,
    group_catalog: GroupCatalog,
    search_catalog: ScheduleSearchCatalog,
    schedule_jobs: ScheduleJobs | None = None,
    ocr_importer: OcrScheduleImporter | None = None,
    error_reporter: AdminErrorReporter | None = None,
) -> Callable[[], tuple[bool, str]] | None:
    """Поднимает VK-бота и возвращает проверку живости long poll для мониторинга."""
    if not settings.vk_bot_token:
        logging.warning("VK_BOT_TOKEN не задан. VK-бот не будет запущен.")
        return None

    vk_bot = build_vk_bot(
        settings,
        db,
        parser,
        broadcaster,
        group_catalog,
        search_catalog,
        schedule_jobs,
        ocr_importer,
        error_reporter=error_reporter,
    )
    if vk_bot is None:
        return None
    # Бот нужен рассылке сразу, ещё до первого события long poll: консьюмер
    # RabbitMQ стартует следом и может получить VK-сообщение немедленно.
    broadcaster.vk_bot = vk_bot
    polling = vk_bot.resilient_polling
    router = vk_bot.router
    dispatcher = VkUpdateDispatcher(
        lambda update: router.route(update, vk_bot.api),
        handler_timeout=vk_handler_timeout(settings),
        on_failure=vk_bot.report_update_failure,
    )

    async def _run_once() -> None:
        await dispatcher.run(polling)

    start_background_task("vk-supervisor", run_forever("VK bot", _run_once, restart_delay_seconds=5.0))

    def probe() -> tuple[bool, str]:
        reference = polling.last_ok_at if polling.last_ok_at is not None else polling.started_at
        age = monotonic() - reference
        details = (
            f"последний ответ long poll {age:.0f} с назад, ошибок подряд: {polling.consecutive_failures}, "
            f"в обработке событий: {dispatcher.pending_tasks}"
        )
        return age < VK_POLLING_STALE_SECONDS, details

    return probe


OCR_READY_HTML = (
    "<b>Распознавание расписания с фото готово</b>"
    "\n\nДвижок: <b>{engine}</b>"
    "\nМодели загружены за {elapsed} с."
    "\n\nМожно присылать фото через админ-панель."
)
OCR_READY_TEXT = (
    "Распознавание расписания с фото готово."
    "\n\nДвижок: {engine}"
    "\nМодели загружены за {elapsed} с."
    "\n\nМожно присылать фото через админ-панель."
)
OCR_FAILED_HTML = (
    "<b>Распознавание расписания с фото не поднялось</b>"
    "\n\n{reason}"
    "\n\nИмпорт фото работать не будет, остальные функции — как обычно."
)
OCR_FAILED_TEXT = (
    "Распознавание расписания с фото не поднялось."
    "\n\n{reason}"
    "\n\nИмпорт фото работать не будет, остальные функции — как обычно."
)
OCR_UNAVAILABLE_HTML = (
    "<b>Распознавание расписания с фото недоступно</b>"
    "\n\n{reason}"
    "\n\nОстальные функции бота работают как обычно."
)
OCR_UNAVAILABLE_TEXT = (
    "Распознавание расписания с фото недоступно."
    "\n\n{reason}"
    "\n\nОстальные функции бота работают как обычно."
)


def should_notify_ocr_ready(marker_path: Path, min_interval_hours: float) -> bool:
    """Не чаще одного сообщения о готовности за указанный срок.

    Контейнер может перезапускаться, и без этой отсечки каждый старт слал бы
    администраторам одно и то же уведомление — при крешлупе это превращается
    в непрерывный спам.
    """
    if min_interval_hours <= 0:
        return True
    now = time()
    try:
        last = marker_path.stat().st_mtime
        if now - last < min_interval_hours * 3600:
            return False
    except OSError:
        pass
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(str(now), encoding="utf-8")
    except OSError as exc:
        logging.warning("Не удалось записать отметку уведомления OCR: %s", exc)
    return True


async def warm_up_ocr_and_notify(
    ocr_importer,
    broadcaster: Broadcaster,
    engine_message: str,
    marker_path: Path,
    notice_interval_hours: float,
) -> None:
    """Греет модели распознавания и сообщает администраторам результат.

    Прогрев идёт десятки секунд и потому вынесен в фон. Админу важно знать, что
    фото уже можно присылать, и тем более важно узнать, если движок не поднялся.
    """
    started = monotonic()
    log_memory("до прогрева OCR")
    await ocr_importer.warm_up()
    elapsed = f"{monotonic() - started:.0f}"
    log_memory("после прогрева OCR")

    if ocr_importer.is_warm:
        logging.info("Распознавание с фото готово за %s с.", elapsed)
        if should_notify_ocr_ready(marker_path, notice_interval_hours):
            await broadcaster.notify_admins(
                OCR_READY_HTML.format(engine=escape(engine_message), elapsed=elapsed),
                OCR_READY_TEXT.format(engine=engine_message, elapsed=elapsed),
            )
        else:
            logging.info("Уведомление о готовности OCR пропущено: недавно уже отправляли.")
        return

    reason = ocr_importer.last_error or "причина неизвестна"
    logging.error("Распознавание с фото не поднялось: %s", reason, extra={"skip_admin_report": True})
    await broadcaster.notify_admins(
        OCR_FAILED_HTML.format(reason=escape(reason)),
        OCR_FAILED_TEXT.format(reason=reason),
    )


async def main() -> None:
    settings = Settings.from_env()
    if not settings.rabbitmq_url:
        logging.error("RABBITMQ_URL is not set. RabbitMQ consumers are disabled; direct delivery fallback remains available.")
    if not settings.telegram_bot_token and not settings.vk_bot_token:
        logging.error("Neither TELEGRAM_BOT_TOKEN nor VK_BOT_TOKEN is set. Bot polling is disabled, background jobs keep running.")

    apply_migrations(settings.database_path)
    restore_logging()
    # Любая ошибка в логе (ERROR и выше), упавшая фоновая задача или задача
    # планировщика уходит админу. Отправитель подключается чуть ниже, когда
    # появится Broadcaster; до этого отчёты просто не отправляются.
    error_reporter = AdminErrorReporter()
    install_error_reporting(error_reporter, asyncio.get_running_loop())
    logging.info("Миграции применены, логирование восстановлено.")
    log_memory("после старта")
    db = Database(settings.database_path)
    await db.initialize()

    group_catalog = GroupCatalog(settings.schedule_url, db=db)
    await group_catalog.ensure_loaded()
    search_catalog = ScheduleSearchCatalog(settings.schedule_url, group_catalog, db=db)
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
    error_reporter.set_notifier(broadcaster.notify_admins)
    alert_manager = SystemAlertManager(db=db, broadcaster=broadcaster)
    jobs = ScheduleJobs(
        db=db,
        parser=parser,
        broadcaster=broadcaster,
        timezone=settings.app_timezone,
        request_delay_seconds=settings.schedule_request_delay_seconds,
        request_jitter_seconds=settings.schedule_request_jitter_seconds,
        lesson_counters_enabled=settings.lesson_counters_enabled,
        lesson_counter_service=lesson_counter_service,
        db_cleanup_broker=db_cleanup_broker,
        auto_daily_lesson_counter_broker=auto_daily_lesson_counter_broker,
        admin_backup_enabled=bool(settings.admin_telegram_id),
        admin_backup_interval_days=2,
        admin_telegram_id=settings.admin_telegram_id,
        lesson_counters_path=settings.lesson_counters_path,
        database_path=settings.database_path,
        alert_manager=alert_manager,
        rabbitmq_url=settings.rabbitmq_url,
        group_catalog=group_catalog,
    )
    ocr_importer = build_ocr_importer(settings, db, jobs, group_catalog, alert_manager)

    # Боты поднимаются раньше всего остального: уведомления админу на старте
    # (например, «OCR недоступен») раньше уходили в пустоту — ботов ещё не было.
    telegram_bot = start_telegram_polling(
        settings, db, parser, broadcaster, group_catalog, search_catalog, jobs, ocr_importer, error_reporter=error_reporter
    )
    if telegram_bot is not None and isinstance(telegram_bot.session, TrackingAiohttpSession):
        jobs.liveness_probes["telegram_polling"] = telegram_polling_probe(telegram_bot.session)
    vk_probe = start_vk_polling(
        settings, db, parser, broadcaster, group_catalog, search_catalog, jobs, ocr_importer, error_reporter=error_reporter
    )
    if vk_probe is not None:
        jobs.liveness_probes["vk_polling"] = vk_probe
    jobs.start()

    ocr_available, ocr_message = ocr_importer.availability()
    if ocr_available:
        logging.info("Импорт расписания из фото доступен (%s).", ocr_message)
        # Модели грузятся десятки секунд. Без прогрева эта цена платится внутри
        # первого запроса, и админ видит только "Распознаю..." и тишину.
        if settings.ocr_warmup_on_start:
            start_background_task(
                "ocr-warmup",
                warm_up_ocr_and_notify(
                    ocr_importer,
                    broadcaster,
                    ocr_message,
                    settings.database_path.parent / ".ocr_ready_notified",
                    settings.ocr_ready_notice_hours,
                ),
            )
        else:
            logging.warning(
                "Прогрев моделей OCR отключён (OCR_WARMUP_ON_START=false): "
                "первое фото будет распознаваться дольше."
            )
    else:
        logging.warning("Импорт расписания из фото недоступен: %s", ocr_message)
        await broadcaster.notify_admins(
            OCR_UNAVAILABLE_HTML.format(reason=escape(ocr_message)),
            OCR_UNAVAILABLE_TEXT.format(reason=ocr_message),
        )

    try:
        await broadcaster.start()
    except (aio_pika.exceptions.AMQPError, ConnectionError, OSError) as exc:
        logging.exception("RabbitMQ consumer failed on startup. Direct delivery fallback remains available.")
        await alert_manager.report_component_status("rabbitmq", False, str(exc), details="Сбой запуска consumer RabbitMQ")
    try:
        await jobs.start_db_cleanup_consumer()
    except (aio_pika.exceptions.AMQPError, ConnectionError, OSError) as exc:
        logging.exception("Database cleanup RabbitMQ consumer failed on startup. Scheduled direct fallback remains available.")
        await alert_manager.report_component_status("rabbitmq", False, str(exc), details="Сбой запуска db_cleanup consumer RabbitMQ")
    try:
        await jobs.start_auto_daily_lesson_counter_consumer()
    except (aio_pika.exceptions.AMQPError, ConnectionError, OSError) as exc:
        logging.exception("Auto daily lesson counter RabbitMQ consumer failed on startup. Scheduled direct fallback remains available.")
        await alert_manager.report_component_status("rabbitmq", False, str(exc), details="Сбой запуска auto_daily_lesson_counter consumer RabbitMQ")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # На Windows add_signal_handler не поддерживается — там остаётся Ctrl+C по умолчанию.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)

    async def initial_sync() -> None:
        try:
            await jobs.sync_current_snapshot()
        except Exception as exc:
            logging.exception("Initial schedule sync failed. Background scheduler will retry later.")
            await alert_manager.report_component_status(
                "schedule_site", False, str(exc), details="Первоначальная синхронизация расписания не удалась"
            )

    # Первичная синхронизация идёт десятки минут (пауза между запросами к сайту),
    # поэтому в фоне — чтобы остановка контейнера не ждала её окончания.
    start_background_task("initial-sync", initial_sync())

    await stop_event.wait()
    await shutdown(jobs, broadcaster)


async def shutdown(jobs: ScheduleJobs, broadcaster: Broadcaster) -> None:
    """Корректная остановка по SIGTERM (docker stop): без неё контейнер убивался через 10 с."""
    logging.info("Получен сигнал остановки, завершаю работу...")
    with contextlib.suppress(Exception):
        jobs.scheduler.shutdown(wait=False)
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.wait(set(_background_tasks), timeout=5)
    with contextlib.suppress(Exception):
        await broadcaster.stop()
    if broadcaster.telegram_bot is not None:
        with contextlib.suppress(Exception):
            await broadcaster.telegram_bot.session.close()
    if broadcaster.vk_bot is not None:
        with contextlib.suppress(Exception):
            await broadcaster.vk_bot.api.http_client.close()
    logging.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())

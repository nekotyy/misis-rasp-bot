"""Единая доставка ошибок администратору.

Любой сбой — упавший обработчик Telegram/VK, исключение в фоновой задаче,
ERROR в логе планировщика, «Task exception was never retrieved» — доходит до
админа сообщением. Одинаковые ошибки склеиваются: первая приходит сразу,
повторы в течение окна считаются и приезжают сводкой, чтобы шторм одной и той
же ошибки не заваливал личку.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import escape
from time import monotonic
from typing import Any

logger = logging.getLogger(__name__)

# Во время отправки отчёта логи не пересылаем — иначе сбой доставки породит новый отчёт.
_reporting = contextvars.ContextVar("admin_error_reporting", default=False)

TRACEBACK_LIMIT = 2500
TEXT_LIMIT = 3500

NotifyAdmins = Callable[[str, str], Awaitable[Any]]


@dataclass(slots=True)
class _Signature:
    first_seen: float
    last_sent: float
    suppressed: int = 0


@dataclass(slots=True)
class ErrorReport:
    source: str
    summary: str
    details: list[tuple[str, str]] = field(default_factory=list)
    traceback_text: str = ""
    signature: str = ""


def format_exception_text(error: BaseException | None, limit: int = TRACEBACK_LIMIT) -> str:
    if error is None:
        return ""
    text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    if len(text) > limit:
        text = f"...{text[-limit:]}"
    return text


def short_error_text(error: BaseException, limit: int = 350) -> str:
    text = f"{type(error).__name__}: {error}"
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _error_location(error: BaseException | None) -> str:
    if error is None or error.__traceback__ is None:
        return ""
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return ""
    frame = frames[-1]
    return f"{frame.filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:{frame.lineno}"


class AdminErrorReporter:
    def __init__(
        self,
        notify: NotifyAdmins | None = None,
        *,
        dedup_window_seconds: float = 1800.0,
        max_reports_per_hour: int = 40,
    ) -> None:
        self._notify = notify
        self.dedup_window_seconds = dedup_window_seconds
        self.max_reports_per_hour = max_reports_per_hour
        self._signatures: dict[str, _Signature] = {}
        self._sent_times: list[float] = []
        self._dropped_over_budget = 0
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._muted = False

    def set_notifier(self, notify: NotifyAdmins | None) -> None:
        self._notify = notify

    def mute(self) -> None:
        """Глушит отчёты на время остановки: обрыв polling и отмена задач при перезапуске — не сбой."""
        self._muted = True

    # --- публичное API -------------------------------------------------

    async def report_exception(
        self,
        source: str,
        error: BaseException,
        *,
        details: list[tuple[str, str]] | None = None,
        summary: str | None = None,
    ) -> None:
        signature = f"{source}|{type(error).__name__}|{_error_location(error)}|{str(error)[:120]}"
        await self.report(
            ErrorReport(
                source=source,
                summary=summary or short_error_text(error),
                details=list(details or []),
                traceback_text=format_exception_text(error),
                signature=signature,
            )
        )

    async def report(self, report: ErrorReport) -> None:
        if _reporting.get() or self._muted or self._notify is None:
            return
        token = _reporting.set(True)
        try:
            message = await self._prepare(report)
            if message is None:
                return
            html_text, plain_text = message
            try:
                await self._notify(html_text, plain_text)
            except Exception as exc:
                logger.warning("Не удалось доставить отчёт об ошибке админу: %s", exc)
        except Exception:
            logger.warning("Сбой в самом репортере ошибок.", exc_info=True)
        finally:
            _reporting.reset(token)

    def report_in_background(self, report: ErrorReport) -> None:
        """Для синхронного кода (logging.Handler, loop exception handler)."""
        if _reporting.get() or self._muted:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._spawn(report)
        else:
            loop.call_soon_threadsafe(self._spawn, report)

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # --- внутреннее ----------------------------------------------------

    def _spawn(self, report: ErrorReport) -> None:
        task = asyncio.create_task(self.report(report), name="admin-error-report")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _prepare(self, report: ErrorReport) -> tuple[str, str] | None:
        now = monotonic()
        signature = report.signature or f"{report.source}|{report.summary[:160]}"
        async with self._lock:
            known = self._signatures.get(signature)
            if known is not None and now - known.last_sent < self.dedup_window_seconds:
                known.suppressed += 1
                return None
            self._sent_times = [moment for moment in self._sent_times if now - moment < 3600]
            if len(self._sent_times) >= self.max_reports_per_hour:
                self._dropped_over_budget += 1
                if known is not None:
                    known.suppressed += 1
                return None
            repeats = known.suppressed if known is not None else 0
            since = (now - known.first_seen) if known is not None else 0.0
            self._signatures[signature] = _Signature(first_seen=now if known is None else known.first_seen, last_sent=now)
            if len(self._signatures) > 500:
                oldest = sorted(self._signatures.items(), key=lambda item: item[1].last_sent)[:100]
                for key, _ in oldest:
                    self._signatures.pop(key, None)
            self._sent_times.append(now)
            dropped = self._dropped_over_budget
            self._dropped_over_budget = 0
        return self._format(report, repeats=repeats, since_seconds=since, dropped=dropped)

    @staticmethod
    def _format(report: ErrorReport, *, repeats: int, since_seconds: float, dropped: int) -> tuple[str, str]:
        html_lines = [f"<b>Сбой: {escape(report.source)}</b>", ""]
        text_lines = [f"Сбой: {report.source}", ""]
        for label, value in report.details:
            html_lines.append(f"{escape(label)}: <b>{escape(value)}</b>")
            text_lines.append(f"{label}: {value}")
        html_lines.append(f"Ошибка: <code>{escape(report.summary)}</code>")
        text_lines.append(f"Ошибка: {report.summary}")
        if repeats:
            minutes = max(1, round(since_seconds / 60))
            html_lines.append(f"Повторялась ещё {repeats} раз за ~{minutes} мин.")
            text_lines.append(f"Повторялась ещё {repeats} раз за ~{minutes} мин.")
        if dropped:
            html_lines.append(f"Пропущено отчётов из-за лимита в час: {dropped}.")
            text_lines.append(f"Пропущено отчётов из-за лимита в час: {dropped}.")
        html_text = "\n".join(html_lines)
        plain_text = "\n".join(text_lines)
        if report.traceback_text:
            budget = max(200, TEXT_LIMIT - len(html_text))
            tb = report.traceback_text if len(report.traceback_text) <= budget else f"...{report.traceback_text[-budget:]}"
            html_text += f"\n\n<pre>{escape(tb)}</pre>"
            plain_text += f"\n\n{tb}"
        return html_text, plain_text


# aiogram пишет ERROR "Failed to fetch updates - <ТипОшибки>: <текст>" на каждый неудачный
# getUpdates и сам переподключается с нарастающей паузой. Сетевые обрывы Telegram (в том
# числе "Server disconnected" и обрыв DNS на хосте) — это шум, а не сбой: если приём
# сообщений реально встал, об этом сообщает проверка живости `telegram_polling`.
# Остальные ошибки (неверный токен, конфликт двух копий бота) по-прежнему идут админу.
_TRANSIENT_POLLING_ERRORS = frozenset({"TelegramNetworkError", "TelegramServerError", "TelegramRetryAfter"})


def is_transient_polling_noise(record: logging.LogRecord) -> bool:
    """Временный сетевой сбой получения апдейтов aiogram, о котором админу писать не нужно."""
    if record.name != "aiogram.dispatcher" or not str(record.msg).startswith("Failed to fetch updates"):
        return False
    args = record.args
    return isinstance(args, tuple) and bool(args) and args[0] in _TRANSIENT_POLLING_ERRORS


class AdminLogHandler(logging.Handler):
    """Пересылает админу записи уровня ERROR и выше из любых логгеров.

    Запись с `extra={"skip_admin_report": True}` не пересылается — так помечаем
    места, которые уже сами уведомили админа (алерты компонентов, обработчики
    ошибок ботов).
    """

    def __init__(self, reporter: AdminErrorReporter, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "skip_admin_report", False) or record.name.startswith(__name__):
            return
        if is_transient_polling_noise(record):
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        error = record.exc_info[1] if record.exc_info and record.exc_info[1] is not None else None
        if isinstance(error, asyncio.CancelledError):
            # Отменённая задача (остановка, таймаут) — штатное завершение, а не ошибка.
            return
        summary = message if error is None else f"{message} — {short_error_text(error)}"
        if len(summary) > 600:
            summary = f"{summary[:597]}..."
        template = str(record.msg)[:160]
        report = ErrorReport(
            source=f"лог {record.name}",
            summary=summary,
            details=[("Где", f"{record.module}:{record.lineno}")],
            traceback_text=format_exception_text(error),
            signature=f"log|{record.name}|{template}|{type(error).__name__ if error else ''}",
        )
        try:
            self.reporter.report_in_background(report)
        except Exception:
            self.handleError(record)


def install_error_reporting(reporter: AdminErrorReporter, loop: asyncio.AbstractEventLoop) -> AdminLogHandler:
    """Подключает репортер к корневому логгеру.

    Этого достаточно и для необработанных исключений asyncio («Task exception was
    never retrieved»), и для упавших задач APScheduler: оба пишут ERROR в лог.
    """
    reporter.attach_loop(loop)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, AdminLogHandler):
            root.removeHandler(existing)
    handler = AdminLogHandler(reporter)
    root.addHandler(handler)
    return handler

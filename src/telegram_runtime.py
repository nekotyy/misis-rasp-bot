"""Таймауты и повторы Telegram API при обрывах и ограничениях хостинга."""

from __future__ import annotations

import asyncio
import logging
import random
from time import monotonic

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import ClientDecodeError, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import GetUpdates
from aiohttp import ClientError, ClientTimeout

logger = logging.getLogger(__name__)

TELEGRAM_REQUEST_ATTEMPTS = 3
TELEGRAM_MAX_BACKOFF_SECONDS = 30.0
TELEGRAM_CONNECT_TIMEOUT_SECONDS = 15.0
TELEGRAM_POLLING_TIMEOUT_MARGIN = 20.0


def is_permanent_telegram_delivery_error(error: BaseException) -> bool:
    text = str(error).lower()
    return isinstance(error, TelegramForbiddenError) or any(marker in text for marker in (
        "bot was blocked by the user", "chat not found", "user is deactivated",
        "have no rights to send a message", "chat_id is empty", "group chat was upgraded to a supergroup chat",
    ))


class TrackingAiohttpSession(AiohttpSession):
    """Повторяет временные ошибки, сохраняя offset long poll и общую HTTP-сессию.

    Приём событий переподключается до восстановления сети. Остальные методы
    ограничены тремя попытками; постоянные ошибки (400/401/403/409) не повторяются
    здесь. Заданный Telegram retry_after всегда выдерживается полностью.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started_at = monotonic()
        self.last_updates_ok_at: float | None = None
        self.consecutive_failures = 0
        self._failure_started_at: float | None = None
        self.outbox = None

    async def direct_request(self, bot, method):
        """Одна попытка воркера; повтор и срок актуальности хранятся в SQLite."""
        timeout = ClientTimeout(total=self.timeout, connect=15, sock_connect=15, sock_read=self.timeout)
        return await super().make_request(bot, method, timeout=timeout)

    async def make_request(self, bot, method, timeout=None):
        if self.outbox is not None and not method.__api_method__.startswith("get"):
            return await self.outbox.execute_telegram(bot, method)
        polling = isinstance(method, GetUpdates)
        total = (
            (method.timeout or 0) + TELEGRAM_POLLING_TIMEOUT_MARGIN
            if polling else (self.timeout if timeout is None else timeout)
        )
        request_timeout = ClientTimeout(
            total=total,
            connect=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
            sock_connect=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
            sock_read=total,
        )
        attempt = 0
        backoff = 1.0
        while True:
            attempt += 1
            try:
                # Внешний deadline включает создание сессии и DNS, а не только
                # session.post(). Отмена при остановке контейнера проходит наружу.
                async with asyncio.timeout(total):
                    result = await super().make_request(bot, method, timeout=request_timeout)
            except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter, ClientDecodeError, ClientError, TimeoutError, OSError) as exc:
                if polling:
                    self.consecutive_failures += 1
                    if self._failure_started_at is None:
                        self._failure_started_at = monotonic()
                elif attempt >= TELEGRAM_REQUEST_ATTEMPTS:
                    raise
                delay = (
                    max(0.0, float(exc.retry_after)) + 0.5
                    if isinstance(exc, TelegramRetryAfter)
                    else min(TELEGRAM_MAX_BACKOFF_SECONDS, backoff * random.uniform(0.8, 1.2))  # noqa: S311
                )
                logger.warning(
                    "Telegram %s: временная ошибка %s (попытка %s), повтор через %.1f с.",
                    method.__api_method__, type(exc).__name__, attempt, delay,
                )
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, TELEGRAM_MAX_BACKOFF_SECONDS)
                continue

            if polling:
                self.last_updates_ok_at = monotonic()
                if self.consecutive_failures:
                    logger.info(
                        "Telegram getUpdates восстановлен после %s ошибок (%.0f с).",
                        self.consecutive_failures, monotonic() - self._failure_started_at,
                    )
                self.consecutive_failures = 0
                self._failure_started_at = None
            return result


def telegram_retry_attempts(bot: Bot | None) -> int:
    """Не умножает повторы UX/рассылки на повторы общей HTTP-сессии."""
    return 1 if isinstance(getattr(bot, "session", None), TrackingAiohttpSession) else TELEGRAM_REQUEST_ATTEMPTS

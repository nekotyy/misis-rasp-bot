from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from src.db import Database

logger = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self, db: Database, telegram_bot: Bot | None = None) -> None:
        self.db = db
        self.telegram_bot = telegram_bot

    async def broadcast(self, message: str) -> None:
        await self._broadcast_telegram(message)

    async def broadcast_test_message(self) -> None:
        await self.broadcast("Тестовое уведомление: бот активен и рассылка работает.")

    async def _broadcast_telegram(self, message: str) -> None:
        if self.telegram_bot is None:
            return
        users = await self.db.get_users_for_platform("telegram")
        for user in users:
            try:
                await self.telegram_bot.send_message(chat_id=user.user_id, text=message)
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                logger.warning("Telegram broadcast failed for %s: %s", user.user_id, exc)

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from vkbottle.bot import Bot as VkBot

from src.db import Database

logger = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self, db: Database, telegram_bot: Bot | None = None, vk_bot: VkBot | None = None) -> None:
        self.db = db
        self.telegram_bot = telegram_bot
        self.vk_bot = vk_bot

    async def broadcast(
        self,
        message: str,
        telegram_message: str | None = None,
        vk_message: str | None = None,
        schedule_id: int | None = None,
    ) -> None:
        await self._broadcast_telegram(telegram_message or message, schedule_id=schedule_id)
        await self._broadcast_vk(vk_message or message, schedule_id=schedule_id)

    async def broadcast_test_message(self) -> None:
        await self.broadcast("Тестовое уведомление: бот активен и рассылка работает.")

    async def broadcast_homework_update(self, message: str, schedule_id: int | None = None) -> None:
        await self._broadcast_telegram(message, homework_only=True, schedule_id=schedule_id)
        await self._broadcast_vk(message, homework_only=True, schedule_id=schedule_id)

    async def _broadcast_telegram(self, message: str, homework_only: bool = False, schedule_id: int | None = None) -> None:
        if self.telegram_bot is None:
            return
        users = (
            await self.db.get_users_for_homework_notifications("telegram", schedule_id=schedule_id)
            if homework_only
            else await self.db.get_users_for_platform("telegram", schedule_id=schedule_id)
        )
        for user in users:
            try:
                await self.telegram_bot.send_message(chat_id=user.user_id, text=message)
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                logger.warning("Telegram broadcast failed for %s: %s", user.user_id, exc)

    async def _broadcast_vk(self, message: str, homework_only: bool = False, schedule_id: int | None = None) -> None:
        if self.vk_bot is None:
            return
        users = (
            await self.db.get_users_for_homework_notifications("vk", schedule_id=schedule_id)
            if homework_only
            else await self.db.get_users_for_platform("vk", schedule_id=schedule_id)
        )
        for user in users:
            try:
                await self.vk_bot.api.messages.send(peer_ids=[user.user_id], message=message, random_id=0)
            except Exception as exc:  # pragma: no cover - depends on VK API
                logger.warning("VK broadcast failed for %s: %s", user.user_id, exc)

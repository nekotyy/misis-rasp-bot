from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from vkbottle.bot import Bot as VkBot

from src.db import Database
from src.message_broker import OutboundMessage, RabbitMQBroker

logger = logging.getLogger(__name__)


class Broadcaster:
    def __init__(
        self,
        db: Database,
        telegram_bot: Bot | None = None,
        vk_bot: VkBot | None = None,
        admin_telegram_id: int | None = None,
        admin_vk_id: int | None = None,
        broker: RabbitMQBroker | None = None,
    ) -> None:
        self.db = db
        self.telegram_bot = telegram_bot
        self.vk_bot = vk_bot
        self.admin_telegram_id = admin_telegram_id
        self.admin_vk_id = admin_vk_id
        self.broker = broker

    async def start(self) -> None:
        if self.broker is None or not self.broker.enabled:
            return
        await self.broker.start_consumer(self.deliver)

    async def stop(self) -> None:
        if self.broker is None or not self.broker.enabled:
            return
        await self.broker.close()

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

    async def notify_admins(self, telegram_message: str, vk_message: str | None = None) -> None:
        if self.telegram_bot is not None and self.admin_telegram_id is not None:
            if not await self._enqueue_or_send(
                OutboundMessage(platform="telegram", user_id=self.admin_telegram_id, text=telegram_message)
            ):
                try:
                    await self.telegram_bot.send_message(chat_id=self.admin_telegram_id, text=telegram_message)
                except (TelegramForbiddenError, TelegramBadRequest) as exc:
                    logger.warning("Telegram admin notify failed for %s: %s", self.admin_telegram_id, exc)
        if self.vk_bot is not None and self.admin_vk_id is not None:
            message_text = vk_message or telegram_message
            if not await self._enqueue_or_send(
                OutboundMessage(platform="vk", user_id=self.admin_vk_id, text=message_text)
            ):
                try:
                    await self.vk_bot.api.messages.send(
                        peer_ids=[self.admin_vk_id],
                        message=message_text,
                        random_id=0,
                    )
                except Exception as exc:  # pragma: no cover - depends on VK API
                    logger.warning("VK admin notify failed for %s: %s", self.admin_vk_id, exc)

    async def deliver(self, payload: OutboundMessage) -> None:
        if payload.platform == "telegram":
            await self._send_telegram(payload.user_id, payload.text)
            return
        if payload.platform == "vk":
            await self._send_vk(payload.user_id, payload.text)
            return
        logger.warning("Unknown outbound platform: %s", payload.platform)

    async def _broadcast_telegram(self, message: str, homework_only: bool = False, schedule_id: int | None = None) -> None:
        if self.telegram_bot is None:
            return
        users = (
            await self.db.get_users_for_homework_notifications("telegram", schedule_id=schedule_id)
            if homework_only
            else await self.db.get_users_for_notifications("telegram", schedule_id=schedule_id)
        )
        for user in users:
            payload = OutboundMessage(platform="telegram", user_id=user.user_id, text=message)
            if not await self._enqueue_or_send(payload):
                await self._send_telegram(user.user_id, message)

    async def _broadcast_vk(self, message: str, homework_only: bool = False, schedule_id: int | None = None) -> None:
        if self.vk_bot is None:
            return
        users = (
            await self.db.get_users_for_homework_notifications("vk", schedule_id=schedule_id)
            if homework_only
            else await self.db.get_users_for_notifications("vk", schedule_id=schedule_id)
        )
        for user in users:
            payload = OutboundMessage(platform="vk", user_id=user.user_id, text=message)
            if not await self._enqueue_or_send(payload):
                await self._send_vk(user.user_id, message)

    async def _enqueue_or_send(self, payload: OutboundMessage) -> bool:
        if self.broker is None or not self.broker.enabled:
            return False
        return await self.broker.publish(payload)

    async def _send_telegram(self, user_id: int, message: str) -> None:
        if self.telegram_bot is None:
            return
        try:
            await self.telegram_bot.send_message(chat_id=user_id, text=message)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning("Telegram broadcast failed for %s: %s", user_id, exc)

    async def _send_vk(self, user_id: int, message: str) -> None:
        if self.vk_bot is None:
            return
        try:
            await self.vk_bot.api.messages.send(peer_ids=[user_id], message=message, random_id=0)
        except Exception as exc:  # pragma: no cover - depends on VK API
            logger.warning("VK broadcast failed for %s: %s", user_id, exc)

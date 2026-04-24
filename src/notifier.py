from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from vkbottle.bot import Bot as VkBot

from src.db import Database
from src.message_broker import OutboundMessage, RabbitMQBroker

logger = logging.getLogger(__name__)

CAMPAIGN_NOTIFICATION = "notification"
CAMPAIGN_ADMIN_BROADCAST = "admin_broadcast"
CAMPAIGN_ADMIN_NOTIFY = "admin_notify"


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
        try:
            disabled_count = await self.db.auto_disable_undeliverable_telegram_users()
            if disabled_count:
                logger.info("Auto-disabled undeliverable telegram users from history: %s", disabled_count)
        except Exception as exc:
            logger.warning("Auto-disable sync failed on startup: %s", exc)
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
        subscription_key: str | None = None,
        campaign_type: str = CAMPAIGN_NOTIFICATION,
    ) -> None:
        await self._broadcast_telegram(
            telegram_message or message,
            schedule_id=schedule_id,
            subscription_key=subscription_key,
            campaign_type=campaign_type,
        )
        await self._broadcast_vk(
            vk_message or message,
            schedule_id=schedule_id,
            subscription_key=subscription_key,
            campaign_type=campaign_type,
        )

    async def broadcast_test_message(self) -> None:
        await self.broadcast(
            "Тестовое уведомление: бот активен и рассылка работает.",
            campaign_type=CAMPAIGN_NOTIFICATION,
        )

    async def notify_admins(self, telegram_message: str, vk_message: str | None = None) -> None:
        if self.telegram_bot is not None and self.admin_telegram_id is not None:
            if not await self._enqueue_or_send(
                OutboundMessage(
                    platform="telegram",
                    user_id=self.admin_telegram_id,
                    text=telegram_message,
                    campaign_type=CAMPAIGN_ADMIN_NOTIFY,
                )
            ):
                await self._send_telegram(
                    self.admin_telegram_id,
                    telegram_message,
                    campaign_type=CAMPAIGN_ADMIN_NOTIFY,
                    via_broker=False,
                )
        if self.vk_bot is not None and self.admin_vk_id is not None:
            message_text = vk_message or telegram_message
            if not await self._enqueue_or_send(
                OutboundMessage(
                    platform="vk",
                    user_id=self.admin_vk_id,
                    text=message_text,
                    campaign_type=CAMPAIGN_ADMIN_NOTIFY,
                )
            ):
                await self._send_vk(
                    self.admin_vk_id,
                    message_text,
                    campaign_type=CAMPAIGN_ADMIN_NOTIFY,
                    via_broker=False,
                )

    async def deliver(self, payload: OutboundMessage) -> None:
        if payload.platform == "telegram":
            await self._send_telegram(
                payload.user_id,
                payload.text,
                campaign_type=payload.campaign_type,
                via_broker=True,
                attempt=payload.attempt,
                message_id=payload.message_id,
                raise_on_failure=True,
            )
            return
        if payload.platform == "vk":
            await self._send_vk(
                payload.user_id,
                payload.text,
                campaign_type=payload.campaign_type,
                via_broker=True,
                attempt=payload.attempt,
                message_id=payload.message_id,
                raise_on_failure=True,
            )
            return
        logger.warning("Unknown outbound platform: %s", payload.platform)

    async def _broadcast_telegram(
        self,
        message: str,
        schedule_id: int | None = None,
        subscription_key: str | None = None,
        campaign_type: str = CAMPAIGN_NOTIFICATION,
    ) -> None:
        if self.telegram_bot is None:
            return
        try:
            await self.db.auto_disable_undeliverable_telegram_users()
        except Exception as exc:
            logger.warning("Auto-disable sync failed before telegram broadcast: %s", exc)
        users = await self.db.get_users_for_notifications(
            "telegram",
            schedule_id=schedule_id,
            subscription_key=subscription_key,
        )
        for user in users:
            payload = OutboundMessage(
                platform="telegram",
                user_id=user.user_id,
                text=message,
                campaign_type=campaign_type,
            )
            if not await self._enqueue_or_send(payload):
                await self._send_telegram(
                    user.user_id,
                    message,
                    campaign_type=campaign_type,
                    via_broker=False,
                )

    async def _broadcast_vk(
        self,
        message: str,
        schedule_id: int | None = None,
        subscription_key: str | None = None,
        campaign_type: str = CAMPAIGN_NOTIFICATION,
    ) -> None:
        if self.vk_bot is None:
            return
        users = await self.db.get_users_for_notifications(
            "vk",
            schedule_id=schedule_id,
            subscription_key=subscription_key,
        )
        for user in users:
            payload = OutboundMessage(
                platform="vk",
                user_id=user.user_id,
                text=message,
                campaign_type=campaign_type,
            )
            if not await self._enqueue_or_send(payload):
                await self._send_vk(
                    user.user_id,
                    message,
                    campaign_type=campaign_type,
                    via_broker=False,
                )

    async def _enqueue_or_send(self, payload: OutboundMessage) -> bool:
        if self.broker is None or not self.broker.enabled:
            return False
        try:
            return await self.broker.publish(payload)
        except Exception as exc:
            logger.warning("RabbitMQ publish failed for %s/%s: %s", payload.platform, payload.user_id, exc)
            return False

    async def _record_delivery_event(self, **kwargs) -> None:
        try:
            await self.db.record_delivery_event(**kwargs)
        except Exception as exc:
            logger.warning("Delivery event logging failed: %s", exc)

    async def _send_telegram(
        self,
        user_id: int,
        message: str,
        *,
        campaign_type: str,
        via_broker: bool,
        attempt: int = 1,
        message_id: str | None = None,
        raise_on_failure: bool = False,
    ) -> bool:
        if self.telegram_bot is None:
            return False
        try:
            await self.telegram_bot.send_message(chat_id=user_id, text=message)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            await self._record_delivery_event(
                campaign_type=campaign_type,
                platform="telegram",
                user_id=user_id,
                via_broker=via_broker,
                status="failed",
                attempt=attempt,
                message_id=message_id,
                error_text=error_text,
            )
            if self._is_permanent_telegram_failure(exc):
                try:
                    await self.db.mark_delivery_auto_disabled("telegram", user_id, True)
                except Exception as disable_exc:
                    logger.warning("Failed to auto-disable telegram user %s: %s", user_id, disable_exc)
            logger.warning("Telegram broadcast failed for %s: %s", user_id, exc)
            if raise_on_failure:
                raise
            return False

        await self._record_delivery_event(
            campaign_type=campaign_type,
            platform="telegram",
            user_id=user_id,
            via_broker=via_broker,
            status="sent",
            attempt=attempt,
            message_id=message_id,
        )
        return True

    async def _send_vk(
        self,
        user_id: int,
        message: str,
        *,
        campaign_type: str,
        via_broker: bool,
        attempt: int = 1,
        message_id: str | None = None,
        raise_on_failure: bool = False,
    ) -> bool:
        if self.vk_bot is None:
            return False
        try:
            await self.vk_bot.api.messages.send(peer_ids=[user_id], message=message, random_id=0)
        except Exception as exc:  # pragma: no cover - depends on VK API
            error_text = f"{type(exc).__name__}: {exc}"
            await self._record_delivery_event(
                campaign_type=campaign_type,
                platform="vk",
                user_id=user_id,
                via_broker=via_broker,
                status="failed",
                attempt=attempt,
                message_id=message_id,
                error_text=error_text,
            )
            logger.warning("VK broadcast failed for %s: %s", user_id, exc)
            if raise_on_failure:
                raise
            return False

        await self._record_delivery_event(
            campaign_type=campaign_type,
            platform="vk",
            user_id=user_id,
            via_broker=via_broker,
            status="sent",
            attempt=attempt,
            message_id=message_id,
        )
        return True

    def _is_permanent_telegram_failure(self, exc: Exception) -> bool:
        error_text = str(exc).lower()
        markers = (
            "forbidden: bot was blocked by the user",
            "bot was blocked by the user",
            "chat not found",
            "user is deactivated",
            "have no rights to send a message",
            "chat_id is empty",
            "group chat was upgraded to a supergroup chat",
        )
        if any(marker in error_text for marker in markers):
            return True
        if isinstance(exc, TelegramForbiddenError):
            return True
        if isinstance(exc, TelegramBadRequest):
            return any(marker in error_text for marker in markers)
        return False

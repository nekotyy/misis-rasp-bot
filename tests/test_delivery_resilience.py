from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from vkbottle.exception_factory.base_exceptions import VKAPIError

from src.db import Database
from src.notifier import (
    CAMPAIGN_ADMIN_BROADCAST,
    CAMPAIGN_NOTIFICATION,
    Broadcaster,
    DeliveryUnavailableError,
)

METHOD = SendMessage(chat_id=1, text="x")


def _vk_bot(side_effect) -> MagicMock:
    bot = MagicMock()
    bot.api.request = AsyncMock(side_effect=side_effect)
    return bot


class DeliveryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "delivery.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()


class VkDeliveryTests(DeliveryTestCase):
    async def test_notification_has_schedule_keyboard_and_uses_peer_id(self) -> None:
        bot = _vk_bot([{"response": 11}])
        broadcaster = Broadcaster(db=self.db, vk_bot=bot)

        ok = await broadcaster._send_vk(5, "Изменения", campaign_type=CAMPAIGN_NOTIFICATION, via_broker=False)

        self.assertTrue(ok)
        params = bot.api.request.await_args.args[1]
        self.assertEqual(params["peer_id"], 5)
        labels = [button["action"]["label"] for row in json.loads(params["keyboard"])["buttons"] for button in row]
        self.assertIn("Расписание на сегодня", labels)

    async def test_admin_broadcast_has_no_keyboard(self) -> None:
        bot = _vk_bot([{"response": 11}])
        await Broadcaster(db=self.db, vk_bot=bot)._send_vk(5, "Текст", campaign_type=CAMPAIGN_ADMIN_BROADCAST, via_broker=False)
        self.assertNotIn("keyboard", bot.api.request.await_args.args[1])

    async def test_permanent_error_disables_user_and_is_not_retried_by_broker(self) -> None:
        await self.db.upsert_user("vk", 5, None, "Студент")
        bot = _vk_bot([VKAPIError[901](error_msg="Can't send messages for users without permission")])
        broadcaster = Broadcaster(db=self.db, vk_bot=bot)

        ok = await broadcaster._send_vk(
            5, "Изменения", campaign_type=CAMPAIGN_NOTIFICATION, via_broker=True, raise_on_failure=True
        )

        self.assertFalse(ok)
        user = await self.db.get_user("vk", 5)
        self.assertTrue(user.delivery_disabled_auto)
        self.assertFalse(user.homework_notifications_enabled)

    async def test_temporary_error_is_raised_for_broker_retry(self) -> None:
        bot = _vk_bot([VKAPIError[10](error_msg="Internal server error")] * 4)
        broadcaster = Broadcaster(db=self.db, vk_bot=bot)
        with patch("src.vk_runtime.asyncio.sleep", AsyncMock()), self.assertRaises(VKAPIError):
            await broadcaster._send_vk(5, "x", campaign_type=CAMPAIGN_NOTIFICATION, via_broker=True, raise_on_failure=True)

    async def test_missing_vk_bot_is_retryable_not_attribute_error(self) -> None:
        broadcaster = Broadcaster(db=self.db, vk_bot=None)
        with self.assertRaises(DeliveryUnavailableError):
            await broadcaster._send_vk(5, "x", campaign_type=CAMPAIGN_NOTIFICATION, via_broker=True, raise_on_failure=True)
        self.assertFalse(await broadcaster._send_vk(5, "x", campaign_type=CAMPAIGN_NOTIFICATION, via_broker=False))


class TelegramDeliveryTests(DeliveryTestCase):
    async def test_retry_after_is_respected(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[TelegramRetryAfter(METHOD, "flood", 3), MagicMock()])
        broadcaster = Broadcaster(db=self.db, telegram_bot=bot)
        sleep = AsyncMock()
        with patch("src.notifier.asyncio.sleep", sleep):
            ok = await broadcaster._send_telegram(1, "x", campaign_type=CAMPAIGN_ADMIN_BROADCAST, via_broker=False)
        self.assertTrue(ok)
        self.assertGreaterEqual(sleep.await_args.args[0], 3)

    async def test_network_error_is_retried(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[TelegramNetworkError(METHOD, "reset"), MagicMock()])
        broadcaster = Broadcaster(db=self.db, telegram_bot=bot)
        with patch("src.notifier.asyncio.sleep", AsyncMock()):
            ok = await broadcaster._send_telegram(1, "x", campaign_type=CAMPAIGN_ADMIN_BROADCAST, via_broker=False)
        self.assertTrue(ok)
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_blocked_bot_is_not_raised_to_broker(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(METHOD, "Forbidden: bot was blocked by the user"))
        broadcaster = Broadcaster(db=self.db, telegram_bot=bot)
        ok = await broadcaster._send_telegram(1, "x", campaign_type=CAMPAIGN_NOTIFICATION, via_broker=True, raise_on_failure=True)
        self.assertFalse(ok)
        self.assertEqual(bot.send_message.await_count, 1)

    async def test_long_message_is_split(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        broadcaster = Broadcaster(db=self.db, telegram_bot=bot)
        await broadcaster._send_telegram(1, "a" * 3000 + "\n" + "b" * 3000, campaign_type=CAMPAIGN_NOTIFICATION, via_broker=False)
        self.assertEqual(bot.send_message.await_count, 2)
        first, second = bot.send_message.await_args_list
        self.assertIsNone(first.kwargs["reply_markup"])
        self.assertIsNotNone(second.kwargs["reply_markup"])


class AutoDisableRecoveryTests(DeliveryTestCase):
    async def test_user_who_returns_gets_notifications_back(self) -> None:
        await self.db.upsert_user("telegram", 1, "u", "User", subscription_type="group", subscription_key="600", subscription_title="ИСП-25-1", schedule_id=600)
        async with self.db._connect() as conn:
            await conn.execute("UPDATE users SET last_seen_at = '2000-01-01T00:00:00' WHERE user_id = 1")
            await conn.commit()
        await self.db.record_delivery_event(
            campaign_type=CAMPAIGN_NOTIFICATION,
            platform="telegram",
            user_id=1,
            via_broker=False,
            status="failed",
            attempt=1,
            message_id=None,
            error_text="TelegramForbiddenError: Forbidden: bot was blocked by the user",
        )
        await self.db.auto_disable_undeliverable_telegram_users()
        self.assertFalse((await self.db.get_user("telegram", 1)).homework_notifications_enabled)

        # Пользователь разблокировал бота и снова написал ему.
        await self.db.upsert_user("telegram", 1, "u", "User")
        user = await self.db.get_user("telegram", 1)
        self.assertTrue(user.homework_notifications_enabled)
        self.assertFalse(user.delivery_disabled_auto)

        # Старая ошибка доставки из истории больше не отключает его перед рассылкой.
        await self.db.auto_disable_undeliverable_telegram_users()
        self.assertTrue((await self.db.get_user("telegram", 1)).homework_notifications_enabled)


if __name__ == "__main__":
    unittest.main()

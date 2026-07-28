import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from src.db import Database
from src.models import UserRecord
from src.notifier import Broadcaster, CAMPAIGN_NOTIFICATION


class CustomNotificationsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_bot.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_record_and_get_custom_notification_text_and_sticker(self):
        await self.db.upsert_user("telegram", 12345, username="testuser", full_name="Test User")

        # Initial state should be None
        user = await self.db.get_user("telegram", 12345)
        self.assertIsNotNone(user)
        self.assertIsNone(user.custom_notification_text)
        self.assertIsNone(user.custom_sticker_file_id)

        # Set custom text
        await self.db.set_user_custom_notification_text("telegram", 12345, "Внимание! Расписание изменилось.")
        user = await self.db.get_user("telegram", 12345)
        self.assertEqual(user.custom_notification_text, "Внимание! Расписание изменилось.")

        # Set custom sticker
        await self.db.set_user_custom_sticker("telegram", 12345, "sticker_file_id_abc123")
        user = await self.db.get_user("telegram", 12345)
        self.assertEqual(user.custom_sticker_file_id, "sticker_file_id_abc123")

        # Clear custom text
        await self.db.clear_user_custom_notification_text("telegram", 12345)
        user = await self.db.get_user("telegram", 12345)
        self.assertIsNone(user.custom_notification_text)
        self.assertEqual(user.custom_sticker_file_id, "sticker_file_id_abc123")

        # Clear custom sticker
        await self.db.clear_user_custom_sticker("telegram", 12345)
        user = await self.db.get_user("telegram", 12345)
        self.assertIsNone(user.custom_sticker_file_id)


class CustomNotificationsNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_sticker_and_text_in_notifier(self):
        mock_db = MagicMock()
        mock_user = UserRecord(
            platform="telegram",
            user_id=999,
            username="user999",
            full_name="User 999",
            subscription_type="group",
            subscription_key="group:1",
            subscription_title="ИСП-25-1",
            subscription_url=None,
            audience_subscription_key=None,
            audience_subscription_title=None,
            audience_subscription_url=None,
            group_name="ИСП-25-1",
            schedule_id=1,
            is_admin=False,
            is_editor=False,
            homework_notifications_enabled=True,
            delivery_disabled_auto=False,
            created_at="2026-01-01T00:00:00",
            last_seen_at="2026-01-01T00:00:00",
            custom_notification_text="Мое предупреждение:",
            custom_sticker_file_id="sticker_123_xyz",
        )
        mock_db.get_user = AsyncMock(return_value=mock_user)
        mock_db.record_delivery_event = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.send_sticker = AsyncMock()
        mock_bot.send_message = AsyncMock()

        broadcaster = Broadcaster(db=mock_db, telegram_bot=mock_bot)

        success = await broadcaster._send_telegram(
            999,
            "Изменения на 29.07.2026: Добавлена пара Литература",
            campaign_type=CAMPAIGN_NOTIFICATION,
            via_broker=False,
        )

        self.assertTrue(success)
        mock_bot.send_sticker.assert_awaited_once_with(chat_id=999, sticker="sticker_123_xyz")
        mock_bot.send_message.assert_awaited_once()
        sent_text = mock_bot.send_message.call_args.kwargs["text"]
        self.assertTrue(sent_text.startswith("Мое предупреждение:\n\nИзменения на 29.07.2026"))


class SmokeCustomNotificationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_bot.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_smoke_no_emojis_in_personalization_texts(self):
        from src.telegram_bot import (
            build_personalization_keyboard,
            format_personalization_settings_text,
        )
        import re

        emoji_pattern = re.compile(
            "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E6-\U0001F1FF]"
        )

        await self.db.upsert_user("telegram", 888, username="smokeuser", full_name="Smoke User")
        text = await format_personalization_settings_text(888, self.db)
        kbd = await build_personalization_keyboard(888, self.db)

        self.assertIsNone(emoji_pattern.search(text), f"Emoji found in text: {text}")

        for row in kbd.inline_keyboard:
            for btn in row:
                self.assertIsNone(emoji_pattern.search(btn.text), f"Emoji found in button: {btn.text}")


if __name__ == "__main__":
    unittest.main()

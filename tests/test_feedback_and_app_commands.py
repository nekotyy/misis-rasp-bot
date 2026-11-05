import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from src.config import Settings
from src.db import Database
from src.telegram_bot import build_dispatcher
from src.vk_bot import build_vk_bot


def create_mock_message(
    chat_id: int,
    user_id: int,
    text: str,
    username: str = "testuser",
    full_name: str = "Test User",
    reply_to: Message | None = None,
) -> Message:
    return Message(
        message_id=101,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name=full_name, username=username),
        text=text,
        reply_to_message=reply_to,
    )


class FeedbackAndAppCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_feedback.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

        self.mock_settings = MagicMock(spec=Settings)
        self.mock_settings.telegram_bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        self.mock_settings.telegram_proxy = ""
        self.mock_settings.vk_bot_token = "vk_test_token"
        self.mock_settings.vk_disable_ssl_verify = True
        self.admin_id = 999001
        self.user_id = 111002
        self.mock_settings.admin_telegram_ids = [self.admin_id]
        self.mock_settings.limited_admin_telegram_ids = []
        self.mock_settings.admin_telegram_id = self.admin_id
        self.mock_settings.admin_vk_id = 888001
        self.mock_settings.schedule_url = "http://localhost/schedule"
        self.mock_settings.database_path = self.db_path
        self.mock_settings.miniapp_url = "https://miniapp.opk.nekoty.ru"
        self.mock_settings.lesson_counters_enabled = False
        self.mock_settings.lesson_counters_path = Path(self.temp_dir.name) / "counters.json"

        self.broadcaster = MagicMock()
        self.broadcaster.deliver = AsyncMock()
        self.broadcaster.notify_admins = AsyncMock()

        self.dispatcher = build_dispatcher(
            settings=self.mock_settings,
            db=self.db,
            parser=MagicMock(),
            broadcaster=self.broadcaster,
            group_catalog=MagicMock(),
            search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_database_feedback_lifecycle(self):
        fb_id = await self.db.create_feedback_message(
            platform="telegram",
            user_id=12345,
            user_name="Иван Тестов",
            message_type="bugreport",
            message_text="Не открывается вторник для группы ИСП-25-1",
        )
        self.assertGreater(fb_id, 0)

        record = await self.db.get_feedback_message(fb_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["platform"], "telegram")
        self.assertEqual(record["user_id"], 12345)
        self.assertEqual(record["message_type"], "bugreport")
        self.assertEqual(record["status"], "open")
        self.assertIsNone(record["reply_text"])

        latest = await self.db.get_latest_open_feedback("telegram", 12345)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], fb_id)

        answered = await self.db.answer_feedback_message(fb_id, "Спасибо, ошибку исправили!")
        self.assertTrue(answered)

        record_after = await self.db.get_feedback_message(fb_id)
        self.assertEqual(record_after["status"], "answered")
        self.assertEqual(record_after["reply_text"], "Спасибо, ошибку исправили!")
        self.assertIsNotNone(record_after["replied_at"])

    async def test_telegram_app_command_sends_miniapp_link(self):
        bot = MagicMock(spec=Bot)
        bot.send_message = AsyncMock(return_value=create_mock_message(self.user_id, self.user_id, "sent"))

        msg = create_mock_message(
            chat_id=self.user_id,
            user_id=self.user_id,
            text="/app",
            username="student",
            full_name="Student Test",
        )
        update = Update(update_id=1001, message=msg)

        await self.dispatcher.feed_update(bot=bot, update=update)

        bot.send_message.assert_called_once()
        call_args = bot.send_message.call_args
        self.assertIn("Расписание пар ОПК", call_args.args[1])
        reply_markup = call_args.kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)
        self.assertTrue(len(reply_markup.inline_keyboard) > 0)

    async def test_telegram_bugreport_command_saves_and_notifies_admin(self):
        bot = MagicMock(spec=Bot)
        bot.send_message = AsyncMock(return_value=create_mock_message(self.admin_id, self.admin_id, "admin_notif"))

        msg = create_mock_message(
            chat_id=self.user_id,
            user_id=self.user_id,
            text="/bugreport Неверный кабинет на 3 паре",
            username="bug_reporter",
            full_name="Bug Hunter",
        )
        update = Update(update_id=1002, message=msg)

        await self.dispatcher.feed_update(bot=bot, update=update)

        # Check DB
        latest = await self.db.get_latest_open_feedback("telegram", self.user_id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["message_type"], "bugreport")
        self.assertEqual(latest["message_text"], "Неверный кабинет на 3 паре")

        # Bot should have sent confirmation to user AND notification to admin
        self.assertGreaterEqual(bot.send_message.call_count, 2)
        admin_call = [call for call in bot.send_message.call_args_list if call.args[0] == self.admin_id]
        self.assertTrue(len(admin_call) > 0)
        self.assertIn("[#", admin_call[0].args[1])
        self.assertIn("3", admin_call[0].args[1])
        self.assertIn("/reply tg", admin_call[0].args[1])

    async def test_telegram_feedback_command_and_admin_reply(self):
        bot = MagicMock(spec=Bot)
        bot.send_message = AsyncMock(return_value=create_mock_message(self.user_id, self.user_id, "ok"))

        # 1. User sends feedback
        user_msg = create_mock_message(
            chat_id=self.user_id,
            user_id=self.user_id,
            text="/feedback Спасибо за отличного бота!",
            username="happy_user",
            full_name="Happy User",
        )
        await self.dispatcher.feed_update(bot=bot, update=Update(update_id=1003, message=user_msg))

        latest = await self.db.get_latest_open_feedback("telegram", self.user_id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["message_type"], "feedback")
        feedback_id = latest["id"]

        # 2. Admin replies via /reply tg <user_id> <reply_text>
        admin_reply_msg = create_mock_message(
            chat_id=self.admin_id,
            user_id=self.admin_id,
            text=f"/reply tg {self.user_id} Рады стараться!",
            username="admin",
            full_name="Admin",
        )
        await self.dispatcher.feed_update(bot=bot, update=Update(update_id=1004, message=admin_reply_msg))

        # Check that user received reply
        user_calls = [
            call for call in bot.send_message.call_args_list
            if call.args[0] == self.user_id and "Ответ от администратора" in call.args[1]
        ]
        self.assertTrue(len(user_calls) > 0)
        self.assertIn("Рады стараться!", user_calls[0].args[1])

        # Check DB status is answered
        fb_record = await self.db.get_feedback_message(feedback_id)
        self.assertEqual(fb_record["status"], "answered")
        self.assertEqual(fb_record["reply_text"], "Рады стараться!")

    async def test_telegram_non_admin_cannot_use_reply(self):
        bot = MagicMock(spec=Bot)
        bot.send_message = AsyncMock(return_value=create_mock_message(self.user_id, self.user_id, "ok"))

        fake_admin_msg = create_mock_message(
            chat_id=self.user_id,
            user_id=self.user_id,
            text=f"/reply tg {self.user_id} Взлом",
            username="hacker",
            full_name="Hacker",
        )
        await self.dispatcher.feed_update(bot=bot, update=Update(update_id=1005, message=fake_admin_msg))

        # User must NOT receive any message with "Взлом"
        user_calls = [
            call for call in bot.send_message.call_args_list
            if "Взлом" in str(call)
        ]
        self.assertEqual(len(user_calls), 0)

    async def test_vk_bot_app_and_feedback(self):
        vk_bot = build_vk_bot(
            settings=self.mock_settings,
            db=self.db,
            parser=MagicMock(),
            broadcaster=self.broadcaster,
            group_catalog=MagicMock(),
            search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )
        self.assertIsNotNone(vk_bot)
        # Test DB feedback creation from VK
        fb_id = await self.db.create_feedback_message(
            platform="vk",
            user_id=555123,
            user_name="VK User",
            message_type="bugreport",
            message_text="Ошибка с расписанием в ВК",
        )
        self.assertGreater(fb_id, 0)
        record = await self.db.get_feedback_message(fb_id)
        self.assertEqual(record["platform"], "vk")
        self.assertEqual(record["message_text"], "Ошибка с расписанием в ВК")

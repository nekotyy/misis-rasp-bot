from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import Settings
from src.db import Database
from src.models import DaySchedule, Lesson
from src.schedule_archive import ScheduleDayArchive
from src.subscription_utils import make_group_subscription
from src.telegram_bot import build_dispatcher
from src.vk_bot import build_vk_bot


class HistoryBotsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "bot.db")
        await self.db.initialize()
        self.settings = Settings.from_env()
        self.settings.database_path = self.db.path
        self.settings.telegram_bot_token = "123456:FAKE-TOKEN"
        self.settings.vk_bot_token = "fake-token"
        self.settings.admin_telegram_ids = []
        self.settings.limited_admin_telegram_ids = []
        self.settings.admin_vk_id = None
        self.settings.ocr_enabled = False
        self.settings.lesson_counters_enabled = False
        for platform in ("telegram", "vk"):
            await self.db.upsert_user(platform, 42, None, "Тест")
            await self.db.set_user_subscription(platform, 42, **make_group_subscription("Группа 1", 1))
        archive = ScheduleDayArchive(self.db.path)
        for key, subject in (("group:1", "Физика"), ("group:2", "Химия")):
            await archive.save_day(key, "group", DaySchedule("Пн", "2026-11-02", [Lesson(1, subject, "Иванов", "101")]),
                                   observed_at=datetime(2026, 11, 2, tzinfo=UTC), origin="site", completeness="unknown")
        self.catalog = MagicMock(find_group=AsyncMock(return_value=SimpleNamespace(group_name="Группа 2", schedule_id=2)))
        self.today = patch("src.telegram_bot.today_in_timezone", return_value=date(2026, 11, 15))
        self.today.start()
        self.addCleanup(self.today.stop)
        self.vk_today = patch("src.vk_bot.today_in_timezone", return_value=date(2026, 11, 15))
        self.vk_today.start()
        self.addCleanup(self.vk_today.stop)

    async def test_telegram_date_navigation_and_group_search(self) -> None:
        dispatcher = build_dispatcher(self.settings, self.db, MagicMock(), None, self.catalog)
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(message_id=1)
        user = SimpleNamespace(id=42, first_name="Тест", last_name=None, username=None)
        message = SimpleNamespace(text="", bot=bot, delete=AsyncMock(), chat=SimpleNamespace(id=42, type="private"), from_user=user)
        schedule = next(h.callback for h in dispatcher.callback_query.handlers if h.callback.__name__ == "handle_schedule_callback")
        history = next(h.callback for h in dispatcher.callback_query.handlers if h.callback.__name__ == "handle_history_callback")
        text_handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == "handle_text_message")

        async def click(handler, data):
            await handler(SimpleNamespace(data=data, message=message, from_user=user, bot=bot, answer=AsyncMock()))

        await click(schedule, "schedule:history")
        await click(history, "history:date")
        message.text = "02.11.2026"
        await text_handler(message)
        self.assertIn("Физика", bot.send_message.call_args.args[1])
        await click(history, "history:group")
        message.text = "Группа 2"
        await text_handler(message)
        await click(history, "history:date")
        message.text = "02.11.2026"
        await text_handler(message)
        self.assertIn("Химия", bot.send_message.call_args.args[1])
        await click(history, "history:next")
        self.assertNotIn("Химия", bot.send_message.call_args.args[1])
        self.catalog.find_group.side_effect = RuntimeError("offline")
        await click(history, "history:group")
        message.text = "Группа 2"
        await text_handler(message)
        self.assertIn("Не удалось проверить группу", bot.send_message.call_args.args[1])

    async def test_vk_date_navigation_and_group_search(self) -> None:
        bot = build_vk_bot(self.settings, self.db, MagicMock(), None, self.catalog)

        async def fake_request(method, params, version=None):
            if method == "users.get":
                return {"response": []}
            if method == "messages.send":
                return {"response": 1}
            return {"response": None}

        bot.api.request = AsyncMock(side_effect=fake_request)
        handler = next(h.handler for h in bot.labeler.message_view.handlers if h.handler.__name__ == "all_messages_handler")

        async def send(text):
            await handler(SimpleNamespace(peer_id=42, from_id=42, text=text, action=None, attachments=[]))

        await send("История расписания")
        await send("Выбрать дату")
        await send("02.11.2026")
        sent = [c.args[1]["message"] for c in bot.api.request.call_args_list if c.args[0] == "messages.send"]
        self.assertIn("Физика", sent[-1])
        await send("Другая группа")
        await send("Группа 2")
        await send("Выбрать дату")
        await send("02.11.2026")
        sent = [c.args[1]["message"] for c in bot.api.request.call_args_list if c.args[0] == "messages.send"]
        self.assertIn("Химия", sent[-1])
        await send("Следующая неделя →")
        sent = [c.args[1]["message"] for c in bot.api.request.call_args_list if c.args[0] == "messages.send"]
        self.assertNotIn("Химия", sent[-1])
        self.catalog.find_group.side_effect = RuntimeError("offline")
        await send("Другая группа")
        await send("Группа 2")
        sent = [c.args[1]["message"] for c in bot.api.request.call_args_list if c.args[0] == "messages.send"]
        self.assertIn("Не удалось проверить группу", sent[-1])

    async def test_vk_stale_history_button_after_restart_explains_how_to_reopen(self) -> None:
        bot = build_vk_bot(self.settings, self.db, MagicMock(), None, self.catalog)

        async def fake_request(method, params, version=None):
            if method == "users.get":
                return {"response": []}
            return {"response": 1}

        bot.api.request = AsyncMock(side_effect=fake_request)
        handler = next(h.handler for h in bot.labeler.message_view.handlers if h.handler.__name__ == "all_messages_handler")
        await handler(SimpleNamespace(peer_id=42, from_id=42, text="← Предыдущая неделя", action=None, attachments=[]))
        sent = [c.args[1]["message"] for c in bot.api.request.call_args_list if c.args[0] == "messages.send"]
        self.assertIn("Открой историю расписания заново", sent[-1])

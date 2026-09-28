from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import Settings
from src.db import Database
from src.lesson_counters import LessonCounterSyncResult
from src.subscription_utils import make_group_subscription
from src.telegram_bot import build_dispatcher
from src.vk_bot import build_vk_bot

FULL_ADMIN_ID = 111
LIMITED_ADMIN_ID = 222
CHAT_ID = 111


class TelegramAdminCallbacksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.db = Database(self.db_path)
        await self.db.initialize()
        for user_id in (FULL_ADMIN_ID, LIMITED_ADMIN_ID):
            await self.db.upsert_user("telegram", user_id, None, "Админ")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _dispatcher(self, *, counters_enabled: bool = True):
        settings = MagicMock(spec=Settings)
        settings.telegram_bot_token = "123456:FAKE-TOKEN"
        settings.vk_bot_token = "vk_fake_token"
        settings.vk_disable_ssl_verify = False
        settings.admin_telegram_ids = [FULL_ADMIN_ID]
        settings.limited_admin_telegram_ids = [LIMITED_ADMIN_ID]
        settings.admin_vk_id = None
        settings.schedule_url = "http://localhost/schedule"
        settings.database_path = self.db_path
        settings.lesson_counters_enabled = counters_enabled
        settings.lesson_counters_path = Path(self._tmp.name) / "lesson_counters.json"
        return build_dispatcher(
            settings=settings, db=self.db,
            parser=MagicMock(), broadcaster=MagicMock(),
            group_catalog=MagicMock(), search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )

    @staticmethod
    def _admin_handler(dispatcher):
        for handler in dispatcher.callback_query.handlers:
            if handler.callback.__name__ == "handle_admin_callback":
                return handler.callback
        raise AssertionError("handle_admin_callback не зарегистрирован")

    @staticmethod
    def _callback(data: str, user_id: int = FULL_ADMIN_ID):
        message = SimpleNamespace(chat=SimpleNamespace(id=CHAT_ID), message_id=7, edit_text=AsyncMock())
        return SimpleNamespace(
            data=data,
            message=message,
            from_user=SimpleNamespace(id=user_id, first_name="Админ", last_name=None, username=None),
            bot=AsyncMock(),
            answer=AsyncMock(),
        )

    @staticmethod
    def _last_edit(callback) -> tuple[str, list[str]]:
        call = callback.message.edit_text.await_args
        markup = call.kwargs.get("reply_markup")
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []
        return call.args[0], callbacks

    async def test_section_button_opens_section_with_its_actions(self) -> None:
        handler = self._admin_handler(self._dispatcher())
        callback = self._callback("admin:sec:counters")

        await handler(callback)

        text, callbacks = self._last_edit(callback)
        self.assertIn("Счётчики пар", text)
        self.assertIn("admin:counter_sync", callbacks)
        self.assertIn("admin:back", callbacks)

    async def test_limited_admin_cannot_open_full_only_section(self) -> None:
        handler = self._admin_handler(self._dispatcher())
        callback = self._callback("admin:sec:counters", user_id=LIMITED_ADMIN_ID)

        await handler(callback)

        callback.message.edit_text.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))

    async def test_limited_admin_can_open_monitoring(self) -> None:
        handler = self._admin_handler(self._dispatcher())
        callback = self._callback("admin:sec:monitor", user_id=LIMITED_ADMIN_ID)

        await handler(callback)

        _, callbacks = self._last_edit(callback)
        self.assertIn("admin:status", callbacks)

    async def test_manual_count_screen_offers_today_and_yesterday(self) -> None:
        handler = self._admin_handler(self._dispatcher())
        callback = self._callback("admin:counter_sync")

        await handler(callback)

        text, callbacks = self._last_edit(callback)
        self.assertIn("Ручной подсчёт", text)
        self.assertEqual(callbacks[:2], ["admin:counter_sync:today", "admin:counter_sync:yesterday"])

    async def test_manual_count_today_runs_sync_and_shows_report(self) -> None:
        handler = self._admin_handler(self._dispatcher())
        callback = self._callback("admin:counter_sync:today")
        result = LessonCounterSyncResult(processed=["ИСП-25-1", "Э-25"], failed=[("ТМ-25-1", "502")])

        with patch("src.telegram_bot.sync_lesson_counters_for_date", AsyncMock(return_value=result)) as sync:
            await handler(callback)

        self.assertEqual(sync.await_args.args[3], datetime.now().date().isoformat())
        text, callbacks = self._last_edit(callback)
        self.assertIn("Учтено групп: 2", text)
        self.assertIn("ТМ-25-1: 502", text)
        self.assertIn("admin:counter_sync", callbacks)

    async def test_manual_count_yesterday_uses_yesterday_date(self) -> None:
        handler = self._admin_handler(self._dispatcher())
        callback = self._callback("admin:counter_sync:yesterday")

        with patch(
            "src.telegram_bot.sync_lesson_counters_for_date", AsyncMock(return_value=LessonCounterSyncResult())
        ) as sync:
            await handler(callback)

        expected = (datetime.now().date() - timedelta(days=1)).isoformat()
        self.assertEqual(sync.await_args.args[3], expected)

    async def test_manual_count_failure_is_reported_not_raised(self) -> None:
        handler = self._admin_handler(self._dispatcher())
        callback = self._callback("admin:counter_sync:today")

        with patch("src.telegram_bot.sync_lesson_counters_for_date", AsyncMock(side_effect=RuntimeError("boom"))):
            await handler(callback)

        text, _ = self._last_edit(callback)
        self.assertIn("Не удалось выполнить подсчёт", text)

    async def test_manual_count_is_blocked_when_counters_disabled(self) -> None:
        handler = self._admin_handler(self._dispatcher(counters_enabled=False))
        callback = self._callback("admin:counter_sync:today")

        with patch("src.telegram_bot.sync_lesson_counters_for_date", AsyncMock()) as sync:
            await handler(callback)

        sync.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))

    async def test_limited_admin_cannot_run_manual_count(self) -> None:
        handler = self._admin_handler(self._dispatcher())
        callback = self._callback("admin:counter_sync:today", user_id=LIMITED_ADMIN_ID)

        with patch("src.telegram_bot.sync_lesson_counters_for_date", AsyncMock()) as sync:
            await handler(callback)

        sync.assert_not_awaited()

    async def test_second_click_while_running_is_rejected(self) -> None:
        handler = self._admin_handler(self._dispatcher())
        release = asyncio.Event()

        async def slow_sync(*_args):
            await release.wait()
            return LessonCounterSyncResult(processed=["ИСП-25-1"])

        first = self._callback("admin:counter_sync:today")
        second = self._callback("admin:counter_sync:today")
        with patch("src.telegram_bot.sync_lesson_counters_for_date", slow_sync):
            running = asyncio.create_task(handler(first))
            await asyncio.sleep(0.05)
            await handler(second)
            release.set()
            await running

        second.message.edit_text.assert_not_awaited()
        self.assertTrue(second.answer.await_args.kwargs.get("show_alert"))
        self.assertIn("Учтено групп: 1", self._last_edit(first)[0])


VK_ADMIN_ID = 504200420
VK_STRANGER_ID = 999


class VkAdminMenuAndManualCountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.db = Database(self.db_path)
        await self.db.initialize()
        for user_id in (VK_ADMIN_ID, VK_STRANGER_ID):
            await self.db.upsert_user("vk", user_id, None, "Пользователь")
            # Без подписки VK-бот трактует любой текст как ввод названия группы.
            await self.db.set_user_subscription("vk", user_id, **make_group_subscription("ИСП-25-1", schedule_id=600))

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _bot(self, *, counters_enabled: bool = True):
        settings = MagicMock(spec=Settings)
        settings.telegram_bot_token = "123456:FAKE-TOKEN"
        settings.vk_bot_token = "vk_fake_token"
        settings.vk_disable_ssl_verify = False
        settings.admin_telegram_ids = []
        settings.limited_admin_telegram_ids = []
        settings.admin_vk_id = VK_ADMIN_ID
        settings.schedule_url = "http://localhost/schedule"
        settings.database_path = self.db_path
        settings.lesson_counters_enabled = counters_enabled
        settings.lesson_counters_path = Path(self._tmp.name) / "lesson_counters.json"
        bot = build_vk_bot(
            settings=settings, db=self.db,
            parser=MagicMock(), broadcaster=None,
            group_catalog=None, search_catalog=None, schedule_jobs=None,
        )

        async def fake_request(method: str, params: dict, version: str | None = None):
            if method == "users.get":
                return {"response": []}
            if method == "messages.send":
                return {"response": [{"peer_id": 0, "message_id": 1}]}
            return {"response": None}

        bot.api.request = AsyncMock(side_effect=fake_request)
        return bot

    @staticmethod
    def _handler(bot):
        for handler in bot.labeler.message_view.handlers:
            if getattr(handler.handler, "__name__", "") == "all_messages_handler":
                return handler.handler
        raise AssertionError("all_messages_handler не зарегистрирован")

    @staticmethod
    def _message(user_id: int, text: str) -> SimpleNamespace:
        return SimpleNamespace(peer_id=user_id, from_id=user_id, text=text, action=None, attachments=[])

    @staticmethod
    def _last_screen(bot) -> tuple[str, list[str]]:
        params = [c.args[1] for c in bot.api.request.call_args_list if c.args[0] == "messages.send"][-1]
        keyboard = json.loads(params["keyboard"]) if params.get("keyboard") else {"buttons": []}
        labels = [b["action"]["label"] for row in keyboard["buttons"] for b in row]
        return params["message"], labels

    async def test_admin_main_menu_shows_only_sections(self) -> None:
        bot = self._bot()

        await self._handler(bot)(self._message(VK_ADMIN_ID, "Админка"))

        text, labels = self._last_screen(bot)
        self.assertIn("Админ-панель", text)
        self.assertEqual(
            labels,
            ["Мониторинг", "Расписание и OCR", "Счётчики пар", "Пользователи и рассылки", "Служебное", "Закрыть админку"],
        )

    async def test_section_button_opens_section_and_back_returns_to_admin_menu(self) -> None:
        bot = self._bot()
        handler = self._handler(bot)
        await handler(self._message(VK_ADMIN_ID, "Админка"))

        await handler(self._message(VK_ADMIN_ID, "Счётчики пар"))
        _, section_labels = self._last_screen(bot)
        self.assertIn("Ручной подсчёт", section_labels)

        await handler(self._message(VK_ADMIN_ID, "Назад в админку"))
        text, labels = self._last_screen(bot)
        self.assertIn("Админ-панель", text)
        self.assertIn("Счётчики пар", labels)

    async def test_manual_count_offers_today_and_yesterday(self) -> None:
        bot = self._bot()
        handler = self._handler(bot)
        await handler(self._message(VK_ADMIN_ID, "Админка"))
        await handler(self._message(VK_ADMIN_ID, "Счётчики пар"))

        await handler(self._message(VK_ADMIN_ID, "Ручной подсчёт"))

        text, labels = self._last_screen(bot)
        self.assertIn("Ручной подсчёт пар", text)
        self.assertEqual(labels[:2], ["Подсчёт за сегодня", "Подсчёт за вчера"])

    async def test_manual_count_today_runs_sync_and_shows_report(self) -> None:
        bot = self._bot()
        handler = self._handler(bot)
        result = LessonCounterSyncResult(processed=["ИСП-25-1"], failed=[("ТМ-25-1", "502")])

        with patch("src.vk_bot.sync_lesson_counters_for_date", AsyncMock(return_value=result)) as sync:
            await handler(self._message(VK_ADMIN_ID, "Подсчёт за сегодня"))

        self.assertEqual(sync.await_args.args[3], datetime.now().date().isoformat())
        text, labels = self._last_screen(bot)
        self.assertIn("Учтено групп: 1", text)
        self.assertIn("ТМ-25-1: 502", text)
        self.assertIn("Ручной подсчёт", labels)

    async def test_manual_count_yesterday_uses_yesterday_date(self) -> None:
        bot = self._bot()

        with patch(
            "src.vk_bot.sync_lesson_counters_for_date", AsyncMock(return_value=LessonCounterSyncResult())
        ) as sync:
            await self._handler(bot)(self._message(VK_ADMIN_ID, "Подсчёт за вчера"))

        expected = (datetime.now().date() - timedelta(days=1)).isoformat()
        self.assertEqual(sync.await_args.args[3], expected)

    async def test_manual_count_failure_is_reported_not_raised(self) -> None:
        bot = self._bot()

        with patch("src.vk_bot.sync_lesson_counters_for_date", AsyncMock(side_effect=RuntimeError("boom"))):
            await self._handler(bot)(self._message(VK_ADMIN_ID, "Подсчёт за сегодня"))

        self.assertIn("Не удалось выполнить подсчёт", self._last_screen(bot)[0])

    async def test_manual_count_is_blocked_when_counters_disabled(self) -> None:
        bot = self._bot(counters_enabled=False)

        with patch("src.vk_bot.sync_lesson_counters_for_date", AsyncMock()) as sync:
            await self._handler(bot)(self._message(VK_ADMIN_ID, "Подсчёт за сегодня"))

        sync.assert_not_awaited()
        self.assertIn("выключены", self._last_screen(bot)[0])

    async def test_non_admin_cannot_run_manual_count(self) -> None:
        bot = self._bot()

        with patch("src.vk_bot.sync_lesson_counters_for_date", AsyncMock()) as sync:
            await self._handler(bot)(self._message(VK_STRANGER_ID, "Подсчёт за сегодня"))

        sync.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

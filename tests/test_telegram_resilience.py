"""Telegram: кнопки не «висят», права ограниченного админа, мониторинг поллинга."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.methods import GetMe, GetUpdates

from src.config import Settings
from src.db import Database
from src.main import TrackingAiohttpSession, telegram_polling_probe
from src.telegram_bot import build_dispatcher

FULL_ADMIN_ID = 1001
LIMITED_ADMIN_ID = 1002


class TelegramResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "tg.db"
        self.db = Database(self.db_path)
        await self.db.initialize()
        settings = MagicMock(spec=Settings)
        settings.telegram_bot_token = "123456:FAKE-TOKEN"
        settings.admin_telegram_ids = [FULL_ADMIN_ID]
        settings.limited_admin_telegram_ids = [LIMITED_ADMIN_ID]
        settings.admin_vk_id = None
        settings.schedule_url = "http://localhost/schedule"
        settings.database_path = self.db_path
        settings.lesson_counters_enabled = False
        settings.lesson_counters_path = Path(self._tmp.name) / "lesson_counters.json"
        self.schedule_jobs = MagicMock()
        self.schedule_jobs.enqueue_or_run_db_cleanup = AsyncMock()
        self.dispatcher = build_dispatcher(
            settings=settings,
            db=self.db,
            parser=MagicMock(),
            broadcaster=MagicMock(),
            group_catalog=MagicMock(),
            search_catalog=MagicMock(),
            schedule_jobs=self.schedule_jobs,
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _message_handler(self, name: str):
        return next(item.callback for item in self.dispatcher.message.handlers if item.callback.__name__ == name)

    @staticmethod
    def _message(user_id: int, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=user_id, type="private"),
            from_user=SimpleNamespace(id=user_id, first_name="A", last_name=None, username=None),
            bot=AsyncMock(),
        )

    async def test_limited_admin_cannot_clean_db(self) -> None:
        await self._message_handler("handle_cleandb_command")(self._message(LIMITED_ADMIN_ID, "/cleandb"))
        self.schedule_jobs.enqueue_or_run_db_cleanup.assert_not_awaited()

        await self._message_handler("handle_cleandb_command")(self._message(FULL_ADMIN_ID, "/cleandb"))
        self.schedule_jobs.enqueue_or_run_db_cleanup.assert_awaited_once()

    async def test_limited_admin_cannot_refund_donations(self) -> None:
        self.db.get_star_donation = AsyncMock()
        await self._message_handler("handle_dnremove_command")(self._message(LIMITED_ADMIN_ID, "/dnremove 1"))
        self.db.get_star_donation.assert_not_awaited()

    async def test_slow_callback_is_answered_early_and_only_once(self) -> None:
        middleware = self.dispatcher.callback_query.outer_middleware._middlewares[0]
        callback = SimpleNamespace(id="cb-1", answer=AsyncMock())
        handler_answer = AsyncMock()

        async def slow_handler(event, data):
            await asyncio.sleep(0.05)
            # Обработчик сам пытается ответить позже — второй ответ не отправляется.
            return handler_answer

        with patch("src.telegram_bot.CALLBACK_AUTO_ANSWER_SECONDS", 0.01):
            await middleware(slow_handler, callback, {})
        callback.answer.assert_awaited_once()

    async def test_fast_callback_is_not_auto_answered(self) -> None:
        middleware = self.dispatcher.callback_query.outer_middleware._middlewares[0]
        callback = SimpleNamespace(id="cb-2", answer=AsyncMock())

        async def fast_handler(event, data):
            return None

        await middleware(fast_handler, callback, {})
        await asyncio.sleep(0.02)
        callback.answer.assert_not_awaited()


class TelegramPollingProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_tracks_successful_get_updates(self) -> None:
        session = TrackingAiohttpSession()
        probe = telegram_polling_probe(session)
        self.assertTrue(probe()[0])

        with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", AsyncMock(return_value=[])):
            await session.make_request(MagicMock(), GetMe())
            self.assertIsNone(session.last_updates_ok_at)
            await session.make_request(MagicMock(), GetUpdates())
        self.assertIsNotNone(session.last_updates_ok_at)

        session.last_updates_ok_at -= 10_000
        alive, details = probe()
        self.assertFalse(alive)
        self.assertIn("getUpdates", details)
        await session.close()


if __name__ == "__main__":
    unittest.main()

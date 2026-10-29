from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from time import time
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from src.db import Database
from src.delivery_outbox import DeliveryExpiredError, DeliveryOutbox
from src.notifier import CAMPAIGN_NOTIFICATION, Broadcaster
from src.outbox_context import delivery_context
from src.telegram_runtime import TrackingAiohttpSession
from src.vk_runtime import build_vk_api


class FakeBroker:
    enabled = True

    def __init__(self, *, unavailable=False):
        self.unavailable = unavailable
        self.callback = None
        self.published = []
        self.tasks = set()

    async def start_consumer(self, callback):
        if self.unavailable:
            raise ConnectionError("RabbitMQ offline")
        self.callback = callback

    async def publish(self, platform, request_id):
        self.published.append((platform, request_id))
        task = asyncio.create_task(self.callback(request_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class DeliveryOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")
        await self.db.initialize()
        self.services = []
        self.sessions = []

    async def asyncTearDown(self):
        for service in self.services:
            await service.stop()
        for session in self.sessions:
            await session.close()
        self.tmp.cleanup()

    def make(self, broker=None):
        service = DeliveryOutbox(self.db, broker, poll_interval=0.01)
        self.services.append(service)
        return service

    def telegram(self, service):
        session = TrackingAiohttpSession()
        self.sessions.append(session)
        bot = Bot("123456:FAKE-TOKEN", session=session, default=DefaultBotProperties(parse_mode="HTML"))
        service.bind_telegram(bot)
        session.direct_request = AsyncMock(return_value={
            "message_id": 7, "date": int(time()), "chat": {"id": 1, "type": "private"}, "text": "ответ",
        })
        return bot

    async def test_real_bot_session_uses_outbox_and_returns_real_message_with_keyboard(self):
        broker = FakeBroker()
        service = self.make(broker)
        bot = self.telegram(service)
        await service.start()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="back")]])
        result = await asyncio.wait_for(bot.send_message(1, "<b>Ответ</b>", reply_markup=keyboard), timeout=3)
        self.assertEqual(result.message_id, 7)
        method = bot.session.direct_request.await_args.args[1]
        self.assertEqual(method.parse_mode, "HTML")
        self.assertEqual(method.reply_markup.inline_keyboard[0][0].text, "Назад")
        self.assertEqual(len(broker.published), 1)
        self.assertEqual(await service.store.counts(), {"sent": 1})

    async def test_broker_unavailable_falls_back_to_durable_sqlite_delivery(self):
        service = self.make(FakeBroker(unavailable=True))
        bot = self.telegram(service)
        await service.start()
        result = await asyncio.wait_for(bot.send_message(1, "x"), timeout=3)
        self.assertEqual(result.message_id, 7)
        self.assertFalse(service._broker_ok)
        self.assertEqual(await service.store.counts(), {"sent": 1})

    async def test_cancelled_waiter_and_stopped_worker_leave_request_for_new_process(self):
        service = self.make()
        bot = self.telegram(service)
        sending = asyncio.Event()

        async def hung(*args):
            sending.set()
            await asyncio.Event().wait()

        bot.session.direct_request.side_effect = hung
        await service.start()
        caller = asyncio.create_task(bot.send_message(1, "После рестарта"))
        await asyncio.wait_for(sending.wait(), timeout=3)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await service.stop()
        self.assertEqual(await service.store.counts(), {"pending": 1})
        restarted = self.make()
        recovered = self.telegram(restarted)
        rows = await restarted.store.reserve_due("telegram")
        self.assertEqual(len(rows), 1)
        await restarted.process(rows[0])
        self.assertEqual(recovered.session.direct_request.await_args.args[1].text, "После рестарта")

    async def test_database_failure_after_api_success_does_not_mark_request_permanently_failed(self):
        service = self.make()
        bot = self.telegram(service)
        row = await service.enqueue_telegram(bot, SendMessage(chat_id=1, text="x"))
        with (patch.object(service.store, "finish", AsyncMock(side_effect=sqlite3.OperationalError("disk busy"))),
              self.assertRaises(sqlite3.OperationalError)):
            await service.process(row["delivery_id"])
        self.assertEqual((await service.store.get(row["delivery_id"]))["status"], "processing")

    async def test_late_consumer_callback_after_shutdown_preserves_job_for_restart(self):
        service = self.make()
        bot = self.telegram(service)
        row = await service.enqueue_telegram(bot, SendMessage(chat_id=1, text="Поздний callback"))
        await service.stop()
        await service.process(row["delivery_id"])
        bot.session.direct_request.assert_not_awaited()
        self.assertEqual((await service.store.get(row["delivery_id"]))["status"], "pending")
        await service.start()
        result = await asyncio.wait_for(service.wait(row["delivery_id"]), timeout=3)
        self.assertEqual(result["message_id"], 7)
        bot.session.direct_request.assert_awaited_once()

    async def test_platform_failure_does_not_block_vk_delivery(self):
        service = self.make()
        bot = self.telegram(service)
        method = SendMessage(chat_id=1, text="x")
        bot.session.direct_request.side_effect = TelegramNetworkError(method, "offline")
        row = await service.enqueue_telegram(bot, method)
        await service.process(row["delivery_id"])
        service._senders["vk"] = AsyncMock(return_value={"response": 77})
        other = await service.enqueue_vk("messages.send", {"peer_id": 1, "message": "VK работает", "random_id": 3})
        await service.process(other["delivery_id"])
        self.assertEqual((await service.store.get(other["delivery_id"]))["status"], "sent")

    async def test_all_broadcast_recipients_are_saved_before_progress_callback(self):
        service = self.make()
        bot = self.telegram(service)
        for user_id in (1, 2, 3):
            await self.db.upsert_user("telegram", user_id, None, "Студент", subscription_type="group", subscription_key="group:1")
        broadcaster = Broadcaster(self.db, telegram_bot=bot, outbox=service)

        async def report(progress):
            self.assertEqual(await service.store.counts(), {"pending": 3})
            self.assertEqual(progress.queued_count, 3)
            self.assertEqual(progress.success_count, 0)

        progress = await broadcaster.broadcast("Важное сообщение", progress_callback=report)
        self.assertTrue(progress.is_finished)
        self.assertEqual(progress.queued_count, 3)
        bot.session.direct_request.assert_not_awaited()

    async def test_vk_long_text_is_fully_saved_before_first_delivery_wait(self):
        from src.vk_runtime import vk_send_message
        service = self.make()
        api = build_vk_api("FAKE-TOKEN")
        service.bind_vk(api)
        call = asyncio.create_task(vk_send_message(api, 1, "x" * 9000, keyboard="{}"))
        try:
            for _ in range(100):
                if (await service.store.counts()).get("pending") == 3:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(await service.store.counts(), {"pending": 3})
            async with self.db._connect() as connection:
                rows = await (await connection.execute("SELECT payload_json FROM delivery_outbox ORDER BY id")).fetchall()
            self.assertNotIn("keyboard", json.loads(rows[0][0]))
            self.assertIn("keyboard", json.loads(rows[-1][0]))
        finally:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)

    async def test_sqlite_attachment_snapshot_is_consistent_with_wal(self):
        service = self.make()
        bot = self.telegram(service)
        await self.db.upsert_user("telegram", 42, None, "Сохранён в WAL")
        row = await service.enqueue_telegram(bot, SendDocument(chat_id=1, document=FSInputFile(self.db.path)))
        snapshot = service.files.decode(json.loads(row["payload_json"]))["document"].path
        with closing(sqlite3.connect(snapshot)) as backup:
            self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("SELECT full_name FROM users WHERE user_id = 42").fetchone()[0], "Сохранён в WAL")

    async def test_request_is_saved_before_any_network_attempt(self):
        service = self.make()
        bot = self.telegram(service)
        row = await service.enqueue_telegram(bot, SendMessage(chat_id=1, text="Сохранить"))
        bot.session.direct_request.assert_not_awaited()
        self.assertEqual(row["status"], "pending")
        await service.process(row["delivery_id"])
        self.assertEqual((await service.store.get(row["delivery_id"]))["status"], "sent")

    async def test_hour_outage_then_restart_delivers_original_payload(self):
        first = self.make()
        bot = self.telegram(first)
        method = SendMessage(chat_id=1, text="Ответ до сбоя")
        row = await first.enqueue_telegram(bot, method)
        bot.session.direct_request.side_effect = TelegramNetworkError(method, "hosting offline")
        await first.process(row["delivery_id"])
        self.assertEqual((await first.store.get(row["delivery_id"]))["status"], "pending")
        restarted = self.make()
        recovered_bot = self.telegram(restarted)
        with patch("src.outbox_store.time", return_value=time() + 3600), patch("src.delivery_outbox.time", return_value=time() + 3600):
            await restarted.process(row["delivery_id"])
        self.assertEqual(recovered_bot.session.direct_request.await_args.args[1].text, "Ответ до сбоя")
        self.assertEqual((await restarted.store.get(row["delivery_id"]))["status"], "sent")

    async def test_same_event_replay_returns_saved_result_without_second_send(self):
        service = self.make()
        bot = self.telegram(service)
        await service.start()
        for _ in range(2):
            with delivery_context("telegram:123456:42"):
                result = await asyncio.wait_for(bot.send_message(1, "Один ответ"), timeout=3)
                self.assertEqual(result.message_id, 7)
        bot.session.direct_request.assert_awaited_once()

    async def test_file_snapshot_survives_deletion_of_original(self):
        service = self.make()
        bot = self.telegram(service)
        original = Path(self.tmp.name) / "report.txt"
        original.write_text("важные данные", encoding="utf-8")
        row = await service.enqueue_telegram(bot, SendDocument(chat_id=1, document=FSInputFile(original)))
        original.unlink()
        await service.process(row["delivery_id"])
        method = bot.session.direct_request.await_args.args[1]
        self.assertEqual(Path(method.document.path).read_text(encoding="utf-8"), "важные данные")
        self.assertEqual(method.document.filename, "report.txt")

    async def test_terminal_error_is_restored_for_existing_ux_fallback(self):
        service = self.make()
        bot = self.telegram(service)
        method = SendMessage(chat_id=1, text="x")
        bot.session.direct_request.side_effect = TelegramForbiddenError(method, "bot was blocked by the user")
        await service.start()
        with self.assertRaises(TelegramForbiddenError):
            await asyncio.wait_for(bot.send_message(1, "x"), timeout=3)
        bot.session.direct_request.assert_awaited_once()
        self.assertEqual(await service.store.counts(), {"failed": 1})

    async def test_retry_after_is_saved_without_shortening(self):
        service = self.make()
        bot = self.telegram(service)
        method = SendMessage(chat_id=1, text="x")
        bot.session.direct_request.side_effect = TelegramRetryAfter(method, "flood", 3600)
        row = await service.enqueue_telegram(bot, method)
        before = time()
        await service.process(row["delivery_id"])
        saved = await service.store.get(row["delivery_id"])
        self.assertGreaterEqual(saved["next_attempt_at"], before + 3600)
        self.assertEqual(saved["status"], "pending")

    async def test_expired_callback_is_not_replayed_after_hour(self):
        service = self.make()
        bot = self.telegram(service)
        method = AnswerCallbackQuery(callback_query_id="callback")
        with delivery_context("telegram:123456:42"):
            row = await service.enqueue_telegram(bot, method)
        await service.store.reserve_due("telegram", now=time() + 3600)
        with self.assertRaises(DeliveryExpiredError):
            await service.wait(row["delivery_id"])
        with delivery_context("telegram:123456:42"):
            self.assertTrue(await service.execute_telegram(bot, method))
        bot.session.direct_request.assert_not_awaited()

    async def test_notification_is_queued_with_buttons_and_deduplicates_legacy_redelivery(self):
        from src.message_broker import OutboundMessage
        service = self.make()
        bot = self.telegram(service)
        await self.db.upsert_user("telegram", 1, None, "Студент")
        broadcaster = Broadcaster(self.db, telegram_bot=bot, outbox=service)
        message = OutboundMessage("telegram", 1, "Замены", CAMPAIGN_NOTIFICATION, message_id="legacy-id")
        await broadcaster.deliver(message)
        await broadcaster.deliver(message)
        self.assertEqual(await service.store.counts(), {"pending": 1})
        row = await service.store.get("notification:legacy-id:text:0")
        payload = json.loads(row["payload_json"])
        self.assertIn("Расписание на сегодня", str(payload["reply_markup"]))

    async def test_vk_random_id_survives_worker_restart(self):
        first = self.make()
        api = build_vk_api("FAKE-TOKEN")
        first.bind_vk(api)
        row = await first.enqueue_vk("messages.send", {"peer_id": 1, "message": "Ответ", "random_id": 123})
        with patch("vkbottle.API.request", AsyncMock(side_effect=TimeoutError())):
            await first.process(row["delivery_id"])
        restarted = self.make()
        restarted.bind_vk(api)
        with patch("vkbottle.API.request", AsyncMock(return_value={"response": 77})) as request, patch(
            "src.outbox_store.time", return_value=time() + 3600
        ):
            await restarted.process(row["delivery_id"])
        self.assertEqual(request.await_args.args[2]["random_id"], 123)
        self.assertEqual((await restarted.store.get(row["delivery_id"]))["status"], "sent")

    async def test_cleanup_removes_old_rows_and_attachments_only(self):
        service = self.make()
        bot = self.telegram(service)
        original = Path(self.tmp.name) / "keep.txt"
        original.write_text("файл", encoding="utf-8")
        row = await service.enqueue_telegram(bot, SendDocument(chat_id=1, document=FSInputFile(original)))
        snapshot = service.files.decode(json.loads(row["payload_json"]))["document"].path
        future = datetime(2027, 3, 1, tzinfo=UTC).timestamp()
        self.assertEqual(await service.cleanup(now=future), 1)
        self.assertFalse(Path(snapshot).exists())
        self.assertTrue(original.exists())

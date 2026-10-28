"""Telegram переживает недоступность API, не теряет offset и не множит повторы."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetMe, GetUpdates, SendMessage
from aiogram.types import Update
from aiohttp import web

from src.main import telegram_polling_probe
from src.telegram_runtime import TrackingAiohttpSession, telegram_retry_attempts


class TelegramTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = TrackingAiohttpSession()
        self.bot = Bot("123456:FAKE-TOKEN", session=self.session)

    async def asyncTearDown(self) -> None:
        await self.session.close()

    async def test_long_outage_keeps_retrying_same_offset_until_connection_recovers(self) -> None:
        method = GetUpdates(offset=123, timeout=25)
        result = [Update(update_id=123)]
        failures = [
            TelegramNetworkError(method, "Temporary failure in name resolution"),
            TelegramNetworkError(method, "Connection reset by peer"),
            TelegramServerError(method, "Bad Gateway"),
        ] * 8
        request = AsyncMock(side_effect=[*failures, result])
        with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", request), patch(
            "src.telegram_runtime.asyncio.sleep", AsyncMock()
        ) as sleep:
            self.assertEqual(await self.session.make_request(self.bot, method), result)

        self.assertEqual(request.await_count, 25)
        self.assertTrue(all(call.args[1] is method and call.args[1].offset == 123 for call in request.await_args_list))
        delays = [call.args[0] for call in sleep.await_args_list]
        self.assertTrue(all(0 < delay <= 30 for delay in delays))
        self.assertGreater(delays[-1], delays[0])
        self.assertIsNotNone(self.session.last_updates_ok_at)
        self.assertEqual(self.session.consecutive_failures, 0)

    async def test_polling_and_send_respect_retry_after_without_truncating_it(self) -> None:
        for method in (GetUpdates(offset=42, timeout=25), SendMessage(chat_id=1, text="x")):
            with self.subTest(method=method.__api_method__):
                request = AsyncMock(side_effect=[TelegramRetryAfter(method, "Too Many Requests", 120), []])
                with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", request), patch(
                    "src.telegram_runtime.asyncio.sleep", AsyncMock()
                ) as sleep:
                    await self.session.make_request(self.bot, method)
                self.assertGreaterEqual(sleep.await_args.args[0], 120)
                self.assertEqual(request.await_count, 2)

    async def test_api_requests_retry_transient_failures_and_stop_after_three_attempts(self) -> None:
        for method in (GetMe(), SendMessage(chat_id=1, text="x")):
            with self.subTest(method=method.__api_method__):
                error = TelegramServerError(method, "Service Unavailable")
                request = AsyncMock(side_effect=error)
                with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", request), patch(
                    "src.telegram_runtime.asyncio.sleep", AsyncMock()
                ) as sleep, self.assertRaises(TelegramServerError):
                    await self.session.make_request(self.bot, method)
                self.assertEqual(request.await_count, 3)
                self.assertEqual(sleep.await_count, 2)
                self.assertIsNone(self.session.last_updates_ok_at)

    async def test_permanent_errors_are_not_retried_by_transport(self) -> None:
        for method in (GetUpdates(timeout=25), SendMessage(chat_id=1, text="x")):
            for error_class in (TelegramBadRequest, TelegramUnauthorizedError, TelegramForbiddenError, TelegramConflictError):
                with self.subTest(method=method.__api_method__, error=error_class.__name__):
                    request = AsyncMock(side_effect=error_class(method, "permanent"))
                    with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", request), patch(
                        "src.telegram_runtime.asyncio.sleep", AsyncMock()
                    ) as sleep, self.assertRaises(error_class):
                        await self.session.make_request(self.bot, method)
                    request.assert_awaited_once()
                    sleep.assert_not_awaited()

    async def test_timeouts_include_connect_dns_and_long_poll_margin(self) -> None:
        request = AsyncMock(return_value=[])
        with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", request):
            await self.session.make_request(self.bot, GetUpdates(timeout=25), timeout=85)
            timeout = request.await_args.kwargs["timeout"]
            self.assertEqual(timeout.total, 45)
            self.assertEqual(timeout.connect, 15)
            self.assertEqual(timeout.sock_connect, 15)
            self.assertEqual(timeout.sock_read, 45)
            await self.session.make_request(self.bot, GetMe(), timeout=7)
            self.assertEqual(request.await_args.kwargs["timeout"].total, 7)

    async def test_hung_request_is_interrupted_by_deadline_and_retried(self) -> None:
        attempts = 0

        async def hung_then_ok(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.Event().wait()
            return []

        with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", hung_then_ok), patch(
            "src.telegram_runtime.TELEGRAM_POLLING_TIMEOUT_MARGIN", 0.01
        ), patch("src.telegram_runtime.asyncio.sleep", AsyncMock()):
            await asyncio.wait_for(self.session.make_request(self.bot, GetUpdates(offset=10)), timeout=1)
        self.assertEqual(attempts, 2)

    async def test_cancelled_polling_during_backoff_stops_without_restarting(self) -> None:
        sleeping = asyncio.Event()

        async def backoff(delay):
            sleeping.set()
            await asyncio.Event().wait()

        request = AsyncMock(side_effect=TelegramNetworkError(GetUpdates(), "down"))
        with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", request), patch(
            "src.telegram_runtime.asyncio.sleep", backoff
        ):
            task = asyncio.create_task(self.session.make_request(self.bot, GetUpdates()))
            await asyncio.wait_for(sleeping.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        request.assert_awaited_once()
        self.assertIsNone(self.session.last_updates_ok_at)

    async def test_polling_backoff_does_not_block_sending_or_mark_polling_healthy(self) -> None:
        sleeping = asyncio.Event()
        release = asyncio.Event()
        polling_attempts = 0

        async def request(session, bot, method, **kwargs):
            nonlocal polling_attempts
            if isinstance(method, GetUpdates):
                polling_attempts += 1
                if polling_attempts == 1:
                    raise TelegramNetworkError(method, "down")
            return []

        async def backoff(delay):
            sleeping.set()
            await release.wait()

        self.session.started_at -= 1000
        with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", request), patch(
            "src.telegram_runtime.asyncio.sleep", backoff
        ):
            task = asyncio.create_task(self.session.make_request(self.bot, GetUpdates()))
            try:
                await asyncio.wait_for(sleeping.wait(), timeout=1)
                await self.session.make_request(self.bot, SendMessage(chat_id=1, text="x"))
                self.assertFalse(telegram_polling_probe(self.session)()[0])
                self.assertEqual(self.session.consecutive_failures, 1)
            finally:
                release.set()
                await task
        self.assertTrue(telegram_polling_probe(self.session)()[0])

    async def test_outer_retries_are_disabled_only_for_resilient_session(self) -> None:
        self.assertEqual(telegram_retry_attempts(self.bot), 1)
        self.assertEqual(telegram_retry_attempts(MagicMock()), 3)

    async def test_aiogram_keeps_offset_through_network_outage_between_batches(self) -> None:
        requested_offsets = []

        async def request(session, bot, method, **kwargs):
            requested_offsets.append(method.offset)
            if len(requested_offsets) == 1:
                return [Update(update_id=100)]
            if len(requested_offsets) == 2:
                raise TelegramNetworkError(method, "reset")
            return [Update(update_id=101)]

        with patch("aiogram.client.session.aiohttp.AiohttpSession.make_request", request), patch(
            "src.telegram_runtime.asyncio.sleep", AsyncMock()
        ):
            updates = Dispatcher._listen_updates(self.bot, polling_timeout=25)
            try:
                self.assertEqual((await asyncio.wait_for(anext(updates), timeout=1)).update_id, 100)
                self.assertEqual((await asyncio.wait_for(anext(updates), timeout=1)).update_id, 101)
            finally:
                await updates.aclose()
        self.assertEqual(requested_offsets, [None, 101, 101])

    async def test_real_http_handles_html_bad_gateway_then_429_then_delivers_update(self) -> None:
        offsets = []

        async def endpoint(request):
            params = await request.post()
            offsets.append(params.get("offset"))
            if len(offsets) == 1:
                return web.Response(status=502, text="<html>Bad Gateway</html>")
            if len(offsets) == 2:
                return web.json_response(
                    {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 90}},
                    status=429,
                )
            return web.json_response({"ok": True, "result": [{"update_id": 42}]})

        app = web.Application()
        app.router.add_post("/bot{token}/getUpdates", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.session.api = TelegramAPIServer.from_base(f"http://127.0.0.1:{port}")
        try:
            with patch("src.telegram_runtime.asyncio.sleep", AsyncMock()) as sleep:
                updates = await self.bot(GetUpdates(offset=42, timeout=25))
            self.assertEqual([update.update_id for update in updates], [42])
            self.assertEqual(offsets, ["42", "42", "42"])
            self.assertGreaterEqual(sleep.await_args_list[-1].args[0], 90)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()

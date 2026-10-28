from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientConnectionError
from vkbottle.exception_factory.base_exceptions import VKAPIError

from src.vk_runtime import (
    ResilientBotPolling,
    StrictJSONResponseValidator,
    VkTransportError,
    VkUpdateDispatcher,
    build_vk_api,
    is_permanent_vk_delivery_error,
    split_vk_text,
    vk_send_message,
)


def _api_with(side_effect) -> MagicMock:
    api = MagicMock()
    api.request = AsyncMock(side_effect=side_effect)
    return api


class SplitTextTests(unittest.TestCase):
    def test_short_text_is_single_chunk(self) -> None:
        self.assertEqual(split_vk_text("привет"), ["привет"])

    def test_long_text_splits_on_lines_within_limit(self) -> None:
        text = "\n".join(f"строка {index} " + "x" * 50 for index in range(200))
        chunks = split_vk_text(text, limit=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_text_without_breaks_is_hard_cut(self) -> None:
        chunks = split_vk_text("x" * 2500, limit=1000)
        self.assertEqual([len(chunk) for chunk in chunks], [1000, 1000, 500])


class SendMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_peer_id_and_returns_message_id(self) -> None:
        api = _api_with([{"response": 42}])
        message_id = await vk_send_message(api, 5, "текст", keyboard="{}")
        self.assertEqual(message_id, 42)
        method, params = api.request.await_args.args
        self.assertEqual(method, "messages.send")
        self.assertEqual(params["peer_id"], 5)
        self.assertNotIn("peer_ids", params)
        self.assertNotEqual(params["random_id"], 0)

    async def test_network_error_is_retried_with_the_same_random_id(self) -> None:
        api = _api_with([ClientConnectionError("dns"), {"response": 7}])
        with patch("src.vk_runtime.asyncio.sleep", AsyncMock()):
            message_id = await vk_send_message(api, 5, "текст")
        self.assertEqual(message_id, 7)
        first, second = (call.args[1] for call in api.request.await_args_list)
        # Один random_id на повторы — VK отбросит дубль, если первый запрос всё же дошёл.
        self.assertEqual(first["random_id"], second["random_id"])

    async def test_permanent_error_is_not_retried(self) -> None:
        api = _api_with([VKAPIError[901](error_msg="Can't send messages")])
        with self.assertRaises(VKAPIError) as ctx:
            await vk_send_message(api, 5, "текст")
        self.assertTrue(is_permanent_vk_delivery_error(ctx.exception))
        self.assertEqual(api.request.await_count, 1)

    async def test_invalid_keyboard_falls_back_to_plain_text(self) -> None:
        api = _api_with([VKAPIError[911](error_msg="Keyboard format is invalid"), {"response": 9}])
        message_id = await vk_send_message(api, 5, "текст", keyboard="{bad}")
        self.assertEqual(message_id, 9)
        retry_params = api.request.await_args_list[1].args[1]
        self.assertNotIn("keyboard", retry_params)
        self.assertEqual(retry_params["message"], "текст")

    async def test_error_inside_peer_ids_style_response_raises(self) -> None:
        api = _api_with([{"response": [{"peer_id": 5, "error": {"code": 901, "description": "no access"}}]}])
        with self.assertRaises(VKAPIError):
            await vk_send_message(api, 5, "текст")

    async def test_long_message_is_split_and_keyboard_goes_last(self) -> None:
        api = _api_with([{"response": 1}, {"response": 2}])
        await vk_send_message(api, 5, "a" * 3000 + "\n" + "b" * 3000, keyboard="KB")
        first, second = (call.args[1] for call in api.request.await_args_list)
        self.assertNotIn("keyboard", first)
        self.assertEqual(second["keyboard"], "KB")


class JsonValidatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_json_raises_transport_error_instead_of_blocking(self) -> None:
        validator = StrictJSONResponseValidator()
        with self.assertRaises(VkTransportError):
            await validator.validate("messages.send", {}, "<html>502 Bad Gateway</html>", None)

    async def test_json_string_is_parsed(self) -> None:
        validator = StrictJSONResponseValidator()
        self.assertEqual(await validator.validate("m", {}, json.dumps({"response": 1}), None), {"response": 1})

    async def test_api_uses_strict_validator(self) -> None:
        api = build_vk_api("token")
        self.assertIsInstance(api.response_validators[0], StrictJSONResponseValidator)


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def _receive_one(self, polling: ResilientBotPolling) -> dict:
        async for event in polling.listen():
            polling.stop()
            return event
        self.fail("VK long poll остановился до получения события")

    async def test_hung_server_setup_is_timed_out_and_retried(self) -> None:
        polling = ResilientBotPolling(MagicMock())
        attempts = 0

        async def get_server():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.Event().wait()
            return {"server": "s", "key": "k", "ts": "100"}

        polling.get_server = get_server
        polling.get_event = AsyncMock(return_value={"ts": "101", "updates": []})
        with patch("src.vk_runtime.VK_POLLING_SETUP_TIMEOUT", 0.01), patch("src.vk_runtime.asyncio.sleep", AsyncMock()):
            event = await asyncio.wait_for(self._receive_one(polling), timeout=1)
        self.assertEqual(event["ts"], "101")
        self.assertEqual(attempts, 2)
        self.assertEqual(polling.consecutive_failures, 0)

    async def test_hung_error_and_recovery_reports_cannot_stop_receiving(self) -> None:
        async def hung_report(*args):
            await asyncio.Event().wait()

        on_error = AsyncMock(side_effect=hung_report)
        on_recovered = AsyncMock(side_effect=hung_report)
        polling = ResilientBotPolling(MagicMock(), on_error=on_error, on_recovered=on_recovered)
        polling.get_server = AsyncMock(side_effect=[ClientConnectionError("down"), {"server": "s", "key": "k", "ts": "100"}])
        polling.get_event = AsyncMock(return_value={"ts": "101", "updates": [{"type": "message_new"}]})
        with patch("src.vk_runtime.VK_POLLING_CALLBACK_TIMEOUT", 0.01), patch("src.vk_runtime.asyncio.sleep", AsyncMock()):
            event = await asyncio.wait_for(self._receive_one(polling), timeout=1)
        self.assertEqual(len(event["updates"]), 1)
        on_error.assert_awaited_once()
        on_recovered.assert_awaited_once()

    async def test_malformed_polling_response_has_backoff_and_does_not_reset_ts(self) -> None:
        polling = ResilientBotPolling(MagicMock())
        polling.get_server = AsyncMock(return_value={"server": "s", "key": "k", "ts": "100"})
        polling.get_event = AsyncMock(side_effect=[{}, {"ts": "101", "updates": []}])
        with patch("src.vk_runtime.asyncio.sleep", AsyncMock()) as sleep:
            event = await self._receive_one(polling)
        self.assertEqual(event["ts"], "101")
        sleep.assert_awaited_once()

    async def test_ts_survives_network_error_so_messages_are_not_lost(self) -> None:
        polling = ResilientBotPolling(MagicMock())
        servers = iter([
            {"server": "s", "key": "k1", "ts": "100"},
            {"server": "s", "key": "k2", "ts": "999"},
        ])
        polling.get_server = AsyncMock(side_effect=lambda: next(servers))
        seen_ts: list[str] = []
        events = iter([
            {"ts": "101", "updates": [{"type": "message_new"}]},
            ClientConnectionError("dns"),
            {"ts": "102", "updates": [{"type": "message_new"}]},
        ])

        async def get_event(server):
            seen_ts.append(server["ts"])
            item = next(events)
            if isinstance(item, Exception):
                raise item
            return item

        polling.get_event = get_event
        received = []
        with patch("src.vk_runtime.asyncio.sleep", AsyncMock()):
            async for event in polling.listen():
                received.append(event)
                if len(received) == 2:
                    polling.stop()
                    break
        # После ошибки взят новый ключ, но ts остался 101, а не свежий 999 от сервера.
        self.assertEqual(seen_ts, ["100", "101", "101"])
        self.assertEqual(polling.consecutive_failures, 0)
        self.assertIsNotNone(polling.last_ok_at)

    async def test_error_callback_and_backoff(self) -> None:
        on_error = AsyncMock()
        polling = ResilientBotPolling(MagicMock(), on_error=on_error, max_backoff=8)
        polling.get_server = AsyncMock(side_effect=ClientConnectionError("down"))
        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= 6:
                polling.stop()

        with patch("src.vk_runtime.asyncio.sleep", fake_sleep):
            async for _ in polling.listen():
                pass
        self.assertEqual(on_error.await_count, 6)
        self.assertTrue(all(delay <= 8 for delay in sleeps))
        self.assertGreater(sleeps[-1], sleeps[0])


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _update(peer_id: int, cmid: int, text: str = "") -> dict:
        return {"type": "message_new", "event_id": f"{peer_id}-{cmid}", "object": {"message": {"peer_id": peer_id, "conversation_message_id": cmid, "text": text}}}

    async def test_same_peer_is_processed_in_order(self) -> None:
        order: list[str] = []

        async def route(update):
            text = update["object"]["message"]["text"]
            await asyncio.sleep(0.05 if text == "first" else 0)
            order.append(text)

        dispatcher = VkUpdateDispatcher(route)
        tasks = [dispatcher.dispatch(self._update(1, 1, "first")), dispatcher.dispatch(self._update(1, 2, "second"))]
        await asyncio.gather(*tasks)
        self.assertEqual(order, ["first", "second"])

    async def test_duplicate_event_is_skipped(self) -> None:
        route = AsyncMock()
        dispatcher = VkUpdateDispatcher(route)
        first = dispatcher.dispatch(self._update(1, 1))
        second = dispatcher.dispatch(self._update(1, 1))
        await first
        self.assertIsNone(second)
        self.assertEqual(route.await_count, 1)

    async def test_hung_handler_is_cut_by_timeout_and_reported(self) -> None:
        on_failure = AsyncMock()

        async def route(update):
            await asyncio.sleep(10)

        dispatcher = VkUpdateDispatcher(route, handler_timeout=0.05, on_failure=on_failure)
        await dispatcher.dispatch(self._update(1, 1))
        on_failure.assert_awaited_once()
        self.assertIsInstance(on_failure.await_args.args[1], TimeoutError)

        # Следующее сообщение того же диалога не заблокировано зависшим обработчиком.
        done = AsyncMock()
        dispatcher._route = done
        await dispatcher.dispatch(self._update(1, 2))
        done.assert_awaited_once()

    async def test_hung_failure_report_does_not_lock_the_dialog(self) -> None:
        async def hung_report(update, error):
            await asyncio.Event().wait()

        route = AsyncMock(side_effect=[RuntimeError("failed"), None])
        dispatcher = VkUpdateDispatcher(route, on_failure=hung_report)
        with patch("src.vk_runtime.VK_POLLING_CALLBACK_TIMEOUT", 0.01):
            await asyncio.wait_for(dispatcher.dispatch(self._update(1, 1)), timeout=1)
        await asyncio.wait_for(dispatcher.dispatch(self._update(1, 2)), timeout=1)
        self.assertEqual(route.await_count, 2)


class PollingCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "bot.vk_polling.json"
        self.requested_ts: dict[ResilientBotPolling, list[str]] = {}

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _polling(self, events: list[dict], *, server_ts: str = "100", group_id: int = 123) -> ResilientBotPolling:
        polling = ResilientBotPolling(MagicMock(), group_id=group_id, checkpoint_path=self.path)
        polling.get_server = AsyncMock(return_value={"server": "s", "key": "k", "ts": server_ts})
        responses = iter(events)
        self.requested_ts[polling] = []

        async def get_event(server):
            self.requested_ts[polling].append(server["ts"])
            event = next(responses)
            if event is events[-1]:
                polling.stop()
            return event

        polling.get_event = AsyncMock(side_effect=get_event)
        return polling

    async def _run(self, polling: ResilientBotPolling, route=None) -> VkUpdateDispatcher:
        dispatcher = VkUpdateDispatcher(route or AsyncMock())
        await dispatcher.run(polling)
        await dispatcher.drain()
        return dispatcher

    def _saved_ts(self) -> str:
        return json.loads(self.path.read_text(encoding="utf-8"))["ts"]

    async def test_restart_resumes_at_saved_position_instead_of_current_server_ts(self) -> None:
        # Даже пустой ответ сохраняется: последний запрос перед остановкой
        # обычно не содержит сообщений. Во время простоя появляется запрос.
        await self._run(self._polling([{"ts": "101", "updates": []}]))
        update = DispatcherTests._update(1, 1, "Расписание на завтра")
        restarted = self._polling([{"ts": "102", "updates": [update]}], server_ts="999")
        route = AsyncMock()

        await self._run(restarted, route)

        self.assertEqual(self.requested_ts[restarted], ["101"])
        route.assert_awaited_once_with(update)
        self.assertEqual(json.loads(self.path.read_text()), {"group_id": 123, "ts": "102"})

    async def test_checkpoint_from_another_community_is_ignored(self) -> None:
        await self._run(self._polling([{"ts": "101", "updates": []}]))
        other = self._polling([{"ts": "1000", "updates": []}], server_ts="999", group_id=456)

        await self._run(other)

        self.assertEqual(self.requested_ts[other], ["999"])
        self.assertEqual(json.loads(self.path.read_text()), {"group_id": 456, "ts": "1000"})

    async def test_expired_key_keeps_saved_position(self) -> None:
        self.path.write_text('{"group_id":123,"ts":"100"}', encoding="utf-8")
        polling = self._polling([{"failed": 2}, {"ts": "101", "updates": []}], server_ts="999")
        await self._run(polling)
        self.assertEqual(self.requested_ts[polling], ["100", "100"])
        self.assertEqual(self._saved_ts(), "101")

    async def test_expired_history_resumes_from_vk_ts(self) -> None:
        self.path.write_text('{"group_id":123,"ts":"100"}', encoding="utf-8")
        polling = self._polling([{"failed": 1, "ts": "999"}, {"ts": "1000", "updates": []}])
        await self._run(polling)
        self.assertEqual(self.requested_ts[polling], ["100", "999"])
        self.assertEqual(self._saved_ts(), "1000")

    async def test_invalid_checkpoint_does_not_prevent_startup(self) -> None:
        for content in ('{"broken', "[]", '{"group_id":123,"ts":false}', '{"group_id":123,"ts":{}}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                polling = self._polling([{"ts": "101", "updates": []}])
                await self._run(polling)
                self.assertEqual(self.requested_ts[polling], ["100"])
                self.assertEqual(self._saved_ts(), "101")

    async def test_failed_atomic_write_preserves_checkpoint_and_message_processing(self) -> None:
        self.path.write_text('{"group_id":123,"ts":"100"}', encoding="utf-8")
        update = DispatcherTests._update(1, 1)
        route = AsyncMock()
        with patch("src.vk_runtime.Path.replace", side_effect=PermissionError("disk unavailable")):
            await self._run(self._polling([{"ts": "101", "updates": [update]}]), route)

        route.assert_awaited_once_with(update)
        self.assertEqual(self._saved_ts(), "100")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    async def test_slow_peer_does_not_block_others_or_let_checkpoint_skip_pending_messages(self) -> None:
        release_slow = asyncio.Event()
        fast_done = asyncio.Event()

        async def route(update):
            if update["object"]["message"]["peer_id"] == 1:
                await release_slow.wait()
            else:
                fast_done.set()

        polling = self._polling([
            {"ts": "101", "updates": [DispatcherTests._update(1, 1)]},
            {"ts": "102", "updates": [DispatcherTests._update(2, 1)]},
            {"ts": "103", "updates": []},
        ])
        dispatcher = VkUpdateDispatcher(route)
        await dispatcher.run(polling)
        try:
            await asyncio.wait_for(fast_done.wait(), timeout=1)
            self.assertEqual(self._saved_ts(), "100")
        finally:
            release_slow.set()
            await dispatcher.drain()

        self.assertEqual(self._saved_ts(), "103")

    async def test_cancelled_message_is_replayed_after_restart(self) -> None:
        started = asyncio.Event()

        async def interrupted_route(update):
            started.set()
            await asyncio.Event().wait()

        update = DispatcherTests._update(1, 1)
        dispatcher = VkUpdateDispatcher(interrupted_route)
        await dispatcher.run(self._polling([{"ts": "101", "updates": [update]}]))
        await asyncio.wait_for(started.wait(), timeout=1)
        tasks = set(dispatcher._tasks)
        await dispatcher.drain(timeout=0)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self._saved_ts(), "100")

        restarted = self._polling([{"ts": "101", "updates": [update]}], server_ts="999")
        route = AsyncMock()
        await self._run(restarted, route)

        self.assertEqual(self.requested_ts[restarted], ["100"])
        route.assert_awaited_once_with(update)
        self.assertEqual(self._saved_ts(), "101")


if __name__ == "__main__":
    unittest.main()

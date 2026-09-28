from __future__ import annotations

import asyncio
import json
import unittest
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


if __name__ == "__main__":
    unittest.main()

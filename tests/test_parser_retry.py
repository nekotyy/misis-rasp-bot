from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from src.parser import ScheduleParser


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://site/rasp/1")
    return httpx.HTTPStatusError("err", request=request, response=httpx.Response(code, request=request))


class ParserRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_found_is_not_retried(self) -> None:
        parser = ScheduleParser("http://site/rasp/1", retry_backoff_seconds=0)
        response = MagicMock()
        response.raise_for_status = MagicMock(side_effect=_status_error(404))
        client = MagicMock()
        client.get = AsyncMock(return_value=response)
        with self.assertRaises(httpx.HTTPStatusError):
            await parser._get_with_retry(client, "http://site/rasp/1")
        self.assertEqual(client.get.await_count, 1)

    async def test_server_error_is_retried(self) -> None:
        parser = ScheduleParser("http://site/rasp/1", retry_backoff_seconds=0)
        response = MagicMock()
        response.raise_for_status = MagicMock(side_effect=_status_error(502))
        client = MagicMock()
        client.get = AsyncMock(return_value=response)
        with self.assertRaises(httpx.HTTPStatusError):
            await parser._get_with_retry(client, "http://site/rasp/1")
        self.assertEqual(client.get.await_count, 3)


if __name__ == "__main__":
    unittest.main()

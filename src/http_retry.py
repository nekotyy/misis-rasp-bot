"""Общий помощник для GET-запросов с ретраями, используемый парсерами каталогов сайта расписания."""

from __future__ import annotations

import asyncio

import httpx


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    retries: int = 3,
    backoff_seconds: float = 1.0,
) -> httpx.Response:
    last_exc: httpx.HTTPError | None = None
    for attempt in range(1, retries + 1):
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt >= retries:
                break
            await asyncio.sleep(backoff_seconds * attempt)
    assert last_exc is not None
    raise last_exc

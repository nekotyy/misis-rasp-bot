"""Восстановление безопасных запросов из непрочитанных диалогов VK."""

from __future__ import annotations

import logging
from time import time
from typing import Any

logger = logging.getLogger(__name__)

SAFE_COMMANDS = frozenset({
    "начать", "расписание", "расписание на сегодня", "расписание на завтра",
    "расписание на 2 дня", "найти расписание", "назад в меню",
})


async def recover_unanswered(api: Any, token: str, db: Any, dispatcher: Any, group_id: int) -> int:
    """Возвращает только недавние, ещё не отвеченные команды личных диалогов."""
    until = time()
    response = await api.http_client.request_json(
        api.API_URL + "messages.getConversations", method="POST",
        data={"filter": "unread", "count": 200},
        params={"access_token": token, "v": api.API_VERSION},
    )
    if "error" in response:
        raise RuntimeError(f"VK getConversations error {response['error'].get('error_code', 'unknown')}")
    data = response.get("response", {})
    items = data.get("items", [])
    if data.get("count", 0) > len(items):
        logger.warning("VK: список непрочитанных диалогов превышает лимит проверки.")
    recovered = 0
    async with db._connect() as connection:
        for item in items:
            message = item.get("last_message", {})
            peer = message.get("peer_id")
            stamp = message.get("date")
            if (not isinstance(peer, int) or not 0 < peer < 2_000_000_000
                    or message.get("from_id") != peer or message.get("out") != 0
                    or not isinstance(stamp, int) or not until - 3600 <= stamp <= until
                    or message.get("attachments") or message.get("fwd_messages")
                    or not isinstance(message.get("conversation_message_id"), int)
                    or str(message.get("text", "")).strip().casefold() not in SAFE_COMMANDS):
                continue
            row = await (await connection.execute(
                "SELECT 1 FROM delivery_outbox WHERE platform = 'vk' AND peer_key = ? AND created_at >= ? "
                "AND coalesce(json_extract(metadata_json, '$.campaign_type'), 'interactive') = 'interactive' LIMIT 1",
                (str(peer), stamp),
            )).fetchone()
            if row:
                continue
            update = {"type": "message_new", "group_id": group_id, "object": {"message": message}}
            if dispatcher.dispatch(update) is not None:
                recovered += 1
    logger.info("VK: возвращено в обработку безопасных непрочитанных запросов: %s.", recovered)
    return recovered

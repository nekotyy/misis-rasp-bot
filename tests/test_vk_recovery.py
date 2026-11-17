from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from time import time
from unittest.mock import AsyncMock, MagicMock

from vkbottle_types.events.bot_events import MessageNew

from src.db import Database
from src.outbox_store import OutboxStore
from src.vk_recovery import recover_unanswered
from src.vk_runtime import VkUpdateDispatcher


class VkRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "bot.db")
        await self.db.initialize()
        self.now = int(time())
        self.route = AsyncMock()
        self.dispatcher = VkUpdateDispatcher(self.route)
        self.api = MagicMock(API_URL="https://api.example/", API_VERSION="5.199")

    def item(self, peer, text="Расписание на завтра", **extra):
        return {"last_message": {"peer_id": peer, "from_id": peer, "out": 0, "date": self.now,
                                 "conversation_message_id": 3, "text": text, **extra}}

    async def test_recovers_only_recent_unanswered_safe_private_commands(self):
        await OutboxStore(self.db).enqueue("already", "vk", "2", "messages.send", {"peer_id": 2},
                                           metadata={"api_version": None}, now=self.now + 0.1)
        items = [self.item(1), self.item(2), self.item(3, text="удали базу"),
                 self.item(4, date=self.now - 4000), self.item(2_000_000_001),
                 self.item(5, attachments=[{"type": "photo"}])]
        self.api.http_client.request_json = AsyncMock(return_value={"response": {"count": len(items), "items": items}})
        self.assertEqual(await recover_unanswered(self.api, "token", self.db, self.dispatcher, 7), 1)
        await self.dispatcher.drain()
        self.assertEqual(self.route.await_args.args[0]["object"]["message"]["peer_id"], 1)

    async def test_live_and_recovered_event_share_message_deduplication(self):
        update = {"type": "message_new", "event_id": "live-event", "object": self.item(1)}
        update["object"] = {"message": update["object"]["last_message"]}
        self.assertIsNotNone(self.dispatcher.dispatch(update))
        recovered = {"type": "message_new", "object": update["object"]}
        self.assertIsNone(self.dispatcher.dispatch(recovered))
        await self.dispatcher.drain()
        self.route.assert_awaited_once()

    async def test_reconstructed_event_matches_vk_model(self):
        item = self.item(1, id=12, version=123)
        self.api.http_client.request_json = AsyncMock(return_value={"response": {"count": 1, "items": [item]}})
        await recover_unanswered(self.api, "token", self.db, self.dispatcher, 7)
        await self.dispatcher.drain()
        event = MessageNew.model_validate(self.route.await_args.args[0])
        self.assertEqual(event.object.message.text, "Расписание на завтра")

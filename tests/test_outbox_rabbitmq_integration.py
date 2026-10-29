"""Настоящий RabbitMQ; очереди тестов изолированы, API ботов не вызываются."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from time import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import aio_pika

from src.db import Database
from src.delivery_outbox import DeliveryOutbox
from src.message_broker import OutboxBroker

RABBITMQ_URL = os.getenv("OUTBOX_TEST_RABBITMQ_URL", "")


@unittest.skipUnless(RABBITMQ_URL, "OUTBOX_TEST_RABBITMQ_URL is not configured")
class RabbitMQOutboxIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        await self.db.initialize()
        self.prefix = f"codex_test_outbox_{uuid4().hex}"
        self.services = []

    def service(self):
        broker = OutboxBroker(RABBITMQ_URL, self.prefix)
        service = DeliveryOutbox(self.db, broker, poll_interval=0.02)
        service._senders["telegram"] = AsyncMock(return_value=True)
        self.services.append(service)
        return service

    async def asyncTearDown(self):
        for service in self.services:
            await service.stop()
        connection = await aio_pika.connect(RABBITMQ_URL)
        try:
            channel = await connection.channel()
            for platform in ("telegram", "vk"):
                name = f"{self.prefix}.outbox.{platform}"
                assert name.startswith("codex_test_outbox_")
                await channel.queue_delete(name, if_unused=False, if_empty=False)
        finally:
            await connection.close()
        self.tmp.cleanup()

    async def save(self, service):
        return await service.store.enqueue("job", "telegram", "1", "sendMessage", {"chat_id": 1, "text": "ответ"})

    async def wait_status(self, service, status):
        async def wait():
            while (await service.store.get("job"))["status"] != status:
                await asyncio.sleep(0.02)
        await asyncio.wait_for(wait(), timeout=10)

    async def test_real_queue_confirms_delivery_and_duplicate_hint_does_not_send_twice(self):
        service = self.service()
        await self.save(service)
        await service.start()
        self.assertTrue(await asyncio.wait_for(service.wait("job"), timeout=10))
        await service.broker.publish("telegram", "job")
        await asyncio.sleep(0.1)
        service._senders["telegram"].assert_awaited_once()
        self.assertTrue(service.broker._queues["telegram"].durable)

    async def test_cancelled_consumer_is_recovered_by_recreated_worker(self):
        first = self.service()
        entered = asyncio.Event()

        async def disconnected(row):
            entered.set()
            await asyncio.Event().wait()

        first._senders["telegram"] = disconnected
        await self.save(first)
        await first.start()
        await asyncio.wait_for(entered.wait(), timeout=10)
        await first.stop()
        self.assertEqual((await first.store.get("job"))["status"], "pending")
        restarted = self.service()
        await restarted.start()
        self.assertTrue(await asyncio.wait_for(restarted.wait("job"), timeout=10))
        restarted._senders["telegram"].assert_awaited_once()

    async def test_hour_network_outage_survives_ack_and_new_worker(self):
        first = self.service()
        first._senders["telegram"] = AsyncMock(side_effect=TimeoutError("network unavailable"))
        await self.save(first)
        await first.start()
        async def failed_once():
            while (await first.store.get("job"))["attempts"] < 1:
                await asyncio.sleep(0.02)
        await asyncio.wait_for(failed_once(), timeout=10)
        await self.wait_status(first, "pending")
        await first.stop()
        restarted = self.service()
        future = time() + 3600
        with patch("src.outbox_store.time", return_value=future), patch("src.delivery_outbox.time", return_value=future):
            await restarted.start()
            self.assertTrue(await asyncio.wait_for(restarted.wait("job"), timeout=10))
        self.assertEqual((await restarted.store.get("job"))["attempts"], 2)

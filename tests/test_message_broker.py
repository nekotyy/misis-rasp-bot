import sqlite3
import unittest
from contextlib import asynccontextmanager
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

from src.message_broker import (
    DatabaseCleanupJob,
    DatabaseCleanupJobBroker,
    OutboundMessage,
    OutboxBroker,
    RabbitMQBroker,
)


class MessageBrokerTests(unittest.TestCase):
    def test_outbound_message_dataclass(self):
        msg = OutboundMessage(
            platform="telegram",
            user_id=12345,
            text="Тестовое сообщение",
            campaign_type="notification",
        )
        self.assertEqual(msg.platform, "telegram")
        self.assertEqual(msg.user_id, 12345)
        self.assertEqual(msg.attempt, 1)

        d = asdict(msg)
        self.assertEqual(d["user_id"], 12345)
        self.assertEqual(d["text"], "Тестовое сообщение")

    def test_database_cleanup_job_dataclass(self):
        job = DatabaseCleanupJob(days=90)
        self.assertEqual(job.days, 90)
        self.assertEqual(job.attempt, 1)

    def test_broker_disabled_when_url_empty(self):
        broker = RabbitMQBroker(url="", queue_name="test_queue")
        self.assertFalse(broker.enabled)

        cleanup_broker = DatabaseCleanupJobBroker(url="", queue_name="test_queue")
        self.assertFalse(cleanup_broker.enabled)


class OutboxBrokerLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_delivery_is_requeued_without_unhandled_exception(self):
        broker = OutboxBroker("amqp://test", "test")
        queue = AsyncMock()
        broker.connect = AsyncMock()
        broker._queues = {"telegram": queue}
        await broker.start_consumer(AsyncMock(side_effect=sqlite3.OperationalError("database is locked")))
        consume = queue.consume.await_args.args[0]

        class Message:
            body = b'{"delivery_id":"one"}'
            requeued = False

            @asynccontextmanager
            async def process(self, requeue):
                try:
                    yield
                except Exception:
                    self.requeued = requeue
                    raise

        message = Message()
        with patch("src.message_broker.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await consume(message)
        self.assertTrue(message.requeued)
        sleep.assert_awaited_once_with(1)


if __name__ == "__main__":
    unittest.main()

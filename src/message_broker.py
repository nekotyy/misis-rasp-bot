from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

import aio_pika
from aio_pika import DeliveryMode, IncomingMessage, Message


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OutboundMessage:
    platform: str
    user_id: int
    text: str


Sender = Callable[[OutboundMessage], Awaitable[None]]


class RabbitMQBroker:
    def __init__(self, url: str, queue_name: str, prefetch_count: int = 20) -> None:
        self.url = url
        self.queue_name = queue_name
        self.prefetch_count = max(1, prefetch_count)
        self._connection: aio_pika.RobustConnection | None = None
        self._channel: aio_pika.abc.AbstractRobustChannel | None = None
        self._queue: aio_pika.abc.AbstractQueue | None = None
        self._consumer_tag: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url.strip())

    async def connect(self) -> None:
        if not self.enabled:
            return
        if self._connection is not None and not self._connection.is_closed:
            return

        self._connection = await aio_pika.connect_robust(self.url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self.prefetch_count)
        self._queue = await self._channel.declare_queue(self.queue_name, durable=True)
        logger.info("RabbitMQ connected. Queue: %s", self.queue_name)

    async def publish(self, payload: OutboundMessage) -> bool:
        if not self.enabled:
            return False
        await self.connect()
        if self._channel is None:
            return False

        body = json.dumps(asdict(payload), ensure_ascii=False).encode("utf-8")
        await self._channel.default_exchange.publish(
            Message(
                body=body,
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
            ),
            routing_key=self.queue_name,
        )
        return True

    async def start_consumer(self, sender: Sender) -> None:
        if not self.enabled:
            return
        await self.connect()
        if self._queue is None:
            return
        if self._consumer_tag is not None:
            return

        async def _consume(message: IncomingMessage) -> None:
            async with message.process(requeue=True):
                payload = OutboundMessage(**json.loads(message.body.decode("utf-8")))
                await sender(payload)

        self._consumer_tag = await self._queue.consume(_consume)
        logger.info("RabbitMQ consumer started for queue %s", self.queue_name)

    async def close(self) -> None:
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._consumer_tag = None
        self._queue = None
        self._channel = None
        self._connection = None

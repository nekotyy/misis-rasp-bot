from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from uuid import uuid4

import aio_pika
from aio_pika import DeliveryMode, IncomingMessage, Message

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OutboundMessage:
    platform: str
    user_id: int
    text: str
    campaign_type: str = "notification"
    attempt: int = 1
    max_attempts: int = 5
    message_id: str | None = None


@dataclass(slots=True)
class LessonCounterJob:
    schedule_id: int
    attempt: int = 1
    max_attempts: int = 8
    job_id: str | None = None


@dataclass(slots=True)
class DatabaseCleanupJob:
    days: int = 90
    attempt: int = 1
    max_attempts: int = 3
    job_id: str | None = None


@dataclass(slots=True)
class AutoDailyLessonCounterJob:
    target_date_iso: str
    attempt: int = 1
    max_attempts: int = 3
    job_id: str | None = None


Sender = Callable[[OutboundMessage], Awaitable[None]]
LessonCounterHandler = Callable[[LessonCounterJob], Awaitable[None]]
DatabaseCleanupHandler = Callable[[DatabaseCleanupJob], Awaitable[None]]
AutoDailyLessonCounterHandler = Callable[[AutoDailyLessonCounterJob], Awaitable[None]]


class _QueueJobBroker:
    """Общая логика поверх RabbitMQ: connect/publish/consume с ретраями/close.

    Конкретные брокеры ниже отличаются только типом payload-датакласса, именем
    его id-поля (`message_id` у уведомлений, `job_id` у остальных задач) и
    словом для логов — вся логика подключения, паблиша и retry/ack/nack одна.
    """

    payload_type: type
    id_field: str
    label: str

    def __init__(self, url: str, queue_name: str, prefetch_count: int = 1) -> None:
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
        logger.info("RabbitMQ connected for %s. Queue: %s", self.label, self.queue_name)

    async def publish(self, payload) -> bool:
        if not self.enabled:
            return False
        await self.connect()
        if self._channel is None:
            return False

        if not getattr(payload, self.id_field):
            setattr(payload, self.id_field, str(uuid4()))
        payload_id = getattr(payload, self.id_field)

        body = json.dumps(asdict(payload), ensure_ascii=False).encode("utf-8")
        await self._channel.default_exchange.publish(
            Message(
                body=body,
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
                message_id=payload_id,
            ),
            routing_key=self.queue_name,
        )
        return True

    async def start_consumer(self, handler: Callable[[object], Awaitable[None]]) -> None:
        if not self.enabled:
            return
        await self.connect()
        if self._queue is None:
            return
        if self._consumer_tag is not None:
            return

        async def _consume(message: IncomingMessage) -> None:
            try:
                payload = self.payload_type(**json.loads(message.body.decode("utf-8")))
            except Exception as exc:
                logger.warning("%s payload decode failed: %s", self.label, exc)
                await message.reject(requeue=False)
                return

            payload_id = getattr(payload, self.id_field)
            try:
                await handler(payload)
            except Exception as exc:
                current_attempt = max(1, payload.attempt)
                if current_attempt < payload.max_attempts:
                    retry_payload = replace(payload, attempt=current_attempt + 1)
                    try:
                        await self.publish(retry_payload)
                    except Exception as publish_exc:
                        logger.warning(
                            "%s retry publish failed for %s (attempt %s/%s): %s",
                            self.label,
                            payload_id,
                            current_attempt,
                            payload.max_attempts,
                            publish_exc,
                        )
                        await message.nack(requeue=True)
                        return

                    logger.warning(
                        "%s %s failed, requeued as attempt %s/%s: %s",
                        self.label,
                        payload_id,
                        retry_payload.attempt,
                        retry_payload.max_attempts,
                        exc,
                    )
                    await message.ack()
                    return

                logger.error(
                    "%s %s failed after %s attempts: %s",
                    self.label,
                    payload_id,
                    current_attempt,
                    exc,
                )
                await message.reject(requeue=False)
                return

            await message.ack()

        self._consumer_tag = await self._queue.consume(_consume)
        logger.info("RabbitMQ consumer started for %s (queue %s)", self.label, self.queue_name)

    async def close(self) -> None:
        try:
            if self._channel is not None and not self._channel.is_closed:
                await self._channel.close()
        finally:
            if self._connection is not None and not self._connection.is_closed:
                await self._connection.close()
        self._consumer_tag = None
        self._queue = None
        self._channel = None
        self._connection = None


class RabbitMQBroker(_QueueJobBroker):
    payload_type = OutboundMessage
    id_field = "message_id"
    label = "message"

    def __init__(self, url: str, queue_name: str, prefetch_count: int = 20) -> None:
        super().__init__(url, queue_name, prefetch_count)

    async def start_consumer(self, sender: Sender) -> None:
        await super().start_consumer(sender)


class LessonCounterJobBroker(_QueueJobBroker):
    payload_type = LessonCounterJob
    id_field = "job_id"
    label = "lesson counter job"

    def __init__(self, url: str, queue_name: str, prefetch_count: int = 5) -> None:
        super().__init__(url, queue_name, prefetch_count)

    async def start_consumer(self, handler: LessonCounterHandler) -> None:
        await super().start_consumer(handler)


class DatabaseCleanupJobBroker(_QueueJobBroker):
    payload_type = DatabaseCleanupJob
    id_field = "job_id"
    label = "database cleanup job"

    def __init__(self, url: str, queue_name: str, prefetch_count: int = 1) -> None:
        super().__init__(url, queue_name, prefetch_count)

    async def start_consumer(self, handler: DatabaseCleanupHandler) -> None:
        await super().start_consumer(handler)


class AutoDailyLessonCounterJobBroker(_QueueJobBroker):
    payload_type = AutoDailyLessonCounterJob
    id_field = "job_id"
    label = "auto daily lesson counter job"

    def __init__(self, url: str, queue_name: str, prefetch_count: int = 1) -> None:
        super().__init__(url, queue_name, prefetch_count)

    async def start_consumer(self, handler: AutoDailyLessonCounterHandler) -> None:
        await super().start_consumer(handler)

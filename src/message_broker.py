from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable
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


Sender = Callable[[OutboundMessage], Awaitable[None]]
LessonCounterHandler = Callable[[LessonCounterJob], Awaitable[None]]


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

        if not payload.message_id:
            payload.message_id = str(uuid4())

        body = json.dumps(asdict(payload), ensure_ascii=False).encode("utf-8")
        await self._channel.default_exchange.publish(
            Message(
                body=body,
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
                message_id=payload.message_id,
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
            try:
                payload = OutboundMessage(**json.loads(message.body.decode("utf-8")))
            except Exception as exc:
                logger.warning("RabbitMQ payload decode failed: %s", exc)
                await message.reject(requeue=False)
                return

            try:
                await sender(payload)
            except Exception as exc:
                current_attempt = max(1, payload.attempt)
                if current_attempt < payload.max_attempts:
                    retry_payload = OutboundMessage(
                        platform=payload.platform,
                        user_id=payload.user_id,
                        text=payload.text,
                        campaign_type=payload.campaign_type,
                        attempt=current_attempt + 1,
                        max_attempts=payload.max_attempts,
                        message_id=payload.message_id,
                    )
                    try:
                        await self.publish(retry_payload)
                    except Exception as publish_exc:
                        logger.warning(
                            "RabbitMQ retry publish failed for message %s (attempt %s/%s): %s",
                            payload.message_id,
                            current_attempt,
                            payload.max_attempts,
                            publish_exc,
                        )
                        await message.nack(requeue=True)
                        return

                    logger.warning(
                        "Delivery failed for message %s, requeued as attempt %s/%s: %s",
                        payload.message_id,
                        retry_payload.attempt,
                        retry_payload.max_attempts,
                        exc,
                    )
                    await message.ack()
                    return

                logger.error(
                    "Delivery failed for message %s after %s attempts: %s",
                    payload.message_id,
                    current_attempt,
                    exc,
                )
                await message.reject(requeue=False)
                return

            await message.ack()

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


class LessonCounterJobBroker:
    def __init__(self, url: str, queue_name: str, prefetch_count: int = 5) -> None:
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
        logger.info("RabbitMQ connected. Lesson counter queue: %s", self.queue_name)

    async def publish(self, payload: LessonCounterJob) -> bool:
        if not self.enabled:
            return False
        await self.connect()
        if self._channel is None:
            return False

        if not payload.job_id:
            payload.job_id = str(uuid4())

        body = json.dumps(asdict(payload), ensure_ascii=False).encode("utf-8")
        await self._channel.default_exchange.publish(
            Message(
                body=body,
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
                message_id=payload.job_id,
            ),
            routing_key=self.queue_name,
        )
        return True

    async def start_consumer(self, handler: LessonCounterHandler) -> None:
        if not self.enabled:
            return
        await self.connect()
        if self._queue is None:
            return
        if self._consumer_tag is not None:
            return

        async def _consume(message: IncomingMessage) -> None:
            try:
                payload = LessonCounterJob(**json.loads(message.body.decode("utf-8")))
            except Exception as exc:
                logger.warning("Lesson counter job decode failed: %s", exc)
                await message.reject(requeue=False)
                return

            try:
                await handler(payload)
            except Exception as exc:
                current_attempt = max(1, payload.attempt)
                if current_attempt < payload.max_attempts:
                    retry_payload = LessonCounterJob(
                        schedule_id=payload.schedule_id,
                        attempt=current_attempt + 1,
                        max_attempts=payload.max_attempts,
                        job_id=payload.job_id,
                    )
                    try:
                        await self.publish(retry_payload)
                    except Exception as publish_exc:
                        logger.warning(
                            "Lesson counter retry publish failed for job %s (attempt %s/%s): %s",
                            payload.job_id,
                            current_attempt,
                            payload.max_attempts,
                            publish_exc,
                        )
                        await message.nack(requeue=True)
                        return

                    logger.warning(
                        "Lesson counter job %s failed, requeued as attempt %s/%s: %s",
                        payload.job_id,
                        retry_payload.attempt,
                        retry_payload.max_attempts,
                        exc,
                    )
                    await message.ack()
                    return

                logger.error(
                    "Lesson counter job %s failed after %s attempts: %s",
                    payload.job_id,
                    current_attempt,
                    exc,
                )
                await message.reject(requeue=False)
                return

            await message.ack()

        self._consumer_tag = await self._queue.consume(_consume)
        logger.info("RabbitMQ lesson counter consumer started for queue %s", self.queue_name)

    async def close(self) -> None:
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._consumer_tag = None
        self._queue = None
        self._channel = None
        self._connection = None


@dataclass(slots=True)
class DatabaseCleanupJob:
    days: int = 90
    attempt: int = 1
    max_attempts: int = 3
    job_id: str | None = None


DatabaseCleanupHandler = Callable[[DatabaseCleanupJob], Awaitable[None]]


class DatabaseCleanupJobBroker:
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
        logger.info("RabbitMQ connected. Database cleanup queue: %s", self.queue_name)

    async def publish(self, payload: DatabaseCleanupJob) -> bool:
        if not self.enabled:
            return False
        await self.connect()
        if self._channel is None:
            return False

        if not payload.job_id:
            payload.job_id = str(uuid4())

        body = json.dumps(asdict(payload), ensure_ascii=False).encode("utf-8")
        await self._channel.default_exchange.publish(
            Message(
                body=body,
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
                message_id=payload.job_id,
            ),
            routing_key=self.queue_name,
        )
        return True

    async def start_consumer(self, handler: DatabaseCleanupHandler) -> None:
        if not self.enabled:
            return
        await self.connect()
        if self._queue is None:
            return
        if self._consumer_tag is not None:
            return

        async def _consume(message: IncomingMessage) -> None:
            try:
                payload = DatabaseCleanupJob(**json.loads(message.body.decode("utf-8")))
            except Exception as exc:
                logger.warning("Database cleanup job decode failed: %s", exc)
                await message.reject(requeue=False)
                return

            try:
                await handler(payload)
            except Exception as exc:
                current_attempt = max(1, payload.attempt)
                if current_attempt < payload.max_attempts:
                    retry_payload = DatabaseCleanupJob(
                        days=payload.days,
                        attempt=current_attempt + 1,
                        max_attempts=payload.max_attempts,
                        job_id=payload.job_id,
                    )
                    try:
                        await self.publish(retry_payload)
                    except Exception as publish_exc:
                        logger.warning(
                            "Database cleanup retry publish failed for job %s (attempt %s/%s): %s",
                            payload.job_id,
                            current_attempt,
                            payload.max_attempts,
                            publish_exc,
                        )
                        await message.nack(requeue=True)
                        return
                    await message.ack()
                    return

                logger.error(
                    "Database cleanup job %s failed after %s attempts: %s",
                    payload.job_id,
                    current_attempt,
                    exc,
                )
                await message.reject(requeue=False)
                return

            await message.ack()

        self._consumer_tag = await self._queue.consume(_consume)
        logger.info("RabbitMQ database cleanup consumer started for queue %s", self.queue_name)

    async def close(self) -> None:
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._consumer_tag = None
        self._queue = None
        self._channel = None
        self._connection = None


@dataclass(slots=True)
class AutoDailyLessonCounterJob:
    target_date_iso: str
    attempt: int = 1
    max_attempts: int = 3
    job_id: str | None = None


AutoDailyLessonCounterHandler = Callable[[AutoDailyLessonCounterJob], Awaitable[None]]


class AutoDailyLessonCounterJobBroker:
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
        logger.info("RabbitMQ connected. Auto daily lesson counter queue: %s", self.queue_name)

    async def publish(self, payload: AutoDailyLessonCounterJob) -> bool:
        if not self.enabled:
            return False
        await self.connect()
        if self._channel is None:
            return False

        if not payload.job_id:
            payload.job_id = str(uuid4())

        body = json.dumps(asdict(payload), ensure_ascii=False).encode("utf-8")
        await self._channel.default_exchange.publish(
            Message(
                body=body,
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
                message_id=payload.job_id,
            ),
            routing_key=self.queue_name,
        )
        return True

    async def start_consumer(self, handler: AutoDailyLessonCounterHandler) -> None:
        if not self.enabled:
            return
        await self.connect()
        if self._queue is None:
            return
        if self._consumer_tag is not None:
            return

        async def _consume(message: IncomingMessage) -> None:
            try:
                payload = AutoDailyLessonCounterJob(**json.loads(message.body.decode("utf-8")))
            except Exception as exc:
                logger.warning("RabbitMQ auto daily lesson counter payload decode failed: %s", exc)
                await message.reject(requeue=False)
                return

            try:
                await handler(payload)
            except Exception as exc:
                current_attempt = payload.attempt
                if current_attempt < payload.max_attempts:
                    payload.attempt += 1
                    try:
                        await self.publish(payload)
                    except Exception as publish_exc:
                        logger.warning(
                            "Auto daily lesson counter retry publish failed for job %s (attempt %s/%s): %s",
                            payload.job_id,
                            current_attempt,
                            payload.max_attempts,
                            publish_exc,
                        )
                        await message.nack(requeue=True)
                        return
                    await message.ack()
                    return

                logger.error(
                    "Auto daily lesson counter job %s failed after %s attempts: %s",
                    payload.job_id,
                    current_attempt,
                    exc,
                )
                await message.reject(requeue=False)
                return

            await message.ack()

        self._consumer_tag = await self._queue.consume(_consume)
        logger.info("RabbitMQ auto daily lesson counter consumer started for queue %s", self.queue_name)

    async def close(self) -> None:
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._consumer_tag = None
        self._queue = None
        self._channel = None
        self._connection = None

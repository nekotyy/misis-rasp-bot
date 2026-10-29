"""Доставка API-запросов через RabbitMQ и SQLite с восстановлением после аварий."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from copy import copy
from time import monotonic, time
from uuid import uuid4

from aiogram import exceptions as telegram_errors
from aiogram.exceptions import ClientDecodeError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiohttp import ClientError
from vkbottle import API
from vkbottle.exception_factory.base_exceptions import VKAPIError

from src.message_broker import OutboxBroker
from src.outbox_codec import OutboxFiles, json_value, telegram_method, telegram_result
from src.outbox_context import current_peer_key, delivery_id
from src.outbox_store import TERMINAL_STATUSES, OutboxStore, complete_on_cancel, retention_cutoff

logger = logging.getLogger(__name__)

DEFAULT_DELIVERY_TTL = 86400.0
EPHEMERAL_DELIVERY_TTL = 8.0
RETRY_DELAYS = (10.0, 30.0, 60.0, 300.0)


class DeliveryExpiredError(TimeoutError):
    """Срок актуальности ответа истёк до подтверждения доставки."""


class DeliveryFailedError(ConnectionError):
    """Постоянная ошибка доставки, восстановленная из SQLite."""


def retry_delay(error: BaseException, attempts: int) -> float | None:
    if isinstance(error, FileNotFoundError):
        return None
    if isinstance(error, TelegramRetryAfter):
        return max(0.0, float(error.retry_after)) + 0.5
    if isinstance(error, (TelegramNetworkError, TelegramServerError, ClientDecodeError, ClientError, TimeoutError, OSError)):
        return RETRY_DELAYS[min(max(attempts - 1, 0), len(RETRY_DELAYS) - 1)]
    if isinstance(error, telegram_errors.TelegramUnauthorizedError):
        return 300.0  # Администратор может обновить токен до истечения TTL.
    if isinstance(error, VKAPIError):
        from src.vk_runtime import VK_RETRYABLE_ERROR_CODES, vk_error_code
        if vk_error_code(error) in VK_RETRYABLE_ERROR_CODES | {5, 27, 28}:
            return RETRY_DELAYS[min(max(attempts - 1, 0), len(RETRY_DELAYS) - 1)]
    return None


def error_payload(error: BaseException) -> dict:
    return {
        "type": type(error).__name__, "message": str(getattr(error, "message", str(error)))[:1500],
        "code": getattr(error, "code", None), "migrate_to_chat_id": getattr(error, "migrate_to_chat_id", None),
    }


class DeliveryOutbox:
    def __init__(self, db, broker: OutboxBroker | None = None, *, poll_interval: float = 1.0, concurrency: int = 20) -> None:
        self.db = db
        self.store = OutboxStore(db)
        self.files = OutboxFiles(db.path.parent / "outbox_files", db.path)
        self.broker = broker
        self.poll_interval = poll_interval
        self._senders: dict = {}
        self._tasks: set[asyncio.Task] = set()
        self._workers: set[asyncio.Task] = set()
        self._pump: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._semaphores = {platform: asyncio.Semaphore(concurrency) for platform in ("telegram", "vk")}
        self._stopping = False
        self._broker_ok = not bool(broker and broker.enabled)
        self._broker_error_at: float | None = None
        self._last_cleanup = 0.0
        self._last_counts: dict = {}
        self._next_broker_connect = 0.0
        self._fallback_active: set[str] = set()
        self._blocked_until = {"telegram": 0.0, "vk": 0.0}
        self._waiters: dict[str, tuple[asyncio.Event, int]] = {}
        self._last_count_refresh = 0.0

    def bind_telegram(self, bot) -> None:
        bot.session.outbox = self

        async def send(row):
            method = telegram_method(row["method"], self.files.decode(json.loads(row["payload_json"])))
            return await bot.session.direct_request(bot, method)

        self._senders["telegram"] = send

    def bind_vk(self, api) -> None:
        api.outbox = self

        async def send(row):
            payload = self.files.decode(json.loads(row["payload_json"]))
            if row["method"] == "outbox.sendDocument":
                from vkbottle.tools import DocMessagesUploader

                from src.vk_runtime import _extract_message_id
                # API без outbox исключает рекурсивную постановку docs.save самим воркером.
                upload_api = copy(api)
                upload_api.outbox = None
                document = await DocMessagesUploader(upload_api).upload(
                    payload["document"].path, peer_id=payload["peer_id"], title=payload["message"],
                )
                response = await API.request(api, "messages.send", {
                    "peer_id": payload["peer_id"], "message": payload["message"],
                    "attachment": document, "keyboard": payload.get("keyboard"), "random_id": payload["random_id"],
                })
                _extract_message_id(response)
                return response
            metadata = json.loads(row["metadata_json"])
            response = await API.request(api, row["method"], payload, version=metadata.get("api_version"))
            if row["method"] == "messages.send":
                from src.vk_runtime import _extract_message_id
                _extract_message_id(response)
            return response

        self._senders["vk"] = send

    async def prepare_telegram(self, bot, method, *, request_id: str | None = None, metadata: dict | None = None) -> dict:
        payload = await self.files.encode(method, bot)
        fingerprint = await asyncio.to_thread(self.files.fingerprint, payload)
        request_id = request_id or delivery_id("telegram", method.__api_method__, {"fingerprint": fingerprint})
        ephemeral = method.__api_method__ in {"answerCallbackQuery", "answerPreCheckoutQuery", "answerShippingQuery", "sendChatAction"}
        peer = payload.get("chat_id", payload.get("user_id", payload.get("inline_message_id")))
        if peer is None:
            peer = f"{method.__api_method__}:{payload.get('callback_query_id', payload.get('pre_checkout_query_id', request_id))}"
        return {
            "delivery_id": request_id, "platform": "telegram", "peer_key": str(peer),
            "method": method.__api_method__, "payload": payload, "metadata": metadata or {},
            "ttl": EPHEMERAL_DELIVERY_TTL if ephemeral else DEFAULT_DELIVERY_TTL,
        }

    async def enqueue_many(self, prepared: list[dict]) -> list[dict]:
        rows = await self.store.enqueue_many(prepared)
        for item, row in zip(prepared, rows, strict=True):
            if row["payload_json"] != json.dumps(item["payload"], ensure_ascii=False):
                await asyncio.to_thread(self.files.remove_payload_files, item["payload"])
        self._wake.set()
        return rows

    async def enqueue_telegram(self, bot, method, *, request_id: str | None = None, metadata: dict | None = None) -> dict:
        prepared = await self.prepare_telegram(bot, method, request_id=request_id, metadata=metadata)
        return (await self.enqueue_many([prepared]))[0]

    async def execute_telegram(self, bot, method):
        row = await self.enqueue_telegram(bot, method)
        try:
            result = await self.wait(row["delivery_id"])
        except DeliveryExpiredError:
            if row["expires_at"] - row["created_at"] <= EPHEMERAL_DELIVERY_TTL:
                return True
            raise
        return telegram_result(method, result, bot)

    async def prepare_vk(
        self, method: str, params: dict, *, request_id: str | None = None, metadata: dict | None = None,
    ) -> dict:
        payload = await self.files.encode(params)
        stable_payload = {key: item for key, item in payload.items() if key != "random_id"}
        fingerprint = await asyncio.to_thread(self.files.fingerprint, stable_payload)
        request_id = request_id or delivery_id("vk", method, {"fingerprint": fingerprint})
        peer = payload.get("peer_id", payload.get("user_id", current_peer_key() or f"{method}:{request_id}"))
        return {
            "delivery_id": request_id, "platform": "vk", "peer_key": str(peer), "method": method,
            "payload": payload, "metadata": metadata or {}, "ttl": DEFAULT_DELIVERY_TTL,
        }

    async def enqueue_vk(self, method: str, params: dict, *, request_id: str | None = None, metadata: dict | None = None) -> dict:
        prepared = await self.prepare_vk(method, params, request_id=request_id, metadata=metadata)
        return (await self.enqueue_many([prepared]))[0]

    async def execute_vk(self, api, method: str, params: dict, version=None):
        row = await self.enqueue_vk(method, params, metadata={"api_version": version})
        return await self.wait(row["delivery_id"])

    async def wait(self, request_id: str):
        event, references = self._waiters.get(request_id, (asyncio.Event(), 0))
        self._waiters[request_id] = (event, references + 1)
        try:
            while True:
                event.clear()
                row = await self.store.get(request_id)
                if row is None:
                    raise DeliveryExpiredError("История запроса уже очищена.")
                if row["status"] == "sent":
                    return json.loads(row["result_json"])
                if row["status"] == "expired":
                    raise DeliveryExpiredError("Истёк срок доставки запроса.")
                if row["status"] == "failed":
                    self._raise_failure(row)
                # Один общий проход проверяет результаты других процессов.
                # Тысяча ожидающих ответов не создаёт тысячи SQLite-соединений в секунду.
                with suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), timeout=30)
        finally:
            _, references = self._waiters[request_id]
            if references <= 1:
                self._waiters.pop(request_id, None)
            else:
                self._waiters[request_id] = (event, references - 1)

    def _raise_failure(self, row) -> None:
        error = json.loads(row["error_json"] or "{}")
        message = error.get("message", "Не удалось доставить запрос")
        if row["platform"] == "vk" and isinstance(error.get("code"), int):
            raise VKAPIError[error["code"]](error_msg=message)
        if row["platform"] == "telegram":
            error_type = getattr(telegram_errors, error.get("type", ""), None)
            if isinstance(error_type, type) and issubclass(error_type, telegram_errors.TelegramAPIError):
                params = {}
                if error.get("migrate_to_chat_id") is not None:
                    params["migrate_to_chat_id"] = error["migrate_to_chat_id"]
                method = telegram_method(row["method"], self.files.decode(json.loads(row["payload_json"])))
                raise error_type(method, message, **params)
        raise DeliveryFailedError(message)

    async def start(self) -> None:
        if self._pump is None or self._pump.done():
            self._stopping = False
            self._pump = asyncio.create_task(self._run(), name="delivery-outbox")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._pump is not None:
            await asyncio.wait([self._pump], timeout=3)
        tasks = {task for task in [self._pump, *self._tasks, *self._workers] if task is not None and task is not asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pump = None
        if self.broker is not None:
            await self.broker.close()

    def probe(self) -> tuple[bool, str]:
        waiting = self._last_counts.get("pending", 0) + self._last_counts.get("processing", 0)
        healthy = self._pump is not None and not self._pump.done()
        details = f"ожидают: {waiting}, доставлены: {self._last_counts.get('sent', 0)}, RabbitMQ: {'ok' if self._broker_ok else 'SQLite fallback'}"
        return healthy, details

    def _spawn(self, request_id: str) -> None:
        if request_id in self._fallback_active:
            return
        self._fallback_active.add(request_id)
        task = asyncio.create_task(self.process(request_id, via_broker=False), name="outbox-fallback")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _task: self._fallback_active.discard(request_id))

        def log_failure(done):
            if not done.cancelled() and done.exception() is not None:
                logger.warning("Задание outbox %s восстановится после истечения блокировки: %s", request_id, type(done.exception()).__name__)

        task.add_done_callback(log_failure)

    async def _run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                if self.broker is not None and self.broker.enabled and monotonic() >= self._next_broker_connect:
                    try:
                        async with asyncio.timeout(2):
                            await self.broker.start_consumer(self.process)
                        self._broker_ok = True
                        self._broker_error_at = None
                    except Exception as error:
                        if self._broker_error_at is None or monotonic() - self._broker_error_at >= 300:
                            logger.warning("RabbitMQ outbox недоступен, включена доставка из SQLite: %s", type(error).__name__)
                            self._broker_error_at = monotonic()
                        self._broker_ok = False
                        self._next_broker_connect = monotonic() + 30
                for platform in self._senders:
                    if self._blocked_until[platform] > time():
                        continue
                    for request_id in await self.store.reserve_due(platform):
                        if self._broker_ok and self.broker is not None and self.broker.enabled:
                            try:
                                async with asyncio.timeout(2):
                                    await self.broker.publish(platform, request_id)
                            except Exception:
                                self._broker_ok = False
                                self._spawn(request_id)
                        else:
                            self._spawn(request_id)
                if time() - self._last_cleanup >= 86400:
                    await self.cleanup()
                    self._last_cleanup = time()
                if monotonic() - self._last_count_refresh >= 5:
                    self._last_counts = await self.store.counts()
                    self._last_count_refresh = monotonic()
                statuses = await self.store.statuses(list(self._waiters))
                for request_id, status in statuses.items():
                    if status in TERMINAL_STATUSES and request_id in self._waiters:
                        self._waiters[request_id][0].set()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Сбой прохода outbox; сохранённые задания будут повторены.", exc_info=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)

    async def process(self, request_id: str, *, via_broker: bool = True) -> None:
        task = asyncio.current_task()
        self._workers.add(task)
        try:
            await self._process(request_id, via_broker=via_broker)
        finally:
            self._workers.discard(task)

    async def _process(self, request_id: str, *, via_broker: bool = True) -> None:
        if self._stopping:
            # Поздний callback уже закрывающегося consumer не начинает новый API-вызов.
            # Его ID можно подтвердить: незавершённое задание остаётся в SQLite.
            return
        initial = await self.store.get(request_id)
        if initial is None or initial["platform"] not in self._senders:
            return
        if self._blocked_until[initial["platform"]] > time():
            return
        async with self._semaphores[initial["platform"]]:
            lease = uuid4().hex
            row = await self.store.claim(request_id, lease)
            if row is None:
                return

            async def heartbeat():
                while True:
                    await asyncio.sleep(30)
                    if not await self.store.renew(request_id, lease):
                        return

            keep_alive = asyncio.create_task(heartbeat(), name="outbox-lease")
            try:
                try:
                    async with asyncio.timeout(min(90.0, max(0.01, row["expires_at"] - time()))):
                        result = await self._senders[row["platform"]](row)
                except Exception as error:
                    delay = retry_delay(error, row["attempts"])
                    if delay is not None and not isinstance(error, TelegramRetryAfter):
                        self._blocked_until[row["platform"]] = max(
                            self._blocked_until[row["platform"]], time() + min(delay, 30),
                        )
                    written = await self.store.finish(request_id, lease, error=error_payload(error), retry_delay=delay)
                    if written:
                        await complete_on_cancel(self._record(row, "failed", via_broker, error))
                        if delay is None:
                            logger.warning("Outbox %s: постоянная ошибка %s.", request_id, type(error).__name__)
                else:
                    # Ошибка SQLite после принятия API не является постоянной
                    # ошибкой получателя: lease сохранится и задание восстановится.
                    written = await self.store.finish(request_id, lease, result=json_value(result))
                    if written:
                        await complete_on_cancel(self._record(row, "sent", via_broker))
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.shield(self.store.release(request_id, lease))
                raise
            finally:
                keep_alive.cancel()
                await asyncio.gather(keep_alive, return_exceptions=True)
                if request_id in self._waiters:
                    self._waiters[request_id][0].set()
                self._wake.set()

    async def _record(self, row, status: str, via_broker: bool, error: BaseException | None = None) -> None:
        payload = json.loads(row["payload_json"])
        metadata = json.loads(row["metadata_json"])
        peer = payload.get("chat_id", payload.get("peer_id", payload.get("user_id", 0)))
        if not str(peer).lstrip("-").isdigit():
            return
        with suppress(Exception):
            await self.db.record_delivery_event(
                campaign_type=metadata.get("campaign_type", "interactive"), platform=row["platform"], user_id=int(peer),
                via_broker=via_broker, status=status, attempt=row["attempts"], message_id=row["delivery_id"],
                error_text=f"{type(error).__name__}: {error}" if error else None,
            )
        if error is not None:
            permanent = isinstance(error, telegram_errors.TelegramForbiddenError)
            if isinstance(error, VKAPIError):
                from src.vk_runtime import is_permanent_vk_delivery_error
                permanent = is_permanent_vk_delivery_error(error)
            if permanent and metadata.get("campaign_type") != "admin_notify":
                with suppress(Exception):
                    await self.db.mark_delivery_auto_disabled(row["platform"], int(peer), True)

    async def cleanup(self, *, now: float | None = None) -> int:
        stamp = time() if now is None else now
        total = 0
        while True:
            removed = await self.store.purge(now=stamp)
            for row in removed:
                await asyncio.to_thread(self.files.remove_payload_files, json.loads(row["payload_json"]))
            total += len(removed)
            if len(removed) < 500:
                break
        if self.files.root.exists():
            for path in self.files.root.iterdir():
                if (len(path.name) == 32 and all(char in "0123456789abcdef" for char in path.name)
                        and path.is_file() and not path.is_symlink() and path.resolve().parent == self.files.root
                        and path.stat().st_mtime < retention_cutoff(stamp) and not await self.store.file_in_use(path.name)):
                    await asyncio.to_thread(path.unlink, missing_ok=True)
        if total:
            logger.info("Outbox: очищено %s записей старше трёх месяцев.", total)
        return total

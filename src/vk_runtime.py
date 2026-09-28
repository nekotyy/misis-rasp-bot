"""Надёжный транспорт VK: HTTP-клиент, long poll, отправка сообщений, раздача событий.

Почему своё, а не голый vkbottle 4.8:

* у aiohttp-сессии vkbottle нет разумных таймаутов (дефолт aiohttp — 5 минут на
  запрос), и повисший long poll молча «глушил» бота на минуты;
* `BlockingRequestRescheduler` при не-JSON ответе VK (502/504 от балансировщика)
  делает `time.sleep()` — это замораживает весь процесс, включая Telegram;
* `BasePolling.listen()` после сетевой ошибки берёт у VK новый `ts`, и все
  сообщения, пришедшие за время сбоя, теряются — пользователь видит, что бот
  его «проигнорировал»;
* при неизвестной ошибке `listen()` крутится без паузы;
* `messages.send(peer_ids=[...])` возвращает ошибку доставки внутри ответа, а не
  исключением, и такие сообщения считались «успешно отправленными».
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from time import monotonic
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector
from vkbottle import API
from vkbottle.api.response_validator import VKAPIErrorResponseValidator
from vkbottle.api.response_validator.abc import ABCResponseValidator
from vkbottle.exception_factory.base_exceptions import VKAPIError
from vkbottle.http import AiohttpClient
from vkbottle.modules import json as vk_json
from vkbottle.polling import BotPolling

logger = logging.getLogger(__name__)

VK_MESSAGE_LIMIT = 4096
VK_CHAT_PEER_ID_THRESHOLD = 2_000_000_000

# Обычные запросы к API: не ограничиваем общий срок (выгрузка БД весит десятки
# мегабайт), но зависшее соединение без данных рвём через минуту.
VK_HTTP_TIMEOUT = ClientTimeout(total=None, sock_connect=15, sock_read=60)
# DNS на сервере (роутер) периодически не отвечает — держим адреса VK в кэше
# дольше, чтобы кратковременный сбой резолвера не ронял запросы.
VK_DNS_CACHE_SECONDS = 600

# Временные ошибки VK API — есть смысл повторить запрос с паузой.
VK_RETRYABLE_ERROR_CODES = frozenset({1, 6, 10, 603})
# Невалидная клавиатура: лучше показать текст без кнопок, чем не показать ничего.
VK_KEYBOARD_ERROR_CODES = frozenset({911, 912})
# Доставка невозможна в принципе: закрыл сообщения, удалён, бота убрали из беседы.
VK_PERMANENT_DELIVERY_ERROR_CODES = frozenset({7, 18, 900, 901, 902, 917, 936, 945, 946})

NETWORK_ERRORS: tuple[type[BaseException], ...] = (ClientError, TimeoutError, OSError)


class VkTransportError(ConnectionError):
    """VK ответил не-JSON (обычно 502/504 от балансировщика) — временный сбой сети."""


def vk_error_code(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def is_retryable_vk_error(exc: BaseException) -> bool:
    if isinstance(exc, VKAPIError):
        return vk_error_code(exc) in VK_RETRYABLE_ERROR_CODES
    return isinstance(exc, NETWORK_ERRORS)


def is_permanent_vk_delivery_error(exc: BaseException) -> bool:
    return isinstance(exc, VKAPIError) and vk_error_code(exc) in VK_PERMANENT_DELIVERY_ERROR_CODES


def is_vk_chat_peer(peer_id: int | None) -> bool:
    return bool(peer_id and peer_id >= VK_CHAT_PEER_ID_THRESHOLD)


class StrictJSONResponseValidator(ABCResponseValidator):
    """Замена `JSONResponseValidator` без блокирующего `time.sleep` внутри.

    Не-JSON ответ превращаем в `VkTransportError`: его ловят наши повторы
    (отправка сообщений, long poll) с обычным `asyncio.sleep`.
    """

    async def validate(self, method: str, data: dict[str, Any], response: Any, ctx_api: Any) -> Any:
        if isinstance(response, dict):
            return response
        if isinstance(response, str):
            try:
                parsed = vk_json.loads(response)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        preview = str(response)[:200].replace("\n", " ")
        raise VkTransportError(f"VK вернул не-JSON ответ на {method}: {preview}")


class ResilientAiohttpClient(AiohttpClient):
    """HTTP-клиент VK с таймаутами, DNS-кэшем и пересозданием закрытой сессии."""

    def __init__(self, *, verify_ssl: bool = True) -> None:
        super().__init__()
        self._verify_ssl = verify_ssl

    def _ensure_session(self) -> ClientSession:
        if self.session is None or self.session.closed:
            connector = TCPConnector(
                ssl=None if self._verify_ssl else False,
                ttl_dns_cache=VK_DNS_CACHE_SECONDS,
                limit=100,
            )
            self.session = ClientSession(
                connector=connector,
                timeout=VK_HTTP_TIMEOUT,
                json_serialize=self.json_processing_module.dumps,
            )
        return self.session

    @asynccontextmanager
    async def request(self, url: str, method: str = "GET", data: dict[str, Any] | None = None, **kwargs: Any):
        session = self._ensure_session()
        async with session.request(url=url, method=method, data=data, **kwargs) as response:
            yield response


def build_vk_api(token: str, *, verify_ssl: bool = True) -> API:
    api = API(token, http_client=ResilientAiohttpClient(verify_ssl=verify_ssl))
    # Список валидаторов по умолчанию — общий объект модуля vkbottle, поэтому
    # подменяем его копией только у своего API.
    api.response_validators = [StrictJSONResponseValidator(), VKAPIErrorResponseValidator()]
    return api


def split_vk_text(text: str, limit: int = VK_MESSAGE_LIMIT) -> list[str]:
    """Режет длинный текст на части не длиннее лимита VK, стараясь по границам строк."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n ")
    if rest:
        chunks.append(rest)
    return [chunk for chunk in chunks if chunk] or [text[:limit]]


def _extract_message_id(response: Any) -> int | None:
    payload = response.get("response") if isinstance(response, dict) else response
    if isinstance(payload, bool):
        return None
    if isinstance(payload, int):
        return payload
    if isinstance(payload, list) and payload:
        item = payload[0]
        if isinstance(item, dict):
            error = item.get("error")
            if error:
                code = error.get("code") or error.get("error_code") or 0
                raise VKAPIError[int(code)](error_msg=str(error.get("description") or error.get("error_msg") or error))
            message_id = item.get("message_id")
            return message_id if isinstance(message_id, int) else None
        if isinstance(item, int):
            return item
    return None


async def vk_call_with_retry(
    api: API,
    method: str,
    params: dict[str, Any],
    *,
    max_attempts: int = 4,
    base_delay: float = 1.0,
) -> Any:
    """Вызов метода VK с повтором временных ошибок (сеть, 6, 10, 502 и т.п.)."""
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            return await api.request(method, dict(params))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not is_retryable_vk_error(exc) or attempt >= max_attempts:
                raise
            logger.warning("VK %s: временная ошибка (попытка %s/%s): %s", method, attempt, max_attempts, exc)
        await asyncio.sleep(delay * random.uniform(0.8, 1.2))  # noqa: S311
        delay = min(delay * 2, 15.0)
    return None


async def vk_send_message(
    api: API,
    peer_id: int,
    message: str = "",
    *,
    keyboard: str | None = None,
    attachment: str | None = None,
    sticker_id: int | None = None,
    max_attempts: int = 4,
    base_delay: float = 1.0,
) -> int | None:
    """Отправляет сообщение в VK и возвращает id последнего отправленного.

    * `peer_id` (а не `peer_ids`) — чтобы ошибка доставки пришла исключением;
    * один `random_id` на все повторы одной части — VK отбросит дубль, если
      первый запрос дошёл, а ответ потерялся в сети;
    * длинный текст режется на части, клавиатура уходит с последней;
    * при невалидной клавиатуре (911/912) текст всё равно доставляется.
    """
    if sticker_id is not None:
        params = {"peer_id": peer_id, "sticker_id": int(sticker_id), "random_id": _random_id()}
        return _extract_message_id(await vk_call_with_retry(api, "messages.send", params, max_attempts=max_attempts, base_delay=base_delay))

    chunks = split_vk_text(message or "") if message else [""]
    last_id: int | None = None
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        params: dict[str, Any] = {"peer_id": peer_id, "message": chunk, "random_id": _random_id()}
        if keyboard is not None and is_last:
            params["keyboard"] = keyboard
        if attachment and index == 0:
            params["attachment"] = attachment
        try:
            response = await vk_call_with_retry(api, "messages.send", params, max_attempts=max_attempts, base_delay=base_delay)
        except VKAPIError as exc:
            if vk_error_code(exc) not in VK_KEYBOARD_ERROR_CODES or "keyboard" not in params:
                raise
            logger.error(
                "VK отклонил клавиатуру (код %s) для peer %s — отправляю текст без кнопок.",
                vk_error_code(exc),
                peer_id,
            )
            params.pop("keyboard", None)
            params["random_id"] = _random_id()
            response = await vk_call_with_retry(api, "messages.send", params, max_attempts=max_attempts, base_delay=base_delay)
        last_id = _extract_message_id(response)
    return last_id


async def vk_edit_message(api: API, peer_id: int, message_id: int, message: str, *, keyboard: str | None = None) -> bool:
    params: dict[str, Any] = {
        "peer_id": peer_id,
        "message_id": message_id,
        "message": split_vk_text(message)[0],
        "keep_forward_messages": 1,
    }
    if keyboard is not None:
        params["keyboard"] = keyboard
    try:
        await vk_call_with_retry(api, "messages.edit", params, max_attempts=2)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("VK messages.edit не удался для peer %s: %s", peer_id, exc)
        return False


def _random_id() -> int:
    return random.randint(1, 2**31 - 1)  # noqa: S311


PollingErrorCallback = Callable[[BaseException, int], Awaitable[None]]
PollingRecoveryCallback = Callable[[int, float], Awaitable[None]]


class ResilientBotPolling(BotPolling):
    """Long poll, который не теряет сообщения при сбоях и не зависает.

    * каждый запрос `a_check` ограничен по времени (wait + запас);
    * после ошибки получаем новый ключ сервера, но продолжаем с последнего `ts` —
      VK отдаст всё, что пришло за время сбоя;
    * любые ошибки — экспоненциальная пауза до `max_backoff` секунд;
    * `last_ok_at` — для мониторинга живости.
    """

    def __init__(
        self,
        api: API,
        *,
        group_id: int | None = None,
        wait: int = 25,
        max_backoff: float = 30.0,
        on_error: PollingErrorCallback | None = None,
        on_recovered: PollingRecoveryCallback | None = None,
    ) -> None:
        super().__init__(api=api, group_id=group_id, wait=wait)
        self.max_backoff = max_backoff
        self.on_error = on_error
        self.on_recovered = on_recovered
        self.last_ok_at: float | None = None
        self.started_at = monotonic()
        self.consecutive_failures = 0
        self._failure_started_at: float | None = None
        self._ts: str | None = None

    def construct(self, api: Any, error_handler: Any = None) -> ResilientBotPolling:
        # Bot.polling вызывает construct() на каждое обращение: API уже наш, error_handler не нужен.
        return self

    async def get_event(self, server: dict[str, Any]) -> dict[str, Any]:
        timeout = ClientTimeout(total=self.wait + 20, sock_connect=15, sock_read=self.wait + 20)
        return await self.api.http_client.request_json(
            url=f"{server['server']}?act=a_check&key={server['key']}&ts={server['ts']}&wait={self.wait}",
            method="POST",
            timeout=timeout,
        )

    async def _fresh_server(self, *, keep_ts: bool) -> dict[str, Any]:
        server = dict(await self.get_server())
        if keep_ts and self._ts is not None:
            server["ts"] = self._ts
        return server

    async def _handle_failed(self, server: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        failed = event.get("failed")
        if failed == 1:
            server["ts"] = event.get("ts", server.get("ts"))
            self._ts = server["ts"]
            return server
        if failed == 2:
            return await self._fresh_server(keep_ts=True)
        # 3 — информация о событиях потеряна, 4 — неверная версия: начинаем заново.
        logger.warning("VK long poll вернул failed=%s — беру новый сервер и ts.", failed)
        fresh = await self._fresh_server(keep_ts=False)
        self._ts = fresh.get("ts")
        return fresh

    async def _mark_ok(self) -> None:
        self.last_ok_at = monotonic()
        if self.consecutive_failures:
            failures = self.consecutive_failures
            outage = monotonic() - (self._failure_started_at or monotonic())
            self.consecutive_failures = 0
            self._failure_started_at = None
            logger.info("VK long poll восстановлен после %s ошибок (%.0f с).", failures, outage)
            if self.on_recovered is not None:
                try:
                    await self.on_recovered(failures, outage)
                except Exception:
                    logger.warning("Обработчик восстановления VK long poll упал.", exc_info=True)

    def backoff_delay(self) -> float:
        exponent = min(self.consecutive_failures - 1, 6)
        return min(self.max_backoff, float(2**exponent)) * random.uniform(0.7, 1.0)  # noqa: S311

    async def listen(self) -> AsyncGenerator[dict[str, Any], None]:  # type: ignore[override]
        self._stop_event = asyncio.Event()
        server: dict[str, Any] = {}
        while not self._stop_event.is_set():
            try:
                if not server:
                    server = await self._fresh_server(keep_ts=True)
                    if self._ts is None:
                        self._ts = server.get("ts")
                event = await self.get_event(server)
                if not isinstance(event, dict):
                    raise VkTransportError(f"long poll вернул {type(event).__name__}")
                if "failed" in event:
                    server = await self._handle_failed(server, event)
                    continue
                if "ts" not in event:
                    server = {}
                    continue
                server["ts"] = event["ts"]
                self._ts = event["ts"]
                await self._mark_ok()
                if event.get("updates"):
                    yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.consecutive_failures += 1
                if self._failure_started_at is None:
                    self._failure_started_at = monotonic()
                server = {}
                delay = self.backoff_delay()
                logger.warning(
                    "VK long poll: ошибка #%s (%s: %s), повтор через %.1f с.",
                    self.consecutive_failures,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                if self.on_error is not None:
                    try:
                        await self.on_error(exc, self.consecutive_failures)
                    except Exception:
                        logger.warning("Обработчик ошибок VK long poll упал.", exc_info=True)
                await asyncio.sleep(delay)


def extract_update_peer_id(update: dict[str, Any]) -> int | None:
    obj = update.get("object")
    if not isinstance(obj, dict):
        return None
    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    peer_id = message.get("peer_id") if isinstance(message, dict) else None
    return peer_id if isinstance(peer_id, int) else None


def update_dedup_key(update: dict[str, Any]) -> str | None:
    event_id = update.get("event_id")
    if event_id:
        return f"e:{event_id}"
    obj = update.get("object")
    message = obj.get("message") if isinstance(obj, dict) and isinstance(obj.get("message"), dict) else None
    if message and message.get("peer_id") is not None and message.get("conversation_message_id") is not None:
        return f"m:{message['peer_id']}:{message['conversation_message_id']}"
    return None


HandlerFailureCallback = Callable[[dict[str, Any], BaseException], Awaitable[None]]


class VkUpdateDispatcher:
    """Раздаёт события long poll обработчикам vkbottle.

    * каждое событие — отдельная задача со строгой ссылкой (иначе asyncio может
      собрать её сборщиком мусора посреди работы);
    * события одного диалога обрабатываются строго по очереди — ответы не
      приходят вперемешку, и у пользователя не остаётся «чужая» клавиатура;
    * обработчик ограничен по времени: зависший запрос не блокирует диалог навсегда;
    * повторно пришедшее событие (после восстановления long poll) не обрабатывается дважды.
    """

    def __init__(
        self,
        route: Callable[[dict[str, Any]], Awaitable[Any]],
        *,
        handler_timeout: float = 300.0,
        on_failure: HandlerFailureCallback | None = None,
        dedup_size: int = 2000,
    ) -> None:
        self._route = route
        self.handler_timeout = handler_timeout
        self.on_failure = on_failure
        self._tasks: set[asyncio.Task] = set()
        self._peer_locks: dict[int, asyncio.Lock] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dedup_size = dedup_size

    @property
    def pending_tasks(self) -> int:
        return len(self._tasks)

    def is_duplicate(self, update: dict[str, Any]) -> bool:
        key = update_dedup_key(update)
        if key is None:
            return False
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > self._dedup_size:
            self._seen.popitem(last=False)
        return False

    def dispatch(self, update: dict[str, Any]) -> asyncio.Task | None:
        if self.is_duplicate(update):
            logger.info("VK: повторное событие %s пропущено.", update_dedup_key(update))
            return None
        task = asyncio.create_task(self._process(update), name="vk-update")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _lock_for(self, peer_id: int | None):
        if peer_id is None:
            return nullcontext()
        lock = self._peer_locks.get(peer_id)
        if lock is None:
            if len(self._peer_locks) > 5000:
                for key in [key for key, value in self._peer_locks.items() if not value.locked()]:
                    self._peer_locks.pop(key, None)
            lock = self._peer_locks[peer_id] = asyncio.Lock()
        return lock

    async def _process(self, update: dict[str, Any]) -> None:
        async with self._lock_for(extract_update_peer_id(update)):
            try:
                await asyncio.wait_for(self._route(update), timeout=self.handler_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    logger.error(
                        "VK: обработка события для peer %s не уложилась в %.0f с и прервана.",
                        extract_update_peer_id(update),
                        self.handler_timeout,
                        extra={"skip_admin_report": True},
                    )
                else:
                    logger.error("VK: обработка события упала.", exc_info=exc, extra={"skip_admin_report": True})
                if self.on_failure is not None:
                    try:
                        await self.on_failure(update, exc)
                    except Exception:
                        logger.warning("Обработчик сбоев VK-событий упал.", exc_info=True)

    async def run(self, polling: ResilientBotPolling) -> None:
        async for event in polling.listen():
            for update in event.get("updates", []) or []:
                if isinstance(update, dict):
                    self.dispatch(update)

    async def drain(self, timeout: float = 10.0) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=timeout)
        for task in pending:
            task.cancel()

"""Контекст исходящих действий одного входящего события, без токенов и сетевых клиентов."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class _DeliveryContext:
    event_id: str
    peer_key: str | None = None
    calls: Counter = field(default_factory=Counter)


_context: ContextVar[_DeliveryContext | None] = ContextVar("outbox_context", default=None)


@contextmanager
def delivery_context(event_id: str | None, peer_key: str | None = None):
    token = _context.set(_DeliveryContext(event_id, peer_key) if event_id else None)
    try:
        yield
    finally:
        _context.reset(token)


def current_peer_key() -> str | None:
    context = _context.get()
    return context.peer_key if context else None


def delivery_id(platform: str, method: str, payload: dict[str, Any]) -> str:
    context = _context.get()
    if context is None:
        return uuid4().hex
    fingerprint = hashlib.sha256(
        json.dumps([platform, method, payload], ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    context.calls[fingerprint] += 1
    return hashlib.sha256(f"{context.event_id}:{fingerprint}:{context.calls[fingerprint]}".encode()).hexdigest()

"""SQLite — источник истины для доставки; сообщения RabbitMQ содержат только ID задания."""

from __future__ import annotations

import asyncio
import calendar
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import time
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from src.db import Database

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    peer_key TEXT NOT NULL,
    method TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL,
    next_publish_at REAL NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    result_json TEXT,
    error_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_due
    ON delivery_outbox(platform, status, next_attempt_at, next_publish_at);
CREATE INDEX IF NOT EXISTS idx_outbox_peer
    ON delivery_outbox(platform, peer_key, status, id);
CREATE INDEX IF NOT EXISTS idx_outbox_retention ON delivery_outbox(created_at);
"""

TERMINAL_STATUSES = frozenset({"sent", "failed", "expired"})


async def complete_on_cancel(awaitable):
    """Закрытие SQLite/короткая транзакция завершаются и при остановке задачи."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def retention_cutoff(now: float) -> float:
    """Ровно три календарных месяца с корректировкой последнего дня месяца."""
    current = datetime.fromtimestamp(now, tz=UTC)
    month_index = current.year * 12 + current.month - 1 - 3
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return current.replace(year=year, month=month, day=min(current.day, calendar.monthrange(year, month)[1])).timestamp()


class OutboxStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @asynccontextmanager
    async def _connect(self):
        connection = self.db._connect()
        try:
            await complete_on_cancel(connection)
        except asyncio.CancelledError:
            await connection.close()
            raise
        try:
            yield connection
        finally:
            await complete_on_cancel(connection.close())

    async def enqueue(
        self, delivery_id: str, platform: str, peer_key: str, method: str,
        payload: dict, *, ttl: float = 86400, metadata: dict | None = None, now: float | None = None,
    ) -> dict:
        rows = await self.enqueue_many([{
            "delivery_id": delivery_id, "platform": platform, "peer_key": peer_key, "method": method,
            "payload": payload, "ttl": ttl, "metadata": metadata or {},
        }], now=now)
        return rows[0]

    async def enqueue_many(self, items: list[dict], *, now: float | None = None) -> list[dict]:
        """Все части ответа/адресаты рассылки фиксируются одной транзакцией."""
        stamp = time() if now is None else now
        rows = []
        async with self._connect() as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            for item in items:
                await connection.execute(
                    """INSERT INTO delivery_outbox
                       (delivery_id, platform, peer_key, method, payload_json, metadata_json,
                        created_at, updated_at, expires_at, next_attempt_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(delivery_id) DO NOTHING""",
                    (item["delivery_id"], item["platform"], item["peer_key"], item["method"],
                     json.dumps(item["payload"], ensure_ascii=False), json.dumps(item.get("metadata", {}), ensure_ascii=False),
                     stamp, stamp, stamp + item.get("ttl", 86400), stamp),
                )
                row = await (await connection.execute(
                    "SELECT * FROM delivery_outbox WHERE delivery_id = ?", (item["delivery_id"],)
                )).fetchone()
                rows.append(dict(row))
            await connection.commit()
        return rows

    async def get(self, delivery_id: str) -> dict | None:
        async with self._connect() as connection:
            connection.row_factory = aiosqlite.Row
            row = await (await connection.execute(
                "SELECT * FROM delivery_outbox WHERE delivery_id = ?", (delivery_id,)
            )).fetchone()
        return dict(row) if row else None

    @staticmethod
    async def _expire(connection, now: float) -> None:
        await connection.execute(
            """UPDATE delivery_outbox SET status = 'expired', updated_at = ?, lease_token = NULL, lease_until = 0
               WHERE status IN ('pending', 'processing') AND expires_at <= ? AND lease_until <= ?""",
            (now, now, now),
        )

    async def reserve_due(self, platform: str, *, now: float | None = None, limit: int = 50) -> list[str]:
        stamp = time() if now is None else now
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await self._expire(connection, stamp)
            rows = await (await connection.execute(
                """SELECT o.delivery_id FROM delivery_outbox AS o
                   WHERE o.platform = ? AND o.status IN ('pending', 'processing')
                     AND o.next_attempt_at <= ? AND o.next_publish_at <= ? AND o.lease_until <= ?
                     AND o.expires_at > ?
                     AND NOT EXISTS (
                         SELECT 1 FROM delivery_outbox AS earlier
                         WHERE earlier.platform = o.platform AND earlier.peer_key = o.peer_key
                           AND earlier.id < o.id AND earlier.status IN ('pending', 'processing')
                     )
                   ORDER BY o.id LIMIT ?""",
                (platform, stamp, stamp, stamp, stamp, limit),
            )).fetchall()
            ids = [row[0] for row in rows]
            await connection.executemany(
                "UPDATE delivery_outbox SET next_publish_at = ? WHERE delivery_id = ?",
                [(stamp + 30, item) for item in ids],
            )
            await connection.commit()
        return ids

    async def publication_failed(self, delivery_id: str, *, now: float | None = None) -> None:
        stamp = time() if now is None else now
        async with self._connect() as connection:
            await connection.execute(
                "UPDATE delivery_outbox SET next_publish_at = ? WHERE delivery_id = ? AND status = 'pending'",
                (stamp + 5, delivery_id),
            )
            await connection.commit()

    async def claim(self, delivery_id: str, lease_token: str, *, now: float | None = None, lease_seconds: float = 120) -> dict | None:
        stamp = time() if now is None else now
        async with self._connect() as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            await self._expire(connection, stamp)
            cursor = await connection.execute(
                """UPDATE delivery_outbox AS o
                   SET status = 'processing', attempts = attempts + 1, lease_token = ?, lease_until = ?, updated_at = ?
                   WHERE delivery_id = ? AND status IN ('pending', 'processing')
                     AND next_attempt_at <= ? AND lease_until <= ? AND expires_at > ?
                     AND NOT EXISTS (
                         SELECT 1 FROM delivery_outbox AS earlier
                         WHERE earlier.platform = o.platform AND earlier.peer_key = o.peer_key
                           AND earlier.id < o.id AND earlier.status IN ('pending', 'processing')
                     )""",
                (lease_token, stamp + lease_seconds, stamp, delivery_id, stamp, stamp, stamp),
            )
            row = None
            if cursor.rowcount:
                row = await (await connection.execute(
                    "SELECT * FROM delivery_outbox WHERE delivery_id = ?", (delivery_id,)
                )).fetchone()
            await connection.commit()
        return dict(row) if row else None

    async def renew(self, delivery_id: str, lease_token: str, *, now: float | None = None) -> bool:
        stamp = time() if now is None else now
        async with self._connect() as connection:
            cursor = await connection.execute(
                """UPDATE delivery_outbox SET lease_until = ? WHERE delivery_id = ?
                   AND status = 'processing' AND lease_token = ?""", (stamp + 120, delivery_id, lease_token),
            )
            await connection.commit()
        return bool(cursor.rowcount)

    async def finish(
        self, delivery_id: str, lease_token: str, *, result: Any = None, error: dict | None = None,
        retry_delay: float | None = None, now: float | None = None,
    ) -> bool:
        stamp = time() if now is None else now
        status = "pending" if retry_delay is not None else ("failed" if error else "sent")
        async with self._connect() as connection:
            cursor = await connection.execute(
                """UPDATE delivery_outbox
                   SET status = ?, updated_at = ?, next_attempt_at = ?, next_publish_at = 0,
                       lease_token = NULL, lease_until = 0, result_json = ?, error_json = ?
                   WHERE delivery_id = ? AND status = 'processing' AND lease_token = ?""",
                (status, stamp, stamp + (retry_delay or 0), json.dumps(result, ensure_ascii=False),
                 json.dumps(error, ensure_ascii=False) if error else None, delivery_id, lease_token),
            )
            await connection.commit()
        return bool(cursor.rowcount)

    async def release(self, delivery_id: str, lease_token: str) -> None:
        async with self._connect() as connection:
            await connection.execute(
                """UPDATE delivery_outbox SET status = 'pending', lease_token = NULL, lease_until = 0, next_publish_at = 0
                   WHERE delivery_id = ? AND status = 'processing' AND lease_token = ?""", (delivery_id, lease_token),
            )
            await connection.commit()

    async def counts(self) -> dict[str, int]:
        async with self._connect() as connection:
            rows = await (await connection.execute(
                "SELECT status, count(*) FROM delivery_outbox GROUP BY status"
            )).fetchall()
        return dict(rows)

    async def statuses(self, delivery_ids: list[str]) -> dict[str, str]:
        result = {}
        async with self._connect() as connection:
            for start in range(0, len(delivery_ids), 500):
                batch = delivery_ids[start:start + 500]
                marks = ",".join("?" for _ in batch)
                rows = await (await connection.execute(
                    f"SELECT delivery_id, status FROM delivery_outbox WHERE delivery_id IN ({marks})", batch,
                )).fetchall()
                result.update(rows)
        return result

    async def file_in_use(self, name: str) -> bool:
        async with self._connect() as connection:
            row = await (await connection.execute(
                "SELECT 1 FROM delivery_outbox WHERE payload_json LIKE ? LIMIT 1", (f'%"{name}"%',)
            )).fetchone()
        return bool(row)

    async def purge(self, *, now: float | None = None, limit: int = 500) -> list[dict]:
        stamp = time() if now is None else now
        async with self._connect() as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            rows = await (await connection.execute(
                "SELECT delivery_id, payload_json FROM delivery_outbox WHERE created_at < ? AND lease_until <= ? ORDER BY id LIMIT ?",
                (retention_cutoff(stamp), stamp, limit),
            )).fetchall()
            ids = [row["delivery_id"] for row in rows]
            await connection.executemany("DELETE FROM delivery_outbox WHERE delivery_id = ?", [(item,) for item in ids])
            await connection.commit()
        return [dict(row) for row in rows]

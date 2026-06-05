from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Any

import aio_pika
import aiosqlite
import httpx


async def collect_metrics(db_path: Path, *, rabbitmq_url: str, telegram_token: str, vk_token: str, started_at: datetime) -> dict[str, Any]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        users = await _fetch_all(db, "SELECT * FROM users ORDER BY created_at DESC")
        active_groups = await _fetch_all(
            db,
            """
            SELECT subscription_title, subscription_key, COUNT(*) AS users_count
            FROM users
            WHERE subscription_type = 'group' AND subscription_key IS NOT NULL
            GROUP BY subscription_key, subscription_title
            ORDER BY users_count DESC, subscription_title ASC
            """,
        )
        latest_parse = await _fetch_one(
            db,
            """
            SELECT source_title, source_key, schedule_id, fetched_at, created_at
            FROM schedule_snapshots
            WHERE snapshot_type = 'current'
            ORDER BY id DESC
            LIMIT 1
            """,
        )
        latest_change = await _fetch_one(db, "SELECT * FROM change_events ORDER BY id DESC LIMIT 1")
        changes = await _fetch_all(db, "SELECT * FROM change_events ORDER BY id DESC LIMIT 30")
        delivery_today = await _delivery_stats(db, today_only=True)
        delivery_total = await _delivery_stats(db, today_only=False)
        lesson_status = await _lesson_counter_status(db)

    tg_users = [user for user in users if user["platform"] == "telegram"]
    vk_users = [user for user in users if user["platform"] == "vk"]
    now = datetime.now()
    new_threshold = (now - timedelta(days=7)).isoformat(timespec="seconds")
    teacher_subscriptions = [user for user in users if user["subscription_type"] == "teacher"]
    group_subscriptions = [user for user in users if user["subscription_type"] == "group"]

    return {
        "uptime_seconds": int((now - started_at).total_seconds()),
        "users": {
            "total": len(users),
            "telegram": len(tg_users),
            "vk": len(vk_users),
            "new_7d": sum(1 for user in users if str(user["created_at"] or "") >= new_threshold),
            "old": sum(1 for user in users if str(user["created_at"] or "") < new_threshold),
            "teachers": len(teacher_subscriptions),
            "groups": len(group_subscriptions),
            "auto_disabled": sum(1 for user in users if int(user["delivery_disabled_auto"] or 0) == 1),
        },
        "user_rows": [_user_row(user, new_threshold) for user in users],
        "services": {
            "telegram": await _telegram_status(telegram_token),
            "vk": await _vk_status(vk_token),
            "rabbitmq": await _rabbitmq_status(rabbitmq_url),
        },
        "schedule": {
            "latest_parse": dict(latest_parse) if latest_parse else None,
            "latest_change": _change_row(latest_change) if latest_change else None,
            "changes": [_change_row(row) for row in changes],
            "active_groups_total": len(active_groups),
            "active_groups": [dict(row) for row in active_groups],
        },
        "delivery": {
            "today": delivery_today,
            "total": delivery_total,
        },
        "lesson_counters": lesson_status,
        "extra": {
            "quiet_users": await _quiet_users_count(db_path),
        },
    }


async def _fetch_all(db: aiosqlite.Connection, query: str, params: tuple = ()) -> list[aiosqlite.Row]:
    cursor = await db.execute(query, params)
    return await cursor.fetchall()


async def _fetch_one(db: aiosqlite.Connection, query: str, params: tuple = ()) -> aiosqlite.Row | None:
    cursor = await db.execute(query, params)
    return await cursor.fetchone()


async def _delivery_stats(db: aiosqlite.Connection, *, today_only: bool) -> dict[str, int]:
    where = "WHERE created_at LIKE ?" if today_only else ""
    params = (f"{datetime.now().date().isoformat()}%",) if today_only else ()
    row = await _fetch_one(
        db,
        f"""
        SELECT
            SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
            SUM(CASE WHEN status = 'sent' AND via_broker = 1 THEN 1 ELSE 0 END) AS via_broker,
            SUM(CASE WHEN status = 'sent' AND via_broker = 0 THEN 1 ELSE 0 END) AS direct,
            SUM(CASE WHEN status = 'sent' AND platform = 'telegram' THEN 1 ELSE 0 END) AS telegram,
            SUM(CASE WHEN status = 'sent' AND platform = 'vk' THEN 1 ELSE 0 END) AS vk,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM delivery_events
        {where}
        """,
        params,
    )
    return {key: int(row[key] or 0) for key in ["sent", "via_broker", "direct", "telegram", "vk", "failed"]} if row else {}


async def _lesson_counter_status(db: aiosqlite.Connection) -> dict[str, Any]:
    counters = await _fetch_all(db, "SELECT * FROM lesson_counters ORDER BY schedule_id, subject")
    last_event = await _fetch_one(db, "SELECT * FROM lesson_counter_events ORDER BY id DESC LIMIT 1")
    today = datetime.now().date().isoformat()
    counted_today = await _fetch_one(db, "SELECT COUNT(*) AS count FROM lesson_counter_events WHERE created_at LIKE ?", (f"{today}%",))
    return {
        "configured": len(counters),
        "groups": len({row["schedule_id"] for row in counters if row["schedule_id"] is not None}),
        "counted_today": int(counted_today["count"] or 0) if counted_today else 0,
        "last_event": dict(last_event) if last_event else None,
        "counters": [dict(row) for row in counters],
    }


async def _telegram_status(token: str) -> dict[str, Any]:
    if not token:
        return {"ok": False, "label": "token missing"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        payload = response.json()
        return {"ok": bool(payload.get("ok")), "label": payload.get("result", {}).get("username") or response.status_code}
    except Exception as exc:
        return {"ok": False, "label": type(exc).__name__}


async def _vk_status(token: str) -> dict[str, Any]:
    if not token:
        return {"ok": False, "label": "token missing"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://api.vk.com/method/groups.getById", params={"access_token": token, "v": "5.199"})
        payload = response.json()
        return {"ok": "error" not in payload, "label": payload.get("error", {}).get("error_msg") or "ok"}
    except Exception as exc:
        return {"ok": False, "label": type(exc).__name__}


async def _rabbitmq_status(url: str) -> dict[str, Any]:
    if not url:
        return {"ok": False, "label": "disabled"}
    connection = None
    try:
        connection = await aio_pika.connect_robust(url, timeout=5)
        return {"ok": True, "label": "connected"}
    except Exception as exc:
        return {"ok": False, "label": type(exc).__name__}
    finally:
        if connection is not None and not connection.is_closed:
            await connection.close()


def _user_row(user: aiosqlite.Row, new_threshold: str) -> dict[str, Any]:
    source = dict(user)
    row = {
        "platform": source.get("platform"),
        "user_id": source.get("user_id"),
        "username": source.get("username"),
        "full_name": source.get("full_name"),
        "subscription_title": source.get("subscription_title"),
        "subscription_type": source.get("subscription_type"),
        "created_at": source.get("created_at"),
        "last_seen_at": source.get("last_seen_at"),
    }
    row["is_new"] = str(row.get("created_at") or "") >= new_threshold
    return row


def _change_row(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("changed_dates_json", "payload_json"):
        if key in data:
            data[key] = data[key][:400] if data[key] else ""
    return data


async def _quiet_users_count(db_path: Path) -> int:
    threshold = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        row = await _fetch_one(db, "SELECT COUNT(*) AS count FROM users WHERE last_seen_at < ?", (threshold,))
    return int(row["count"] or 0) if row else 0

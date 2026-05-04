from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from src.models import ScheduleSnapshot, UserRecord


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS users (
                    platform TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    subscription_type TEXT,
                    subscription_key TEXT,
                    subscription_title TEXT,
                    subscription_url TEXT,
                    group_name TEXT,
                    schedule_id INTEGER,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_editor INTEGER NOT NULL DEFAULT 0,
                    homework_notifications_enabled INTEGER NOT NULL DEFAULT 1,
                    delivery_disabled_auto INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (platform, user_id)
                );

                CREATE TABLE IF NOT EXISTS schedule_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_type TEXT NOT NULL,
                    source_type TEXT,
                    source_key TEXT,
                    source_title TEXT,
                    source_url TEXT,
                    group_name TEXT,
                    schedule_id INTEGER,
                    snapshot_hash TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS change_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT,
                    source_key TEXT,
                    source_title TEXT,
                    source_url TEXT,
                    group_name TEXT,
                    schedule_id INTEGER,
                    snapshot_hash TEXT NOT NULL,
                    message TEXT NOT NULL,
                    changed_dates_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS delivery_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT,
                    campaign_type TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    via_broker INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    error_text TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lesson_counters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER,
                    subject TEXT NOT NULL,
                    teacher TEXT NOT NULL,
                    subject_norm TEXT NOT NULL,
                    teacher_norm TEXT NOT NULL,
                    passed_count INTEGER NOT NULL DEFAULT 0,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(schedule_id, subject_norm, teacher_norm)
                );

                CREATE TABLE IF NOT EXISTS lesson_counter_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    counter_id INTEGER NOT NULL,
                    schedule_id INTEGER,
                    date_iso TEXT NOT NULL,
                    lesson_number INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    teacher TEXT NOT NULL,
                    classroom TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(counter_id, date_iso, lesson_number),
                    FOREIGN KEY(counter_id) REFERENCES lesson_counters(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS web_users (
                    login TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    permissions_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            await self._ensure_column(db, "users", "group_name", "TEXT")
            await self._ensure_column(db, "users", "schedule_id", "INTEGER")
            await self._ensure_column(db, "users", "subscription_type", "TEXT")
            await self._ensure_column(db, "users", "subscription_key", "TEXT")
            await self._ensure_column(db, "users", "subscription_title", "TEXT")
            await self._ensure_column(db, "users", "subscription_url", "TEXT")
            await self._ensure_column(db, "users", "is_editor", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "users", "homework_notifications_enabled", "INTEGER NOT NULL DEFAULT 1")
            await self._ensure_column(db, "users", "delivery_disabled_auto", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "schedule_snapshots", "source_type", "TEXT")
            await self._ensure_column(db, "schedule_snapshots", "source_key", "TEXT")
            await self._ensure_column(db, "schedule_snapshots", "source_title", "TEXT")
            await self._ensure_column(db, "schedule_snapshots", "source_url", "TEXT")
            await self._ensure_column(db, "schedule_snapshots", "group_name", "TEXT")
            await self._ensure_column(db, "schedule_snapshots", "schedule_id", "INTEGER")
            await self._ensure_column(db, "change_events", "source_type", "TEXT")
            await self._ensure_column(db, "change_events", "source_key", "TEXT")
            await self._ensure_column(db, "change_events", "source_title", "TEXT")
            await self._ensure_column(db, "change_events", "source_url", "TEXT")
            await self._ensure_column(db, "change_events", "group_name", "TEXT")
            await self._ensure_column(db, "change_events", "schedule_id", "INTEGER")
            await self._ensure_column(db, "delivery_events", "message_id", "TEXT")
            await self._ensure_column(db, "delivery_events", "campaign_type", "TEXT NOT NULL DEFAULT 'notification'")
            await self._ensure_column(db, "delivery_events", "platform", "TEXT NOT NULL DEFAULT 'telegram'")
            await self._ensure_column(db, "delivery_events", "user_id", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "delivery_events", "via_broker", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "delivery_events", "status", "TEXT NOT NULL DEFAULT 'sent'")
            await self._ensure_column(db, "delivery_events", "attempt", "INTEGER NOT NULL DEFAULT 1")
            await self._ensure_column(db, "delivery_events", "error_text", "TEXT")
            await self._ensure_column(db, "delivery_events", "created_at", "TEXT")
            await self._ensure_column(db, "lesson_counters", "schedule_id", "INTEGER")
            await self._ensure_column(db, "lesson_counters", "subject", "TEXT NOT NULL DEFAULT ''")
            await self._ensure_column(db, "lesson_counters", "teacher", "TEXT NOT NULL DEFAULT ''")
            await self._ensure_column(db, "lesson_counters", "subject_norm", "TEXT NOT NULL DEFAULT ''")
            await self._ensure_column(db, "lesson_counters", "teacher_norm", "TEXT NOT NULL DEFAULT ''")
            await self._ensure_column(db, "lesson_counters", "passed_count", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "lesson_counters", "total_count", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "lesson_counters", "created_at", "TEXT")
            await self._ensure_column(db, "lesson_counters", "updated_at", "TEXT")
            await self._ensure_column(db, "lesson_counter_events", "counter_id", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "lesson_counter_events", "schedule_id", "INTEGER")
            await self._ensure_column(db, "lesson_counter_events", "date_iso", "TEXT NOT NULL DEFAULT ''")
            await self._ensure_column(db, "lesson_counter_events", "lesson_number", "INTEGER NOT NULL DEFAULT 0")
            await self._ensure_column(db, "lesson_counter_events", "subject", "TEXT NOT NULL DEFAULT ''")
            await self._ensure_column(db, "lesson_counter_events", "teacher", "TEXT NOT NULL DEFAULT ''")
            await self._ensure_column(db, "lesson_counter_events", "classroom", "TEXT")
            await self._ensure_column(db, "lesson_counter_events", "created_at", "TEXT")
            await db.execute(
                """
                UPDATE users
                SET
                    subscription_type = COALESCE(subscription_type, CASE WHEN schedule_id IS NOT NULL THEN 'group' END),
                    subscription_key = COALESCE(subscription_key, CASE WHEN schedule_id IS NOT NULL THEN 'group:' || schedule_id END),
                    subscription_title = COALESCE(subscription_title, group_name),
                    subscription_url = COALESCE(subscription_url, CASE WHEN schedule_id IS NOT NULL THEN 'rasp:' || schedule_id END)
                """
            )
            await db.execute(
                """
                UPDATE schedule_snapshots
                SET
                    source_type = COALESCE(source_type, CASE WHEN schedule_id IS NOT NULL THEN 'group' END),
                    source_key = COALESCE(source_key, CASE WHEN schedule_id IS NOT NULL THEN 'group:' || schedule_id END),
                    source_title = COALESCE(source_title, group_name),
                    source_url = COALESCE(source_url, CASE WHEN schedule_id IS NOT NULL THEN 'rasp:' || schedule_id END)
                """
            )
            await db.execute(
                """
                UPDATE change_events
                SET
                    source_type = COALESCE(source_type, CASE WHEN schedule_id IS NOT NULL THEN 'group' END),
                    source_key = COALESCE(source_key, CASE WHEN schedule_id IS NOT NULL THEN 'group:' || schedule_id END),
                    source_title = COALESCE(source_title, group_name),
                    source_url = COALESCE(source_url, CASE WHEN schedule_id IS NOT NULL THEN 'rasp:' || schedule_id END)
                """
            )
            await db.execute(
                """
                DELETE FROM change_events
                WHERE id IN (
                    SELECT newer.id
                    FROM change_events AS newer
                    JOIN change_events AS older
                        ON newer.source_key = older.source_key
                        AND newer.snapshot_hash = older.snapshot_hash
                        AND substr(newer.created_at, 1, 10) = substr(older.created_at, 1, 10)
                        AND newer.source_key IS NOT NULL
                        AND newer.id > older.id
                )
                """
            )
            await db.execute(
                """
                DROP INDEX IF EXISTS idx_change_events_source_key_snapshot_hash
                """
            )
            await db.execute(
                """
                DROP INDEX IF EXISTS idx_change_events_source_key_snapshot_hash_day
                """
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_change_events_source_key_snapshot_hash_day
                ON change_events(source_key, snapshot_hash, substr(created_at, 1, 10))
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_events_status_campaign
                ON delivery_events(status, campaign_type)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_events_status_broker
                ON delivery_events(status, via_broker)
                """
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_lesson_counters_source
                ON lesson_counters(schedule_id, subject_norm, teacher_norm)
                """
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_lesson_counter_events_once
                ON lesson_counter_events(counter_id, date_iso, lesson_number)
                """
            )
            await db.commit()

    async def _ensure_column(self, db: aiosqlite.Connection, table_name: str, column_name: str, definition: str) -> None:
        cursor = await db.execute(f"PRAGMA table_info({table_name})")
        columns = await cursor.fetchall()
        if any(column[1] == column_name for column in columns):
            return
        await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    async def upsert_user(
        self,
        platform: str,
        user_id: int,
        username: str | None,
        full_name: str | None,
        subscription_type: str | None = None,
        subscription_key: str | None = None,
        subscription_title: str | None = None,
        subscription_url: str | None = None,
        group_name: str | None = None,
        schedule_id: int | None = None,
        is_admin: bool = False,
        is_editor: bool = False,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users (
                    platform, user_id, username, full_name, subscription_type, subscription_key, subscription_title, subscription_url, group_name, schedule_id, is_admin, is_editor, homework_notifications_enabled, delivery_disabled_auto, created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    subscription_type = COALESCE(excluded.subscription_type, users.subscription_type),
                    subscription_key = COALESCE(excluded.subscription_key, users.subscription_key),
                    subscription_title = COALESCE(excluded.subscription_title, users.subscription_title),
                    subscription_url = COALESCE(excluded.subscription_url, users.subscription_url),
                    group_name = COALESCE(excluded.group_name, users.group_name),
                    schedule_id = COALESCE(excluded.schedule_id, users.schedule_id),
                    is_admin = excluded.is_admin,
                    is_editor = COALESCE(users.is_editor, excluded.is_editor),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    platform,
                    user_id,
                    username,
                    full_name,
                    subscription_type,
                    subscription_key,
                    subscription_title,
                    subscription_url,
                    group_name,
                    schedule_id,
                    int(is_admin),
                    int(is_editor),
                    1,
                    0,
                    now,
                    now,
                ),
            )
            await db.commit()

    async def list_users(
        self,
        platform: str | None = None,
        schedule_id: int | None = None,
        subscription_key: str | None = None,
    ) -> list[UserRecord]:
        query = """
            SELECT
                platform,
                user_id,
                username,
                full_name,
                subscription_type,
                subscription_key,
                subscription_title,
                subscription_url,
                group_name,
                schedule_id,
                is_admin,
                is_editor,
                homework_notifications_enabled,
                delivery_disabled_auto,
                created_at,
                last_seen_at
            FROM users
        """
        clauses: list[str] = []
        params: list[object] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if schedule_id is not None:
            clauses.append("schedule_id = ?")
            params.append(schedule_id)
        if subscription_key is not None:
            clauses.append("subscription_key = ?")
            params.append(subscription_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY platform, created_at"

        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()

        return [
            UserRecord(
                platform=row[0],
                user_id=row[1],
                username=row[2],
                full_name=row[3],
                subscription_type=row[4],
                subscription_key=row[5],
                subscription_title=row[6],
                subscription_url=row[7],
                group_name=row[8],
                schedule_id=row[9],
                is_admin=bool(row[10]),
                is_editor=bool(row[11]),
                homework_notifications_enabled=bool(row[12]),
                delivery_disabled_auto=bool(row[13]),
                created_at=row[14],
                last_seen_at=row[15],
            )
            for row in rows
        ]

    async def get_users_for_platform(
        self,
        platform: str,
        schedule_id: int | None = None,
        subscription_key: str | None = None,
    ) -> list[UserRecord]:
        return await self.list_users(platform=platform, schedule_id=schedule_id, subscription_key=subscription_key)

    async def get_user(self, platform: str, user_id: int) -> UserRecord | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT
                    platform,
                    user_id,
                    username,
                    full_name,
                    subscription_type,
                    subscription_key,
                    subscription_title,
                    subscription_url,
                    group_name,
                    schedule_id,
                    is_admin,
                    is_editor,
                    homework_notifications_enabled,
                    delivery_disabled_auto,
                    created_at,
                    last_seen_at
                FROM users
                WHERE platform = ? AND user_id = ?
                LIMIT 1
                """,
                (platform, user_id),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return UserRecord(
            platform=row[0],
            user_id=row[1],
            username=row[2],
            full_name=row[3],
            subscription_type=row[4],
            subscription_key=row[5],
            subscription_title=row[6],
            subscription_url=row[7],
            group_name=row[8],
            schedule_id=row[9],
            is_admin=bool(row[10]),
            is_editor=bool(row[11]),
            homework_notifications_enabled=bool(row[12]),
            delivery_disabled_auto=bool(row[13]),
            created_at=row[14],
            last_seen_at=row[15],
        )

    async def set_user_group(self, platform: str, user_id: int, group_name: str, schedule_id: int) -> None:
        await self.set_user_subscription(
            platform=platform,
            user_id=user_id,
            subscription_type="group",
            subscription_key=f"group:{schedule_id}",
            subscription_title=group_name,
            subscription_url=f"rasp:{schedule_id}",
            group_name=group_name,
            schedule_id=schedule_id,
        )

    async def set_user_subscription(
        self,
        platform: str,
        user_id: int,
        subscription_type: str,
        subscription_key: str,
        subscription_title: str,
        subscription_url: str,
        group_name: str | None = None,
        schedule_id: int | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE users
                SET
                    subscription_type = ?,
                    subscription_key = ?,
                    subscription_title = ?,
                    subscription_url = ?,
                    group_name = ?,
                    schedule_id = ?
                WHERE platform = ? AND user_id = ?
                """,
                (
                    subscription_type,
                    subscription_key,
                    subscription_title,
                    subscription_url,
                    group_name,
                    schedule_id,
                    platform,
                    user_id,
                ),
            )
            await db.commit()

    async def clear_user_group(self, platform: str, user_id: int) -> None:
        await self.clear_user_subscription(platform, user_id)

    async def clear_user_subscription(self, platform: str, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE users
                SET
                    subscription_type = NULL,
                    subscription_key = NULL,
                    subscription_title = NULL,
                    subscription_url = NULL,
                    group_name = NULL,
                    schedule_id = NULL
                WHERE platform = ? AND user_id = ?
                """,
                (platform, user_id),
            )
            await db.commit()

    async def set_editor(self, platform: str, user_id: int, is_editor: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE users
                SET is_editor = ?
                WHERE platform = ? AND user_id = ?
                """,
                (int(is_editor), platform, user_id),
            )
            await db.commit()

    async def set_notifications_enabled(self, platform: str, user_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE users
                SET
                    homework_notifications_enabled = ?,
                    delivery_disabled_auto = CASE WHEN ? = 1 THEN 0 ELSE delivery_disabled_auto END
                WHERE platform = ? AND user_id = ?
                """,
                (int(enabled), int(enabled), platform, user_id),
            )
            await db.commit()

    async def mark_delivery_auto_disabled(self, platform: str, user_id: int, disabled: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE users
                SET
                    delivery_disabled_auto = ?,
                    homework_notifications_enabled = CASE WHEN ? = 1 THEN 0 ELSE homework_notifications_enabled END
                WHERE platform = ? AND user_id = ?
                """,
                (int(disabled), int(disabled), platform, user_id),
            )
            await db.commit()

    async def count_auto_disabled_users(self, platform: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM users
                WHERE platform = ? AND delivery_disabled_auto = 1
                """,
                (platform,),
            )
            row = await cursor.fetchone()
        return int(row[0] if row else 0)

    async def auto_disable_undeliverable_telegram_users(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE users
                SET
                    delivery_disabled_auto = 1,
                    homework_notifications_enabled = 0
                WHERE
                    platform = 'telegram'
                    AND homework_notifications_enabled = 1
                    AND EXISTS (
                        SELECT 1
                        FROM delivery_events AS last_event
                        WHERE
                            last_event.platform = 'telegram'
                            AND last_event.user_id = users.user_id
                            AND last_event.id = (
                                SELECT last_inner.id
                                FROM delivery_events AS last_inner
                                WHERE
                                    last_inner.platform = 'telegram'
                                    AND last_inner.user_id = users.user_id
                                ORDER BY last_inner.id DESC
                                LIMIT 1
                            )
                            AND last_event.status = 'failed'
                            AND (
                                lower(COALESCE(last_event.error_text, '')) LIKE '%telegramforbiddenerror%'
                                OR lower(COALESCE(last_event.error_text, '')) LIKE '%forbidden: bot was blocked by the user%'
                                OR lower(COALESCE(last_event.error_text, '')) LIKE '%bot was blocked by the user%'
                                OR lower(COALESCE(last_event.error_text, '')) LIKE '%chat not found%'
                                OR lower(COALESCE(last_event.error_text, '')) LIKE '%user is deactivated%'
                                OR lower(COALESCE(last_event.error_text, '')) LIKE '%have no rights to send a message%'
                            )
                    )
                """
            )
            await db.commit()
        return max(0, int(cursor.rowcount or 0))

    async def get_users_for_notifications(
        self,
        platform: str,
        schedule_id: int | None = None,
        subscription_key: str | None = None,
    ) -> list[UserRecord]:
        users = await self.get_users_for_platform(platform, schedule_id=schedule_id, subscription_key=subscription_key)
        return [user for user in users if user.homework_notifications_enabled]

    async def get_active_groups(self) -> list[dict]:
        groups = await self.get_active_sources()
        return [group for group in groups if group["source_type"] == "group"]

    async def get_group_user_stats(self) -> list[dict[str, int | str]]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT
                    subscription_title,
                    COUNT(*) AS users_count
                FROM users
                WHERE subscription_type = 'group'
                  AND subscription_title IS NOT NULL
                  AND subscription_title != ''
                  AND NOT (platform = 'telegram' AND user_id < 0)
                GROUP BY subscription_key, subscription_title
                ORDER BY users_count DESC, subscription_title ASC
                """
            )
            rows = await cursor.fetchall()

        return [
            {
                "group_name": str(row[0]),
                "users_count": int(row[1] or 0),
            }
            for row in rows
        ]

    async def get_active_sources(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT subscription_type, subscription_key, subscription_title, subscription_url, schedule_id, group_name, COUNT(*)
                FROM users
                WHERE subscription_key IS NOT NULL
                GROUP BY subscription_type, subscription_key, subscription_title, subscription_url, schedule_id, group_name
                ORDER BY subscription_title
                """
            )
            rows = await cursor.fetchall()
        return [
            {
                "source_type": row[0],
                "source_key": row[1],
                "source_title": row[2],
                "source_url": row[3],
                "schedule_id": row[4],
                "group_name": row[5],
                "users_count": row[6],
            }
            for row in rows
            if row[1] is not None and row[2]
        ]

    async def save_snapshot(
        self,
        snapshot_type: str,
        snapshot_hash: str,
        snapshot: ScheduleSnapshot,
        schedule_id: int | None,
        group_name: str | None,
        source_type: str | None = None,
        source_key: str | None = None,
        source_title: str | None = None,
        source_url: str | None = None,
    ) -> None:
        content_json = json.dumps(self._snapshot_to_dict(snapshot), ensure_ascii=False)
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO schedule_snapshots (
                    snapshot_type, source_type, source_key, source_title, source_url, group_name, schedule_id, snapshot_hash, content_json, fetched_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_type,
                    source_type,
                    source_key,
                    source_title or group_name or snapshot.group_name,
                    source_url,
                    group_name or source_title or snapshot.group_name,
                    schedule_id,
                    snapshot_hash,
                    content_json,
                    snapshot.fetched_at.isoformat(timespec="seconds"),
                    now,
                ),
            )
            await db.commit()

    async def get_latest_snapshot(
        self,
        snapshot_type: str,
        schedule_id: int | None = None,
        source_key: str | None = None,
    ) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            if source_key is not None:
                cursor = await db.execute(
                    """
                    SELECT source_type, source_key, source_title, source_url, group_name, schedule_id, snapshot_hash, content_json, fetched_at, created_at
                    FROM schedule_snapshots
                    WHERE snapshot_type = ? AND source_key = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (snapshot_type, source_key),
                )
            elif schedule_id is None:
                cursor = await db.execute(
                    """
                    SELECT source_type, source_key, source_title, source_url, group_name, schedule_id, snapshot_hash, content_json, fetched_at, created_at
                    FROM schedule_snapshots
                    WHERE snapshot_type = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (snapshot_type,),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT source_type, source_key, source_title, source_url, group_name, schedule_id, snapshot_hash, content_json, fetched_at, created_at
                    FROM schedule_snapshots
                    WHERE snapshot_type = ? AND schedule_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (snapshot_type, schedule_id),
                )
            row = await cursor.fetchone()

        if not row:
            return None
        return {
            "source_type": row[0],
            "source_key": row[1],
            "source_title": row[2],
            "source_url": row[3],
            "group_name": row[4],
            "schedule_id": row[5],
            "snapshot_hash": row[6],
            "content": json.loads(row[7]),
            "fetched_at": row[8],
            "created_at": row[9],
        }

    async def record_change(
        self,
        snapshot_hash: str,
        message: str,
        changed_dates: list[str],
        payload: dict,
        schedule_id: int | None,
        group_name: str | None,
        source_type: str | None = None,
        source_key: str | None = None,
        source_title: str | None = None,
        source_url: str | None = None,
    ) -> bool:
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO change_events (
                    source_type, source_key, source_title, source_url, group_name, schedule_id, snapshot_hash, message, changed_dates_json, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_type,
                    source_key,
                    source_title or group_name,
                    source_url,
                    group_name,
                    schedule_id,
                    snapshot_hash,
                    message,
                    json.dumps(changed_dates, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def get_last_change(
        self,
        schedule_id: int | None = None,
        source_key: str | None = None,
    ) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            if source_key is not None:
                cursor = await db.execute(
                    """
                    SELECT source_type, source_key, source_title, source_url, group_name, schedule_id, message, changed_dates_json, payload_json, created_at
                    FROM change_events
                    WHERE source_key = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (source_key,),
                )
            elif schedule_id is None:
                cursor = await db.execute(
                    """
                    SELECT source_type, source_key, source_title, source_url, group_name, schedule_id, message, changed_dates_json, payload_json, created_at
                    FROM change_events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT source_type, source_key, source_title, source_url, group_name, schedule_id, message, changed_dates_json, payload_json, created_at
                    FROM change_events
                    WHERE schedule_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (schedule_id,),
                )
            row = await cursor.fetchone()

        if not row:
            return None
        return {
            "source_type": row[0],
            "source_key": row[1],
            "source_title": row[2],
            "source_url": row[3],
            "group_name": row[4],
            "schedule_id": row[5],
            "message": row[6],
            "changed_dates": json.loads(row[7]),
            "payload": json.loads(row[8]),
            "created_at": row[9],
        }

    async def get_daily_change_groups(self, day_prefix: str) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT source_title, MAX(created_at) AS created_at
                FROM change_events
                WHERE created_at LIKE ?
                GROUP BY source_key, source_title
                ORDER BY source_title
                """,
                (f"{day_prefix}%",),
            )
            rows = await cursor.fetchall()

        return [
            {
                "group_name": row[0],
                "created_at": row[1],
            }
            for row in rows
            if row[0] and row[1]
        ]

    async def record_delivery_event(
        self,
        *,
        campaign_type: str,
        platform: str,
        user_id: int,
        via_broker: bool,
        status: str,
        attempt: int = 1,
        message_id: str | None = None,
        error_text: str | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        safe_error_text = (error_text or "").strip() or None
        if safe_error_text and len(safe_error_text) > 500:
            safe_error_text = f"{safe_error_text[:497]}..."
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO delivery_events (
                    message_id,
                    campaign_type,
                    platform,
                    user_id,
                    via_broker,
                    status,
                    attempt,
                    error_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    campaign_type,
                    platform,
                    user_id,
                    int(via_broker),
                    status,
                    max(1, attempt),
                    safe_error_text,
                    now,
                ),
            )
            await db.commit()

    async def get_delivery_stats(self) -> dict[str, int]:
        threshold_24h = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'sent' AND campaign_type = 'notification' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'sent' AND campaign_type = 'admin_broadcast' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'sent' AND campaign_type = 'admin_notify' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'sent' AND via_broker = 1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'sent' AND via_broker = 0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'sent' AND via_broker = 1 AND attempt > 1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'sent' AND platform = 'telegram' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'sent' AND platform = 'vk' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND platform = 'telegram' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND platform = 'vk' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'sent' AND created_at >= ? THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND created_at >= ? THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND via_broker = 1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND via_broker = 0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND platform = 'telegram' AND via_broker = 1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND platform = 'telegram' AND via_broker = 0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND platform = 'telegram' AND created_at >= ? THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND platform = 'telegram' AND (
                        lower(COALESCE(error_text, '')) LIKE '%telegramforbiddenerror%'
                        OR lower(COALESCE(error_text, '')) LIKE '%bot was blocked by the user%'
                        OR lower(COALESCE(error_text, '')) LIKE '%chat not found%'
                        OR lower(COALESCE(error_text, '')) LIKE '%user is deactivated%'
                        OR lower(COALESCE(error_text, '')) LIKE '%have no rights to send a message%'
                    ) THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' AND platform = 'telegram' AND created_at >= ? AND (
                        lower(COALESCE(error_text, '')) LIKE '%telegramforbiddenerror%'
                        OR lower(COALESCE(error_text, '')) LIKE '%bot was blocked by the user%'
                        OR lower(COALESCE(error_text, '')) LIKE '%chat not found%'
                        OR lower(COALESCE(error_text, '')) LIKE '%user is deactivated%'
                        OR lower(COALESCE(error_text, '')) LIKE '%have no rights to send a message%'
                    ) THEN 1 ELSE 0 END)
                FROM delivery_events
                """,
                (threshold_24h, threshold_24h, threshold_24h, threshold_24h),
            )
            row = await cursor.fetchone()

        if not row:
            return {
                "events_total": 0,
                "sent_total": 0,
                "failed_total": 0,
                "notifications_sent": 0,
                "admin_broadcast_sent": 0,
                "admin_notify_sent": 0,
                "sent_via_rabbitmq": 0,
                "sent_direct": 0,
                "sent_after_retry": 0,
                "tg_sent": 0,
                "vk_sent": 0,
                "tg_failed": 0,
                "vk_failed": 0,
                "sent_last_24h": 0,
                "failed_last_24h": 0,
                "failed_via_rabbitmq": 0,
                "failed_direct": 0,
                "tg_failed_via_rabbitmq": 0,
                "tg_failed_direct": 0,
                "tg_failed_last_24h": 0,
                "tg_failed_permanent": 0,
                "tg_failed_permanent_last_24h": 0,
            }

        return {
            "events_total": int(row[0] or 0),
            "sent_total": int(row[1] or 0),
            "failed_total": int(row[2] or 0),
            "notifications_sent": int(row[3] or 0),
            "admin_broadcast_sent": int(row[4] or 0),
            "admin_notify_sent": int(row[5] or 0),
            "sent_via_rabbitmq": int(row[6] or 0),
            "sent_direct": int(row[7] or 0),
            "sent_after_retry": int(row[8] or 0),
            "tg_sent": int(row[9] or 0),
            "vk_sent": int(row[10] or 0),
            "tg_failed": int(row[11] or 0),
            "vk_failed": int(row[12] or 0),
            "sent_last_24h": int(row[13] or 0),
            "failed_last_24h": int(row[14] or 0),
            "failed_via_rabbitmq": int(row[15] or 0),
            "failed_direct": int(row[16] or 0),
            "tg_failed_via_rabbitmq": int(row[17] or 0),
            "tg_failed_direct": int(row[18] or 0),
            "tg_failed_last_24h": int(row[19] or 0),
            "tg_failed_permanent": int(row[20] or 0),
            "tg_failed_permanent_last_24h": int(row[21] or 0),
        }

    async def get_top_delivery_errors(
        self,
        *,
        platform: str,
        status: str = "failed",
        hours: int = 24,
        limit: int = 5,
    ) -> list[dict[str, int | str]]:
        safe_limit = max(1, min(limit, 20))
        threshold = (datetime.now() - timedelta(hours=max(1, hours))).isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT
                    COALESCE(NULLIF(error_text, ''), '(без текста)') AS error_text,
                    COUNT(*) AS total_count,
                    SUM(CASE WHEN via_broker = 1 THEN 1 ELSE 0 END) AS via_broker_count,
                    SUM(CASE WHEN via_broker = 0 THEN 1 ELSE 0 END) AS direct_count
                FROM delivery_events
                WHERE platform = ? AND status = ? AND created_at >= ?
                GROUP BY error_text
                ORDER BY total_count DESC, error_text ASC
                LIMIT ?
                """,
                (platform, status, threshold, safe_limit),
            )
            rows = await cursor.fetchall()

        return [
            {
                "error_text": str(row[0]),
                "count": int(row[1] or 0),
                "via_broker": int(row[2] or 0),
                "direct": int(row[3] or 0),
            }
            for row in rows
        ]


    async def has_baseline_for_date(
        self,
        day_prefix: str,
        schedule_id: int | None = None,
        source_key: str | None = None,
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            if source_key is not None:
                cursor = await db.execute(
                    """
                    SELECT 1
                    FROM schedule_snapshots
                    WHERE snapshot_type = 'daily_baseline'
                      AND source_key = ?
                      AND created_at LIKE ?
                    LIMIT 1
                    """,
                    (source_key, f"{day_prefix}%"),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT 1
                    FROM schedule_snapshots
                    WHERE snapshot_type = 'daily_baseline'
                      AND schedule_id = ?
                      AND created_at LIKE ?
                    LIMIT 1
                    """,
                    (schedule_id, f"{day_prefix}%"),
                )
            row = await cursor.fetchone()
        return row is not None

    async def upsert_lesson_counter_seed(
        self,
        *,
        schedule_id: int | None,
        subject: str,
        teacher: str,
        subject_norm: str,
        teacher_norm: str,
        passed_count: int,
        total_count: int,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO lesson_counters (
                    schedule_id, subject, teacher, subject_norm, teacher_norm, passed_count, total_count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(schedule_id, subject_norm, teacher_norm) DO UPDATE SET
                    subject = excluded.subject,
                    teacher = excluded.teacher,
                    total_count = excluded.total_count,
                    updated_at = excluded.updated_at
                """,
                (
                    schedule_id,
                    subject,
                    teacher,
                    subject_norm,
                    teacher_norm,
                    max(0, passed_count),
                    max(0, total_count),
                    now,
                    now,
                ),
            )
            await db.commit()

    async def list_lesson_counters(self, schedule_id: int | None = None) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            if schedule_id is None:
                cursor = await db.execute(
                    """
                    SELECT id, schedule_id, subject, teacher, subject_norm, teacher_norm, passed_count, total_count, updated_at
                    FROM lesson_counters
                    ORDER BY subject COLLATE NOCASE, teacher COLLATE NOCASE
                    """
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT id, schedule_id, subject, teacher, subject_norm, teacher_norm, passed_count, total_count, updated_at
                    FROM lesson_counters
                    WHERE schedule_id = ?
                    ORDER BY subject COLLATE NOCASE, teacher COLLATE NOCASE
                    """,
                    (schedule_id,),
                )
            rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "schedule_id": row[1],
                "subject": row[2],
                "teacher": row[3],
                "subject_norm": row[4],
                "teacher_norm": row[5],
                "passed_count": row[6],
                "total_count": row[7],
                "updated_at": row[8],
            }
            for row in rows
        ]

    async def record_lesson_counter_event(
        self,
        *,
        counter_id: int,
        schedule_id: int | None,
        date_iso: str,
        lesson_number: int,
        subject: str,
        teacher: str,
        classroom: str,
    ) -> bool:
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO lesson_counter_events (
                    counter_id, schedule_id, date_iso, lesson_number, subject, teacher, classroom, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (counter_id, schedule_id, date_iso, lesson_number, subject, teacher, classroom, now),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                await db.execute(
                    """
                    UPDATE lesson_counters
                    SET passed_count = passed_count + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, counter_id),
                )
            await db.commit()
        return inserted

    async def delete_lesson_counters_not_in(self, keys: set[tuple[int | None, str, str]]) -> int:
        existing = await self.list_lesson_counters()
        ids_to_delete = [
            int(counter["id"])
            for counter in existing
            if (counter["schedule_id"], counter["subject_norm"], counter["teacher_norm"]) not in keys
        ]
        if not ids_to_delete:
            return 0
        placeholders = ",".join("?" for _ in ids_to_delete)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"DELETE FROM lesson_counters WHERE id IN ({placeholders})", ids_to_delete)
            await db.commit()
        return len(ids_to_delete)

    def _snapshot_to_dict(self, snapshot: ScheduleSnapshot) -> dict:
        return {
            "group_name": snapshot.group_name,
            "fetched_at": snapshot.fetched_at.isoformat(timespec="seconds"),
            "days": [
                {
                    "date_label": day.date_label,
                    "date_iso": day.date_iso,
                    "lessons": [
                        {
                            "number": lesson.number,
                            "subject": lesson.subject,
                            "teacher": lesson.teacher,
                            "classroom": lesson.classroom,
                        }
                        for lesson in day.lessons
                    ],
                }
                for day in snapshot.days
            ],
        }

from __future__ import annotations

import json
from datetime import datetime
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
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (platform, user_id)
                );

                CREATE TABLE IF NOT EXISTS schedule_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_type TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS change_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_hash TEXT NOT NULL,
                    message TEXT NOT NULL,
                    changed_dates_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            await db.commit()

    async def upsert_user(
        self,
        platform: str,
        user_id: int,
        username: str | None,
        full_name: str | None,
        is_admin: bool = False,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users (platform, user_id, username, full_name, is_admin, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    is_admin = excluded.is_admin,
                    last_seen_at = excluded.last_seen_at
                """,
                (platform, user_id, username, full_name, int(is_admin), now, now),
            )
            await db.commit()

    async def list_users(self, platform: str | None = None) -> list[UserRecord]:
        query = """
            SELECT platform, user_id, username, full_name, is_admin, created_at, last_seen_at
            FROM users
        """
        params: tuple = ()
        if platform:
            query += " WHERE platform = ?"
            params = (platform,)
        query += " ORDER BY platform, created_at"

        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

        return [
            UserRecord(
                platform=row[0],
                user_id=row[1],
                username=row[2],
                full_name=row[3],
                is_admin=bool(row[4]),
                created_at=row[5],
                last_seen_at=row[6],
            )
            for row in rows
        ]

    async def get_users_for_platform(self, platform: str) -> list[UserRecord]:
        return await self.list_users(platform=platform)

    async def save_snapshot(self, snapshot_type: str, snapshot_hash: str, snapshot: ScheduleSnapshot) -> None:
        content_json = json.dumps(self._snapshot_to_dict(snapshot), ensure_ascii=False)
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO schedule_snapshots (snapshot_type, snapshot_hash, content_json, fetched_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot_type, snapshot_hash, content_json, snapshot.fetched_at.isoformat(timespec="seconds"), now),
            )
            await db.commit()

    async def get_latest_snapshot(self, snapshot_type: str) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT snapshot_hash, content_json, fetched_at, created_at
                FROM schedule_snapshots
                WHERE snapshot_type = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (snapshot_type,),
            )
            row = await cursor.fetchone()

        if not row:
            return None
        return {
            "snapshot_hash": row[0],
            "content": json.loads(row[1]),
            "fetched_at": row[2],
            "created_at": row[3],
        }

    async def record_change(self, snapshot_hash: str, message: str, changed_dates: list[str], payload: dict) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO change_events (snapshot_hash, message, changed_dates_json, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot_hash,
                    message,
                    json.dumps(changed_dates, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            await db.commit()

    async def get_last_change(self) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT message, changed_dates_json, payload_json, created_at
                FROM change_events
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = await cursor.fetchone()

        if not row:
            return None
        return {
            "message": row[0],
            "changed_dates": json.loads(row[1]),
            "payload": json.loads(row[2]),
            "created_at": row[3],
        }

    async def has_baseline_for_date(self, day_prefix: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM schedule_snapshots
                WHERE snapshot_type = 'daily_baseline'
                  AND created_at LIKE ?
                LIMIT 1
                """,
                (f"{day_prefix}%",),
            )
            row = await cursor.fetchone()
        return row is not None

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

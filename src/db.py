from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from src.models import HomeworkAttachment, ScheduleSnapshot, UserRecord


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
                    is_editor INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS homework_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_key TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    teacher TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_by_platform TEXT NOT NULL,
                    created_by_user_id INTEGER NOT NULL,
                    created_by_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS homework_attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    homework_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    file_name TEXT,
                    mime_type TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(homework_id) REFERENCES homework_entries(id) ON DELETE CASCADE
                );
                """
            )
            await self._ensure_column(db, "users", "is_editor", "INTEGER NOT NULL DEFAULT 0")
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
        is_admin: bool = False,
        is_editor: bool = False,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users (platform, user_id, username, full_name, is_admin, is_editor, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    is_admin = excluded.is_admin,
                    is_editor = COALESCE(users.is_editor, excluded.is_editor),
                    last_seen_at = excluded.last_seen_at
                """,
                (platform, user_id, username, full_name, int(is_admin), int(is_editor), now, now),
            )
            await db.commit()

    async def list_users(self, platform: str | None = None) -> list[UserRecord]:
        query = """
            SELECT platform, user_id, username, full_name, is_admin, is_editor, created_at, last_seen_at
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
                is_editor=bool(row[5]),
                created_at=row[6],
                last_seen_at=row[7],
            )
            for row in rows
        ]

    async def get_users_for_platform(self, platform: str) -> list[UserRecord]:
        return await self.list_users(platform=platform)

    async def get_user(self, platform: str, user_id: int) -> UserRecord | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT platform, user_id, username, full_name, is_admin, is_editor, created_at, last_seen_at
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
            is_admin=bool(row[4]),
            is_editor=bool(row[5]),
            created_at=row[6],
            last_seen_at=row[7],
        )

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

    async def create_homework(
        self,
        subject_key: str,
        subject: str,
        teacher: str,
        text: str,
        created_by_platform: str,
        created_by_user_id: int,
        created_by_name: str,
        attachments: list[HomeworkAttachment],
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO homework_entries (
                    subject_key, subject, teacher, text, created_by_platform, created_by_user_id, created_by_name, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject_key,
                    subject,
                    teacher,
                    text,
                    created_by_platform,
                    created_by_user_id,
                    created_by_name,
                    now,
                ),
            )
            homework_id = cursor.lastrowid
            for attachment in attachments:
                await db.execute(
                    """
                    INSERT INTO homework_attachments (homework_id, file_id, file_type, file_name, mime_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        homework_id,
                        attachment.file_id,
                        attachment.file_type,
                        attachment.file_name,
                        attachment.mime_type,
                        now,
                    ),
                )
            await db.commit()
        return int(homework_id)

    async def get_homework_for_subject(self, subject_key: str, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT id, subject_key, subject, teacher, text, created_by_platform, created_by_user_id, created_by_name, created_at
                FROM homework_entries
                WHERE subject_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (subject_key, limit),
            )
            rows = await cursor.fetchall()

        entries: list[dict] = []
        for row in rows:
            attachments = await self.get_homework_attachments(int(row[0]))
            entries.append(
                {
                    "id": row[0],
                    "subject_key": row[1],
                    "subject": row[2],
                    "teacher": row[3],
                    "text": row[4],
                    "created_by_platform": row[5],
                    "created_by_user_id": row[6],
                    "created_by_name": row[7],
                    "created_at": row[8],
                    "attachments": attachments,
                }
            )
        return entries

    async def get_homework_attachments(self, homework_id: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT id, file_id, file_type, file_name, mime_type, created_at
                FROM homework_attachments
                WHERE homework_id = ?
                ORDER BY id
                """,
                (homework_id,),
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "file_id": row[1],
                "file_type": row[2],
                "file_name": row[3],
                "mime_type": row[4],
                "created_at": row[5],
            }
            for row in rows
        ]

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

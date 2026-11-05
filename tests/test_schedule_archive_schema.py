from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config

from src.db import Database
from src.db_migrations import _build_sqlite_url, apply_migrations


class ScheduleArchiveMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_migration_and_reinitialization_preserve_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            root = Path(__file__).resolve().parents[1]
            config = Config(str(root / "alembic.ini"))
            config.set_main_option("script_location", str(root / "migrations"))
            config.set_main_option("sqlalchemy.url", _build_sqlite_url(path))
            try:
                with patch.dict("os.environ", {"DATABASE_PATH": str(path)}):
                    command.upgrade(config, "20261029_0003")
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute(
                            "INSERT INTO users (platform, user_id, full_name, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                            ("telegram", 42, "Сохранить", "2026-01-01", "2026-01-01"),
                        )
                        connection.execute(
                            "INSERT INTO schedule_snapshots (snapshot_type, source_key, snapshot_hash, content_json, fetched_at, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            ("current", "group:1", "hash", '{"days":[]}', "2026-01-01", "2026-01-01"),
                        )
                        connection.commit()
                    apply_migrations(path)
                    apply_migrations(path)
                    await Database(path).initialize()
                    await Database(path).initialize()
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute(
                            "INSERT INTO schedule_day_archive VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            ("group:1", "group", "2026-01-01", 0, '{"lessons":[]}', "unknown", "unknown", "2026-01-01", "2026-01-01", 1),
                        )
                        connection.commit()
                    with self.assertRaisesRegex(RuntimeError, "откат архива запрещён"):
                        command.downgrade(config, "20261029_0003")
            finally:
                from src.main import restore_logging
                restore_logging()

            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("SELECT full_name FROM users WHERE user_id = 42").fetchone()[0], "Сохранить")
                self.assertEqual(connection.execute("SELECT content_json FROM schedule_snapshots").fetchone()[0], '{"days":[]}')
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM schedule_day_archive").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM schedule_day_references").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "20261105_0005")
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")

    async def test_archive_keys_variants_and_references_survive_snapshot_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.db"
            await Database(path).initialize()
            await Database(path).initialize()
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "INSERT INTO schedule_snapshots (id, snapshot_type, source_key, snapshot_hash, content_json, fetched_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (7, "current", "group:1", "hash", '{"days":[]}', "2026-11-05", "2026-11-05"),
                )
                row = ("group:1", "group", "2026-11-05", 1, '{"lessons":[]}', "unknown", "unknown", "2026-11-05T09:00:00+03:00", "2026-11-05T09:00:00+03:00", 7)
                connection.execute("INSERT INTO schedule_day_archive VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
                connection.execute("INSERT INTO schedule_day_archive VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*row[:3], 2, *row[4:]))
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO schedule_day_archive VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO schedule_day_archive VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*row[:3], 3, *row[4:]))
                reference = ("group:1", "2026-11-05", 1, "first_seen", '{"lessons":[]}', "unknown", "unknown", row[7], row[8], 7)
                connection.execute("INSERT INTO schedule_day_references VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", reference)
                connection.execute("INSERT INTO schedule_day_references VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*reference[:3], "day_start", *reference[4:]))
                connection.execute("DELETE FROM schedule_snapshots")
                connection.commit()
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM schedule_day_archive").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM schedule_day_references").fetchone()[0], 2)

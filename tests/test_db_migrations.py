from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.db_migrations import _build_sqlite_url, _has_user_tables, _table_exists, apply_migrations


class TestDbMigrationHelpers(unittest.TestCase):
    """Тесты для вспомогательных функций миграций."""

    def test_build_sqlite_url(self) -> None:
        url = _build_sqlite_url(Path("/tmp/test.db"))
        self.assertTrue(url.startswith("sqlite:///"))
        self.assertIn("test.db", url)

    def test_table_exists_missing_db(self) -> None:
        self.assertFalse(_table_exists(Path("/nonexistent/path/db.sqlite"), "users"))

    def test_table_exists_no_table(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "empty.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE other_table (id INTEGER)")
            self.assertFalse(_table_exists(db_path, "users"))

    def test_table_exists_with_table(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE users (id INTEGER)")
            self.assertTrue(_table_exists(db_path, "users"))

    def test_has_user_tables_empty_db(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "empty.db"
            sqlite3.connect(db_path).close()
            self.assertFalse(_has_user_tables(db_path))

    def test_has_user_tables_with_tables(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE users (id INTEGER)")
            self.assertTrue(_has_user_tables(db_path))

    def test_has_user_tables_ignores_alembic(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
            self.assertFalse(_has_user_tables(db_path))

    def test_has_user_tables_missing_db(self) -> None:
        self.assertFalse(_has_user_tables(Path("/nonexistent/path/db.sqlite")))

    def test_outbox_migration_on_fresh_and_legacy_database_preserves_users(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "migration.db"
                if legacy:
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute("CREATE TABLE users (user_id INTEGER, full_name TEXT)")
                        connection.execute("INSERT INTO users VALUES (42, 'Сохранить')")
                        connection.commit()
                try:
                    with patch.dict("os.environ", {"DATABASE_PATH": str(path)}):
                        apply_migrations(path)
                        apply_migrations(path)
                finally:
                    from src.main import restore_logging
                    restore_logging()
                self.assertTrue(_table_exists(path, "delivery_outbox"))
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "20261029_0002")
                    if legacy:
                        self.assertEqual(connection.execute("SELECT full_name FROM users WHERE user_id = 42").fetchone()[0], "Сохранить")


if __name__ == "__main__":
    unittest.main()

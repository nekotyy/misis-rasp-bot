from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db_migrations import _build_sqlite_url, _has_user_tables, _table_exists


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


if __name__ == "__main__":
    unittest.main()

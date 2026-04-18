from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


logger = logging.getLogger(__name__)


def _build_sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.resolve().as_posix()}"


def _table_exists(db_path: Path, table_name: str) -> bool:
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table_name,),
        ).fetchone()
    return row is not None


def _has_user_tables(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND name != 'alembic_version'
            LIMIT 1
            """,
        ).fetchone()
    return row is not None


def apply_migrations(database_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    alembic_ini = repo_root / "alembic.ini"
    migrations_dir = repo_root / "migrations"

    if not alembic_ini.exists() or not migrations_dir.exists():
        logger.warning("Alembic config is missing. Skipping migrations.")
        return

    db_path = database_path.expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(migrations_dir))
    config.set_main_option("sqlalchemy.url", _build_sqlite_url(db_path))

    has_user_tables = _has_user_tables(db_path)
    has_alembic_version = _table_exists(db_path, "alembic_version")
    if has_user_tables and not has_alembic_version:
        logger.info("Existing database without alembic history detected. Stamping head.")
        command.stamp(config, "head")

    command.upgrade(config, "head")

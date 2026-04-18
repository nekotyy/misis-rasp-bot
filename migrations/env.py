from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path
import os

from alembic import context
from sqlalchemy import engine_from_config, pool


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_sqlalchemy_url() -> str:
    env_db_path = os.getenv("DATABASE_PATH", "").strip()
    if env_db_path:
        db_path = Path(env_db_path).expanduser()
        if not db_path.is_absolute():
            db_path = (Path.cwd() / db_path).resolve()
        return f"sqlite:///{db_path.as_posix()}"
    return config.get_main_option("sqlalchemy.url")


config.set_main_option("sqlalchemy.url", _resolve_sqlalchemy_url())
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

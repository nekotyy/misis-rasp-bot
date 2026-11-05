"""Добавил схему дневного архива и независимых опор замен."""

from alembic import op

from src.schedule_archive_schema import SCHEDULE_ARCHIVE_SCHEMA_V1

revision = "20261105_0004"
down_revision = "20261029_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in SCHEDULE_ARCHIVE_SCHEMA_V1.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Автоматический откат архива запрещён: он может удалить сохранённые дни")

"""Добавил состояние восстановления архива."""

from alembic import op

from src.schedule_archive_schema import SCHEDULE_ARCHIVE_BACKFILL_SCHEMA_V1

revision = "20261105_0005"
down_revision = "20261105_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(SCHEDULE_ARCHIVE_BACKFILL_SCHEMA_V1)


def downgrade() -> None:
    raise RuntimeError("Автоматический откат архива запрещён: будет потерян прогресс восстановления")

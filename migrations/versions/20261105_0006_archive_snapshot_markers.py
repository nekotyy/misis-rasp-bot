"""Исключил обработанные снимки из исторического восстановления."""

from alembic import op

from src.schedule_archive_schema import SCHEDULE_ARCHIVE_MARKERS_SCHEMA_V1

revision = "20261105_0006"
down_revision = "20261105_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(SCHEDULE_ARCHIVE_MARKERS_SCHEMA_V1)


def downgrade() -> None:
    raise RuntimeError("Автоматический откат маркеров архива запрещён")

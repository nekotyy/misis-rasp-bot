"""Надёжная очередь исходящих запросов Telegram/VK."""

from alembic import op

from src.outbox_store import OUTBOX_SCHEMA

revision = "20261029_0002"
down_revision = "20260418_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in OUTBOX_SCHEMA.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("delivery_outbox")

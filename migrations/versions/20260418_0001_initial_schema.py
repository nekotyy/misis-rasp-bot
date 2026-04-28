"""Начальная схема базы данных

Revision ID: 20260418_0001
Revises:
Create Date: 2026-04-18 23:00:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260418_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("full_name", sa.Text(), nullable=True),
        sa.Column("subscription_type", sa.Text(), nullable=True),
        sa.Column("subscription_key", sa.Text(), nullable=True),
        sa.Column("subscription_title", sa.Text(), nullable=True),
        sa.Column("subscription_url", sa.Text(), nullable=True),
        sa.Column("group_name", sa.Text(), nullable=True),
        sa.Column("schedule_id", sa.Integer(), nullable=True),
        sa.Column("is_admin", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_editor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("homework_notifications_enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("delivery_disabled_auto", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("platform", "user_id"),
    )

    op.create_table(
        "schedule_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_type", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=True),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("source_title", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("group_name", sa.Text(), nullable=True),
        sa.Column("schedule_id", sa.Integer(), nullable=True),
        sa.Column("snapshot_hash", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "change_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_type", sa.Text(), nullable=True),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("source_title", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("group_name", sa.Text(), nullable=True),
        sa.Column("schedule_id", sa.Integer(), nullable=True),
        sa.Column("snapshot_hash", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("changed_dates_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "delivery_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("campaign_type", sa.Text(), nullable=False, server_default="notification"),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("via_broker", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_change_events_source_key_snapshot_hash_day
        ON change_events(source_key, snapshot_hash, substr(created_at, 1, 10))
        """
    )
    op.create_index(
        "idx_delivery_events_status_campaign",
        "delivery_events",
        ["status", "campaign_type"],
        unique=False,
    )
    op.create_index(
        "idx_delivery_events_status_broker",
        "delivery_events",
        ["status", "via_broker"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_delivery_events_status_broker", table_name="delivery_events")
    op.drop_index("idx_delivery_events_status_campaign", table_name="delivery_events")
    op.execute("DROP INDEX IF EXISTS idx_change_events_source_key_snapshot_hash_day")

    op.drop_table("delivery_events")
    op.drop_table("change_events")
    op.drop_table("schedule_snapshots")
    op.drop_table("users")

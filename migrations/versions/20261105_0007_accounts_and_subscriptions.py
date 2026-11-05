"""Добавил таблицы общих аккаунтов, связей и подписок миниаппа."""

import uuid
from datetime import datetime, timezone
from alembic import op
from sqlalchemy import text

from src.account_schema import ACCOUNTS_SCHEMA_V1

revision = "20261105_0007"
down_revision = "20261105_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in ACCOUNTS_SCHEMA_V1.split(";"):
        if statement.strip():
            op.execute(statement)

    conn = op.get_bind()
    # Идемпотентный перенос существующих пользователей в аккаунты
    try:
        users = conn.execute(
            text(
                """SELECT platform, user_id, username, full_name, subscription_type, subscription_key,
                          subscription_title, subscription_url, audience_subscription_key,
                          audience_subscription_title, audience_subscription_url, group_name,
                          schedule_id, university_subgroup, homework_notifications_enabled,
                          created_at, last_seen_at
                   FROM users"""
            )
        ).fetchall()
    except Exception:
        users = []

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in users:
        (
            platform, user_id, username, full_name, sub_type, sub_key,
            sub_title, sub_url, aud_key, aud_title, aud_url, group_name,
            schedule_id, uni_subgroup, notif_enabled, created_at, last_seen_at,
        ) = row

        existing = conn.execute(
            text("SELECT account_id FROM account_identities WHERE platform = :p AND user_id = :u"),
            {"p": platform, "u": user_id},
        ).fetchone()

        if existing:
            account_id = existing[0]
        else:
            account_id = f"acc_{uuid.uuid4().hex[:16]}"
            conn.execute(
                text(
                    """INSERT OR IGNORE INTO accounts
                       (id, theme, accent, density, reduced_motion, notifications_enabled, created_at, updated_at)
                       VALUES (:id, 'system', 'default', 'comfortable', 0, :notif, :created_at, :updated_at)"""
                ),
                {"id": account_id, "notif": 1 if notif_enabled else 0, "created_at": created_at or now, "updated_at": now},
            )
            conn.execute(
                text(
                    """INSERT OR IGNORE INTO account_identities
                       (account_id, platform, user_id, username, full_name, created_at, last_seen_at)
                       VALUES (:acc, :plat, :uid, :uname, :fname, :created, :seen)"""
                ),
                {
                    "acc": account_id, "plat": platform, "uid": user_id,
                    "uname": username, "fname": full_name,
                    "created": created_at or now, "seen": last_seen_at or now,
                },
            )

        if sub_key:
            conn.execute(
                text(
                    """INSERT OR IGNORE INTO account_subscriptions
                       (account_id, subscription_type, subscription_key, subscription_title, subscription_url,
                        group_name, schedule_id, university_subgroup, is_primary, notifications_enabled, created_at, updated_at)
                       VALUES (:acc, :stype, :skey, :stitle, :surl, :gname, :sid, :usub, 1, :notif, :created, :updated)"""
                ),
                {
                    "acc": account_id, "stype": sub_type or "group", "skey": sub_key,
                    "stitle": sub_title, "surl": sub_url, "gname": group_name,
                    "sid": schedule_id, "usub": uni_subgroup or 0,
                    "notif": 1 if notif_enabled else 0, "created": created_at or now, "updated": now,
                },
            )

        if aud_key:
            conn.execute(
                text(
                    """INSERT OR IGNORE INTO account_subscriptions
                       (account_id, subscription_type, subscription_key, subscription_title, subscription_url,
                        group_name, schedule_id, university_subgroup, is_primary, notifications_enabled, created_at, updated_at)
                       VALUES (:acc, 'audience', :akey, :atitle, :aurl, :atitle, NULL, 0, 0, 0, :created, :updated)"""
                ),
                {
                    "acc": account_id, "akey": aud_key, "atitle": aud_title,
                    "aurl": aud_url, "created": created_at or now, "updated": now,
                },
            )


def downgrade() -> None:
    raise RuntimeError("Автоматический откат аккаунтов запрещён")

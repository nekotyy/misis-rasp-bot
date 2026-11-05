"""Сервис управления общими аккаунтами, связыванием и подписками миниаппа."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import aiosqlite


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    account_id: str
    platform: str
    user_id: int
    username: str | None
    full_name: str | None
    created_at: str
    last_seen_at: str


@dataclass(frozen=True, slots=True)
class AccountSubscription:
    id: int
    account_id: str
    subscription_type: str
    subscription_key: str
    subscription_title: str | None
    subscription_url: str | None
    group_name: str | None
    schedule_id: int | None
    university_subgroup: int
    is_primary: bool
    notifications_enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    theme: str
    accent: str
    density: str
    reduced_motion: bool
    notifications_enabled: bool
    created_at: str
    updated_at: str
    identities: tuple[AccountIdentity, ...]
    subscriptions: tuple[AccountSubscription, ...]

    @property
    def primary_subscription(self) -> AccountSubscription | None:
        for sub in self.subscriptions:
            if sub.is_primary and sub.subscription_type == "group":
                return sub
        for sub in self.subscriptions:
            if sub.is_primary:
                return sub
        return None


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class AccountService:
    @staticmethod
    async def get_or_create_account(
        db: aiosqlite.Connection,
        platform: str,
        user_id: int,
        username: str | None = None,
        full_name: str | None = None,
    ) -> Account:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        cursor = await db.execute(
            "SELECT account_id FROM account_identities WHERE platform = ? AND user_id = ?",
            (platform, user_id),
        )
        row = await cursor.fetchone()
        if row:
            account_id = row[0]
            await db.execute(
                """UPDATE account_identities
                   SET username = COALESCE(?, username), full_name = COALESCE(?, full_name), last_seen_at = ?
                   WHERE platform = ? AND user_id = ?""",
                (username, full_name, now, platform, user_id),
            )
        else:
            account_id = f"acc_{uuid.uuid4().hex[:16]}"
            await db.execute(
                """INSERT INTO accounts
                   (id, theme, accent, density, reduced_motion, notifications_enabled, created_at, updated_at)
                   VALUES (?, 'system', 'default', 'comfortable', 0, 1, ?, ?)""",
                (account_id, now, now),
            )
            await db.execute(
                """INSERT INTO account_identities
                   (account_id, platform, user_id, username, full_name, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (account_id, platform, user_id, username, full_name, now, now),
            )
            # Если пользователь уже существовал в таблице users, переносим его подписку
            user_cursor = await db.execute(
                """SELECT subscription_type, subscription_key, subscription_title, subscription_url,
                          group_name, schedule_id, university_subgroup, homework_notifications_enabled
                   FROM users WHERE platform = ? AND user_id = ?""",
                (platform, user_id),
            )
            user_row = await user_cursor.fetchone()
            if user_row and user_row[1]:
                await db.execute(
                    """INSERT OR IGNORE INTO account_subscriptions
                       (account_id, subscription_type, subscription_key, subscription_title, subscription_url,
                        group_name, schedule_id, university_subgroup, is_primary, notifications_enabled, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (
                        account_id, user_row[0] or "group", user_row[1], user_row[2], user_row[3],
                        user_row[4], user_row[5], user_row[6] or 0, 1 if user_row[7] else 0,
                        now, now,
                    ),
                )
        account = await AccountService.get_account(db, account_id)
        if account is None:
            raise RuntimeError(f"Не удалось получить созданный аккаунт {account_id}")
        return account

    @staticmethod
    async def get_account(db: aiosqlite.Connection, account_id: str) -> Account | None:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        acc_row = await cursor.fetchone()
        if not acc_row:
            return None

        id_cursor = await db.execute(
            "SELECT * FROM account_identities WHERE account_id = ? ORDER BY platform, user_id",
            (account_id,),
        )
        id_rows = await id_cursor.fetchall()
        identities = tuple(
            AccountIdentity(
                account_id=r["account_id"],
                platform=r["platform"],
                user_id=r["user_id"],
                username=r["username"],
                full_name=r["full_name"],
                created_at=r["created_at"],
                last_seen_at=r["last_seen_at"],
            )
            for r in id_rows
        )

        sub_cursor = await db.execute(
            """SELECT * FROM account_subscriptions
               WHERE account_id = ?
               ORDER BY is_primary DESC, subscription_type, subscription_title, id""",
            (account_id,),
        )
        sub_rows = await sub_cursor.fetchall()
        subscriptions = tuple(
            AccountSubscription(
                id=r["id"],
                account_id=r["account_id"],
                subscription_type=r["subscription_type"],
                subscription_key=r["subscription_key"],
                subscription_title=r["subscription_title"],
                subscription_url=r["subscription_url"],
                group_name=r["group_name"],
                schedule_id=r["schedule_id"],
                university_subgroup=r["university_subgroup"],
                is_primary=bool(r["is_primary"]),
                notifications_enabled=bool(r["notifications_enabled"]),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in sub_rows
        )

        return Account(
            id=acc_row["id"],
            theme=acc_row["theme"],
            accent=acc_row["accent"],
            density=acc_row["density"],
            reduced_motion=bool(acc_row["reduced_motion"]),
            notifications_enabled=bool(acc_row["notifications_enabled"]),
            created_at=acc_row["created_at"],
            updated_at=acc_row["updated_at"],
            identities=identities,
            subscriptions=subscriptions,
        )

    @staticmethod
    async def get_account_by_identity(
        db: aiosqlite.Connection, platform: str, user_id: int,
    ) -> Account | None:
        cursor = await db.execute(
            "SELECT account_id FROM account_identities WHERE platform = ? AND user_id = ?",
            (platform, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return await AccountService.get_account(db, row[0])

    @staticmethod
    async def update_preferences(
        db: aiosqlite.Connection,
        account_id: str,
        *,
        theme: str | None = None,
        accent: str | None = None,
        density: str | None = None,
        reduced_motion: bool | None = None,
        notifications_enabled: bool | None = None,
    ) -> Account | None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if theme is not None:
            if theme not in ("system", "light", "dark"):
                raise ValueError("Некорректная тема")
            updates.append("theme = ?")
            params.append(theme)
        if accent is not None:
            updates.append("accent = ?")
            params.append(accent)
        if density is not None:
            if density not in ("comfortable", "compact"):
                raise ValueError("Некорректная плотность")
            updates.append("density = ?")
            params.append(density)
        if reduced_motion is not None:
            updates.append("reduced_motion = ?")
            params.append(1 if reduced_motion else 0)
        if notifications_enabled is not None:
            updates.append("notifications_enabled = ?")
            params.append(1 if notifications_enabled else 0)

        params.append(account_id)
        cursor = await db.execute(
            f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        if cursor.rowcount == 0:
            return None

        if notifications_enabled is not None:
            # Синхронизируем флаг уведомлений со всеми связанными профилями users
            await db.execute(
                """UPDATE users
                   SET homework_notifications_enabled = ?
                   WHERE (platform, user_id) IN (
                       SELECT platform, user_id FROM account_identities WHERE account_id = ?
                   )""",
                (1 if notifications_enabled else 0, account_id),
            )

        return await AccountService.get_account(db, account_id)

    @staticmethod
    async def add_subscription(
        db: aiosqlite.Connection,
        account_id: str,
        *,
        subscription_type: str,
        subscription_key: str,
        subscription_title: str | None,
        subscription_url: str | None,
        group_name: str | None = None,
        schedule_id: int | None = None,
        university_subgroup: int = 0,
        is_primary: bool = False,
        notifications_enabled: bool = True,
    ) -> AccountSubscription:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        # Если это первая подписка на группу у аккаунта, делаем её основной автоматически
        if subscription_type == "group" and not is_primary:
            existing_primary = await db.execute(
                "SELECT 1 FROM account_subscriptions WHERE account_id = ? AND subscription_type = 'group' AND is_primary = 1",
                (account_id,),
            )
            if not await existing_primary.fetchone():
                is_primary = True

        if is_primary:
            await db.execute(
                "UPDATE account_subscriptions SET is_primary = 0 WHERE account_id = ? AND subscription_type = ?",
                (account_id, subscription_type),
            )

        cursor = await db.execute(
            """INSERT INTO account_subscriptions
               (account_id, subscription_type, subscription_key, subscription_title, subscription_url,
                group_name, schedule_id, university_subgroup, is_primary, notifications_enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id, subscription_type, subscription_key) DO UPDATE SET
                   subscription_title = excluded.subscription_title,
                   subscription_url = excluded.subscription_url,
                   group_name = excluded.group_name,
                   schedule_id = excluded.schedule_id,
                   university_subgroup = excluded.university_subgroup,
                   is_primary = CASE WHEN excluded.is_primary = 1 THEN 1 ELSE account_subscriptions.is_primary END,
                   notifications_enabled = excluded.notifications_enabled,
                   updated_at = excluded.updated_at""",
            (
                account_id, subscription_type, subscription_key, subscription_title, subscription_url,
                group_name, schedule_id, university_subgroup, 1 if is_primary else 0,
                1 if notifications_enabled else 0, now, now,
            ),
        )

        if is_primary:
            await AccountService._sync_primary_to_users(db, account_id)

        sub_id = cursor.lastrowid
        if not sub_id:
            c = await db.execute(
                "SELECT id FROM account_subscriptions WHERE account_id = ? AND subscription_type = ? AND subscription_key = ?",
                (account_id, subscription_type, subscription_key),
            )
            sub_id = (await c.fetchone())[0]

        account = await AccountService.get_account(db, account_id)
        assert account is not None
        for sub in account.subscriptions:
            if sub.id == sub_id:
                return sub
        raise RuntimeError(f"Подписка {sub_id} не найдена после создания")

    @staticmethod
    async def update_subscription(
        db: aiosqlite.Connection,
        account_id: str,
        subscription_id: int,
        *,
        is_primary: bool | None = None,
        university_subgroup: int | None = None,
        notifications_enabled: bool | None = None,
    ) -> AccountSubscription | None:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM account_subscriptions WHERE id = ? AND account_id = ?",
            (subscription_id, account_id),
        )
        row = await cur.fetchone()
        if not row:
            return None

        now = datetime.now(UTC).isoformat(timespec="seconds")
        updates = ["updated_at = ?"]
        params: list[Any] = [now]

        if is_primary is not None:
            if is_primary and not row["is_primary"]:
                await db.execute(
                    "UPDATE account_subscriptions SET is_primary = 0 WHERE account_id = ? AND subscription_type = ?",
                    (account_id, row["subscription_type"]),
                )
            updates.append("is_primary = ?")
            params.append(1 if is_primary else 0)

        if university_subgroup is not None:
            updates.append("university_subgroup = ?")
            params.append(university_subgroup)

        if notifications_enabled is not None:
            updates.append("notifications_enabled = ?")
            params.append(1 if notifications_enabled else 0)

        params.extend([subscription_id, account_id])
        await db.execute(
            f"UPDATE account_subscriptions SET {', '.join(updates)} WHERE id = ? AND account_id = ?",
            tuple(params),
        )

        if is_primary or (row["is_primary"] and university_subgroup is not None):
            await AccountService._sync_primary_to_users(db, account_id)

        account = await AccountService.get_account(db, account_id)
        if not account:
            return None
        for sub in account.subscriptions:
            if sub.id == subscription_id:
                return sub
        return None

    @staticmethod
    async def remove_subscription(
        db: aiosqlite.Connection,
        account_id: str,
        subscription_id: int,
    ) -> bool:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM account_subscriptions WHERE id = ? AND account_id = ?",
            (subscription_id, account_id),
        )
        row = await cur.fetchone()
        if not row:
            return False

        was_primary = bool(row["is_primary"])
        sub_type = row["subscription_type"]

        await db.execute(
            "DELETE FROM account_subscriptions WHERE id = ? AND account_id = ?",
            (subscription_id, account_id),
        )

        if was_primary:
            # Назначаем следующую группу основной, если есть другие подписки на группу
            next_sub = await db.execute(
                """SELECT id FROM account_subscriptions
                   WHERE account_id = ? AND subscription_type = ?
                   ORDER BY id ASC LIMIT 1""",
                (account_id, sub_type),
            )
            next_row = await next_sub.fetchone()
            if next_row:
                await db.execute(
                    "UPDATE account_subscriptions SET is_primary = 1 WHERE id = ?",
                    (next_row[0],),
                )
            await AccountService._sync_primary_to_users(db, account_id)

        return True

    @staticmethod
    async def _sync_primary_to_users(db: aiosqlite.Connection, account_id: str) -> None:
        """Синхронизирует основную группу аккаунта в совместимую проекцию таблицы users."""
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM account_subscriptions
               WHERE account_id = ? AND is_primary = 1
               LIMIT 1""",
            (account_id,),
        )
        primary = await cur.fetchone()
        if primary:
            await db.execute(
                """UPDATE users
                   SET subscription_type = ?,
                       subscription_key = ?,
                       subscription_title = ?,
                       subscription_url = ?,
                       group_name = ?,
                       schedule_id = ?,
                       university_subgroup = CASE WHEN subscription_key IS ? THEN ? ELSE NULL END
                   WHERE (platform, user_id) IN (
                       SELECT platform, user_id FROM account_identities WHERE account_id = ?
                   )""",
                (
                    primary["subscription_type"],
                    primary["subscription_key"],
                    primary["subscription_title"],
                    primary["subscription_url"],
                    primary["group_name"],
                    primary["schedule_id"],
                    primary["subscription_key"],
                    primary["university_subgroup"] or None,
                    account_id,
                ),
            )
        else:
            await db.execute(
                """UPDATE users
                   SET subscription_type = NULL,
                       subscription_key = NULL,
                       subscription_title = NULL,
                       subscription_url = NULL,
                       group_name = NULL,
                       schedule_id = NULL,
                       university_subgroup = NULL
                   WHERE (platform, user_id) IN (
                       SELECT platform, user_id FROM account_identities WHERE account_id = ?
                   )""",
                (account_id,),
            )

    @staticmethod
    async def create_link_request(
        db: aiosqlite.Connection,
        account_id: str,
        platform: str,
        user_id: int,
        ttl_seconds: int = 300,
    ) -> tuple[str, str]:
        raw_token = secrets.token_urlsafe(32)
        token_hash = hash_token(raw_token)
        now_dt = datetime.now(UTC)
        expires_dt = now_dt + timedelta(seconds=ttl_seconds)
        now_iso = now_dt.isoformat(timespec="seconds")
        expires_iso = expires_dt.isoformat(timespec="seconds")

        await db.execute(
            """INSERT INTO account_link_requests
               (token_hash, initiator_account_id, initiator_platform, initiator_user_id, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (token_hash, account_id, platform, user_id, expires_iso, now_iso),
        )
        return raw_token, expires_iso

    @staticmethod
    async def get_link_request(
        db: aiosqlite.Connection, raw_token: str,
    ) -> dict[str, Any] | None:
        token_hash = hash_token(raw_token)
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM account_link_requests
               WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?""",
            (token_hash, now_iso),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)

    @staticmethod
    async def confirm_link(
        db: aiosqlite.Connection,
        raw_token: str,
        target_platform: str,
        target_user_id: int,
        primary_choice: Literal["initiator", "target"] = "initiator",
    ) -> bool:
        req = await AccountService.get_link_request(db, raw_token)
        if not req:
            return False

        initiator_account_id = req["initiator_account_id"]
        token_hash = req["token_hash"]

        target_acc = await AccountService.get_or_create_account(db, target_platform, target_user_id)
        target_account_id = target_acc.id

        if target_account_id == initiator_account_id:
            # Уже привязаны к одному аккаунту
            return False

        now = datetime.now(UTC).isoformat(timespec="seconds")

        # 1. Переносим identities целевого аккаунта к инициатору
        await db.execute(
            "UPDATE account_identities SET account_id = ? WHERE account_id = ?",
            (initiator_account_id, target_account_id),
        )

        # 2. Объединяем подписки
        target_subs_cursor = await db.execute(
            "SELECT * FROM account_subscriptions WHERE account_id = ?",
            (target_account_id,),
        )
        target_subs = await target_subs_cursor.fetchall()

        for sub in target_subs:
            # Проверяем, есть ли уже такая подписка у инициатора
            check_cursor = await db.execute(
                """SELECT id, is_primary FROM account_subscriptions
                   WHERE account_id = ? AND subscription_type = ? AND subscription_key = ?""",
                (initiator_account_id, sub["subscription_type"], sub["subscription_key"]),
            )
            existing = await check_cursor.fetchone()
            if existing:
                continue

            make_primary = bool(sub["is_primary"]) and primary_choice == "target"
            await db.execute(
                """INSERT INTO account_subscriptions
                   (account_id, subscription_type, subscription_key, subscription_title, subscription_url,
                    group_name, schedule_id, university_subgroup, is_primary, notifications_enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    initiator_account_id, sub["subscription_type"], sub["subscription_key"],
                    sub["subscription_title"], sub["subscription_url"], sub["group_name"],
                    sub["schedule_id"], sub["university_subgroup"], 1 if make_primary else 0,
                    sub["notifications_enabled"], now, now,
                ),
            )

        if primary_choice == "target":
            # Убеждаемся, что только целевая подписка основная
            target_primary = target_acc.primary_subscription
            if target_primary:
                await db.execute(
                    """UPDATE account_subscriptions
                       SET is_primary = CASE WHEN subscription_key = ? THEN 1 ELSE 0 END
                       WHERE account_id = ? AND subscription_type = 'group'""",
                    (target_primary.subscription_key, initiator_account_id),
                )

        # 3. Синхронизируем основную группу во все связанные профили users
        await AccountService._sync_primary_to_users(db, initiator_account_id)

        # 4. Помечаем запрос связывания использованным
        await db.execute(
            """UPDATE account_link_requests
               SET used_at = ?, confirmed_target_account_id = ?
               WHERE token_hash = ?""",
            (now, target_account_id, token_hash),
        )

        # 5. Удаляем старый пустой аккаунт
        await db.execute("DELETE FROM accounts WHERE id = ?", (target_account_id,))

        return True

    @staticmethod
    async def unlink_identity(
        db: aiosqlite.Connection,
        account_id: str,
        platform: str,
    ) -> str:
        """Отвязывает платформу от общего аккаунта в отдельный новый аккаунт."""
        account = await AccountService.get_account(db, account_id)
        if not account or len(account.identities) < 2:
            raise ValueError("Нельзя отвязать единственную платформу аккаунта")

        identity_to_unlink: AccountIdentity | None = None
        for ident in account.identities:
            if ident.platform == platform:
                identity_to_unlink = ident
                break

        if not identity_to_unlink:
            raise ValueError(f"Платформа {platform} не привязана к аккаунту {account_id}")

        now = datetime.now(UTC).isoformat(timespec="seconds")
        new_account_id = f"acc_{uuid.uuid4().hex[:16]}"

        # Создаем новый аккаунт для отвязанного пользователя с сохранением настроек
        await db.execute(
            """INSERT INTO accounts
               (id, theme, accent, density, reduced_motion, notifications_enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_account_id, account.theme, account.accent, account.density,
                1 if account.reduced_motion else 0, 1 if account.notifications_enabled else 0,
                now, now,
            ),
        )

        # Переносим identity в новый аккаунт
        await db.execute(
            "UPDATE account_identities SET account_id = ? WHERE platform = ? AND user_id = ?",
            (new_account_id, identity_to_unlink.platform, identity_to_unlink.user_id),
        )

        # Копируем подписки в новый аккаунт
        for sub in account.subscriptions:
            await db.execute(
                """INSERT INTO account_subscriptions
                   (account_id, subscription_type, subscription_key, subscription_title, subscription_url,
                    group_name, schedule_id, university_subgroup, is_primary, notifications_enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_account_id, sub.subscription_type, sub.subscription_key, sub.subscription_title,
                    sub.subscription_url, sub.group_name, sub.schedule_id, sub.university_subgroup,
                    1 if sub.is_primary else 0, 1 if sub.notifications_enabled else 0, now, now,
                ),
            )

        # Отзываем сессии миниаппа для этой платформы
        await db.execute(
            """UPDATE miniapp_sessions
               SET revoked_at = ?
               WHERE platform = ? AND user_id = ? AND revoked_at IS NULL""",
            (now, identity_to_unlink.platform, identity_to_unlink.user_id),
        )

        return new_account_id

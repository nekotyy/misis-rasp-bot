from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALL_PERMISSIONS = [
    "stats_overview",
    "stats_users",
    "stats_services",
    "stats_schedule",
    "stats_delivery",
    "stats_lesson_counters",
    "config_lesson_counters",
    "manage_bot_admin",
    "manage_web_users",
]

PERMISSION_LABELS = {
    "stats_overview": "Обзор статистики",
    "stats_users": "Пользователи",
    "stats_services": "Статус сервисов",
    "stats_schedule": "Парсинг и изменения",
    "stats_delivery": "Доставка сообщений",
    "stats_lesson_counters": "Подсчет пар",
    "config_lesson_counters": "Редактор пар",
    "manage_web_users": "Веб-пользователи",
    "manage_bot_admin": "Управление ботом",
}

INSECURE_SECRET_VALUES = {"", "change-me", "change-me-long-random-secret", "dev-secret-change-me"}
INSECURE_SUPERUSER_PASSWORDS = {"", "change-me", "admin", "password", "12345678"}
LOGIN_GUARD_MAX_ATTEMPTS = 3
LOGIN_GUARD_BLOCK_SECONDS = 6 * 60 * 60


@dataclass(slots=True)
class WebUser:
    login: str
    permissions: list[str]
    is_superuser: bool = False


class WebAuthStore:
    def __init__(
        self,
        database_path: Path,
        superuser_login: str,
        superuser_password: str,
        legacy_json_path: Path | None = None,
    ) -> None:
        self.database_path = database_path
        self.superuser_login = superuser_login.strip() or "admin"
        self.superuser_password = superuser_password
        self.legacy_json_path = legacy_json_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._import_legacy_json_if_needed()

    def authenticate(self, login: str, password: str) -> WebUser | None:
        login = login.strip()
        if login == self.superuser_login and self.superuser_password and hmac.compare_digest(password, self.superuser_password):
            return WebUser(login=login, permissions=list(ALL_PERMISSIONS), is_superuser=True)

        row = self._get_row(login)
        if row is None:
            return None
        if verify_password(password, str(row["password_hash"] or "")):
            return WebUser(login=login, permissions=self._parse_permissions(row["permissions_json"]))
        return None

    def get_user(self, login: str) -> WebUser | None:
        login = login.strip()
        if not login:
            return None
        if login == self.superuser_login:
            return WebUser(login=login, permissions=list(ALL_PERMISSIONS), is_superuser=True)

        row = self._get_row(login)
        if row is None:
            return None
        return WebUser(login=login, permissions=self._parse_permissions(row["permissions_json"]))

    def list_users(self) -> list[dict[str, Any]]:
        users = [
            {
                "login": self.superuser_login,
                "permissions": list(ALL_PERMISSIONS),
                "is_superuser": True,
            }
        ]
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT login, permissions_json
                FROM web_users
                ORDER BY login COLLATE NOCASE
                """
            ).fetchall()
        for row in rows:
            users.append(
                {
                    "login": str(row["login"]),
                    "permissions": self._parse_permissions(row["permissions_json"]),
                    "is_superuser": False,
                }
            )
        return users

    def upsert_user(self, login: str, password: str | None, permissions: list[str]) -> None:
        login = login.strip()
        if not login or login == self.superuser_login:
            raise ValueError("Некорректный логин.")

        permissions_json = json.dumps([item for item in permissions if item in ALL_PERMISSIONS], ensure_ascii=False)
        current = self._get_row(login)
        password_hash = str(current["password_hash"]) if current is not None else ""
        if password:
            password_hash = hash_password(password)
        if not password_hash:
            raise ValueError("Для нового пользователя нужен пароль.")

        now = int(time.time())
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO web_users (login, password_hash, permissions_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(login) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    permissions_json = excluded.permissions_json,
                    updated_at = excluded.updated_at
                """,
                (login, password_hash, permissions_json, now, now),
            )
            connection.commit()

    def delete_user(self, login: str) -> None:
        login = login.strip()
        if not login or login == self.superuser_login:
            return
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM web_users WHERE login = ?", (login,))
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_users (
                    login TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    permissions_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.commit()

    def _import_legacy_json_if_needed(self) -> None:
        if self.legacy_json_path is None or not self.legacy_json_path.exists():
            return
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM web_users").fetchone()
            if int(row["total"] or 0) > 0:
                return

        legacy_users = self._load_legacy_users(self.legacy_json_path)
        if not legacy_users:
            return

        now = int(time.time())
        rows_to_insert: list[tuple[str, str, str, int, int]] = []
        for login, payload in legacy_users.items():
            clean_login = str(login).strip()
            password_hash = str(payload.get("password_hash") or "").strip()
            if not clean_login or not password_hash or clean_login == self.superuser_login:
                continue
            permissions_json = json.dumps(
                [item for item in payload.get("permissions", []) if item in ALL_PERMISSIONS],
                ensure_ascii=False,
            )
            rows_to_insert.append((clean_login, password_hash, permissions_json, now, now))

        if not rows_to_insert:
            return

        with closing(self._connect()) as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO web_users (login, password_hash, permissions_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
            connection.commit()

    def _get_row(self, login: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT login, password_hash, permissions_json
                FROM web_users
                WHERE login = ?
                LIMIT 1
                """,
                (login,),
            ).fetchone()

    def _parse_permissions(self, permissions_json: object) -> list[str]:
        try:
            payload = json.loads(str(permissions_json or "[]"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if item in ALL_PERMISSIONS]

    def _load_legacy_users(self, path: Path) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}


class SessionSigner:
    def __init__(self, secret: str) -> None:
        self.secret = (secret or "dev-secret-change-me").encode("utf-8")

    def sign(self, login: str) -> str:
        payload = {"login": login, "iat": int(time.time()), "nonce": secrets.token_hex(8)}
        data = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
        sig = hmac.new(self.secret, data.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{data}.{sig}"

    def unsign(self, token: str | None, max_age_seconds: int = 60 * 60 * 12) -> str | None:
        if not token or "." not in token:
            return None
        data, sig = token.rsplit(".", 1)
        expected = hmac.new(self.secret, data.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None
        if int(time.time()) - int(payload.get("iat", 0)) > max_age_seconds:
            return None
        return str(payload.get("login") or "")


class LoginRateLimiter:
    def __init__(self, database_path: Path, *, max_attempts: int = LOGIN_GUARD_MAX_ATTEMPTS, block_seconds: int = LOGIN_GUARD_BLOCK_SECONDS) -> None:
        self.database_path = database_path
        self.max_attempts = max_attempts
        self.block_seconds = block_seconds
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def is_blocked(self, keys: list[str]) -> bool:
        now = int(time.time())
        normalized = list(dict.fromkeys(key.strip() for key in keys if key.strip()))
        if not normalized:
            return False
        placeholders = ",".join("?" for _ in normalized)
        with closing(self._connect()) as connection:
            self._cleanup_expired(connection, now)
            row = connection.execute(
                f"""
                SELECT 1
                FROM web_login_guard
                WHERE fingerprint_key IN ({placeholders}) AND blocked_until > ?
                LIMIT 1
                """,
                (*normalized, now),
            ).fetchone()
            return row is not None

    def register_failure(self, keys: list[str]) -> None:
        now = int(time.time())
        normalized = list(dict.fromkeys(key.strip() for key in keys if key.strip()))
        if not normalized:
            return
        blocked_until = 0
        with closing(self._connect()) as connection:
            self._cleanup_expired(connection, now)
            for key in normalized:
                row = connection.execute(
                    """
                    SELECT fail_count, blocked_until
                    FROM web_login_guard
                    WHERE fingerprint_key = ?
                    LIMIT 1
                    """,
                    (key,),
                ).fetchone()
                fail_count = 1
                previous_block = 0
                if row is not None:
                    previous_block = int(row["blocked_until"] or 0)
                    fail_count = int(row["fail_count"] or 0) + 1
                if fail_count >= self.max_attempts:
                    blocked_until = max(blocked_until, now + self.block_seconds, previous_block)
                connection.execute(
                    """
                    INSERT INTO web_login_guard (fingerprint_key, fail_count, blocked_until, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(fingerprint_key) DO UPDATE SET
                        fail_count = excluded.fail_count,
                        blocked_until = CASE
                            WHEN excluded.blocked_until > web_login_guard.blocked_until THEN excluded.blocked_until
                            ELSE web_login_guard.blocked_until
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (key, fail_count, previous_block, now),
                )
            if blocked_until > now:
                placeholders = ",".join("?" for _ in normalized)
                connection.execute(
                    f"""
                    UPDATE web_login_guard
                    SET blocked_until = ?, updated_at = ?
                    WHERE fingerprint_key IN ({placeholders})
                    """,
                    (blocked_until, now, *normalized),
                )
            connection.commit()

    def reset(self, keys: list[str]) -> None:
        normalized = list(dict.fromkeys(key.strip() for key in keys if key.strip()))
        if not normalized:
            return
        placeholders = ",".join("?" for _ in normalized)
        with closing(self._connect()) as connection:
            connection.execute(f"DELETE FROM web_login_guard WHERE fingerprint_key IN ({placeholders})", normalized)
            connection.commit()

    def record_attempt(
        self,
        *,
        requested_login: str,
        ip: str,
        user_agent: str,
        device_id_hash: str,
        fingerprint_hash: str,
        outcome: str,
        reason: str,
    ) -> None:
        now = int(time.time())
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO web_login_attempts (
                    created_at,
                    requested_login,
                    ip,
                    user_agent,
                    device_id_hash,
                    fingerprint_hash,
                    outcome,
                    reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    requested_login.strip()[:128],
                    ip.strip()[:128],
                    user_agent.strip()[:512],
                    device_id_hash[:96],
                    fingerprint_hash[:96],
                    outcome[:32],
                    reason[:64],
                ),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_login_guard (
                    fingerprint_key TEXT PRIMARY KEY,
                    fail_count INTEGER NOT NULL,
                    blocked_until INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_login_guard_blocked_until
                ON web_login_guard(blocked_until)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_login_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    requested_login TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    device_id_hash TEXT NOT NULL,
                    fingerprint_hash TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_login_attempts_created_at
                ON web_login_attempts(created_at DESC)
                """
            )
            connection.commit()

    def _cleanup_expired(self, connection: sqlite3.Connection, now: int) -> None:
        connection.execute(
            """
            DELETE FROM web_login_guard
            WHERE blocked_until > 0 AND blocked_until <= ?
            """,
            (now,),
        )
        connection.execute(
            """
            DELETE FROM web_login_guard
            WHERE blocked_until = 0 AND updated_at <= ?
            """,
            (now - self.block_seconds,),
        )


def validate_security_config(secret: str, superuser_password: str) -> None:
    issues: list[str] = []
    normalized_secret = (secret or "").strip()
    normalized_password = (superuser_password or "").strip()

    if normalized_secret in INSECURE_SECRET_VALUES or len(normalized_secret) < 32:
        issues.append("WEB_CONFIG_SECRET must be unique and at least 32 characters long.")
    if normalized_password in INSECURE_SUPERUSER_PASSWORDS or len(normalized_password) < 12:
        issues.append("WEB_SUPERUSER_PASSWORD must be set and at least 12 characters long.")

    if issues:
        raise RuntimeError("Unsafe web admin configuration: " + " ".join(issues))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180_000)
    return f"pbkdf2_sha256${base64.b64encode(salt).decode('ascii')}${base64.b64encode(digest).decode('ascii')}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, salt_raw, digest_raw = password_hash.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_raw)
        expected = base64.b64decode(digest_raw)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180_000)
    return hmac.compare_digest(actual, expected)

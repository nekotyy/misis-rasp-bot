from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
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
}


@dataclass(slots=True)
class WebUser:
    login: str
    permissions: list[str]
    is_superuser: bool = False


class WebAuthStore:
    def __init__(self, path: Path, superuser_login: str, superuser_password: str) -> None:
        self.path = path
        self.superuser_login = superuser_login.strip() or "admin"
        self.superuser_password = superuser_password
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def authenticate(self, login: str, password: str) -> WebUser | None:
        login = login.strip()
        if login == self.superuser_login and self.superuser_password and hmac.compare_digest(password, self.superuser_password):
            return WebUser(login=login, permissions=list(ALL_PERMISSIONS), is_superuser=True)

        user = self._load_users().get(login)
        if not user:
            return None
        if verify_password(password, str(user.get("password_hash", ""))):
            permissions = [item for item in user.get("permissions", []) if item in ALL_PERMISSIONS]
            return WebUser(login=login, permissions=permissions)
        return None

    def get_user(self, login: str) -> WebUser | None:
        if login == self.superuser_login:
            return WebUser(login=login, permissions=list(ALL_PERMISSIONS), is_superuser=True)
        user = self._load_users().get(login)
        if not user:
            return None
        permissions = [item for item in user.get("permissions", []) if item in ALL_PERMISSIONS]
        return WebUser(login=login, permissions=permissions)

    def list_users(self) -> list[dict[str, Any]]:
        users = [
            {
                "login": self.superuser_login,
                "permissions": list(ALL_PERMISSIONS),
                "is_superuser": True,
            }
        ]
        for login, user in sorted(self._load_users().items()):
            users.append(
                {
                    "login": login,
                    "permissions": [item for item in user.get("permissions", []) if item in ALL_PERMISSIONS],
                    "is_superuser": False,
                }
            )
        return users

    def upsert_user(self, login: str, password: str | None, permissions: list[str]) -> None:
        login = login.strip()
        if not login or login == self.superuser_login:
            raise ValueError("Некорректный логин.")
        users = self._load_users()
        current = users.get(login, {})
        password_hash = current.get("password_hash")
        if password:
            password_hash = hash_password(password)
        if not password_hash:
            raise ValueError("Для нового пользователя нужен пароль.")
        users[login] = {
            "password_hash": password_hash,
            "permissions": [item for item in permissions if item in ALL_PERMISSIONS],
        }
        self._save_users(users)

    def delete_user(self, login: str) -> None:
        users = self._load_users()
        users.pop(login, None)
        self._save_users(users)

    def _load_users(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_users(self, users: dict[str, dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


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

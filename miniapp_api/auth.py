"""Аутентификация Telegram/VK и управление отзывными сессиями миниаппа."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
from fastapi import Header, HTTPException, status

from src.account_service import Account, AccountIdentity, AccountService, hash_token


def verify_telegram_init_data(
    init_data_raw: str,
    bot_token: str,
    max_age_seconds: int = 86400,
) -> dict[str, Any] | None:
    """Проверяет подпись initData Telegram WebApp по спецификации Telegram."""
    if not init_data_raw or not bot_token:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data_raw, keep_blank_values=True))
    except Exception:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(received_hash, expected_hash):
        return None

    try:
        auth_date = int(parsed.get("auth_date", 0))
    except (TypeError, ValueError):
        return None

    if max_age_seconds > 0:
        now_ts = datetime.now(UTC).timestamp()
        if now_ts - auth_date > max_age_seconds:
            return None

    user_str = parsed.get("user")
    user_data = json.loads(user_str) if user_str else {}
    user_id = user_data.get("id")
    if not user_id:
        return None

    return {
        "user_id": int(user_id),
        "username": user_data.get("username"),
        "first_name": user_data.get("first_name"),
        "last_name": user_data.get("last_name"),
        "auth_date": auth_date,
    }


def verify_vk_launch_params(
    launch_params_raw: str,
    client_secret: str,
) -> dict[str, Any] | None:
    """Проверяет подпись параметров запуска VK Mini Apps."""
    if not launch_params_raw or not client_secret:
        return None
    try:
        normalized = launch_params_raw.strip().lstrip("?#")
        parsed = dict(urllib.parse.parse_qsl(normalized, keep_blank_values=True))
    except Exception:
        return None

    received_sign = parsed.pop("sign", None)
    if not received_sign:
        return None

    vk_params = {k: v for k, v in parsed.items() if k.startswith("vk_")}
    if not vk_params:
        return None

    sorted_query = urllib.parse.urlencode(sorted(vk_params.items()))
    hash_bytes = hmac.new(client_secret.encode("utf-8"), sorted_query.encode("utf-8"), hashlib.sha256).digest()
    expected_sign = base64.b64encode(hash_bytes).decode("utf-8").rstrip("=").replace("+", "-").replace("/", "_")

    received_sign_clean = received_sign.rstrip("=").replace("+", "-").replace("/", "_")
    if not hmac.compare_digest(received_sign_clean, expected_sign):
        return None

    try:
        user_id = int(vk_params.get("vk_user_id", 0))
        app_id = int(vk_params.get("vk_app_id", 0))
    except (TypeError, ValueError):
        return None

    if not user_id:
        return None

    return {
        "user_id": user_id,
        "app_id": app_id,
    }


class SessionManager:
    @staticmethod
    async def create_session(
        db: aiosqlite.Connection,
        account_id: str,
        platform: str,
        user_id: int,
        ttl_seconds: int = 86400,
    ) -> str:
        raw_token = secrets.token_urlsafe(32)
        tok_hash = hash_token(raw_token)
        now_dt = datetime.now(UTC)
        expires_dt = now_dt + timedelta(seconds=ttl_seconds)

        now_iso = now_dt.isoformat(timespec="seconds")
        expires_iso = expires_dt.isoformat(timespec="seconds")

        await db.execute(
            """INSERT INTO miniapp_sessions
               (token_hash, account_id, platform, user_id, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (tok_hash, account_id, platform, user_id, expires_iso, now_iso),
        )
        return raw_token

    @staticmethod
    async def authenticate_session(
        db: aiosqlite.Connection,
        raw_token: str,
    ) -> tuple[Account, AccountIdentity] | None:
        if not raw_token:
            return None
        tok_hash = hash_token(raw_token)
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT account_id, platform, user_id
               FROM miniapp_sessions
               WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?""",
            (tok_hash, now_iso),
        )
        row = await cur.fetchone()
        if not row:
            return None

        account = await AccountService.get_account(db, row["account_id"])
        if not account:
            return None

        current_ident: AccountIdentity | None = None
        for ident in account.identities:
            if ident.platform == row["platform"] and ident.user_id == row["user_id"]:
                current_ident = ident
                break

        if not current_ident:
            return None

        return account, current_ident

    @staticmethod
    async def revoke_session(
        db: aiosqlite.Connection,
        raw_token: str,
    ) -> bool:
        tok_hash = hash_token(raw_token)
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        cur = await db.execute(
            """UPDATE miniapp_sessions
               SET revoked_at = ?
               WHERE token_hash = ? AND revoked_at IS NULL""",
            (now_iso, tok_hash),
        )
        return cur.rowcount > 0


def get_token_from_header(authorization: str | None = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Необходима авторизация Bearer",
        )
    return authorization.removeprefix("Bearer ").strip()

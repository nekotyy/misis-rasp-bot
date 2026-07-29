from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _parse_int_list(raw: str) -> list[int]:
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.append(int(part))
            except ValueError:
                continue
    return result


@dataclass(slots=True)
class Settings:
    schedule_url: str
    database_path: Path
    app_timezone: str
    schedule_request_delay_seconds: float
    schedule_request_jitter_seconds: float
    telegram_bot_token: str
    vk_bot_token: str
    vk_disable_ssl_verify: bool
    admin_telegram_id: int | None
    admin_telegram_ids: list[int]
    limited_admin_telegram_ids: list[int]
    admin_vk_id: int | None
    rabbitmq_url: str
    rabbitmq_queue: str
    lesson_counters_queue: str
    db_cleanup_queue: str
    auto_daily_lesson_counter_queue: str
    rabbitmq_prefetch_count: int
    lesson_counters_enabled: bool
    lesson_counters_path: Path
    web_cookie_secure: bool
    web_session_ttl_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        database_path = Path(os.getenv("DATABASE_PATH", "bot.db")).resolve()
        admin_telegram_id_raw = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
        admin_vk_id_raw = os.getenv("ADMIN_VK_ID", "").strip()
        schedule_url = os.getenv("SCHEDULE_URL", "http://asu.sf-misis.ru/rasp/600")
        admin_telegram_ids = _parse_int_list(admin_telegram_id_raw)
        limited_admin_telegram_ids = _parse_int_list(os.getenv("LIMITED_ADMIN_TELEGRAM_IDS", "").strip())
        return cls(
            schedule_url=schedule_url,
            database_path=database_path,
            app_timezone=os.getenv("APP_TIMEZONE", "Europe/Moscow"),
            schedule_request_delay_seconds=float(os.getenv("SCHEDULE_REQUEST_DELAY_SECONDS", "8").strip()),
            schedule_request_jitter_seconds=float(os.getenv("SCHEDULE_REQUEST_JITTER_SECONDS", "4").strip()),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            vk_bot_token=os.getenv("VK_BOT_TOKEN", "").strip(),
            vk_disable_ssl_verify=os.getenv("VK_DISABLE_SSL_VERIFY", "").strip().lower() in {"1", "true", "yes", "on"},
            admin_telegram_id=admin_telegram_ids[0] if admin_telegram_ids else None,
            admin_telegram_ids=admin_telegram_ids,
            limited_admin_telegram_ids=limited_admin_telegram_ids,
            admin_vk_id=int(admin_vk_id_raw) if admin_vk_id_raw else None,
            rabbitmq_url=os.getenv("RABBITMQ_URL", "").strip(),
            rabbitmq_queue=os.getenv("RABBITMQ_QUEUE", "misis_notifications").strip(),
            lesson_counters_queue=os.getenv("LESSON_COUNTERS_QUEUE", "misis_lesson_counters").strip(),
            db_cleanup_queue=os.getenv("DB_CLEANUP_QUEUE", "misis_db_cleanup").strip(),
            auto_daily_lesson_counter_queue=os.getenv("AUTO_DAILY_LESSON_COUNTER_QUEUE", "misis_auto_daily_lesson_counter").strip(),
            rabbitmq_prefetch_count=int(os.getenv("RABBITMQ_PREFETCH_COUNT", "20").strip()),
            lesson_counters_enabled=_env_bool("LESSON_COUNTERS_ENABLED", default=False),
            lesson_counters_path=Path(os.getenv("LESSON_COUNTERS_PATH", "storage/lesson_counters.json")).resolve(),
            web_cookie_secure=_env_bool("WEB_COOKIE_SECURE", default=True),
            web_session_ttl_seconds=max(300, int(os.getenv("WEB_SESSION_TTL_SECONDS", str(60 * 60 * 12)).strip())),
        )


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да", "вкл"}

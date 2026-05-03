from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


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
    admin_vk_id: int | None
    rabbitmq_url: str
    rabbitmq_queue: str
    lesson_counters_queue: str
    rabbitmq_prefetch_count: int
    lesson_counters_enabled: bool
    lesson_counters_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        database_path = Path(os.getenv("DATABASE_PATH", "bot.db")).resolve()
        admin_telegram_id_raw = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
        admin_vk_id_raw = os.getenv("ADMIN_VK_ID", "").strip()
        schedule_url = os.getenv("SCHEDULE_URL", "http://asu.sf-misis.ru/rasp/600")
        return cls(
            schedule_url=schedule_url,
            database_path=database_path,
            app_timezone=os.getenv("APP_TIMEZONE", "Europe/Moscow"),
            schedule_request_delay_seconds=float(os.getenv("SCHEDULE_REQUEST_DELAY_SECONDS", "8").strip()),
            schedule_request_jitter_seconds=float(os.getenv("SCHEDULE_REQUEST_JITTER_SECONDS", "4").strip()),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            vk_bot_token=os.getenv("VK_BOT_TOKEN", "").strip(),
            vk_disable_ssl_verify=os.getenv("VK_DISABLE_SSL_VERIFY", "").strip().lower() in {"1", "true", "yes", "on"},
            admin_telegram_id=int(admin_telegram_id_raw) if admin_telegram_id_raw else None,
            admin_vk_id=int(admin_vk_id_raw) if admin_vk_id_raw else None,
            rabbitmq_url=os.getenv("RABBITMQ_URL", "").strip(),
            rabbitmq_queue=os.getenv("RABBITMQ_QUEUE", "misis_notifications").strip(),
            lesson_counters_queue=os.getenv("LESSON_COUNTERS_QUEUE", "misis_lesson_counters").strip(),
            rabbitmq_prefetch_count=int(os.getenv("RABBITMQ_PREFETCH_COUNT", "20").strip()),
            lesson_counters_enabled=_env_bool("LESSON_COUNTERS_ENABLED", default=False),
            lesson_counters_path=Path(os.getenv("LESSON_COUNTERS_PATH", "storage/lesson_counters.json")).resolve(),
        )


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да", "вкл"}

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(slots=True)
class Settings:
    schedule_url: str
    group_name: str
    database_path: Path
    app_timezone: str
    telegram_bot_token: str
    vk_bot_token: str
    vk_disable_ssl_verify: bool
    admin_telegram_id: int | None

    @classmethod
    def from_env(cls) -> "Settings":
        database_path = Path(os.getenv("DATABASE_PATH", "bot.db")).resolve()
        admin_telegram_id_raw = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
        return cls(
            schedule_url=os.getenv("SCHEDULE_URL", "http://asu.sf-misis.ru/rasp/600"),
            group_name=os.getenv("GROUP_NAME", "ИСП-25-1"),
            database_path=database_path,
            app_timezone=os.getenv("APP_TIMEZONE", "Europe/Moscow"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            vk_bot_token=os.getenv("VK_BOT_TOKEN", "").strip(),
            vk_disable_ssl_verify=os.getenv("VK_DISABLE_SSL_VERIFY", "").strip().lower() in {"1", "true", "yes", "on"},
            admin_telegram_id=int(admin_telegram_id_raw) if admin_telegram_id_raw else None,
        )

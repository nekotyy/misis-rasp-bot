from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Lesson:
    number: int
    subject: str
    teacher: str
    classroom: str


@dataclass(slots=True)
class DaySchedule:
    date_label: str
    date_iso: str
    lessons: list[Lesson]


@dataclass(slots=True)
class ScheduleSnapshot:
    group_name: str
    fetched_at: datetime
    days: list[DaySchedule]


@dataclass(slots=True)
class UserRecord:
    platform: str
    user_id: int
    username: str | None
    full_name: str | None
    subscription_type: str | None
    subscription_key: str | None
    subscription_title: str | None
    subscription_url: str | None
    audience_subscription_key: str | None
    audience_subscription_title: str | None
    audience_subscription_url: str | None
    group_name: str | None
    schedule_id: int | None
    is_admin: bool
    is_editor: bool
    homework_notifications_enabled: bool
    delivery_disabled_auto: bool
    created_at: str
    last_seen_at: str
    custom_sticker_file_id: str | None = None


@dataclass(slots=True)
class ChangeSummary:
    changed_dates: list[str]
    message: str
    payload: dict
    telegram_message: str | None = None
    vk_message: str | None = None

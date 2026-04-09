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
    is_admin: bool
    created_at: str
    last_seen_at: str


@dataclass(slots=True)
class ChangeSummary:
    changed_dates: list[str]
    message: str
    payload: dict

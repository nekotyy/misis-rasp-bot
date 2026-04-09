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
    is_editor: bool
    created_at: str
    last_seen_at: str


@dataclass(slots=True)
class ChangeSummary:
    changed_dates: list[str]
    message: str
    payload: dict


@dataclass(slots=True)
class HomeworkAttachment:
    file_id: str
    file_type: str
    file_name: str | None
    mime_type: str | None


@dataclass(slots=True)
class HomeworkDraft:
    subject_key: str
    subject_name: str
    teacher_name: str
    text: str = ""
    attachments: list[HomeworkAttachment] | None = None
    awaiting_text: bool = True
    awaiting_attachments: bool = False

    def __post_init__(self) -> None:
        if self.attachments is None:
            self.attachments = []

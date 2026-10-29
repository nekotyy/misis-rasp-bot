"""Личная подгруппа: общие занятия плюс выбранная часть полного расписания."""

from __future__ import annotations

import re
from dataclasses import replace

from src.models import DaySchedule, ScheduleSnapshot

_SUBGROUPS = re.compile(r"(\d+(?:\s*(?:,|/|и|–|-)\s*\d+)*)\s*(?:подгрупп[а-я]*|п\s*/\s*г)\b", re.IGNORECASE)


def is_university_group(user) -> bool:
    return bool(user and getattr(user, "subscription_type", None) == "group"
                and isinstance(getattr(user, "schedule_id", None), int) and user.schedule_id < 0)


def personal_subgroup(user) -> int | None:
    return getattr(user, "university_subgroup", None) if is_university_group(user) else None


def lesson_subgroups(subject: str) -> set[int]:
    result: set[int] = set()
    for match in _SUBGROUPS.finditer(subject):
        numbers = [int(number) for number in re.findall(r"\d+", match[1])]
        if len(numbers) == 2 and re.search(r"[–-]", match[1]) and 0 < numbers[0] <= numbers[1] <= 99:
            result.update(range(numbers[0], numbers[1] + 1))
        else:
            result.update(number for number in numbers if 0 < number <= 99)
    return result


def available_subgroups(content: dict) -> list[int]:
    return sorted({number for day in content.get("days", []) for lesson in day.get("lessons", [])
                   for number in lesson_subgroups(str(lesson.get("subject") or ""))})


def filter_day(day: DaySchedule, subgroup: int | None) -> DaySchedule:
    if subgroup is None:
        return day
    return replace(day, lessons=[lesson for lesson in day.lessons
                                if not (numbers := lesson_subgroups(lesson.subject)) or subgroup in numbers])


def filter_snapshot(snapshot: ScheduleSnapshot, subgroup: int | None) -> ScheduleSnapshot:
    return replace(snapshot, days=[filter_day(day, subgroup) for day in snapshot.days])


def filter_content(content: dict, subgroup: int | None) -> dict:
    if subgroup is None:
        return content
    return dict(content, days=[dict(day, lessons=[lesson for lesson in day.get("lessons", [])
                                                if not (numbers := lesson_subgroups(str(lesson.get("subject") or "")))
                                                or subgroup in numbers]) for day in content.get("days", [])])

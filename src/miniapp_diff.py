"""Структурированное сравнение пар для будущего интерфейса миниаппа."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from src.models import Lesson
from src.university_subgroups import lesson_subgroups

LessonStatus = Literal["unchanged", "changed", "added", "ambiguous"]
LessonField = Literal["subject", "teacher", "classroom"]


@dataclass(frozen=True, slots=True)
class LessonChange:
    lesson: Lesson
    status: LessonStatus
    before: Lesson | None = None
    changed_fields: tuple[LessonField, ...] = ()


@dataclass(frozen=True, slots=True)
class DayChanges:
    lessons: tuple[LessonChange, ...]
    missing: tuple[Lesson, ...]


def _identity(lesson: Lesson) -> tuple[int, str, str, str]:
    return lesson.number, lesson.subject, lesson.teacher, lesson.classroom


def _slot(lesson: Lesson) -> tuple[int, tuple[int, ...]]:
    return lesson.number, tuple(sorted(lesson_subgroups(lesson.subject)))


def _could_share_slot(first: Lesson, second: Lesson) -> bool:
    if first.number != second.number:
        return False
    first_groups = lesson_subgroups(first.subject)
    second_groups = lesson_subgroups(second.subject)
    return not first_groups or not second_groups or bool(first_groups & second_groups)


def compare_lessons(before: list[Lesson] | None, after: list[Lesson]) -> DayChanges:
    """Сравнивает одну дату и один источник; None означает отсутствие опоры.

    Неизменные пары учитываются как мультимножество. Остатки сопоставляются
    только при единственном кандидате с обеих сторон для номера и подгруппы.
    Исчезновение само по себе не подтверждает отмену.
    """
    if before is None:
        return DayChanges(tuple(LessonChange(lesson, "unchanged") for lesson in after), ())

    remaining = Counter(_identity(lesson) for lesson in before)
    unmatched_after: list[int] = []
    changes: list[LessonChange | None] = [None] * len(after)
    for index, lesson in enumerate(after):
        identity = _identity(lesson)
        if remaining[identity]:
            remaining[identity] -= 1
            changes[index] = LessonChange(lesson, "unchanged")
        else:
            unmatched_after.append(index)

    unmatched_before: list[Lesson] = []
    for lesson in before:
        identity = _identity(lesson)
        if remaining[identity]:
            unmatched_before.append(lesson)
            remaining[identity] -= 1

    matched_old_ids: set[int] = set()
    for index in unmatched_after:
        lesson = after[index]
        candidates = [old for old in unmatched_before if _could_share_slot(old, lesson)]
        if len(candidates) == 1 and _slot(candidates[0]) == _slot(lesson) and sum(
            _could_share_slot(candidates[0], after[other]) for other in unmatched_after
        ) == 1:
            previous = candidates[0]
            fields: tuple[LessonField, ...] = tuple(
                field for field in ("subject", "teacher", "classroom")
                if getattr(previous, field) != getattr(lesson, field)
            )
            changes[index] = LessonChange(lesson, "changed", previous, fields)
            matched_old_ids.add(id(previous))
        else:
            changes[index] = LessonChange(lesson, "ambiguous" if candidates else "added")

    return DayChanges(
        tuple(change for change in changes if change is not None),
        tuple(lesson for lesson in unmatched_before if id(lesson) not in matched_old_ids),
    )

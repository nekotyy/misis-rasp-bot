from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.group_catalog import GroupCatalog
from src.lesson_counters import normalize_lesson_text, teacher_matches
from src.parser import ScheduleParser


def load_lesson_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"groups": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"groups": []}
    if isinstance(payload, list):
        return {"groups": payload}
    if isinstance(payload, dict):
        payload.setdefault("groups", [])
        return payload
    return {"groups": []}


def save_lesson_config(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def validate_lesson_config(
    payload: dict[str, Any],
    *,
    group_catalog: GroupCatalog,
    parser: ScheduleParser,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    groups = payload.get("groups", [])
    if not isinstance(groups, list):
        raise ValueError("Поле groups должно быть списком.")

    normalized_groups: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []

    for group_index, group_item in enumerate(groups):
        if not isinstance(group_item, dict):
            problems.append({"level": "error", "message": f"Группа #{group_index + 1}: запись должна быть объектом."})
            continue

        schedule_id = await _resolve_schedule_id(group_item, group_catalog)
        if schedule_id is None:
            problems.append({"level": "error", "message": f"Группа #{group_index + 1}: не нашел schedule_id/group_name."})
            continue

        catalog_group = await group_catalog.get_by_schedule_id(schedule_id)
        group_name = group_item.get("group_name") or (catalog_group.group_name if catalog_group else str(schedule_id))
        subjects = group_item.get("subjects", [])
        if not isinstance(subjects, list):
            problems.append({"level": "error", "message": f"{group_name}: subjects должен быть списком."})
            continue

        schedule_subjects, schedule_teachers = await _load_group_subjects(parser, schedule_id)
        normalized_subjects: list[dict[str, Any]] = []
        for subject_index, subject_item in enumerate(subjects):
            if not isinstance(subject_item, dict):
                problems.append({"level": "error", "message": f"{group_name}: дисциплина #{subject_index + 1} должна быть объектом."})
                continue
            subject = str(subject_item.get("subject") or "").strip()
            teacher = str(subject_item.get("teacher") or "").strip()
            if not subject or not teacher:
                problems.append({"level": "error", "message": f"{group_name}: у дисциплины #{subject_index + 1} нужны subject и teacher."})
                continue

            canonical_subject = _canonical_subject(subject, schedule_subjects)
            if canonical_subject is None:
                problems.append({"level": "error", "message": f"{group_name}: дисциплины «{subject}» нет в расписании группы."})
                continue

            if not _teacher_exists_for_subject(teacher, canonical_subject, schedule_teachers):
                problems.append(
                    {
                        "level": "warning",
                        "message": f"{group_name}: преподаватель «{teacher}» не найден у дисциплины «{canonical_subject}» в текущем расписании.",
                    }
                )

            normalized_subjects.append(
                {
                    "subject": canonical_subject,
                    "teacher": teacher,
                    "passed": max(0, int(subject_item.get("passed", 0) or 0)),
                    "total": max(0, int(subject_item.get("total", 0) or 0)),
                }
            )

        normalized_groups.append(
            {
                "schedule_id": schedule_id,
                "group_name": group_name,
                "subjects": normalized_subjects,
            }
        )

    return {"groups": normalized_groups}, problems


async def _resolve_schedule_id(item: dict[str, Any], group_catalog: GroupCatalog) -> int | None:
    raw_schedule_id = item.get("schedule_id")
    if raw_schedule_id not in (None, ""):
        try:
            return int(raw_schedule_id)
        except (TypeError, ValueError):
            return None
    group_name = str(item.get("group_name") or "").strip()
    if not group_name:
        return None
    group = await group_catalog.find_group(group_name)
    return group.schedule_id if group else None


async def _load_group_subjects(parser: ScheduleParser, schedule_id: int) -> tuple[dict[str, str], dict[str, set[str]]]:
    snapshot, _ = await parser.parse(schedule_id)
    subjects: dict[str, str] = {}
    teachers_by_subject: dict[str, set[str]] = {}
    for day in snapshot.days:
        for lesson in day.lessons:
            subject_norm = normalize_lesson_text(lesson.subject)
            subjects.setdefault(subject_norm, lesson.subject)
            teachers_by_subject.setdefault(subject_norm, set()).add(lesson.teacher)
    return subjects, teachers_by_subject


def _canonical_subject(subject: str, subjects: dict[str, str]) -> str | None:
    subject_norm = normalize_lesson_text(subject)
    if subject_norm in subjects:
        return subjects[subject_norm]
    compact = subject_norm.replace(".", "").replace("-", " ")
    compact = " ".join(compact.split())
    for candidate_norm, candidate in subjects.items():
        candidate_compact = " ".join(candidate_norm.replace(".", "").replace("-", " ").split())
        if compact == candidate_compact:
            return candidate
    return None


def _teacher_exists_for_subject(teacher: str, canonical_subject: str, teachers_by_subject: dict[str, set[str]]) -> bool:
    subject_norm = normalize_lesson_text(canonical_subject)
    expected_teacher_norm = normalize_lesson_text(teacher)
    return any(teacher_matches(expected_teacher_norm, teacher_from_schedule) for teacher_from_schedule in teachers_by_subject.get(subject_norm, set()))

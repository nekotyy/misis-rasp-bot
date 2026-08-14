from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from src.group_catalog import GroupCatalog
from src.lesson_counters import normalize_lesson_text, subject_matches, teacher_matches
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
    temp_file = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    try:
        temp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_file.replace(path)
    finally:
        if temp_file.exists():
            with contextlib.suppress(OSError):
                temp_file.unlink()


def upsert_lesson_subject(
    payload: dict[str, Any],
    *,
    schedule_id: int,
    group_name: str,
    subject: str,
    teacher: str,
    passed: int,
    total: int,
) -> bool:
    groups = payload.setdefault("groups", [])
    group = next(
        (
            item
            for item in groups
            if isinstance(item, dict) and int(item.get("schedule_id") or 0) == schedule_id
        ),
        None,
    )
    if group is None:
        group = {"schedule_id": schedule_id, "group_name": group_name, "subjects": []}
        groups.append(group)
    subjects = group.setdefault("subjects", [])
    subject_norm = normalize_lesson_text(subject)
    teacher_norm = normalize_lesson_text(teacher)
    for index, item in enumerate(subjects):
        if not isinstance(item, dict):
            continue
        item_subject = str(item.get("subject") or "")
        item_teacher = str(item.get("teacher") or "")
        if subject_matches(subject_norm, item_subject) and teacher_matches(teacher_norm, item_teacher):
            subjects[index] = {"subject": subject, "teacher": teacher, "passed": passed, "total": total}
            return True
    subjects.append({"subject": subject, "teacher": teacher, "passed": passed, "total": total})
    return False


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
        if subject_matches(subject_norm, candidate):
            return candidate
    return None


def _teacher_exists_for_subject(teacher: str, canonical_subject: str, teachers_by_subject: dict[str, set[str]]) -> bool:
    subject_norm = normalize_lesson_text(canonical_subject)
    expected_teacher_norm = normalize_lesson_text(teacher)
    return any(teacher_matches(expected_teacher_norm, teacher_from_schedule) for teacher_from_schedule in teachers_by_subject.get(subject_norm, set()))


def parse_imported_json_payload(raw_input: str | bytes | dict | list) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(raw_input, (str, bytes)):
        try:
            payload = json.loads(raw_input)
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"Ошибка парсинга JSON: {exc}"
    else:
        payload = raw_input

    if isinstance(payload, list):
        groups = payload
    elif isinstance(payload, dict):
        groups = payload.get("groups") or payload.get("counters")
        if groups is None and ("subject" in payload or "subjects" in payload or "schedule_id" in payload or "group_name" in payload):
            groups = [payload]
    else:
        return None, "JSON должен быть объектом или списком групп."

    if not isinstance(groups, list) or not groups:
        return None, "В JSON не найдено списка групп ('groups')."

    normalized_groups: list[dict[str, Any]] = []
    for g_item in groups:
        if not isinstance(g_item, dict):
            continue
        schedule_id = g_item.get("schedule_id")
        if schedule_id not in (None, ""):
            try:
                schedule_id = int(schedule_id)
            except (ValueError, TypeError):
                schedule_id = None
        else:
            schedule_id = None

        group_name = str(g_item.get("group_name") or g_item.get("group") or "").strip()
        raw_subjects = g_item.get("subjects") or g_item.get("counters") or []
        if isinstance(raw_subjects, dict):
            raw_subjects = [raw_subjects]
        if not isinstance(raw_subjects, list):
            raw_subjects = []

        norm_subjects: list[dict[str, Any]] = []
        for s_item in raw_subjects:
            if not isinstance(s_item, dict):
                continue
            subject = str(s_item.get("subject") or "").strip()
            teacher = str(s_item.get("teacher") or "").strip()
            if not subject or not teacher:
                continue
            try:
                passed = max(0, int(s_item.get("passed", 0) or 0))
                total = max(0, int(s_item.get("total", 0) or 0))
            except (ValueError, TypeError):
                passed, total = 0, 0

            norm_subjects.append({
                "subject": subject,
                "teacher": teacher,
                "passed": passed,
                "total": total,
            })

        if norm_subjects:
            normalized_groups.append({
                "schedule_id": schedule_id,
                "group_name": group_name,
                "subjects": norm_subjects,
            })

    if not normalized_groups:
        return None, "В предоставленном JSON нет корректных пар/дисциплин."

    return {"groups": normalized_groups}, None


async def format_import_preview(
    parsed_payload: dict[str, Any],
    group_catalog: GroupCatalog,
    *,
    html: bool = True,
    max_groups_preview: int = 5,
    max_subjects_per_group: int = 4,
) -> tuple[str, int, int]:
    groups = parsed_payload.get("groups", [])
    total_groups = len(groups)
    total_subjects = sum(len(g.get("subjects", [])) for g in groups)

    header = "<b>Предпросмотр импорта пар из JSON</b>\n" if html else "Предпросмотр импорта пар из JSON\n"
    stats = (
        f"\nНайдено групп: <b>{total_groups}</b>\nНайдено пар/дисциплин: <b>{total_subjects}</b>\n\n"
        if html
        else f"\nНайдено групп: {total_groups}\nНайдено пар/дисциплин: {total_subjects}\n\n"
    )

    lines = [header, stats]
    lines.append("<b>Группы и пары для добавления:</b>\n" if html else "Группы и пары для добавления:\n")

    for group_item in groups[:max_groups_preview]:
        raw_schedule_id = group_item.get("schedule_id")
        raw_group_name = group_item.get("group_name")
        group_display = raw_group_name
        if not group_display and raw_schedule_id is not None:
            catalog_g = await group_catalog.get_by_schedule_id(int(raw_schedule_id))
            group_display = catalog_g.group_name if catalog_g else f"ID {raw_schedule_id}"
        if not group_display:
            group_display = "Неизвестная группа"

        subjects = group_item.get("subjects", [])
        sub_count = len(subjects)

        group_title = f"• <b>{group_display}</b> ({sub_count} пар):" if html else f"• {group_display} ({sub_count} пар):"
        lines.append(group_title)

        for s_item in subjects[:max_subjects_per_group]:
            subj = s_item.get("subject")
            teach = s_item.get("teacher")
            passed = s_item.get("passed", 0)
            total = s_item.get("total", 0)
            sub_line = f"  — {subj} ({teach}) [{passed}/{total}]"
            lines.append(sub_line)

        if len(subjects) > max_subjects_per_group:
            more_subs = len(subjects) - max_subjects_per_group
            lines.append(f"  <i>...и еще {more_subs} пар</i>" if html else f"  ...и еще {more_subs} пар")

    if total_groups > max_groups_preview:
        more_groups = total_groups - max_groups_preview
        lines.append(f"\n<i>...и еще {more_groups} групп(ы).</i>" if html else f"\n...и еще {more_groups} групп(ы).")

    lines.append("\n<b>Подтвердить импорт пар в базу данных?</b>" if html else "\nПодтвердить импорт пар в базу данных?")

    return "\n".join(lines), total_groups, total_subjects


async def apply_imported_lessons_config(
    parsed_payload: dict[str, Any],
    current_payload: dict[str, Any],
    group_catalog: GroupCatalog,
) -> tuple[dict[str, Any], int, int]:
    imported_groups_set = set()
    imported_subjects_count = 0

    for group_item in parsed_payload.get("groups", []):
        schedule_id = group_item.get("schedule_id")
        group_name = group_item.get("group_name") or ""

        resolved_schedule_id = None
        resolved_group_name = None

        if schedule_id is not None:
            try:
                resolved_schedule_id = int(schedule_id)
            except (ValueError, TypeError):
                resolved_schedule_id = None

        if resolved_schedule_id is not None:
            cat_g = await group_catalog.get_by_schedule_id(resolved_schedule_id)
            resolved_group_name = cat_g.group_name if cat_g else (group_name or str(resolved_schedule_id))
        elif group_name:
            cat_g = await group_catalog.find_group(group_name)
            if cat_g:
                resolved_schedule_id = cat_g.schedule_id
                resolved_group_name = cat_g.group_name
            else:
                resolved_group_name = group_name

        if resolved_schedule_id is None and not resolved_group_name:
            continue

        if resolved_schedule_id is None:
            continue

        imported_groups_set.add(resolved_schedule_id)
        for s_item in group_item.get("subjects", []):
            subj = s_item.get("subject")
            teach = s_item.get("teacher")
            passed = s_item.get("passed", 0)
            total = s_item.get("total", 0)
            if subj and teach:
                upsert_lesson_subject(
                    current_payload,
                    schedule_id=resolved_schedule_id,
                    group_name=resolved_group_name,
                    subject=subj,
                    teacher=teach,
                    passed=passed,
                    total=total,
                )
                imported_subjects_count += 1

    return current_payload, len(imported_groups_set), imported_subjects_count

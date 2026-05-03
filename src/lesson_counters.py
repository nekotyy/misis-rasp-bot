from __future__ import annotations

import logging
import json
import re
import unicodedata
from datetime import datetime
from html import escape
from pathlib import Path

from src.db import Database
from src.group_catalog import GroupCatalog
from src.models import ScheduleSnapshot


logger = logging.getLogger(__name__)


def normalize_lesson_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w\s-]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def teacher_matches(config_teacher_norm: str, lesson_teacher: str) -> bool:
    lesson_teacher_norm = normalize_lesson_text(lesson_teacher)
    if not config_teacher_norm or not lesson_teacher_norm:
        return False
    if config_teacher_norm == lesson_teacher_norm:
        return True
    config_parts = config_teacher_norm.split()
    lesson_parts = lesson_teacher_norm.split()
    if config_parts and lesson_parts and config_parts[0] == lesson_parts[0]:
        return len(config_parts) == 1 or config_teacher_norm in lesson_teacher_norm
    return False


class LessonCounterService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def load_config_file(self, path: Path, group_catalog: GroupCatalog) -> list[dict]:
        if not path.exists():
            logger.warning("Lesson counters config file does not exist: %s", path)
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Lesson counters config load failed for %s: %s", path, exc)
            return []

        counters: list[dict] = []
        if isinstance(payload, list):
            raw_groups = payload
        elif isinstance(payload, dict):
            raw_groups = payload.get("groups", [])
            if not raw_groups and "counters" in payload:
                raw_groups = [payload]
        else:
            return []

        for group_item in raw_groups:
            if not isinstance(group_item, dict):
                continue
            schedule_id = await self._resolve_schedule_id(group_item, group_catalog)
            if schedule_id is None:
                logger.warning("Lesson counters group skipped because schedule_id/group_name is missing: %s", group_item)
                continue

            if "subject" in group_item and "teacher" in group_item:
                counter = self._parse_counter_item(group_item, schedule_id)
                if counter is not None:
                    counters.append(counter)
                continue

            raw_counters = group_item.get("subjects") or group_item.get("counters") or []
            if not isinstance(raw_counters, list):
                continue
            for counter_item in raw_counters:
                counter = self._parse_counter_item(counter_item, schedule_id)
                if counter is not None:
                    counters.append(counter)
        return counters

    async def sync_config(self, counters_config: list[dict]) -> None:
        active_keys: set[tuple[int | None, str, str]] = set()
        for item in counters_config:
            subject = str(item["subject"]).strip()
            teacher = str(item["teacher"]).strip()
            subject_norm = normalize_lesson_text(subject)
            teacher_norm = normalize_lesson_text(teacher)
            schedule_id = item.get("schedule_id")
            active_keys.add((schedule_id, subject_norm, teacher_norm))
            await self.db.upsert_lesson_counter_seed(
                schedule_id=schedule_id,
                subject=subject,
                teacher=teacher,
                subject_norm=subject_norm,
                teacher_norm=teacher_norm,
                passed_count=int(item.get("passed", 0)),
                total_count=int(item.get("total", 0)),
            )
        deleted = await self.db.delete_lesson_counters_not_in(active_keys)
        if deleted:
            logger.info("Lesson counters: removed %s counter(s) missing from JSON config.", deleted)

    async def configured_schedule_ids(self) -> list[int]:
        counters = await self.db.list_lesson_counters()
        return sorted({int(counter["schedule_id"]) for counter in counters if counter["schedule_id"] is not None})

    async def _resolve_schedule_id(self, item: dict, group_catalog: GroupCatalog) -> int | None:
        raw_schedule_id = item.get("schedule_id")
        if raw_schedule_id is not None:
            try:
                return int(raw_schedule_id)
            except (TypeError, ValueError):
                return None
        group_name = str(item.get("group_name") or "").strip()
        if not group_name:
            return None
        group = await group_catalog.find_group(group_name)
        return group.schedule_id if group is not None else None

    def _parse_counter_item(self, item: object, schedule_id: int) -> dict | None:
        if not isinstance(item, dict):
            return None
        subject = str(item.get("subject") or "").strip()
        teacher = str(item.get("teacher") or "").strip()
        if not subject or not teacher:
            return None
        try:
            passed = int(item.get("passed", 0))
            total = int(item.get("total", 0))
        except (TypeError, ValueError):
            return None
        return {
            "schedule_id": schedule_id,
            "subject": subject,
            "teacher": teacher,
            "passed": max(0, passed),
            "total": max(0, total),
        }

    async def count_today_for_snapshot(self, schedule_id: int | None, snapshot: ScheduleSnapshot) -> int:
        if schedule_id is None:
            return 0

        counters = await self.db.list_lesson_counters(schedule_id)
        if not counters:
            return 0

        today_iso = datetime.now().date().isoformat()
        today = next((day for day in snapshot.days if day.date_iso == today_iso), None)
        if today is None or not today.lessons:
            return 0

        added = 0
        for lesson in today.lessons:
            subject_norm = normalize_lesson_text(lesson.subject)
            for counter in counters:
                if subject_norm != counter["subject_norm"]:
                    continue
                if not teacher_matches(counter["teacher_norm"], lesson.teacher):
                    continue
                if await self.db.record_lesson_counter_event(
                    counter_id=counter["id"],
                    schedule_id=schedule_id,
                    date_iso=today.date_iso,
                    lesson_number=lesson.number,
                    subject=lesson.subject,
                    teacher=lesson.teacher,
                    classroom=lesson.classroom,
                ):
                    added += 1
                break
        if added:
            logger.info("Lesson counters: added %s lesson(s) for schedule_id=%s.", added, schedule_id)
        return added

    async def format_counters_text(self, schedule_id: int | None = None, *, html: bool = False) -> str:
        counters = await self.db.list_lesson_counters(schedule_id)
        if not counters:
            return "Список дисциплин пока не настроен."

        blocks: list[str] = []
        for counter in counters:
            subject = str(counter["subject"])
            teacher = str(counter["teacher"])
            if html:
                subject = f"<b>{escape(subject)}</b>"
                teacher = escape(teacher)
            passed = int(counter["passed_count"] or 0)
            total = int(counter["total_count"] or 0)
            blocks.append(f"{subject}\n{teacher}\nПрошло - {passed}, всего - {total}")
        return "\n\n".join(blocks)

from __future__ import annotations

import contextlib
import json
import logging
import os
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


SUBJECT_NOISE_PREFIXES = (
    "консульт",
    "консультац",
)


SUBJECT_PARSE_PATTERNS = (
    re.compile(r"(?:^| )по (?P<subject>.+?)(?: у |$)"),
    re.compile(r"(?:^| )по(?P<subject>[a-zа-я0-9 _-]+?)(?: у |$)"),
)


def _add_subject_candidate(candidates: set[str], value: str) -> None:
    normalized = normalize_lesson_text(value)
    if not normalized:
        return
    normalized = re.sub(r"(^[-\s]+|[-\s]+$)", "", normalized)
    if normalized:
        candidates.add(normalized)



def extract_subject_candidates(value: str) -> set[str]:
    candidates: set[str] = set()
    normalized = normalize_lesson_text(value)
    if not normalized:
        return candidates

    _add_subject_candidate(candidates, normalized)
    simplified = re.sub(r"[-_]+", " ", normalized)
    _add_subject_candidate(candidates, simplified)

    for pattern in SUBJECT_PARSE_PATTERNS:
        for match in pattern.finditer(simplified):
            segment = match.group("subject").strip()
            if not segment:
                continue
            _add_subject_candidate(candidates, segment)
            words = segment.split()
            while words and any(words[0].startswith(prefix) for prefix in SUBJECT_NOISE_PREFIXES):
                words.pop(0)
            if words:
                cleaned_segment = " ".join(words)
                _add_subject_candidate(candidates, cleaned_segment)
                _add_subject_candidate(candidates, words[-1])

    return candidates



def subject_matches(config_subject_norm: str, lesson_subject: str) -> bool:
    if not config_subject_norm:
        return False
    candidates = extract_subject_candidates(lesson_subject)
    if config_subject_norm in candidates:
        return True
    config_words = config_subject_norm.split()
    if len(config_words) == 1:
        needle = config_words[0]
        return any(needle in candidate.split() for candidate in candidates)
    return False



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
    def __init__(self, db: Database, lesson_counters_path: Path | None = None) -> None:
        self.db = db
        self.lesson_counters_path = lesson_counters_path

    async def load_config_file(self, path: Path, group_catalog: GroupCatalog) -> list[dict]:
        self.lesson_counters_path = path
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
            for counter in counters:
                if not subject_matches(counter["subject_norm"], lesson.subject):
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

    async def format_counters_text(
        self,
        schedule_id: int | None = None,
        *,
        group_name: str | None = None,
        html: bool = False,
    ) -> str:
        json_counters: list[dict] = []
        if self.lesson_counters_path and self.lesson_counters_path.exists():
            try:
                with open(self.lesson_counters_path, encoding="utf-8") as f:
                    data = json.load(f)
                groups = data.get("groups", [])
                target_group = None
                for g in groups:
                    if group_name and str(g.get("group_name") or g.get("name") or "").strip().lower() == group_name.strip().lower():
                        target_group = g
                        break
                    if schedule_id and g.get("schedule_id") == schedule_id:
                        target_group = g
                        break
                if target_group:
                    json_counters = target_group.get("subjects", [])
            except Exception as exc:
                logger.warning("Failed to load lesson counters JSON: %s", exc)

        db_counters = await self.db.list_lesson_counters(schedule_id) if schedule_id else []

        if not json_counters and not db_counters:
            return "Список дисциплин пока не настроен."

        lines: list[str] = []
        if group_name:
            title = escape(group_name) if html else group_name
            lines.extend(
                [
                    f"Информация для - <b>{title}</b>" if html else f"Информация для - {title}",
                    "",
                    "В текущем семестре будут следующие пары:",
                    "",
                ]
            )

        blocks: list[str] = []
        has_unspecified = False

        if json_counters:
            for item in json_counters:
                subject = str(item.get("display_name") or item.get("subject") or "").strip()
                teacher = str(item.get("teacher") or "").strip()
                passed = item.get("passed", 0)
                total = item.get("total")

                try:
                    passed_int = max(0, int(passed or 0))
                except (ValueError, TypeError):
                    passed_int = 0

                if total is None or total == "" or str(total).strip() == "##?":
                    total_str = "##?"
                    has_unspecified = True
                else:
                    try:
                        total_int = int(total)
                        total_str = str(total_int) if total_int > 0 else "##?"
                        if total_int <= 0:
                            has_unspecified = True
                    except (ValueError, TypeError):
                        total_str = "##?"
                        has_unspecified = True

                if html:
                    subj_fmt = f"<b>{escape(subject)}</b>" if subject else ""
                    teach_fmt = escape(teacher) if teacher else ""
                else:
                    subj_fmt = subject
                    teach_fmt = teacher

                if teach_fmt:
                    blocks.append(f"{subj_fmt}\n{teach_fmt}\nПрошло - {passed_int}, всего - {total_str}")
                else:
                    blocks.append(f"{subj_fmt}\nПрошло - {passed_int}, всего - {total_str}")
        else:
            for counter in db_counters:
                subject = str(counter["subject"])
                teacher = str(counter["teacher"])
                if html:
                    subject = f"<b>{escape(subject)}</b>"
                    teacher = escape(teacher)
                passed = int(counter["passed_count"] or 0)
                total = int(counter["total_count"] or 0)
                total_str = str(total) if total > 0 else "##?"
                if total <= 0:
                    has_unspecified = True
                blocks.append(f"{subject}\n{teacher}\nПрошло - {passed}, всего - {total_str}")

        lines.append("\n\n".join(blocks))

        if has_unspecified:
            if html:
                lines.extend([
                    "",
                    "💡 <i>Замечена пара с не указанным итоговым количеством пар (##?)! Обратитесь к администратору (<a href=\"https://t.me/nekoty\">t.me/nekoty</a> или <a href=\"https://vk.ru/nekotyy\">vk.ru/nekotyy</a>), чтобы указать итоговое количество пар.</i>"
                ])
            else:
                lines.extend([
                    "",
                    "💡 Замечена пара с не указанным итоговым количеством пар (##?)! Обратитесь к администратору (t.me/nekoty или vk.ru/nekotyy), чтобы указать итоговое количество пар."
                ])

        return "\n".join(lines)

    def auto_increment_or_create_subject_in_json(
        self,
        group_name: str,
        schedule_id: int | None,
        subject: str,
        teacher: str,
        count: int = 1,
    ) -> bool:
        if not self.lesson_counters_path:
            return False
        self.lesson_counters_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {"groups": []}
        if self.lesson_counters_path.exists():
            try:
                with open(self.lesson_counters_path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                logger.warning("Failed to read JSON file before auto-increment: %s", exc)
                data = {"groups": []}

        groups = data.get("groups", [])
        target_group = None
        for g in groups:
            g_name = str(g.get("group_name") or g.get("name") or "").strip()
            if g_name.lower() == group_name.strip().lower():
                target_group = g
                break
            if schedule_id and g.get("schedule_id") == schedule_id:
                target_group = g
                break

        if target_group is None:
            target_group = {
                "group_name": group_name,
                "schedule_id": schedule_id,
                "subjects": [],
            }
            groups.append(target_group)
            data["groups"] = groups

        subjects = target_group.setdefault("subjects", [])
        found_subject = None
        subj_norm = normalize_lesson_text(subject)
        teach_norm = normalize_lesson_text(teacher)

        for item in subjects:
            item_subj = normalize_lesson_text(str(item.get("display_name") or item.get("subject") or ""))
            item_teach = normalize_lesson_text(str(item.get("teacher") or ""))
            if subject_matches(item_subj, subject) and (not item_teach or teacher_matches(item_teach, teacher)):
                found_subject = item
                break
            if item_subj == subj_norm and item_teach == teach_norm:
                found_subject = item
                break

        if found_subject is not None:
            current_passed = found_subject.get("passed", 0)
            try:
                current_passed = int(current_passed or 0)
            except (ValueError, TypeError):
                current_passed = 0
            found_subject["passed"] = current_passed + count
        else:
            subjects.append({
                "group_name": group_name,
                "subject": subject,
                "display_name": subject,
                "teacher": teacher,
                "passed": count,
                "total": None,
            })

        return _atomic_write_json(self.lesson_counters_path, data)

    def reset_group_counters(self, group_name: str | None = None) -> bool:
        if not self.lesson_counters_path or not self.lesson_counters_path.exists():
            return False
        try:
            with open(self.lesson_counters_path, encoding="utf-8") as f:
                data = json.load(f)
            groups = data.get("groups", [])
            for g in groups:
                g_name = str(g.get("group_name") or g.get("name") or "").strip()
                if group_name is None or g_name.lower() == group_name.strip().lower():
                    for s in g.get("subjects", []):
                        s["passed"] = 0
            return _atomic_write_json(self.lesson_counters_path, data)
        except Exception as exc:
            logger.warning("Failed to reset lesson counters: %s", exc)
            return False


def _atomic_write_json(path: Path, data: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_file.replace(path)
        return True
    except Exception as exc:
        logger.warning("Failed to save JSON atomically to %s: %s", path, exc)
        return False
    finally:
        if temp_file.exists():
            with contextlib.suppress(OSError):
                temp_file.unlink()

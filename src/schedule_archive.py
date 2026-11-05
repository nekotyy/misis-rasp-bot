"""Запись и чтение отдельных дней будущего архива миниаппа."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from src.models import DaySchedule


class ScheduleDayArchive:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def save_day(
        self, source_key: str, source_type: str, day: DaySchedule, *,
        observed_at: datetime, origin: str, completeness: Literal["unknown", "complete"],
        snapshot_id: int | None = None,
    ) -> bool:
        """Сохраняет более свежий день, не затрагивая опоры и другие даты."""
        if not source_key.strip() or not source_type.strip() or not origin.strip():
            raise ValueError("Источник и происхождение должны быть указаны")
        if observed_at.utcoffset() is None:
            raise ValueError("Время наблюдения должно содержать часовой пояс")
        if date.fromisoformat(day.date_iso).isoformat() != day.date_iso:
            raise ValueError("Дата должна быть в формате YYYY-MM-DD")
        if day.week_variant not in (None, 1, 2) or completeness not in ("unknown", "complete"):
            raise ValueError("Некорректный вариант недели или признак полноты")
        if any(item.number <= 0 for item in day.lessons):
            raise ValueError("Номер пары должен быть положительным")
        content = json.dumps(day.to_dict(), ensure_ascii=False, sort_keys=True)
        observed = observed_at.astimezone(UTC).isoformat(timespec="microseconds")
        recorded = datetime.now(UTC).isoformat(timespec="microseconds")
        async with aiosqlite.connect(self.path, timeout=30) as db:
            cursor = await db.execute(
                """INSERT INTO schedule_day_archive
                   (source_key, source_type, date_iso, week_variant, content_json, completeness,
                    origin, observed_at, recorded_at, snapshot_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_key, date_iso, week_variant) DO UPDATE SET
                       source_type=excluded.source_type, content_json=excluded.content_json,
                       completeness=excluded.completeness, origin=excluded.origin,
                       observed_at=excluded.observed_at, recorded_at=excluded.recorded_at,
                       snapshot_id=excluded.snapshot_id
                   WHERE excluded.observed_at > schedule_day_archive.observed_at""",
                (source_key, source_type, day.date_iso, day.week_variant or 0, content, completeness,
                 origin, observed, recorded, snapshot_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def get_day(self, source_key: str, day: date, week_variant: int | None = None) -> dict | None:
        if week_variant not in (None, 1, 2):
            raise ValueError("Некорректный вариант недели")
        async with aiosqlite.connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM schedule_day_archive WHERE source_key = ? AND date_iso = ? AND week_variant = ?",
                (source_key, day.isoformat(), week_variant or 0),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        return result

    async def get_days(self, source_key: str, start: date, end: date) -> list[dict]:
        """Читает сохранённые дни источника за включительный интервал."""
        if not source_key.strip() or start > end:
            raise ValueError("Не указан источник или неверный интервал дат")
        async with aiosqlite.connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM schedule_day_archive
                   WHERE source_key = ? AND date_iso BETWEEN ? AND ?
                   ORDER BY date_iso DESC, week_variant ASC""",
                (source_key, start.isoformat(), end.isoformat()),
            )
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["content"] = json.loads(item.pop("content_json"))
            result.append(item)
        return result

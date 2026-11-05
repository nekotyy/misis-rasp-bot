"""Запись и чтение отдельных дней будущего архива миниаппа."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import aiosqlite

from src.models import DaySchedule, ScheduleSnapshot


async def archive_accepted_snapshot(
    db: aiosqlite.Connection, snapshot: ScheduleSnapshot, *, source_key: str, source_type: str,
    origin: str, timezone: str, snapshot_id: int, dates: set[str] | None = None,
) -> None:
    """Обновляет проекцию в транзакции сохранения уже принятого снимка."""
    if not source_key or not source_type:
        return
    zone = ZoneInfo(timezone)
    observed = snapshot.fetched_at
    if observed.utcoffset() is None:
        observed = observed.replace(tzinfo=zone)
    observed_iso = observed.astimezone(UTC).isoformat(timespec="microseconds")
    observed_date = observed.astimezone(zone).date()
    recorded = datetime.now(UTC).isoformat(timespec="microseconds")
    keys = [(day.date_iso, day.week_variant or 0) for day in snapshot.days]
    for day in snapshot.days:
        if dates is not None and day.date_iso not in dates:
            continue
        try:
            day_date = date.fromisoformat(day.date_iso)
            if day_date.isoformat() != day.date_iso or day.week_variant not in (None, 1, 2):
                continue
            if keys.count((day.date_iso, day.week_variant or 0)) != 1:
                continue
            if any(item.number <= 0 for item in day.lessons):
                continue
        except (TypeError, ValueError):
            continue
        content = json.dumps(day.to_dict(), ensure_ascii=False, sort_keys=True)
        variant = day.week_variant or 0
        await db.execute(
            """INSERT INTO schedule_day_archive
               (source_key, source_type, date_iso, week_variant, content_json, completeness,
                origin, observed_at, recorded_at, snapshot_id)
               VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?)
               ON CONFLICT(source_key, date_iso, week_variant) DO UPDATE SET
                   source_type=excluded.source_type, content_json=excluded.content_json,
                   completeness=excluded.completeness, origin=excluded.origin,
                   observed_at=excluded.observed_at, recorded_at=excluded.recorded_at,
                   snapshot_id=excluded.snapshot_id
               WHERE excluded.observed_at > schedule_day_archive.observed_at""",
            (source_key, source_type, day.date_iso, variant, content, origin, observed_iso, recorded, snapshot_id),
        )
        # Пустая запись неизвестной полноты не является надёжной опорой.
        if not day.lessons or day_date < observed_date:
            continue
        kinds = ("first_seen", "day_start") if day_date == observed_date else ("first_seen",)
        for kind in kinds:
            await db.execute(
                """INSERT INTO schedule_day_references
                   (source_key, date_iso, week_variant, reference_kind, content_json, completeness,
                    origin, observed_at, recorded_at, snapshot_id)
                   VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?)
                   ON CONFLICT(source_key, date_iso, week_variant, reference_kind) DO UPDATE SET
                       content_json=excluded.content_json, completeness=excluded.completeness,
                       origin=excluded.origin, observed_at=excluded.observed_at,
                       recorded_at=excluded.recorded_at, snapshot_id=excluded.snapshot_id
                   WHERE excluded.observed_at < schedule_day_references.observed_at""",
                (source_key, day.date_iso, variant, kind, content, origin, observed_iso, recorded, snapshot_id),
            )


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

    async def get_reference(
        self, source_key: str, day: date, *, today: date, week_variant: int | None = None,
    ) -> dict | None:
        """Возвращает дневную опору для сегодня или первую известную для будущей даты."""
        if not source_key.strip() or week_variant not in (None, 1, 2):
            raise ValueError("Не указан источник или неверный вариант недели")
        if day < today:
            return None
        kind = "day_start" if day == today else "first_seen"
        async with aiosqlite.connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM schedule_day_references
                   WHERE source_key = ? AND date_iso = ? AND week_variant = ? AND reference_kind = ?""",
                (source_key, day.isoformat(), week_variant or 0, kind),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        return result

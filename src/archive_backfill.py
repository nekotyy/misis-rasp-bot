"""Пакетное восстановление дней из сохранённых снимков расписания."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

logger = logging.getLogger(__name__)


class ArchiveBackfill:
    """Каждый пакет и его позиция фиксируются в одной транзакции SQLite."""

    def __init__(self, path: Path, timezone: str) -> None:
        self.path = path
        self.timezone = ZoneInfo(timezone)

    async def run(self, limit: int = 100) -> dict[str, int | bool]:
        """Восстанавливает доступные снимки, уступая управление между пакетами."""
        while True:
            result = await self.run_batch(limit)
            if result["done"]:
                logger.info("Восстановление архива: обработано снимков %s, пропущено записей %s.", result["processed"], result["skipped"])
                return result
            await asyncio.sleep(0)

    async def run_batch(self, limit: int = 100) -> dict[str, int | bool]:
        if limit <= 0:
            raise ValueError("Размер пакета должен быть положительным")
        async with aiosqlite.connect(self.path, timeout=30) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("INSERT OR IGNORE INTO schedule_archive_backfill (name) VALUES ('snapshots_v1')")
                cursor = await db.execute("SELECT last_id, processed, skipped FROM schedule_archive_backfill WHERE name = 'snapshots_v1'")
                last_id, processed, skipped = await cursor.fetchone()
                cursor = await db.execute(
                    """SELECT id, snapshot_type, source_type, source_key, schedule_id, content_json, fetched_at
                       FROM schedule_snapshots WHERE id > ? ORDER BY id LIMIT ?""", (last_id, limit),
                )
                rows = await cursor.fetchall()
                for snapshot_id, kind, source_type, source_key, schedule_id, raw, fetched_at in rows:
                    last_id = snapshot_id
                    processed += 1
                    if kind not in ("current", "daily_baseline"):
                        skipped += 1
                        continue
                    if not source_key and schedule_id is not None:
                        source_key = f"group:{schedule_id}"
                        source_type = source_type or "group"
                    if not source_key or not source_type:
                        skipped += 1
                        continue
                    try:
                        observed = datetime.fromisoformat(fetched_at)
                        if observed.utcoffset() is None:
                            observed = observed.replace(tzinfo=self.timezone)
                        observed_iso = observed.astimezone(UTC).isoformat(timespec="microseconds")
                        content = json.loads(raw)
                        days = content["days"]
                        if not isinstance(days, list):
                            raise ValueError("Некорректный список дней")
                    except (TypeError, ValueError, KeyError):
                        skipped += 1
                        continue
                    keys = Counter(
                        (item.get("date_iso"), item.get("week_variant") or 0)
                        for item in days if isinstance(item, dict)
                        and isinstance(item.get("date_iso"), str)
                        and (item.get("week_variant") is None or type(item.get("week_variant")) is int)
                    )
                    for day in days:
                        try:
                            if not isinstance(day, dict):
                                raise ValueError("Некорректный день")
                            day_iso = day["date_iso"]
                            if not isinstance(day_iso, str):
                                raise ValueError("Некорректная дата")
                            if date.fromisoformat(day_iso).isoformat() != day_iso:
                                raise ValueError("Некорректная дата")
                            raw_variant = day.get("week_variant")
                            if raw_variant is not None and type(raw_variant) is not int:
                                raise ValueError("Некорректный вариант недели")
                            variant = raw_variant or 0
                            if type(variant) is not int or variant not in (0, 1, 2) or keys[(day_iso, variant)] != 1:
                                raise ValueError("Неоднозначный вариант недели")
                            lessons = day["lessons"]
                            if not isinstance(lessons, list) or any(
                                not isinstance(item, dict) or not isinstance(item.get("number"), int)
                                or item["number"] <= 0 or any(not isinstance(item.get(field), str)
                                                              for field in ("subject", "teacher", "classroom"))
                                for item in lessons
                            ):
                                raise ValueError("Некорректные пары")
                            if schedule_id is not None and schedule_id < 0 and day_iso < observed.astimezone(self.timezone).date().isoformat():
                                raise ValueError("Шаблон недели не подтверждает прошедший день")
                        except (KeyError, TypeError, ValueError):
                            skipped += 1
                            continue
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
                               WHERE excluded.observed_at > schedule_day_archive.observed_at
                                  OR (excluded.observed_at = schedule_day_archive.observed_at
                                      AND excluded.origin = 'legacy_current'
                                      AND schedule_day_archive.origin = 'legacy_baseline')""",
                            (source_key, source_type, day_iso, variant, json.dumps(day, ensure_ascii=False, sort_keys=True),
                             "legacy_current" if kind == "current" else "legacy_baseline", observed_iso,
                             datetime.now(UTC).isoformat(timespec="microseconds"), snapshot_id),
                        )
                await db.execute(
                    "UPDATE schedule_archive_backfill SET last_id = ?, processed = ?, skipped = ? WHERE name = 'snapshots_v1'",
                    (last_id, processed, skipped),
                )
                await db.commit()
                return {"last_id": last_id, "processed": processed, "skipped": skipped, "done": len(rows) < limit}
            except BaseException:
                await db.rollback()
                raise

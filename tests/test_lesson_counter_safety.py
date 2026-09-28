"""Счётчики пар не должны теряться: битый файл, пустой ответ сайта, прошедший день."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx

from src.db import Database
from src.lesson_counters import LessonConfigReadError, LessonCounterService, sync_lesson_counters_for_date
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from web_configurator.lesson_editor import load_lesson_config

GROUP = {"groups": [{"schedule_id": 600, "group_name": "ИСП-25-1", "subjects": []}]}


def _snapshot(days: dict[str, list[str]]) -> ScheduleSnapshot:
    return ScheduleSnapshot(
        group_name="ИСП-25-1",
        fetched_at=datetime.now(),
        days=[
            DaySchedule(
                date_label=date_iso,
                date_iso=date_iso,
                lessons=[Lesson(number=index + 1, subject=subject, teacher="Иванов И.И.", classroom="1") for index, subject in enumerate(subjects)],
            )
            for date_iso, subjects in days.items()
        ],
    )


class LessonCounterSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.json_path = Path(self._tmp.name) / "lesson_counters.json"
        self.db = Database(Path(self._tmp.name) / "counters.db")
        await self.db.initialize()
        self.service = LessonCounterService(self.db, lesson_counters_path=self.json_path)
        self.parser = MagicMock()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _cache(self, snapshot: ScheduleSnapshot) -> None:
        await self.db.save_snapshot("current", "h", snapshot, schedule_id=600, group_name="ИСП-25-1")

    def _passed(self, subject: str) -> int | None:
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        for item in data["groups"][0]["subjects"]:
            if item["subject"] == subject:
                return item["passed"]
        return None

    async def test_corrupt_file_is_never_overwritten(self) -> None:
        self.json_path.write_text('{"groups": [ broken', encoding="utf-8")
        ok = self.service.auto_increment_or_create_subject_in_json("ИСП-25-1", 600, "Физика", "Иванов", 1)
        self.assertFalse(ok)
        self.assertEqual(self.json_path.read_text(encoding="utf-8"), '{"groups": [ broken')

    async def test_admin_loader_refuses_corrupt_file(self) -> None:
        self.json_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(LessonConfigReadError):
            load_lesson_config(self.json_path)

    async def test_corrupt_file_is_not_marked_processed(self) -> None:
        self.json_path.write_text("not json", encoding="utf-8")
        self.parser.parse = AsyncMock(return_value=(_snapshot({"2026-09-01": ["Физика"]}), "h"))
        self.service.configured_groups = MagicMock(return_value=[{"schedule_id": 600, "group_name": "ИСП-25-1"}])

        result = await sync_lesson_counters_for_date(self.db, self.parser, self.service, "2026-09-01")

        self.assertEqual([name for name, _ in result.failed], ["ИСП-25-1"])
        self.assertFalse(await self.db.is_daily_counter_processed("2026-09-01", "ИСП-25-1"))

    async def test_empty_site_glitch_uses_cached_snapshot(self) -> None:
        self.json_path.write_text(json.dumps(GROUP), encoding="utf-8")
        await self._cache(_snapshot({"2026-09-01": ["Физика", "Физика"]}))
        self.parser.parse = AsyncMock(return_value=(_snapshot({"2026-09-01": [], "2026-09-02": []}), "h"))

        result = await sync_lesson_counters_for_date(self.db, self.parser, self.service, "2026-09-01")

        self.assertEqual(result.processed, ["ИСП-25-1"])
        self.assertEqual(self._passed("Физика"), 2)

    async def test_past_day_missing_on_site_uses_cache(self) -> None:
        self.json_path.write_text(json.dumps(GROUP), encoding="utf-8")
        await self._cache(_snapshot({"2026-09-01": ["Химия"], "2026-09-02": ["Химия"]}))
        # После полуночи сайт уже показывает только с сегодняшнего дня.
        self.parser.parse = AsyncMock(return_value=(_snapshot({"2026-09-02": ["Химия"]}), "h"))

        await sync_lesson_counters_for_date(self.db, self.parser, self.service, "2026-09-01")

        self.assertEqual(self._passed("Химия"), 1)

    async def test_unknown_day_is_failed_not_zeroed(self) -> None:
        self.json_path.write_text(json.dumps(GROUP), encoding="utf-8")
        self.parser.parse = AsyncMock(return_value=(_snapshot({"2026-09-02": ["Химия"]}), "h"))

        result = await sync_lesson_counters_for_date(self.db, self.parser, self.service, "2026-09-01")

        self.assertEqual([name for name, _ in result.failed], ["ИСП-25-1"])
        self.assertFalse(await self.db.is_daily_counter_processed("2026-09-01", "ИСП-25-1"))

    async def test_day_inside_published_range_without_lessons_is_empty(self) -> None:
        self.json_path.write_text(json.dumps(GROUP), encoding="utf-8")
        # Воскресенье между субботой и понедельником — пар просто нет.
        self.parser.parse = AsyncMock(return_value=(_snapshot({"2026-09-05": ["Химия"], "2026-09-07": ["Химия"]}), "h"))

        result = await sync_lesson_counters_for_date(self.db, self.parser, self.service, "2026-09-06")

        self.assertEqual(result.processed, ["ИСП-25-1"])
        self.assertIsNone(self._passed("Химия"))

    async def test_site_down_without_cache_is_failed(self) -> None:
        self.json_path.write_text(json.dumps(GROUP), encoding="utf-8")
        self.parser.parse = AsyncMock(side_effect=httpx.ConnectError("down"))

        result = await sync_lesson_counters_for_date(self.db, self.parser, self.service, "2026-09-01")

        self.assertEqual(len(result.failed), 1)


if __name__ == "__main__":
    unittest.main()

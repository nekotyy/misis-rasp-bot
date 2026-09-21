from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.db import Database
from src.group_catalog import GroupCatalog
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.schedule_search import ScheduleSearchCatalog
from src.subscription_utils import make_teacher_subscription


def _mock_group_catalog() -> MagicMock:
    group_catalog = MagicMock(spec=GroupCatalog)
    group_catalog.ensure_loaded = AsyncMock()
    group_catalog.find_group = AsyncMock(return_value=None)
    return group_catalog


class TeacherDiscoveryFromGroupsTests(unittest.IsolatedAsyncioTestCase):
    """Если препода нет в /prep (сайт лежит, а свой кэш ещё пуст) — его можно найти

    по ФИО, которые уже встречаются в известных группах в БД (с сайта или из OCR),
    точно так же, как уже строится его личное расписание."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()

        snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="14 сентября", date_iso="2026-09-14", lessons=[
                Lesson(number=1, subject="Математика", teacher="Коврижных О.А.", classroom="301"),
            ])],
        )
        await self.db.save_snapshot(
            "current", "hash_a", snapshot, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _catalog_with_empty_prep(self) -> ScheduleSearchCatalog:
        catalog = ScheduleSearchCatalog(
            schedule_url="http://test-schedule.local",
            group_catalog=_mock_group_catalog(),
            db=self.db,
        )
        # /prep недоступен (сайт лежит), и в БД-кэше пока пусто — свежий кэш,
        # ни разу не заполнялся. Патчим сетевой поход, а не трогаем внутренние
        # флаги напрямую, чтобы не гонять реальный DNS-résolve в каждом тесте.
        catalog._fetch_pairs = AsyncMock(side_effect=RuntimeError("сайт недоступен"))
        return catalog

    async def test_finds_teacher_by_surname_from_group_lessons(self) -> None:
        catalog = self._catalog_with_empty_prep()

        target = await catalog.find("Коврижных")

        self.assertIsNotNone(target)
        self.assertEqual(target.kind, "teacher")
        self.assertEqual(target.title, "Коврижных О.А.")
        self.assertEqual(target.url, "")

    async def test_ambiguous_surname_across_groups_returns_none(self) -> None:
        other_snapshot = ScheduleSnapshot(
            group_name="КИБ-24-1",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="14 сентября", date_iso="2026-09-14", lessons=[
                Lesson(number=2, subject="Физика", teacher="Коврижных А.Б.", classroom="202"),
            ])],
        )
        await self.db.save_snapshot(
            "current", "hash_b", other_snapshot, schedule_id=202, group_name="КИБ-24-1",
            source_type="group", source_key="group:202", source_title="КИБ-24-1", source_url="rasp:202",
        )
        catalog = self._catalog_with_empty_prep()

        target = await catalog.find("Коврижных")

        self.assertIsNone(target, "Две разные Коврижных — неоднозначно, нельзя угадывать")

    async def test_no_match_returns_none(self) -> None:
        catalog = self._catalog_with_empty_prep()

        target = await catalog.find("Совершенно Другая Фамилия")

        self.assertIsNone(target)

    async def test_minor_ocr_formatting_variants_are_not_treated_as_different_people(self) -> None:
        messy_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-4",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="14 сентября", date_iso="2026-09-14", lessons=[
                Lesson(number=1, subject="Математика", teacher="Коврижных  О. А.", classroom="301"),
            ])],
        )
        await self.db.save_snapshot(
            "current", "hash_c", messy_snapshot, schedule_id=104, group_name="ИСП-25-4",
            source_type="group", source_key="group:104", source_title="ИСП-25-4", source_url="rasp:104",
        )
        catalog = self._catalog_with_empty_prep()

        target = await catalog.find("Коврижных О.А.")

        self.assertIsNotNone(target, "Мелкие OCR-отличия в написании — тот же человек, а не неоднозначность")


class MakeTeacherSubscriptionWithoutUrlTests(unittest.TestCase):
    """Регресс: у препода, найденного не на сайте (без URL), ключ подписки не должен

    схлопываться в один и тот же 'teacher:' для всех таких случаев."""

    def test_teacher_pending_key_is_unique_per_name(self) -> None:
        from src.schedule_search import SearchTarget

        target_a = SearchTarget(kind="teacher", title="Коврижных О.А.", url="")
        target_b = SearchTarget(kind="teacher", title="Иванов И.И.", url="")

        sub_a = make_teacher_subscription(target_a)
        sub_b = make_teacher_subscription(target_b)

        self.assertNotEqual(sub_a["subscription_key"], sub_b["subscription_key"])
        self.assertTrue(str(sub_a["subscription_key"]).startswith("teacher-pending:"))

    def test_site_sourced_teacher_still_keys_by_numeric_id(self) -> None:
        from src.schedule_search import SearchTarget

        target = SearchTarget(kind="teacher", title="Коврижных О.А.", url="http://test/raspprep/42")

        sub = make_teacher_subscription(target)

        self.assertEqual(sub["subscription_key"], "teacher:42")


if __name__ == "__main__":
    unittest.main()

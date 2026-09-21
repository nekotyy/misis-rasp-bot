from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.db import Database
from src.group_catalog import GroupCatalog
from src.schedule_search import ScheduleSearchCatalog


def _mock_group_catalog() -> MagicMock:
    group_catalog = MagicMock(spec=GroupCatalog)
    group_catalog.ensure_loaded = AsyncMock()
    group_catalog.find_group = AsyncMock(return_value=None)
    return group_catalog


class ScheduleSearchDbPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """Справочник преподавателей/аудиторий должен переживать недоступность сайта через БД,
    так же как это уже устроено для GroupCatalog."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_successful_prep_fetch_writes_to_db(self) -> None:
        catalog = ScheduleSearchCatalog(
            schedule_url="http://test-schedule.local",
            group_catalog=_mock_group_catalog(),
            db=self.db,
        )
        pairs = [("Иванов И.И.", "http://test-schedule.local/raspprep/1")]
        with patch.object(catalog, "_fetch_pairs", AsyncMock(return_value=pairs)):
            match = await catalog.find("Иванов И.И.")

        self.assertIsNotNone(match)
        self.assertEqual(match.kind, "teacher")

        rows = await self.db.get_search_targets("teacher")
        self.assertEqual(rows, [{"title": "Иванов И.И.", "url": "http://test-schedule.local/raspprep/1"}])

    async def test_failed_prep_fetch_falls_back_to_db(self) -> None:
        # Тёплый каталог успешно грузится с сайта и сохраняет снимок в БД.
        warm_catalog = ScheduleSearchCatalog(
            schedule_url="http://test-schedule.local",
            group_catalog=_mock_group_catalog(),
            db=self.db,
        )
        pairs = [("Иванов И.И.", "http://test-schedule.local/raspprep/1")]
        with patch.object(warm_catalog, "_fetch_pairs", AsyncMock(return_value=pairs)):
            await warm_catalog.find("Иванов И.И.")

        # Новый процесс (например, после перезапуска бота) не может достучаться до сайта.
        cold_catalog = ScheduleSearchCatalog(
            schedule_url="http://test-schedule.local",
            group_catalog=_mock_group_catalog(),
            db=self.db,
        )
        with patch.object(cold_catalog, "_fetch_pairs", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            match = await cold_catalog.find("Иванов И.И.")

        self.assertIsNotNone(match, "Преподаватель из БД должен находиться даже при недоступном сайте")
        self.assertEqual(match.url, "http://test-schedule.local/raspprep/1")

    async def test_failed_prep_fetch_without_db_data_returns_none(self) -> None:
        catalog = ScheduleSearchCatalog(
            schedule_url="http://test-schedule.local",
            group_catalog=_mock_group_catalog(),
            db=self.db,
        )
        with patch.object(catalog, "_fetch_pairs", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            match = await catalog.find("Иванов И.И.")

        self.assertIsNone(match)

    async def test_failed_audience_fetch_falls_back_to_db(self) -> None:
        warm_catalog = ScheduleSearchCatalog(
            schedule_url="http://test-schedule.local",
            group_catalog=_mock_group_catalog(),
            db=self.db,
        )
        aud_pairs = [("312", "http://test-schedule.local/raspAud/5")]
        with patch.object(warm_catalog, "_fetch_pairs", AsyncMock(side_effect=[[], aud_pairs])):
            await warm_catalog.find("312")

        cold_catalog = ScheduleSearchCatalog(
            schedule_url="http://test-schedule.local",
            group_catalog=_mock_group_catalog(),
            db=self.db,
        )
        with patch.object(cold_catalog, "_fetch_pairs", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            match = await cold_catalog.find("312")

        self.assertIsNotNone(match, "Аудитория из БД должна находиться даже при недоступном сайте")
        self.assertEqual(match.kind, "audience")
        self.assertEqual(match.url, "http://test-schedule.local/raspAud/5")

    async def test_no_db_disables_persistence(self) -> None:
        catalog = ScheduleSearchCatalog(
            schedule_url="http://test-schedule.local",
            group_catalog=_mock_group_catalog(),
        )
        pairs = [("Иванов И.И.", "http://test-schedule.local/raspprep/1")]
        with patch.object(catalog, "_fetch_pairs", AsyncMock(return_value=pairs)):
            match = await catalog.find("Иванов И.И.")

        self.assertIsNotNone(match)


if __name__ == "__main__":
    unittest.main()

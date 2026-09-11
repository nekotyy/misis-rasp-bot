import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.db import Database
from src.group_catalog import GroupCatalog, GroupInfo
from src.parser import ScheduleParser
from src.schedule_search import ScheduleSearchCatalog, SearchTarget

SAMPLE_HTML = """
<html>
<body>
<div id="titleF">ИСП-25-1</div>
<div class="titleDate">Понедельник 01 сентября 2026</div>
<div class="rasp">
<table>
    <tr><th>Пара</th><th>Предмет</th><th>Преподаватель</th><th>Аудитория</th></tr>
    <tr>
        <td>1</td>
        <td>Математика</td>
        <td>Иванов И.И.</td>
        <td>каб. 301</td>
    </tr>
</table>
</div>
</body>
</html>
"""


class ParserAndCatalogTests(unittest.IsolatedAsyncioTestCase):
    def test_schedule_parser_extracts_lessons(self):
        parser = ScheduleParser(schedule_url="http://test-schedule.local")
        snapshot = parser.parse_html(SAMPLE_HTML)
        self.assertEqual(snapshot.group_name, "ИСП-25-1")
        self.assertEqual(len(snapshot.days), 1)
        self.assertEqual(snapshot.days[0].lessons[0].subject, "Математика")

    async def test_group_catalog_loading_and_matching(self):
        catalog = GroupCatalog(schedule_url="http://test-schedule.local")
        catalog._loaded = True
        info = GroupInfo(
            department_id=1,
            department_code="IT",
            department_name="ИТ",
            group_name="ИСП-25-1",
            schedule_id=600,
            url="http://test/600",
        )
        catalog._groups_by_name = {"ИСП-25-1": info}
        catalog._groups_by_compact_name = {"исп251": info}

        group = await catalog.find_group("ИСП-25-1")
        self.assertIsNotNone(group)
        self.assertEqual(group.schedule_id, 600)

        group_compact = await catalog.find_group("исп-25-1")
        self.assertIsNotNone(group_compact)

    async def test_schedule_search_catalog(self):
        group_catalog = MagicMock(spec=GroupCatalog)
        group_catalog.ensure_loaded = AsyncMock()
        group_catalog.find_group = AsyncMock(return_value=None)

        search_catalog = ScheduleSearchCatalog(
            schedule_url="http://test-schedule.local",
            group_catalog=group_catalog,
        )
        target = SearchTarget(
            kind="teacher",
            title="Иванов И.И.",
            url="http://test-schedule.local/prep/123",
        )
        search_catalog._loaded = True
        search_catalog._preps_loaded = True
        search_catalog._auds_loaded = True
        search_catalog._preps = {"иванов и.и.": target, "иванов": target}
        search_catalog._prep_items = [("иванов и.и.", target), ("иванов", target)]
        search_catalog._auds = {}
        search_catalog._aud_items = []

        match = await search_catalog.find("Иванов")
        self.assertIsNotNone(match)
        self.assertEqual(match.kind, "teacher")
        self.assertEqual(match.title, "Иванов И.И.")

    def test_date_label_to_iso_year_rollover(self):
        from datetime import datetime
        parser = ScheduleParser(schedule_url="http://test-schedule.local")

        # In December 2025, viewing January 12 -> 2026-01-12
        dec_ref = datetime(2025, 12, 28, 10, 0, 0)
        self.assertEqual(parser._date_label_to_iso("12 января", now=dec_ref), "2026-01-12")

        # In January 2026, viewing December 28 -> 2025-12-28
        jan_ref = datetime(2026, 1, 3, 10, 0, 0)
        self.assertEqual(parser._date_label_to_iso("28 декабря", now=jan_ref), "2025-12-28")

        # Standard same-year conversion
        self.assertEqual(parser._date_label_to_iso("15 марта", now=jan_ref), "2026-03-15")

        # Explicit year specified in label
        self.assertEqual(parser._date_label_to_iso("12 января 2030", now=dec_ref), "2030-01-12")

    def test_group_catalog_transliteration_uppercase_y(self):
        self.assertEqual(GroupCatalog.normalize("Y-24-1"), "у-24-1")
        self.assertEqual(GroupCatalog.normalize("y-24-1"), "у-24-1")


SAMPLE_GROUP = GroupInfo(
    department_id=1,
    department_code="IT",
    department_name="ИТ",
    group_name="ИСП-25-1",
    schedule_id=600,
    url="http://test/600",
)


class GroupCatalogDbPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """Каталог групп должен переживать недоступность сайта и перезапуск бота через БД."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_successful_fetch_writes_catalog_to_db(self) -> None:
        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(
            catalog, "_fetch_from_site", AsyncMock(return_value=({"исп-25-1": SAMPLE_GROUP}, {600: SAMPLE_GROUP}))
        ):
            await catalog.refresh()

        rows = await self.db.get_all_groups()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schedule_id"], 600)
        group = await catalog.find_group("ИСП-25-1")
        self.assertIsNotNone(group)
        self.assertEqual(group.schedule_id, 600)

    async def test_failed_fetch_falls_back_to_db(self) -> None:
        # Первый каталог успешно грузится с сайта и сохраняет снимок в БД.
        warm_catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(
            warm_catalog, "_fetch_from_site", AsyncMock(return_value=({"исп-25-1": SAMPLE_GROUP}, {600: SAMPLE_GROUP}))
        ):
            await warm_catalog.refresh()

        # Новый процесс (например, после перезапуска бота) не может достучаться до сайта.
        cold_catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(cold_catalog, "_fetch_from_site", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            await cold_catalog.refresh()

        group = await cold_catalog.find_group("ИСП-25-1")
        self.assertIsNotNone(group, "Группа из БД должна находиться даже при недоступном сайте")
        self.assertEqual(group.schedule_id, 600)
        self.assertIsInstance(cold_catalog.last_error, RuntimeError)

    async def test_failed_fetch_without_db_data_leaves_catalog_empty(self) -> None:
        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(catalog, "_fetch_from_site", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            await catalog.refresh()
            group = await catalog.find_group("ИСП-25-1")

        self.assertIsNone(group)

    async def test_bootstraps_from_existing_user_subscriptions_when_db_is_empty(self) -> None:
        """Самый первый запуск после обновления: groups пуста, а сайт недоступен."""
        await self.db.upsert_user(
            "telegram", 1, "alice", "Alice",
            subscription_type="group", subscription_key="group:600",
            subscription_title="ИСП-25-1", schedule_id=600, group_name="ИСП-25-1",
        )

        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(catalog, "_fetch_from_site", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            await catalog.refresh()
            group = await catalog.find_group("ИСП-25-1")

        self.assertIsNotNone(group, "Группа уже известна по подписке пользователя — должна находиться")
        self.assertEqual(group.schedule_id, 600)
        # Восстановленное так же попадает в БД, чтобы не повторять работу при следующем падении.
        self.assertEqual(len(await self.db.get_all_groups()), 1)

    async def test_bootstrap_ignores_teacher_and_audience_subscriptions(self) -> None:
        await self.db.upsert_user(
            "telegram", 1, "alice", "Alice",
            subscription_type="teacher", subscription_key="prep_1", subscription_title="Иванов И.И.",
        )

        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(catalog, "_fetch_from_site", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            await catalog.refresh()
            group = await catalog.find_group("Иванов И.И.")

        self.assertIsNone(group)

    async def test_finds_pending_group_without_schedule_id(self) -> None:
        """Группа, увиденная на фото, а не на сайте — доступна поиску без ID."""
        await self.db.add_pending_groups(["МТО-26"])

        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(catalog, "_fetch_from_site", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            await catalog.refresh()
            group = await catalog.find_group("МТО-26")

        self.assertIsNotNone(group)
        self.assertIsNone(group.schedule_id)

    async def test_add_pending_group_updates_db_and_live_catalog(self) -> None:
        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)

        await catalog.add_pending_groups(["МТО-26"])

        group = await catalog.find_group("мто - 26")
        self.assertIsNotNone(group)
        self.assertIsNone(group.schedule_id)
        self.assertEqual([item.group_name for item in await catalog.list_groups()], ["МТО-26"])
        self.assertEqual([row["group_name"] for row in await self.db.get_all_groups()], ["МТО-26"])

    async def test_pending_groups_excluded_from_schedule_id_lookup(self) -> None:
        await self.db.add_pending_groups(["МТО-26"])

        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(catalog, "_fetch_from_site", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            await catalog.refresh()

        self.assertEqual(len(catalog), 1)  # всё же известная группа
        self.assertEqual(catalog._groups_by_schedule_id, {}, "У pending-группы нет ID — ей нечего делать в этом индексе")

    async def test_pending_group_gets_real_id_once_site_resolves_it(self) -> None:
        """Как только сайт возвращает группу с реальным ID, она вытесняет pending-запись."""
        await self.db.add_pending_groups(["МТО-26"])
        resolved = GroupInfo(
            department_id=1, department_code="MTO", department_name="МТО",
            group_name="МТО-26", schedule_id=700, url="http://test/700",
        )

        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(
            catalog, "_fetch_from_site", AsyncMock(return_value=({"мто-26": resolved}, {700: resolved}))
        ):
            await catalog.refresh()

        group = await catalog.find_group("МТО-26")
        self.assertEqual(group.schedule_id, 700)
        # И в БД — тоже, а не только в памяти этого процесса.
        rows = await self.db.get_all_groups()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schedule_id"], 700)

    async def test_no_db_disables_persistence(self) -> None:
        catalog = GroupCatalog(schedule_url="http://test-schedule.local")
        catalog._groups_by_schedule_id = {600: SAMPLE_GROUP}

        await catalog._save_to_db()  # не должно бросать и не должно ничего сохранять

        self.assertIsNone(catalog.db)

    async def test_forced_refresh_refetches_even_when_already_loaded(self) -> None:
        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        fetch = AsyncMock(return_value=({"исп-25-1": SAMPLE_GROUP}, {600: SAMPLE_GROUP}))
        with patch.object(catalog, "_fetch_from_site", fetch):
            await catalog.refresh()
            await catalog.refresh()  # уже загружен - повторного похода на сайт быть не должно
            self.assertEqual(fetch.await_count, 1)

            await catalog.refresh(force=True)
            self.assertEqual(fetch.await_count, 2)

    async def test_forced_refresh_failure_keeps_previous_data(self) -> None:
        catalog = GroupCatalog(schedule_url="http://test-schedule.local", db=self.db)
        with patch.object(
            catalog, "_fetch_from_site", AsyncMock(return_value=({"исп-25-1": SAMPLE_GROUP}, {600: SAMPLE_GROUP}))
        ):
            await catalog.refresh()

        with patch.object(catalog, "_fetch_from_site", AsyncMock(side_effect=RuntimeError("сайт лежит"))):
            await catalog.refresh(force=True)

        group = await catalog.find_group("ИСП-25-1")
        self.assertIsNotNone(group, "Неудачный принудительный рефреш не должен стирать уже загруженные данные")

    async def test_empty_db_read_before_any_sync(self) -> None:
        rows = await self.db.get_all_groups()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()

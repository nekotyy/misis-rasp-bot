import unittest
from unittest.mock import AsyncMock, MagicMock

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


if __name__ == "__main__":
    unittest.main()

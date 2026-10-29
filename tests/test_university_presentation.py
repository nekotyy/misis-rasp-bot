from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.lesson_counters import resolve_group_preview_content, snapshot_to_search_content
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.parser import compute_snapshot_hash
from src.schedule_search import ScheduleSearchCatalog, SearchTarget, search_choice_label
from src.schedule_service import ScheduleFormatter, day_from_content, university_current_week
from src.schedule_sources import UNIVERSITY_BELL_TIMES, UNIVERSITY_BELLS_HTML, UNIVERSITY_BELLS_TEXT


class UniversityPresentationTests(unittest.IsolatedAsyncioTestCase):
    def day(self, *, variant=2, current=1, lessons=True):
        return DaySchedule("2026-10-29", "2026-10-29", [
            Lesson(3, "Физика (Лабораторная работа, 1 подгруппа)", "Сурков В. П.", "4/411"),
            Lesson(3, "Математика (Лабораторная работа, 2 подгруппа)", "Головченко Т. В.", "2/204"),
        ] if lessons else [], variant, current)

    def test_current_week_and_selected_variant_are_distinct_in_both_formats(self):
        for html in (False, True):
            text = ScheduleFormatter.format_day_card(self.day(), "") if html else ScheduleFormatter.format_day_plain(self.day())
            template = "<b>{}</b>" if html else "{}"
            self.assertIn("Учимся по: " + template.format("Числитель"), text)
            self.assertIn("Выбрано: " + template.format("Знаменатель"), text)
            self.assertIn("29 октября 2026 года", text)
            self.assertEqual(text.count("3.</b>" if html else "3. в"), 2)
            self.assertIn("1 подгруппа", text)
            self.assertIn("2 подгруппа", text)

    def test_empty_days_keep_week_labels(self):
        text = ScheduleFormatter.format_day_plain(self.day(lessons=False))
        self.assertIn("Учимся по: Числитель", text)
        self.assertIn("Выбрано: Знаменатель", text)
        self.assertIn("Пар нет.", text)

    def test_html_from_source_remains_escaped(self):
        day = self.day()
        day.lessons[0].subject = "Физика <script> & оптика"
        text = ScheduleFormatter.format_day_card(day, "")
        self.assertIn("Физика &lt;script&gt; &amp; оптика", text)
        self.assertNotIn("<script>", text)

    def test_week_metadata_survives_storage_and_search_serialization(self):
        snapshot = ScheduleSnapshot("ИТ-26д", datetime(2026, 10, 29, 12), [self.day()])
        content = snapshot.to_dict()
        self.assertEqual(content, snapshot_to_search_content(snapshot))
        self.assertEqual(content["days"][0]["week_variant"], 2)
        self.assertEqual(content["days"][0]["current_week_variant"], 1)
        restored = day_from_content(content["days"][0], fetched_at=datetime.now().isoformat())
        self.assertEqual(restored.week_variant, 2)
        self.assertEqual(restored.current_week_variant, 1)

    def test_legacy_college_content_and_hash_are_compatible(self):
        day = DaySchedule("29 октября", "2026-10-29", [])
        self.assertEqual(day.to_dict(), {"date_label": "29 октября", "date_iso": "2026-10-29", "lessons": []})
        restored = day_from_content(day.to_dict())
        self.assertIsNone(restored.week_variant)
        snapshot = ScheduleSnapshot("ИСП-25-1", datetime(2026, 10, 29), [day])
        previous = compute_snapshot_hash(snapshot)
        day.week_variant = 1
        self.assertNotEqual(compute_snapshot_hash(snapshot), previous)

    def test_current_week_rolls_over_monday_and_across_year_boundary(self):
        content = {"fetched_at": "2026-12-27T20:00:00", "days": [self.day(variant=2, current=1).to_dict()]}
        for today, expected in ((date(2026, 12, 27), 1), (date(2026, 12, 28), 2), (date(2027, 1, 4), 1)):
            with self.subTest(today=today):
                self.assertEqual(university_current_week(content, today=today), expected)
        self.assertEqual(content["days"][0]["week_variant"], 2)

    def test_unknown_or_invalid_reference_does_not_invent_current_week(self):
        self.assertIsNone(university_current_week({"days": []}))
        self.assertIsNone(university_current_week({"days": [self.day().to_dict()], "fetched_at": "invalid"}))

    def test_bells_preserve_all_requested_times_without_html_in_vk(self):
        self.assertEqual(UNIVERSITY_BELL_TIMES, ("09:00–10:30", "10:40–12:10", "12:40–14:10", "14:20–15:50", "16:00–17:30", "18:30–20:00", "20:10–21:40"))
        for value in UNIVERSITY_BELL_TIMES:
            self.assertIn(value, UNIVERSITY_BELLS_TEXT)
            self.assertIn(f"<code>{value}</code>", UNIVERSITY_BELLS_HTML)
        self.assertNotIn("<", UNIVERSITY_BELLS_TEXT)

    async def test_legacy_university_cache_upgrades_and_remains_usable_offline(self):
        legacy = {"content": {"group_name": "ИТ-26д", "days": [DaySchedule("2026-10-29", "2026-10-29", []).to_dict()]}}
        snapshot = ScheduleSnapshot("ИТ-26д", datetime.now(), [self.day()])
        db = MagicMock(get_latest_snapshot=AsyncMock(return_value=legacy))
        parser = MagicMock(parse_from_url=AsyncMock(return_value=(snapshot, "new")))
        url = "http://r.sf-misis.ru/group/3910"
        upgraded = await resolve_group_preview_content(db, parser, url)
        self.assertEqual(upgraded["days"][0]["week_variant"], 2)
        parser.parse_from_url.side_effect = httpx.ConnectError("offline")
        self.assertEqual(await resolve_group_preview_content(db, parser, url), legacy["content"])

    async def test_slow_metadata_upgrade_does_not_hold_legacy_cached_schedule(self):
        legacy = {"content": {"group_name": "ИТ-26д", "days": [DaySchedule("2026-10-29", "2026-10-29", []).to_dict()]}}
        db = MagicMock(get_latest_snapshot=AsyncMock(return_value=legacy))

        async def unavailable(url):
            await asyncio.Event().wait()

        parser = MagicMock(parse_from_url=AsyncMock(side_effect=unavailable))
        timeout = asyncio.timeout
        with patch("src.lesson_counters.asyncio.timeout", side_effect=lambda _delay: timeout(0)):
            result = await resolve_group_preview_content(db, parser, "http://r.sf-misis.ru/group/3910")
        self.assertEqual(result, legacy["content"])

    async def test_teacher_in_both_sources_requires_choice_even_with_initials(self):
        catalog = ScheduleSearchCatalog("http://college", MagicMock(find_group=AsyncMock(return_value=None)))
        catalog._populate_preps([
            ("Уварова И. В.", "http://college/raspprep/3"),
            ("Уварова Ирина Владимировна", "http://r.sf-misis.ru/teacher/3"),
        ])
        catalog._preps_loaded = True
        for query in ("Уварова", "Уварова И.В."):
            matches = await catalog.find_all(query)
            self.assertEqual(len(matches), 2)
            self.assertIsNone(await catalog.find(query))
            labels = [search_choice_label(target, matches) for target in matches]
            self.assertTrue(any(label.endswith("(колледж)") for label in labels))
            self.assertTrue(any(label.endswith("(университет)") for label in labels))

    def test_single_source_teacher_labels_remain_short(self):
        target = SearchTarget("teacher", "Уварова И. В.", "http://college/raspprep/3")
        self.assertEqual(search_choice_label(target, [target]), target.title)

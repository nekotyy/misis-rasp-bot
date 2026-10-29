from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from bs4 import BeautifulSoup

from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog, GroupInfo
from src.lesson_counters import resolve_audience_preview_content, resolve_group_preview_content, resolve_teacher_snapshot
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.parser import ScheduleParser, compute_snapshot_hash
from src.schedule_search import ScheduleSearchCatalog, SearchTarget, search_choice_label
from src.schedule_service import university_day_variant
from src.schedule_sources import parse_university_directory, schedule_numeric_id
from src.scheduler import ScheduleJobs
from src.subscription_utils import make_audience_subscription, make_group_subscription, make_teacher_subscription
from src.telegram_bot import build_dispatcher, university_schedule_keyboard
from src.vk_bot import build_vk_bot

ROOT = "http://r.sf-misis.ru"
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")


def weekly_html(variant: int = 1, current: int = 1, *, title: str = "Расписание для ТЭ/ПТ-26д") -> str:
    names = {1: "числителю", 2: "знаменателю"}
    lesson = f"""<td class="table-subgroups"><div class="table-subgroup-item">
        <div class="table-sg-name">1 подгруппа</div><div class="table-subject">Физика {variant}</div>
        <div class="table-lessontype">Лабораторная работа</div>
        <div class="table-teacher">Иванов&nbsp;И.&nbsp;И.</div><div class="table-room">1/405</div></div>
        <div class="table-subgroup-item"><div class="table-sg-name">2 подгруппа</div>
        <div class="table-subject">Химия</div><div class="table-teacher">Петров П.П.</div>
        <div class="table-room">1/406</div></div></td>"""
    rows = "".join(f'<tr><th class="table-weekdays">{name}</th>{lesson}<td class="table-empty">—</td></tr>' for name in WEEKDAYS)
    return f"""<a id="title-value">{title}</a><div id="schedule-title"><div class="misc-schedule">
        <span>На этой неделе учимся по {names[current]} <br>(показано по {names[variant]})</span>
        <button class="search-button-active" onclick="window.location='/group/3917/{variant}'">Вариант</button>
        </div></div><table id="schedule-table"><tr><th></th>
        <th><div class="table-lesson-num">1</div></th><th><div class="table-lesson-num">2</div></th></tr>{rows}</table>"""


def directory_response(kind: str, rows: list[dict]) -> httpx.Response:
    return httpx.Response(200, text="var database = " + json.dumps({kind: rows}) + ";")


class UniversityParserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.parser = ScheduleParser("http://college/rasp/600")

    def test_urls_and_ids_do_not_collide(self) -> None:
        self.assertEqual(self.parser.build_schedule_url(3917), "http://college/rasp/3917")
        self.assertEqual(self.parser.build_schedule_url(-3917), ROOT + "/group/3917")
        self.assertEqual(schedule_numeric_id(ROOT + "/group/3917/2"), -3917)
        self.assertEqual(schedule_numeric_id(ROOT + "/teacher/539"), -539)
        self.assertIsNone(schedule_numeric_id(""))

    def test_parse_week_preserves_subgroups_and_lesson_types(self) -> None:
        snapshot = self.parser._parse_university_html(BeautifulSoup(weekly_html(), "html.parser"), datetime(2026, 10, 29))
        self.assertEqual(snapshot.group_name, "ТЭ/ПТ-26д")
        self.assertEqual(len(snapshot.days), 7)
        self.assertEqual(snapshot.days[0].date_iso, "2026-10-26")
        lessons = snapshot.days[0].lessons
        self.assertEqual([lesson.number for lesson in lessons], [1, 1])
        self.assertEqual(lessons[0].teacher, "Иванов И. И.")
        self.assertIn("1 подгруппа", lessons[0].subject)
        self.assertIn("Лабораторная работа", lessons[0].subject)
        self.assertEqual(lessons[1].classroom, "1/406")

    def test_parity_comes_from_site_and_handles_year_boundary(self) -> None:
        soup = BeautifulSoup(weekly_html(1, 2), "html.parser")
        snapshot = self.parser._parse_university_html(soup, datetime(2026, 12, 31))
        self.assertEqual(snapshot.days[0].date_iso, "2027-01-04")

    async def test_default_loads_current_and_next_week(self) -> None:
        self.parser.fetch_html_from_url = AsyncMock(side_effect=[weekly_html(2, 2), weekly_html(1, 2)])
        with patch("src.parser.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 1)  # воскресенье
            snapshot, digest = await self.parser.parse(-3917)
        self.assertEqual(len(snapshot.days), 14)
        self.assertEqual(snapshot.days[0].date_iso, "2026-10-26")
        self.assertEqual(snapshot.days[7].date_iso, "2026-11-02")
        self.assertIn("Физика 2", snapshot.days[6].lessons[0].subject)
        self.assertIn("Физика 1", snapshot.days[7].lessons[0].subject)
        self.assertEqual(digest, compute_snapshot_hash(snapshot))
        self.assertEqual(self.parser.fetch_html_from_url.await_args_list[1].args[0], ROOT + "/group/3917/1")

    async def test_chosen_variant_is_aligned_to_requested_date(self) -> None:
        self.parser.fetch_html_from_url = AsyncMock(return_value=weekly_html(2, 1))
        day = await university_day_variant(self.parser, ROOT + "/group/3917", "2026-10-29", 2)
        self.assertEqual(day.date_iso, "2026-10-29")
        self.assertIn("Физика 2", day.lessons[0].subject)
        self.parser.fetch_html_from_url.assert_awaited_once_with(ROOT + "/group/3917/2")

    async def test_missing_table_and_wrong_parity_are_rejected(self) -> None:
        for pages in (["<html>Сайт временно недоступен</html>"], [weekly_html(), weekly_html()]):
            with self.subTest(pages=len(pages)):
                self.parser.fetch_html_from_url = AsyncMock(side_effect=pages)
                with self.assertRaises(ValueError):
                    await self.parser.parse(-3917)
        self.parser.fetch_html_from_url = AsyncMock(return_value=weekly_html())
        with self.assertRaises(ValueError):
            await self.parser.parse_university_variant(ROOT + "/group/3917", 2, "2026-10-29")

    def test_directory_wrapper_is_data_and_invalid_rows_are_rejected(self) -> None:
        row = {"value": 3917, "content": "ТЭ/ПТ-26д"}
        self.assertEqual(parse_university_directory(directory_response("groups", [row]).text, "groups"), [row])
        with self.assertRaises(ValueError):
            parse_university_directory('{"groups":[{"value":-1,"content":"bad"}]}', "groups")

    def test_source_configuration_and_callback_dates(self) -> None:
        with patch.dict(os.environ, {"UNIVERSITY_SCHEDULE_URL": "http://university/"}):
            self.assertEqual(Settings.from_env().university_schedule_url, "http://university")
        rows = university_schedule_keyboard("2026-10-29").inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["schedule:week:2026-10-29:1", "schedule:week:2026-10-29:2"])
        self.assertEqual(rows[1][0].callback_data, "schedule:week:2026-10-29:0")


class UniversityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        await self.db.initialize()
        self.college = GroupInfo(1, "ИТ", "ИТ", "ИСП-25-1", 3917, "http://college/rasp/3917")
        self.row = {"value": 3917, "content": "ТЭ/ПТ-26д", "faculty_id": 1, "faculty_name": "Факультет"}

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def catalog(self) -> GroupCatalog:
        catalog = GroupCatalog("http://college/rasp", university_url=ROOT, db=self.db)
        catalog._fetch_college_site = AsyncMock(return_value=({"исп-25-1": self.college}, {3917: self.college}, set()))
        catalog._get_with_retry = AsyncMock(return_value=directory_response("groups", [self.row]))
        return catalog

    async def test_both_catalogs_persist_and_partial_outage_keeps_cached_source(self) -> None:
        catalog = self.catalog()
        await catalog.ensure_loaded()
        self.assertEqual((await catalog.find_group("тэ/пт-26д")).schedule_id, -3917)
        self.assertEqual((await catalog.find_group("ИСП-25-1")).schedule_id, 3917)
        self.assertEqual({row["schedule_id"] for row in await self.db.get_all_groups()}, {-3917, 3917})
        cold = self.catalog()
        cold._fetch_college_site.side_effect = httpx.HTTPError("college offline")
        await cold.ensure_loaded()
        self.assertEqual({group.schedule_id for group in await cold.list_groups()}, {-3917, 3917})
        cold._get_with_retry.side_effect = httpx.HTTPError("university offline")
        await cold.refresh(force=True)
        self.assertEqual(len(await cold.list_groups()), 2)
        self.assertTrue(cold._failed_origins)
        cold._fetch_college_site.side_effect = None
        cold._get_with_retry.side_effect = None
        self.assertTrue(await cold.retry_failed_departments())

    async def test_search_directories_merge_and_restore_independently(self) -> None:
        groups = MagicMock(spec=GroupCatalog)
        groups.find_group = AsyncMock(return_value=None)
        catalog = ScheduleSearchCatalog("http://college", groups, db=self.db, university_url=ROOT)
        college_url = "http://college/raspprep/539"
        catalog._fetch_college_pairs = AsyncMock(return_value=[("Петров П.П.", college_url)])
        catalog._get_with_retry = AsyncMock(
            return_value=directory_response("teachers", [{"value": 539, "content": "Цыганков Юрий Александрович"}])
        )
        target = await catalog.find("Цыганков")
        self.assertEqual(target.url, ROOT + "/teacher/539")
        self.assertEqual((await catalog.find("Петров")).url, college_url)
        catalog._get_with_retry.side_effect = httpx.HTTPError("offline")
        self.assertTrue(await catalog.refresh_teachers())
        self.assertEqual((await catalog.find("Цыганков")).url, target.url)
        catalog._fetch_college_pairs.side_effect = httpx.HTTPError("offline")
        self.assertTrue(await catalog.refresh_teachers())
        self.assertEqual(len(await self.db.get_search_targets("teacher")), 2)

    async def test_rooms_with_same_name_are_not_silently_combined(self) -> None:
        groups = MagicMock(spec=GroupCatalog)
        groups.find_group = AsyncMock(return_value=None)
        catalog = ScheduleSearchCatalog("http://college", groups)
        catalog._populate_preps([("Петров П.П.", "http://college/raspprep/3")])
        catalog._preps_loaded = catalog._auds_loaded = True
        catalog._populate_auds([("1/405", "http://college/raspAud/16"), ("1/405", ROOT + "/room/16")])
        self.assertEqual(len(await catalog.find_all("1/405")), 2)
        self.assertIsNone(await catalog.find("1/405"))
        labels = [search_choice_label(target, await catalog.find_all("1/405")) for target in await catalog.find_all("1/405")]
        self.assertEqual(set(labels), {"1/405 (колледж)", "1/405 (университет)"})

    async def test_subscriptions_preview_and_background_sync_share_existing_storage(self) -> None:
        parser = ScheduleParser("http://college/rasp")
        today = datetime.now().date().isoformat()
        snapshot = ScheduleSnapshot("ТЭ/ПТ-26д", datetime.now(), [DaySchedule(today, today, [Lesson(1, "Физика", "Иванов", "1/405")])])
        parser.parse = AsyncMock(return_value=(snapshot, compute_snapshot_hash(snapshot)))
        parser.parse_from_url = AsyncMock(return_value=(snapshot, compute_snapshot_hash(snapshot)))
        subscription = make_group_subscription("ТЭ/ПТ-26д", -3917)
        await self.db.upsert_user("telegram", 1, None, "Тест")
        await self.db.set_user_subscription("telegram", 1, **subscription)
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db, jobs.parser = self.db, parser
        jobs.broadcaster = AsyncMock()
        source = (await self.db.get_active_sources())[0]
        await jobs._sync_source(source)
        parser.parse.assert_awaited_once_with(-3917)
        saved = await self.db.get_latest_snapshot("current", source_key="group:-3917")
        self.assertIsNotNone(saved)
        preview = await resolve_group_preview_content(self.db, parser, ROOT + "/group/3917")
        self.assertEqual(preview, saved["content"])
        parser.parse_from_url.assert_not_called()
        snapshot.days[0].lessons[0].subject = "Математика"
        await jobs.apply_snapshot(source, snapshot, compute_snapshot_hash(snapshot))
        self.assertEqual(jobs.broadcaster.broadcast.await_args.kwargs["subscription_key"], "group:-3917")

        target = SearchTarget("audience", "1/405", ROOT + "/room/16")
        self.assertEqual(make_audience_subscription(target)["audience_subscription_key"], "audience:-16")
        await self.db.save_snapshot(
            "current", "room", snapshot, None, None, source_type="audience", source_key="audience:-16", source_url=target.url
        )
        await resolve_audience_preview_content(self.db, parser, target.url)
        parser.parse_from_url.assert_not_called()
        teacher = SearchTarget("teacher", "Цыганков Юрий Александрович", ROOT + "/teacher/539")
        self.assertEqual(make_teacher_subscription(teacher)["subscription_key"], "teacher:-539")
        await resolve_teacher_snapshot(self.db, parser, teacher.title, teacher.url)
        parser.parse_from_url.assert_awaited_once_with(teacher.url)
        teacher_source = {"source_type": "teacher", "source_title": teacher.title, "source_url": teacher.url}
        await jobs._parse_source(teacher_source)
        self.assertEqual(parser.parse_from_url.await_count, 2)
        college_teacher = await resolve_teacher_snapshot(self.db, parser, "Иванов", "http://college/raspprep/539")
        self.assertEqual(college_teacher.days, [], "Однофамилец из университета не попадает в подписку колледжа")


class UniversityBotVariantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")
        await self.db.initialize()
        self.settings = Settings.from_env()
        self.settings.telegram_bot_token = "123456:FAKE-TOKEN"
        self.settings.vk_bot_token = "fake_token"
        self.settings.admin_telegram_ids = [42]
        self.settings.admin_vk_id = 42
        self.settings.limited_admin_telegram_ids = []
        self.settings.ocr_enabled = False
        self.settings.lesson_counters_enabled = False
        self.settings.database_path = self.db.path
        date_iso = datetime.now().date().isoformat()
        self.snapshot = ScheduleSnapshot(
            "ТЭ/ПТ-26д", datetime.now(), [DaySchedule(date_iso, date_iso, [Lesson(1, "Актуальная физика", "Иванов", "1/405")])]
        )
        alternate = ScheduleSnapshot(
            "ТЭ/ПТ-26д", datetime.now(), [DaySchedule(date_iso, date_iso, [Lesson(1, "Другой вариант", "Петров", "1/406")])]
        )
        self.parser = ScheduleParser("http://college/rasp")
        self.parser.parse_university_variant = AsyncMock(return_value=alternate)
        self.parser.parse_from_url = AsyncMock(return_value=(self.snapshot, "current"))
        for platform in ("telegram", "vk"):
            await self.db.upsert_user(platform, 42, None, "Тест")
            await self.db.set_user_subscription(platform, 42, **make_group_subscription("ТЭ/ПТ-26д", -3917))
        await self.db.save_snapshot(
            "current", "original", self.snapshot, schedule_id=-3917, group_name="ТЭ/ПТ-26д", source_key="group:-3917"
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_telegram_actual_variant_and_return_preserve_subscription_and_cache(self) -> None:
        dispatcher = build_dispatcher(self.settings, self.db, self.parser, None, None, None)
        handler = next(h.callback for h in dispatcher.callback_query.handlers if h.callback.__name__ == "handle_schedule_callback")
        bot = AsyncMock()
        date_iso = self.snapshot.days[0].date_iso
        for action, expected in (
            ("today", "Актуальная физика"),
            (f"week:{date_iso}:2", "Другой вариант"),
            (f"week:{date_iso}:0", "Актуальная физика"),
        ):
            callback = SimpleNamespace(
                data="schedule:" + action,
                message=SimpleNamespace(chat=SimpleNamespace(id=42)),
                from_user=SimpleNamespace(id=42, first_name="Тест", last_name=None, username=None),
                bot=bot,
                answer=AsyncMock(),
            )
            await handler(callback)
            self.assertIn(expected, bot.send_message.call_args.args[1])
            keyboard = bot.send_message.call_args.kwargs["reply_markup"]
            self.assertIn("Числитель", [button.text for row in keyboard.inline_keyboard for button in row])
        self.assertEqual((await self.db.get_latest_snapshot("current", source_key="group:-3917"))["snapshot_hash"], "original")
        self.assertEqual((await self.db.get_user("telegram", 42)).subscription_key, "group:-3917")

    async def test_vk_variant_buttons_work_without_session_and_keep_same_date(self) -> None:
        bot = build_vk_bot(self.settings, self.db, self.parser, None)

        async def request(method, params, version=None):
            return {"response": [] if method == "users.get" else 1}

        bot.api.request = AsyncMock(side_effect=request)
        handler = next(h.handler for h in bot.labeler.message_view.handlers if h.handler.__name__ == "all_messages_handler")
        date_iso = self.snapshot.days[0].date_iso
        date_label = datetime.fromisoformat(date_iso).strftime("%d.%m.%Y")
        for text, expected in (
            ("Расписание на сегодня", "Актуальная физика"),
            (f"Знаменатель на {date_label}", "Другой вариант"),
            (f"Актуальное на {date_label}", "Актуальная физика"),
        ):
            await handler(SimpleNamespace(peer_id=42, from_id=42, text=text, action=None, attachments=[]))
            sent = [call.args[1] for call in bot.api.request.await_args_list if call.args[0] == "messages.send"][-1]
            self.assertIn(expected, sent["message"])
            labels = [b["action"]["label"] for row in json.loads(sent["keyboard"])["buttons"] for b in row]
            self.assertIn(f"Числитель на {date_label}", labels)
        self.parser.parse_university_variant.assert_awaited_once_with(ROOT + "/group/3917", 2, date_iso)
        self.assertEqual((await self.db.get_latest_snapshot("current", source_key="group:-3917"))["snapshot_hash"], "original")

    async def test_variant_date_does_not_depend_on_today(self) -> None:
        requested = (datetime.now().date() + timedelta(days=1)).isoformat()
        self.parser.parse_university_variant.return_value.days[0].date_iso = requested
        day = await university_day_variant(self.parser, ROOT + "/group/3917", requested, 1)
        self.assertEqual(day.date_iso, requested)

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import Settings
from src.course_stats import (
    Audience,
    build_course_stats,
    collect_course_stats,
    course_of_group,
    current_academic_year,
    format_course_stats_report,
    is_chat,
    parse_admission_year,
)
from src.db import Database
from src.subscription_utils import make_group_subscription
from src.telegram_bot import ADMIN_SECTION_KEYBOARDS, build_dispatcher
from src.vk_bot import VK_ADMIN_SECTIONS, build_vk_bot

TODAY = date(2026, 10, 12)


def sub(platform: str, user_id: int, kind: str, title: str, key: str | None = None) -> dict:
    return {
        "platform": platform,
        "user_id": user_id,
        "subscription_type": kind,
        "subscription_key": key or f"{kind}:{title}",
        "subscription_title": title,
        "group_name": title if kind == "group" else None,
    }


class CourseOfGroupTests(unittest.TestCase):
    def test_course_is_derived_from_admission_year_in_group_name(self) -> None:
        self.assertEqual(course_of_group("ТЭС-26", TODAY), 1)
        self.assertEqual(course_of_group("ИСП-25-1", TODAY), 2)
        self.assertEqual(course_of_group("МП-М-24", TODAY), 3)
        self.assertEqual(course_of_group("ИСП-23-5", TODAY), 4)
        self.assertEqual(course_of_group("ТЭОР-26-2", TODAY), 1)
        self.assertEqual(course_of_group("Э-25", TODAY), 2)

    def test_graduated_and_not_yet_admitted_groups_are_not_valid_courses(self) -> None:
        self.assertIsNone(course_of_group("ИСП-22-1", TODAY))
        self.assertIsNone(course_of_group("ИСП-27-1", TODAY))

    def test_unparsable_names_are_rejected(self) -> None:
        for name in ("", "Общая", "ИСП", "ИСП-2025", "ИСП-5", "ИСП-25-1-2-3", "25-1", "ИСП-25-А", "ИСП 25 1 лишнее"):
            with self.subTest(name=name):
                self.assertIsNone(parse_admission_year(name), name)

    def test_name_variants_are_normalised(self) -> None:
        self.assertEqual(course_of_group("исп-25-1", TODAY), 2)
        self.assertEqual(course_of_group("ИСП - 25 - 1", TODAY), 2)
        self.assertEqual(course_of_group("ИСП–25–1", TODAY), 2)
        # Латинские «C» и «P», похожие на кириллические.
        self.assertEqual(course_of_group("ИCП-25-1", TODAY), 2)
        self.assertEqual(parse_admission_year("ТЭС-26"), 2026)

    def test_academic_year_switches_on_first_of_september(self) -> None:
        self.assertEqual(current_academic_year(date(2026, 8, 31)), 2025)
        self.assertEqual(current_academic_year(date(2026, 9, 1)), 2026)
        self.assertEqual(current_academic_year(date(2027, 1, 15)), 2026)

        self.assertEqual(course_of_group("ИСП-25-1", date(2026, 8, 31)), 1)
        self.assertEqual(course_of_group("ИСП-25-1", date(2026, 9, 1)), 2)
        self.assertIsNone(course_of_group("ТЭС-26", date(2026, 8, 31)))
        self.assertEqual(course_of_group("ТЭС-26", date(2026, 9, 1)), 1)
        self.assertEqual(course_of_group("ИСП-23-1", date(2026, 8, 31)), 3)
        self.assertEqual(course_of_group("ИСП-22-1", date(2026, 8, 31)), 4)
        self.assertIsNone(course_of_group("ИСП-22-1", date(2026, 9, 1)))

    def test_is_chat_follows_platform_rules(self) -> None:
        self.assertFalse(is_chat("telegram", 1001))
        self.assertTrue(is_chat("telegram", -100123))
        self.assertFalse(is_chat("vk", 2001))
        self.assertTrue(is_chat("vk", 2000000005))


class PluralTests(unittest.TestCase):
    def test_russian_plural_forms(self) -> None:
        from src.course_stats import _plural

        forms = ("группа", "группы", "групп")
        expected = {0: "групп", 1: "группа", 2: "группы", 4: "группы", 5: "групп", 11: "групп", 12: "групп", 14: "групп",
                    21: "группа", 22: "группы", 25: "групп", 101: "группа", 111: "групп", 112: "групп"}
        for count, form in expected.items():
            with self.subTest(count=count):
                self.assertEqual(_plural(count, *forms), form)


class BuildCourseStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog = ["ИСП-25-1", "ИСП-25-2", "ТЭС-26", "МП-М-24", "ИСП-23-1", "ИСП-22-1", "ИСП-27-1", "Общая группа"]
        subscriptions = [
            sub("telegram", 1, "group", "ИСП-25-1"),
            sub("telegram", 2, "group", "ИСП-25-1"),
            sub("telegram", -100500, "group", "ИСП-25-1"),
            sub("vk", 10, "group", "ТЭС-26"),
            sub("vk", 2000000001, "group", "ТЭС-26"),
            sub("telegram", 3, "group", "ИСП-22-1"),
            sub("telegram", 5, "group", "ИСП-25-3 "),
            sub("telegram", 6, "group", "исп-25-1"),
            sub("telegram", 4, "teacher", "Иванов И. И.", key="teacher:1"),
            sub("vk", 11, "teacher", "Иванов И. И.", key="teacher:1"),
            sub("telegram", -100600, "teacher", "Петрова А. А.", key="teacher:2"),
        ]
        self.report = build_course_stats(catalog, subscriptions, known_teachers=85, today=TODAY)

    def course(self, number: int):
        return self.report.courses[number - 1]

    def test_always_four_courses_with_admission_years(self) -> None:
        self.assertEqual([c.course for c in self.report.courses], [1, 2, 3, 4])
        self.assertEqual([c.admission_year for c in self.report.courses], [2026, 2025, 2024, 2023])
        self.assertEqual(self.report.academic_year_label, "2026/2027")

    def test_students_are_personal_accounts_split_by_platform(self) -> None:
        first = self.course(1).audience
        self.assertEqual((first.tg_personal, first.vk_personal, first.personal), (0, 1, 1))
        second = self.course(2).audience
        # ИСП-25-1: tg 1, 2, 6 (регистр не важен), ИСП-25-3 (нет в каталоге, но подписан): tg 5.
        self.assertEqual((second.tg_personal, second.vk_personal, second.personal), (4, 0, 4))

    def test_chats_are_counted_separately_from_students(self) -> None:
        self.assertEqual((self.course(1).audience.vk_chats, self.course(1).audience.tg_chats), (1, 0))
        self.assertEqual((self.course(2).audience.tg_chats, self.course(2).audience.vk_chats), (1, 0))
        self.assertEqual(self.report.courses_audience.chats, 2)

    def test_groups_per_course_are_sorted_and_include_subscribed_groups_missing_from_catalog(self) -> None:
        self.assertEqual([g.name for g in self.course(1).groups], ["ТЭС-26"])
        self.assertEqual([g.name for g in self.course(2).groups], ["ИСП-25-1", "ИСП-25-2", "ИСП-25-3"])
        self.assertEqual([g.name for g in self.course(3).groups], ["МП-М-24"])
        self.assertEqual([g.name for g in self.course(4).groups], ["ИСП-23-1"])
        self.assertEqual(self.course(2).groups_with_audience, 2)
        self.assertEqual(self.course(3).groups_with_audience, 0)

    def test_totals_cover_four_courses_only(self) -> None:
        total = self.report.courses_audience
        self.assertEqual(total.personal, 5)
        self.assertEqual(self.report.courses_groups, 6)

    def test_groups_outside_four_courses_are_ignored_not_counted(self) -> None:
        self.assertEqual({g.name for g in self.report.ignored_groups}, {"ИСП-22-1", "ИСП-27-1", "Общая группа"})
        graduated = next(g for g in self.report.ignored_groups if g.name == "ИСП-22-1")
        self.assertEqual(graduated.audience.personal, 1)

    def test_teachers_are_reported_separately(self) -> None:
        teachers = self.report.teachers
        self.assertEqual((teachers.audience.tg_personal, teachers.audience.vk_personal), (1, 1))
        self.assertEqual(teachers.audience.chats, 1)
        self.assertEqual(teachers.with_subscribers, 2)
        self.assertEqual(teachers.known, 85)
        self.assertEqual(self.report.everyone_personal, 5 + 2)

    def test_empty_input_gives_zeroed_courses(self) -> None:
        report = build_course_stats([], [], today=TODAY)

        self.assertEqual(len(report.courses), 4)
        self.assertEqual(report.courses_audience.personal, 0)
        self.assertEqual(report.ignored_groups, [])
        self.assertEqual(report.teachers.known, 0)

    def test_audience_ignores_unknown_platform(self) -> None:
        audience = Audience()
        audience.add("max", 1)
        self.assertEqual((audience.personal, audience.chats), (0, 0))


class FormatCourseStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog = ["ИСП-25-1", "ТЭС-26", "<b>ИСП-21</b>"]
        subscriptions = [
            sub("telegram", 1, "group", "ИСП-25-1"),
            sub("vk", 2, "group", "ТЭС-26"),
            sub("telegram", 3, "teacher", "Иванов И. И.", key="teacher:1"),
        ]
        self.report = build_course_stats(catalog, subscriptions, known_teachers=7, today=TODAY)

    def test_telegram_text_uses_html_and_escapes_group_names(self) -> None:
        text = format_course_stats_report(self.report, html=True)

        self.assertIn("<b>Статистика по курсам</b>", text)
        self.assertIn("Учебный год 2026/2027, данные на 12.10.2026", text)
        self.assertIn("<b>1 курс</b> (набор 2026): <b>1</b> студ. (TG 0 / VK 1)", text)
        self.assertIn("<b>2 курс</b> (набор 2025): <b>1</b> студ. (TG 1 / VK 0)", text)
        self.assertIn("<b>4 курс</b> (набор 2023): <b>0</b> студ.", text)
        self.assertIn("<b>Итого за 4 курса</b>: <b>2</b> студ.", text)
        self.assertIn("<b>Преподаватели</b>: <b>1</b> подписчик (TG 1 / VK 0)", text)
        self.assertIn("Не учтено: 1 группа (0 подписчиков)", text)
        self.assertIn("в справочнике сайта: 7", text)
        self.assertIn("<b>Всего (4 курса + преподаватели)</b>: <b>3</b>", text)
        self.assertIn("&lt;b&gt;ИСП-21&lt;/b&gt;", text)
        self.assertNotIn("<b>ИСП-21</b>", text)

    def test_vk_text_is_plain(self) -> None:
        text = format_course_stats_report(self.report, html=False)

        self.assertNotIn("<b>Статистика по курсам</b>", text)
        self.assertNotIn("<b>1</b>", text)
        self.assertIn("1 курс (набор 2026): 1 студ. (TG 0 / VK 1)", text)
        self.assertIn("Итого за 4 курса: 2 студ.", text)
        self.assertIn("Всего (4 курса + преподаватели): 3", text)
        # В обычном тексте VK экранирование не нужно: название выводится как есть.
        self.assertIn("<b>ИСП-21</b>", text)

    def test_long_ignored_list_is_truncated_and_fits_message_limits(self) -> None:
        catalog = [f"ИСП-{year:02d}-{number}" for year in (10, 11, 12, 13, 14) for number in range(1, 6)]
        report = build_course_stats(catalog, [], today=TODAY)

        text = format_course_stats_report(report, html=True)

        self.assertIn("Не учтено: 25 групп", text)
        self.assertIn("и ещё 15", text)
        self.assertLess(len(text), 4096)


class CollectCourseStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_collects_catalog_subscriptions_and_teacher_directory_from_db(self) -> None:
        await self.db.save_groups(
            [
                {
                    "schedule_id": schedule_id,
                    "group_name": name,
                    "department_id": 1,
                    "department_code": "",
                    "department_name": "",
                    "url": "",
                }
                for schedule_id, name in ((1, "ТЭС-26"), (2, "ИСП-25-1"), (3, "ИСП-21-1"))
            ]
        )
        await self.db.save_search_targets(
            "teacher",
            [{"title": "Иванов И. И.", "url": "http://x/1"}, {"title": "Петров П. П.", "url": "http://x/2"}],
        )
        await self.db.upsert_user("telegram", 1001, "a", "A")
        await self.db.set_user_subscription("telegram", 1001, **make_group_subscription("ИСП-25-1", schedule_id=2))
        await self.db.upsert_user("vk", 2001, "b", "B")
        await self.db.set_user_subscription("vk", 2001, **make_group_subscription("ТЭС-26", schedule_id=1))
        await self.db.upsert_user("telegram", 1002, "c", "C")
        await self.db.set_user_subscription("telegram", 1002, "teacher", "teacher:1", "Иванов И. И.", "http://x/1")
        await self.db.upsert_user("telegram", 1003, "d", "D")

        report = await collect_course_stats(self.db, today=TODAY)

        self.assertEqual(report.courses[0].audience.vk_personal, 1)
        self.assertEqual(report.courses[1].audience.tg_personal, 1)
        self.assertEqual([g.name for g in report.ignored_groups], ["ИСП-21-1"])
        self.assertEqual(report.teachers.known, 2)
        self.assertEqual(report.teachers.audience.personal, 1)
        # Пользователь без подписки никуда не попадает.
        self.assertEqual(report.everyone_personal, 3)

    async def test_works_on_empty_database(self) -> None:
        report = await collect_course_stats(self.db, today=TODAY)

        self.assertEqual(report.courses_audience.personal, 0)
        self.assertEqual(report.teachers.known, 0)


def current_group(course: int) -> str:
    """Название группы, которая прямо сейчас учится на указанном курсе."""
    admission_year = current_academic_year(datetime.now().date()) - course + 1
    return f"ТЭС-{admission_year % 100:02d}"


FULL_ADMIN_ID = 111
LIMITED_ADMIN_ID = 222
VK_ADMIN_ID = 504200420


class TelegramCourseStatsButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.db = Database(self.db_path)
        await self.db.initialize()
        for user_id in (FULL_ADMIN_ID, LIMITED_ADMIN_ID):
            await self.db.upsert_user("telegram", user_id, None, "Админ")
        await self.db.upsert_user("telegram", 5001, None, "Студент")
        await self.db.set_user_subscription("telegram", 5001, **make_group_subscription(current_group(1), schedule_id=1))

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _handler(self):
        settings = MagicMock(spec=Settings)
        settings.telegram_bot_token = "123456:FAKE-TOKEN"
        settings.vk_bot_token = "vk_fake_token"
        settings.vk_disable_ssl_verify = False
        settings.admin_telegram_ids = [FULL_ADMIN_ID]
        settings.limited_admin_telegram_ids = [LIMITED_ADMIN_ID]
        settings.admin_vk_id = None
        settings.schedule_url = "http://localhost/schedule"
        settings.database_path = self.db_path
        settings.lesson_counters_enabled = False
        settings.lesson_counters_path = Path(self._tmp.name) / "lesson_counters.json"
        dispatcher = build_dispatcher(
            settings=settings, db=self.db,
            parser=MagicMock(), broadcaster=MagicMock(),
            group_catalog=MagicMock(), search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )
        for handler in dispatcher.callback_query.handlers:
            if handler.callback.__name__ == "handle_admin_callback":
                return handler.callback
        raise AssertionError("handle_admin_callback не зарегистрирован")

    @staticmethod
    def _callback(data: str, user_id: int):
        message = SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=7, edit_text=AsyncMock())
        return SimpleNamespace(
            data=data,
            message=message,
            from_user=SimpleNamespace(id=user_id, first_name="Админ", last_name=None, username=None),
            bot=AsyncMock(),
            answer=AsyncMock(),
        )

    def test_button_lives_in_monitoring_section(self) -> None:
        callbacks = [b.callback_data for row in ADMIN_SECTION_KEYBOARDS["monitor"].inline_keyboard for b in row]
        self.assertIn("admin:course_stats", callbacks)

    async def test_full_admin_gets_course_report_with_back_button(self) -> None:
        callback = self._callback("admin:course_stats", FULL_ADMIN_ID)

        await self._handler()(callback)

        call = callback.message.edit_text.await_args
        self.assertIn("<b>Статистика по курсам</b>", call.args[0])
        self.assertIn("<b>1 курс</b>", call.args[0])
        self.assertIn("<b>Итого за 4 курса</b>: <b>1</b> студ.", call.args[0])
        buttons = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(buttons, ["admin:back"])

    async def test_limited_admin_can_read_course_report(self) -> None:
        callback = self._callback("admin:course_stats", LIMITED_ADMIN_ID)

        await self._handler()(callback)

        self.assertIn("Статистика по курсам", callback.message.edit_text.await_args.args[0])

    async def test_non_admin_is_rejected(self) -> None:
        callback = self._callback("admin:course_stats", 5001)

        await self._handler()(callback)

        callback.message.edit_text.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))


class VkCourseStatsButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.db = Database(self.db_path)
        await self.db.initialize()
        await self.db.upsert_user("vk", VK_ADMIN_ID, None, "Админ")
        # Без подписки VK-бот трактует любой текст как ввод названия группы.
        await self.db.set_user_subscription("vk", VK_ADMIN_ID, **make_group_subscription(current_group(2), schedule_id=2))

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _bot(self):
        settings = MagicMock(spec=Settings)
        settings.telegram_bot_token = "123456:FAKE-TOKEN"
        settings.vk_bot_token = "vk_fake_token"
        settings.vk_disable_ssl_verify = False
        settings.admin_telegram_ids = []
        settings.limited_admin_telegram_ids = []
        settings.admin_vk_id = VK_ADMIN_ID
        settings.schedule_url = "http://localhost/schedule"
        settings.database_path = self.db_path
        settings.lesson_counters_enabled = False
        settings.lesson_counters_path = Path(self._tmp.name) / "lesson_counters.json"
        bot = build_vk_bot(
            settings=settings, db=self.db,
            parser=MagicMock(), broadcaster=None,
            group_catalog=None, search_catalog=None, schedule_jobs=None,
        )

        async def fake_request(method: str, params: dict, version: str | None = None):
            if method == "users.get":
                return {"response": []}
            if method == "messages.send":
                return {"response": [{"peer_id": 0, "message_id": 1}]}
            return {"response": None}

        bot.api.request = AsyncMock(side_effect=fake_request)
        return bot

    @staticmethod
    def _handler(bot):
        for handler in bot.labeler.message_view.handlers:
            if getattr(handler.handler, "__name__", "") == "all_messages_handler":
                return handler.handler
        raise AssertionError("all_messages_handler не зарегистрирован")

    @staticmethod
    def _message(text: str) -> SimpleNamespace:
        return SimpleNamespace(peer_id=VK_ADMIN_ID, from_id=VK_ADMIN_ID, text=text, action=None, attachments=[])

    @staticmethod
    def _last_screen(bot) -> tuple[str, list[str]]:
        params = [c.args[1] for c in bot.api.request.call_args_list if c.args[0] == "messages.send"][-1]
        keyboard = json.loads(params["keyboard"]) if params.get("keyboard") else {"buttons": []}
        labels = [b["action"]["label"] for row in keyboard["buttons"] for b in row]
        return params["message"], labels

    def test_button_lives_in_monitoring_section(self) -> None:
        labels = {label for row in VK_ADMIN_SECTIONS["Мониторинг"]["rows"] for label in row}
        self.assertIn("Статистика по курсам", labels)

    async def test_admin_opens_course_report_from_monitoring(self) -> None:
        bot = self._bot()
        handler = self._handler(bot)
        await handler(self._message("Админка"))
        await handler(self._message("Мониторинг"))
        _, section_labels = self._last_screen(bot)
        self.assertIn("Статистика по курсам", section_labels)

        await handler(self._message("Статистика по курсам"))

        text, labels = self._last_screen(bot)
        self.assertIn("Статистика по курсам", text)
        self.assertNotIn("<b>", text)
        self.assertIn("2 курс", text)
        self.assertIn("Итого за 4 курса: 1 студ.", text)
        self.assertEqual(labels, ["Назад в админку"])


class CoursesWebPageTests(unittest.TestCase):
    """Страница /courses: права доступа и вывод. Приложение импортируем на временной БД."""

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls._env = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(base / "web.db"),
                "WEB_CONFIG_SECRET": "x" * 40,
                "WEB_SUPERUSER_LOGIN": "root",
                "WEB_SUPERUSER_PASSWORD": "correct-horse-battery",
                "WEB_USERS_JSON_IMPORT_PATH": str(base / "no_users.json"),
                "LESSON_COUNTERS_PATH": str(base / "lesson_counters.json"),
            },
        )
        cls._env.start()
        sys.modules.pop("web_configurator.app", None)
        cls.app_module = importlib.import_module("web_configurator.app")

        async def seed() -> None:
            db = Database(base / "web.db")
            await db.initialize()
            await db.upsert_user("telegram", 1, "a", "A")
            await db.set_user_subscription("telegram", 1, **make_group_subscription("ИСП-25-1", schedule_id=2))
            await db.upsert_user("vk", 2, "b", "B")
            await db.set_user_subscription("vk", 2, **make_group_subscription("ТЭС-26", schedule_id=1))
            await db.upsert_user("telegram", 3, "c", "C")
            await db.set_user_subscription("telegram", 3, "group", "group:9", "<script>alert(1)</script>", "rasp:9")

        asyncio.run(seed())

        auth_store = cls.app_module.auth_store
        auth_store.upsert_user("viewer", "viewer-password-1", ["stats_courses"])
        auth_store.upsert_user("other", "other-password-1", ["stats_users"])

        real_collect = cls.app_module.collect_course_stats

        async def collect_on_fixed_date(db):
            return await real_collect(db, today=TODAY)

        cls._collect_patch = patch.object(cls.app_module, "collect_course_stats", collect_on_fixed_date)
        cls._collect_patch.start()
        cls.client = TestClient(cls.app_module.app, follow_redirects=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls._collect_patch.stop()
        sys.modules.pop("web_configurator.app", None)
        cls._env.stop()
        cls._tmp.cleanup()

    def _get(self, login: str | None):
        headers = {}
        if login is not None:
            headers["Cookie"] = f"{self.app_module.SESSION_COOKIE_NAME}={self.app_module.signer.sign(login)}"
        return self.client.get("/courses", headers=headers)

    def test_anonymous_is_redirected_to_login(self) -> None:
        response = self._get(None)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_user_without_permission_gets_403(self) -> None:
        self.assertEqual(self._get("other").status_code, 403)

    def test_permission_is_registered_for_web_users_screen(self) -> None:
        from web_configurator.security import ALL_PERMISSIONS, PERMISSION_LABELS

        self.assertIn("stats_courses", ALL_PERMISSIONS)
        self.assertIn("stats_courses", PERMISSION_LABELS)
        self.assertIn("stats_courses", self.app_module.PERMISSION_DESCRIPTIONS)
        self.assertEqual(
            sorted(set(ALL_PERMISSIONS) - set(self.app_module.PERMISSION_DESCRIPTIONS)),
            [],
        )

    def test_permitted_user_sees_course_statistics(self) -> None:
        response = self._get("viewer")

        self.assertEqual(response.status_code, 200)
        page = response.text
        self.assertIn("Статистика по курсам", page)
        self.assertIn("Учебный год 2026/2027", page)
        self.assertIn("<span>1 курс</span><b>1</b>", page)
        self.assertIn("<span>2 курс</span><b>1</b>", page)
        self.assertIn("<span>3 курс</span><b>0</b>", page)
        self.assertIn("<span>Всего за 4 курса</span><b>2</b>", page)
        self.assertIn("ИСП-25-1", page)
        self.assertIn("Преподаватели: в справочнике сайта 0, из них с подписчиками в боте — 0.", page)
        self.assertIn("href='/courses'", page)

    def test_group_names_from_users_are_html_escaped(self) -> None:
        page = self._get("viewer").text

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_superuser_sees_page_and_menu_item_is_hidden_without_permission(self) -> None:
        self.assertEqual(self._get("root").status_code, 200)

        index = self.client.get(
            "/",
            headers={"Cookie": f"{self.app_module.SESSION_COOKIE_NAME}={self.app_module.signer.sign('other')}"},
        )
        self.assertEqual(index.status_code, 200)
        self.assertNotIn("href='/courses'", index.text)


if __name__ == "__main__":
    unittest.main()

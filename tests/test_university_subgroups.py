from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot

from src.config import Settings
from src.db import Database
from src.delivery_outbox import DeliveryOutbox
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.notifier import Broadcaster
from src.parser import ScheduleParser, compute_snapshot_hash
from src.schedule_search import SearchTarget
from src.schedule_service import ScheduleComparator, ScheduleFormatter
from src.scheduler import ScheduleJobs
from src.subscription_utils import make_group_subscription
from src.telegram_bot import build_dispatcher
from src.university_subgroups import available_subgroups, filter_content, filter_day, lesson_subgroups, personal_subgroup
from src.vk_bot import build_vk_bot

GROUP_ID = -3910
GROUP_URL = "http://r.sf-misis.ru/group/3910"


def snapshot():
    today = datetime.now().date().isoformat()
    return ScheduleSnapshot("ИТ-26д", datetime.now(), [DaySchedule(today, today, [
        Lesson(1, "Общая лекция", "Преподаватель", "1/510"),
        Lesson(3, "Физика (Лабораторная работа, 1 подгруппа)", "Первый", "4/411"),
        Lesson(3, "Математика (Лабораторная работа, 2 подгруппа)", "Второй", "2/204"),
        Lesson(3, "Право (Семинар, 3 подгруппа)", "Третий", "1/404"),
    ], 1, 1)])


class SubgroupFilteringTests(unittest.TestCase):
    def test_common_and_own_lessons_are_kept_without_mutating_full_schedule(self):
        day = snapshot().days[0]
        filtered = filter_day(day, 1)
        self.assertEqual([lesson.number for lesson in filtered.lessons], [1, 3])
        self.assertIn("Общая лекция", filtered.lessons[0].subject)
        self.assertIn("Физика", filtered.lessons[1].subject)
        self.assertEqual(len(day.lessons), 4)
        self.assertEqual((filtered.week_variant, filtered.current_week_variant), (1, 1))
        self.assertEqual(available_subgroups(snapshot().to_dict()), [1, 2, 3])

    def test_shared_subgroup_lists_and_ranges_are_supported(self):
        self.assertEqual(lesson_subgroups("Физика (1 и 2 подгруппа)"), {1, 2})
        self.assertEqual(lesson_subgroups("Физика (1, 3 подгруппы)"), {1, 3})
        self.assertEqual(lesson_subgroups("Физика (1–3 подгруппы)"), {1, 2, 3})
        self.assertEqual(lesson_subgroups("Теория групп (Лекция)"), set())

    def test_personal_render_is_filtered_but_search_keeps_all_subgroups(self):
        full = snapshot()
        for text in (ScheduleFormatter.format_day_card(full.days[0], "", subgroup=1),
                     ScheduleFormatter.format_day_plain(full.days[0], subgroup=1)):
            self.assertIn("Физика", text)
            self.assertIn("Общая лекция", text)
            self.assertNotIn("Математика", text)
            self.assertNotIn("Право (", text)
        for html in (False, True):
            text = ScheduleFormatter.format_search_snapshot("ИТ-26д", full.to_dict(), html=html)
            for subject in ("Общая лекция", "Физика", "Математика", "Право"):
                self.assertIn(subject, text)
        self.assertIs(filter_day(full.days[0], None), full.days[0])

    def test_university_teacher_does_not_get_student_filter(self):
        user = SimpleNamespace(subscription_type="teacher", schedule_id=None, university_subgroup=1)
        self.assertIsNone(personal_subgroup(user))

    def test_cancellation_notifies_own_subgroup_and_does_not_invent_empty_changes(self):
        before = snapshot()
        before.days[0].lessons = before.days[0].lessons[1:]
        after = copy.deepcopy(before)
        after.days[0].lessons = after.days[0].lessons[1:]
        baseline = {"content": before.to_dict()}
        change = ScheduleComparator.compare(baseline, after, subgroup=1, notify_cancellations=True)
        self.assertIsNotNone(change)
        self.assertIn("Пар нет.", change.message)
        self.assertIsNone(ScheduleComparator.compare(baseline, after, subgroup=2, notify_cancellations=True))
        self.assertIsNone(ScheduleComparator.compare({"content": filter_content(before.to_dict(), 9)}, after,
                                                    subgroup=9, notify_cancellations=True))


class SubgroupBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")
        await self.db.initialize()
        self.full = snapshot()
        self.parser = ScheduleParser("http://college/rasp")
        self.parser.parse_university_variant = AsyncMock(return_value=copy.deepcopy(self.full))
        self.parser.parse_from_url = AsyncMock(return_value=(self.full, "hash"))
        self.settings = Settings.from_env()
        self.settings.database_path = self.db.path
        self.settings.telegram_bot_token = "123456:FAKE-TOKEN"
        self.settings.vk_bot_token = "fake-token"
        self.settings.admin_telegram_ids = []
        self.settings.limited_admin_telegram_ids = []
        self.settings.admin_vk_id = None
        self.settings.ocr_enabled = False
        self.settings.lesson_counters_enabled = False
        self.catalog = MagicMock(find_all=AsyncMock(return_value=[SearchTarget("group", "ИТ-26д", GROUP_URL)]))
        for platform in ("telegram", "vk"):
            await self.db.upsert_user(platform, 42, None, "Тест")
            await self.db.set_user_subscription(platform, 42, **make_group_subscription("ИТ-26д", GROUP_ID))
        await self.db.save_snapshot("current", "full", self.full, schedule_id=GROUP_ID, group_name="ИТ-26д", source_key=f"group:{GROUP_ID}")
        for module in ("src.telegram_bot", "src.vk_bot"):
            limiter = patch(f"{module}.monotonic", side_effect=iter(range(1_000_000)))
            limiter.start()
            self.addCleanup(limiter.stop)
        self.dispatcher = self.telegram()
        self.bot = AsyncMock()
        self.bot.send_message.return_value = SimpleNamespace(message_id=1)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def telegram(self):
        return build_dispatcher(self.settings, self.db, self.parser, None, None, self.catalog)

    def message(self, text=""):
        return SimpleNamespace(text=text, bot=self.bot, delete=AsyncMock(), chat=SimpleNamespace(id=42, type="private"),
                               from_user=SimpleNamespace(id=42, first_name="Тест", last_name=None, username=None))

    async def callback(self, name, data, *, chat_id=42):
        handler = next(item.callback for item in self.dispatcher.callback_query.handlers if item.callback.__name__ == name)
        message = self.message()
        message.chat.id = chat_id
        callback = SimpleNamespace(data=data, message=message, from_user=message.from_user, bot=self.bot, answer=AsyncMock())
        await handler(callback)
        return callback

    async def test_database_preference_survives_restart_and_registration_but_resets_on_new_group(self):
        self.assertTrue(await self.db.set_university_subgroup("telegram", 42, GROUP_ID, 1))
        await self.db.upsert_user("telegram", 42, None, "Снова пишет")
        self.assertEqual((await Database(self.db.path).get_user("telegram", 42)).university_subgroup, 1)
        self.assertEqual((await self.db.list_users("telegram"))[0].university_subgroup, 1)
        self.assertFalse(await self.db.set_university_subgroup("telegram", 42, -999, 2))
        with self.assertRaises(ValueError):
            await self.db.set_university_subgroup("telegram", 42, GROUP_ID, 0)
        await self.db.set_user_subscription("telegram", 42, **make_group_subscription("ИСП-25-1", 600))
        self.assertIsNone((await self.db.get_user("telegram", 42)).university_subgroup)
        self.assertFalse(await self.db.set_university_subgroup("telegram", 42, 600, 1))

    async def test_telegram_start_choice_and_personal_schedule_survive_new_dispatcher(self):
        handler = next(item.callback for item in self.dispatcher.message.handlers if item.callback.__name__ == "handle_start")
        await handler(self.message("/start"))
        keyboard = self.bot.send_message.call_args.kwargs["reply_markup"]
        self.assertIn("menu:subgroup", [button.callback_data for row in keyboard.inline_keyboard for button in row])
        await self.callback("handle_university_subgroup", "menu:subgroup")
        keyboard = self.bot.send_message.call_args.kwargs["reply_markup"]
        self.assertIn("3 подгруппа", [button.text for row in keyboard.inline_keyboard for button in row])
        await self.callback("handle_university_subgroup", f"subgroup:{GROUP_ID}:1")
        self.dispatcher = self.telegram()
        await self.callback("handle_schedule_callback", "schedule:today")
        text = self.bot.send_message.call_args.args[1]
        self.assertIn("Общая лекция", text)
        self.assertIn("Физика", text)
        self.assertNotIn("Математика", text)
        day = self.full.days[0].date_iso
        await self.callback("handle_schedule_callback", f"schedule:week:{day}:2")
        self.assertNotIn("Математика", self.bot.send_message.call_args.args[1])
        await self.callback("handle_university_subgroup", f"subgroup:{GROUP_ID}:all")
        await self.callback("handle_schedule_callback", "schedule:today")
        self.assertIn("Математика", self.bot.send_message.call_args.args[1])
        self.assertEqual((await self.db.get_latest_snapshot("current", source_key=f"group:{GROUP_ID}"))["snapshot_hash"], "full")

    async def test_telegram_search_shows_all_even_with_personal_subgroup(self):
        await self.db.set_university_subgroup("telegram", 42, GROUP_ID, 1)
        await self.callback("handle_schedule_callback", "schedule:find")
        handler = next(item.callback for item in self.dispatcher.message.handlers if item.callback.__name__ == "handle_text_message")
        await handler(self.message("ИТ-26д"))
        text = self.bot.send_message.call_args.args[1]
        self.assertIn("Математика", text)
        self.assertIn("3 подгруппа", text)

    async def test_stale_invalid_or_shared_chat_callbacks_do_not_change_preference(self):
        await self.db.set_university_subgroup("telegram", 42, GROUP_ID, 1)
        for data, chat in (("subgroup:-999:2", 42), (f"subgroup:{GROUP_ID}:99", 42), (f"subgroup:{GROUP_ID}:2", -100)):
            await self.callback("handle_university_subgroup", data, chat_id=chat)
            self.assertEqual((await self.db.get_user("telegram", 42)).university_subgroup, 1)

    async def test_vk_choice_personal_day_and_full_search(self):
        bot = build_vk_bot(self.settings, self.db, self.parser, None, search_catalog=self.catalog)
        async def request(method, params, version=None):
            return {"response": [] if method == "users.get" else 1}

        bot.api.request = AsyncMock(side_effect=request)
        handler = next(item.handler for item in bot.labeler.message_view.handlers if item.handler.__name__ == "all_messages_handler")

        async def send(text):
            await handler(SimpleNamespace(peer_id=42, from_id=42, text=text, action=None, attachments=[]))
            return [call.args[1] for call in bot.api.request.await_args_list if call.args[0] == "messages.send"][-1]

        await send("Выбрать подгруппу")
        await send("1 подгруппа")
        self.assertEqual((await self.db.get_user("vk", 42)).university_subgroup, 1)
        text = (await send("Расписание на сегодня"))["message"]
        self.assertIn("Физика", text)
        self.assertIn("Общая лекция", text)
        self.assertNotIn("Математика", text)
        await send("Найти расписание")
        self.assertIn("Математика", (await send("ИТ-26д"))["message"])
        await send("Все подгруппы")
        self.assertIsNone((await self.db.get_user("vk", 42)).university_subgroup)


class SubgroupNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "notify.db")
        await self.db.initialize()
        self.before = snapshot()
        for user_id, subgroup in ((1, 1), (2, 2), (3, None)):
            await self.db.upsert_user("telegram", user_id, None, "Студент")
            await self.db.set_user_subscription("telegram", user_id, **make_group_subscription("ИТ-26д", GROUP_ID))
            await self.db.set_university_subgroup("telegram", user_id, GROUP_ID, subgroup)
        self.outbox = DeliveryOutbox(self.db)
        self.bot = Bot("123456:FAKE-TOKEN")
        self.jobs = ScheduleJobs.__new__(ScheduleJobs)
        self.jobs.db = self.db
        self.jobs.broadcaster = Broadcaster(self.db, telegram_bot=self.bot, outbox=self.outbox)
        self.jobs._notify_affected_teachers = AsyncMock()
        self.source = dict(source_type="group", source_key=f"group:{GROUP_ID}", source_title="ИТ-26д", source_url=GROUP_URL,
                           group_name="ИТ-26д", schedule_id=GROUP_ID)

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.tmp.cleanup()

    async def apply(self, after):
        await self.db.save_snapshot("daily_baseline", "before", self.before, schedule_id=GROUP_ID, group_name="ИТ-26д", source_key=f"group:{GROUP_ID}")
        await self.jobs.apply_snapshot(self.source, after, compute_snapshot_hash(after))
        async with self.db._connect() as db:
            rows = await (await db.execute("SELECT payload_json FROM delivery_outbox WHERE method = 'sendMessage'")).fetchall()
        return {value["chat_id"]: value["text"] for row in rows for value in [json.loads(row[0])]}

    async def test_other_subgroup_change_skips_unaffected_students_and_keeps_full_notifications(self):
        after = copy.deepcopy(self.before)
        after.days[0].lessons[2].classroom = "Новый кабинет"
        messages = await self.apply(after)
        self.assertEqual(set(messages), {2, 3})
        self.assertIn("Новый кабинет", messages[2])
        self.assertNotIn("Физика", messages[2])
        self.assertIn("Физика", messages[3])

    async def test_common_lesson_change_notifies_all_preferences(self):
        after = copy.deepcopy(self.before)
        after.days[0].lessons[0].teacher = "Новый преподаватель"
        messages = await self.apply(after)
        self.assertEqual(set(messages), {1, 2, 3})
        self.assertTrue(all("Новый преподаватель" in value for value in messages.values()))

    async def test_cancelled_last_personal_lesson_notifies_no_classes(self):
        self.before.days[0].lessons = self.before.days[0].lessons[1:]
        after = copy.deepcopy(self.before)
        after.days[0].lessons = after.days[0].lessons[1:]
        messages = await self.apply(after)
        self.assertEqual(set(messages), {1, 3})
        self.assertIn("Пар нет.", messages[1])

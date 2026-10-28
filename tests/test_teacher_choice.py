from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.schedule_search import ScheduleSearchCatalog
from src.subscription_utils import make_group_subscription
from src.telegram_bot import SEARCH_NOT_FOUND_TEXT, build_dispatcher
from src.vk_bot import build_vk_bot

USER_ID = 101
NEW_USER_ID = 202
PAIRS = [
    ("Иванов И.И.", "http://test/raspprep/1"),
    ("Иванов В.В.", "http://test/raspprep/2"),
    ("Швабрин К.К.", "http://test/raspprep/3"),
]


class TeacherChoiceScenarios:
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "test.db"
        self.db = Database(db_path)
        await self.db.initialize()
        for user_id in (USER_ID, NEW_USER_ID):
            await self.db.upsert_user(self.platform, user_id, None, "Тест")
        await self.db.set_user_subscription(self.platform, USER_ID, **make_group_subscription("ИСП-25-1", 600))
        today = datetime.now()
        await self.db.save_snapshot(
            "current", "hash", ScheduleSnapshot(
                group_name="ИСП-25-1", fetched_at=today,
                days=[DaySchedule(date_label=today.strftime("%d.%m.%Y"), date_iso=today.date().isoformat(), lessons=[
                    Lesson(number=1, subject="Математика", teacher="Иванов И.И.", classroom="301"),
                    Lesson(number=2, subject="Физика", teacher="Иванов В.В.", classroom="302"),
                ])],
            ), schedule_id=600, group_name="ИСП-25-1", source_type="group", source_key="group:600",
        )
        self.groups = MagicMock(spec=GroupCatalog)
        self.groups.find_group = AsyncMock(return_value=None)
        self.groups.last_error = None
        self.catalog = ScheduleSearchCatalog("http://test", self.groups, db=self.db)
        self.catalog._fetch_pairs = AsyncMock(side_effect=lambda path, selector: PAIRS if path == "/prep" else [])
        settings = MagicMock(spec=Settings)
        settings.telegram_bot_token = "123456:FAKE-TOKEN"
        settings.vk_bot_token = "fake-token"
        settings.vk_disable_ssl_verify = False
        settings.admin_telegram_ids = []
        settings.limited_admin_telegram_ids = []
        settings.admin_vk_id = None
        settings.schedule_url = "http://test"
        settings.database_path = db_path
        settings.lesson_counters_enabled = False
        settings.lesson_counters_path = Path(self.tmp.name) / "counters.json"
        # Убираем только задержки антифлуда; обработчики, поиск и SQLite настоящие.
        for module in ("src.telegram_bot", "src.vk_bot"):
            limiter_clock = patch(f"{module}.monotonic", side_effect=iter(range(1_000_000)))
            limiter_clock.start()
            self.addCleanup(limiter_clock.stop)
        kwargs = dict(settings=settings, db=self.db, parser=MagicMock(), broadcaster=None,
                      group_catalog=self.groups, search_catalog=self.catalog, schedule_jobs=None)
        if self.platform == "telegram":
            self.dispatcher = build_dispatcher(**kwargs)
            self.bot = AsyncMock()
            self.bot.send_message.return_value = SimpleNamespace(message_id=1)
        else:
            self.bot = build_vk_bot(**kwargs)

            async def fake_request(method, params, version=None):
                return {"response": [] if method == "users.get" else 1}

            self.bot.api.request = AsyncMock(side_effect=fake_request)

    def tg_handler(self, name, *, callback=False):
        observer = self.dispatcher.callback_query if callback else self.dispatcher.message
        return next(item.callback for item in observer.handlers if item.callback.__name__ == name)

    async def send(self, text, user_id=USER_ID):
        message = SimpleNamespace(
            text=text, chat=SimpleNamespace(id=user_id, type="private"),
            from_user=SimpleNamespace(id=user_id, first_name="Тест", last_name=None, username=None),
            bot=self.bot, delete=AsyncMock(), peer_id=user_id, from_id=user_id, action=None, attachments=[],
        )
        if self.platform == "telegram":
            await self.tg_handler("handle_text_message")(message)
        else:
            handler = next(item.handler for item in self.bot.labeler.message_view.handlers
                           if item.handler.__name__ == "all_messages_handler")
            await handler(message)

    def screen(self):
        if self.platform == "telegram":
            call = self.bot.send_message.await_args_list[-1]
            markup = call.kwargs.get("reply_markup")
            buttons = [button for row in markup.inline_keyboard for button in row] if markup else []
            return call.args[1], {button.text: button.callback_data for button in buttons}
        params = [call.args[1] for call in self.bot.api.request.await_args_list if call.args[0] == "messages.send"][-1]
        markup = json.loads(params["keyboard"]) if params.get("keyboard") else {"buttons": []}
        assert len(markup["buttons"]) <= 10
        return params["message"], {button["action"]["label"]: None for row in markup["buttons"] for button in row}

    async def callback(self, data, user_id=USER_ID):
        callback = SimpleNamespace(
            data=data, message=SimpleNamespace(chat=SimpleNamespace(id=user_id)),
            from_user=SimpleNamespace(id=user_id, first_name="Тест", last_name=None, username=None),
            bot=self.bot, answer=AsyncMock(),
        )
        name = "handle_teacher_choice" if data.startswith("teacher:") else (
            "handle_menu_start" if data == "menu:start" else "handle_schedule_callback"
        )
        await self.tg_handler(name, callback=True)(callback)
        return callback

    async def press(self, label, user_id=USER_ID):
        if self.platform == "telegram":
            await self.callback(self.screen()[1][label], user_id)
        else:
            await self.send(label, user_id)

    async def search(self, query="Иванов"):
        if self.platform == "telegram":
            await self.callback("schedule:find")
        else:
            await self.send("Найти расписание")
        await self.send(query)

    async def test_ambiguous_search_then_pick_shows_only_chosen_teacher(self):
        await self.search()
        text, buttons = self.screen()
        self.assertIn("Какой именно нужен", text)
        self.assertIn("Иванов И.И.", buttons)
        self.assertIn("Иванов В.В.", buttons)
        await self.press("Иванов В.В.")
        text, _ = self.screen()
        self.assertIn("Иванов В.В.", text)
        self.assertIn("Физика", text)
        self.assertNotIn("Математика", text)
        user = await self.db.get_user(self.platform, USER_ID)
        self.assertEqual(user.subscription_key, "group:600")

    async def test_subscription_is_saved_only_after_choice(self):
        await self.send("Иванов", NEW_USER_ID)
        self.assertIn("Иванов В.В.", self.screen()[1])
        user = await self.db.get_user(self.platform, NEW_USER_ID)
        self.assertFalse(user.subscription_key)
        await self.press("Иванов В.В.", NEW_USER_ID)
        user = await self.db.get_user(self.platform, NEW_USER_ID)
        self.assertEqual(user.subscription_key, "teacher:2")
        self.assertEqual(user.subscription_title, "Иванов В.В.")

    async def test_unique_surname_works_without_choice(self):
        await self.search("Швабрин")
        self.assertIn("Швабрин К.К.", self.screen()[0])
        self.assertNotIn("Какой именно нужен", self.screen()[0])
        await self.send("Швабрин", NEW_USER_ID)
        user = await self.db.get_user(self.platform, NEW_USER_ID)
        self.assertEqual(user.subscription_key, "teacher:3")

    async def test_initials_work_without_choice(self):
        await self.search("иванов и. и.")
        self.assertIn("Математика", self.screen()[0])
        self.assertNotIn("Физика", self.screen()[0])

    async def test_query_can_be_refined_after_choices(self):
        await self.search()
        await self.send("Иванов И.И.")
        self.assertIn("Математика", self.screen()[0])

    async def test_missing_teacher_shows_not_found(self):
        await self.search("Несуществующий")
        self.assertIn(SEARCH_NOT_FOUND_TEXT, self.screen()[0])

    async def test_back_cancels_choice(self):
        await self.search()
        await self.press("Назад" if self.platform == "telegram" else "Назад в меню")
        self.assertIn("расписания колледжа", self.screen()[0])
        user = await self.db.get_user(self.platform, USER_ID)
        self.assertEqual(user.subscription_key, "group:600")

    async def test_all_candidates_are_reachable_with_pagination(self):
        pairs = [(f"Иванов {letter}.{letter}.", f"http://test/raspprep/{index}")
                 for index, letter in enumerate("АБВГДЕЖЗИКЛМН", 10)]
        self.catalog._populate_preps(pairs)
        self.catalog._preps_loaded = True
        await self.search()
        first_page = set(self.screen()[1])
        self.assertEqual(len(first_page & {title for title, _ in pairs}), 8)
        await self.press("Следующая страница")
        second_page = set(self.screen()[1])
        self.assertEqual((first_page | second_page) & {title for title, _ in pairs}, {title for title, _ in pairs})
        await self.press("Предыдущая страница")
        self.assertEqual(set(self.screen()[1]), first_page)
        await self.press("Следующая страница")
        await self.press(pairs[-1][0])
        self.assertIn(pairs[-1][0], self.screen()[0])

    async def test_choices_use_group_snapshots_when_site_is_down(self):
        self.catalog._fetch_pairs.side_effect = RuntimeError("сайт недоступен")
        await self.send("Иванов", NEW_USER_ID)
        self.assertIn("Иванов В.В.", self.screen()[1])
        await self.press("Иванов В.В.", NEW_USER_ID)
        user = await self.db.get_user(self.platform, NEW_USER_ID)
        self.assertEqual(user.subscription_type, "teacher")
        self.assertTrue(user.subscription_key.startswith("teacher-pending:"))
        self.assertEqual(user.subscription_title, "Иванов В.В.")


class TelegramTeacherChoiceTests(TeacherChoiceScenarios, unittest.IsolatedAsyncioTestCase):
    platform = "telegram"

    async def test_old_button_cannot_select_from_a_new_query(self):
        await self.search()
        old_button = self.screen()[1]["Иванов В.В."]
        await self.send("Иванов")
        callback = await self.callback(old_button)
        self.assertIn("устарел", callback.answer.await_args.args[0])
        self.assertIn("Какой именно нужен", self.screen()[0])

    async def test_other_user_cannot_use_choice(self):
        await self.search()
        button = self.screen()[1]["Иванов В.В."]
        callback = await self.callback(button, NEW_USER_ID)
        self.assertIn("устарел", callback.answer.await_args.args[0])
        user = await self.db.get_user(self.platform, NEW_USER_ID)
        self.assertFalse(user.subscription_key)

    async def test_back_invalidates_old_button(self):
        await self.search()
        old_button = self.screen()[1]["Иванов В.В."]
        await self.press("Назад")
        callback = await self.callback(old_button)
        self.assertIn("устарел", callback.answer.await_args.args[0])


class VkTeacherChoiceTests(TeacherChoiceScenarios, unittest.IsolatedAsyncioTestCase):
    platform = "vk"

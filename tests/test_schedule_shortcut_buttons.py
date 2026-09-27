"""Регресс: кнопки "Расписание на сегодня/завтра/2 дня/звонки" в VK-боте раньше

обрабатывались только при peer_modes[peer_id] == "schedule_menu". peer_modes —
обычный dict в памяти процесса, без сохранения на диск, поэтому после любого
перезапуска бота (деплой, краш) он становится пустым для всех. Пользователь,
у которого на экране осталась старая VK-клавиатура с этими кнопками, нажимал
на них и тихо получал главное меню вместо расписания — без единой ошибки в
логах и без уведомления, потому что это не исключение, а просто непойманный
текст, который проваливался в дефолтный fallback.

В Telegram-боте эти же кнопки — inline-кнопки с callback_data вида
"schedule:today", обрабатываемые глобальным callback-хендлером без привязки
к какому-либо состоянию, поэтому аналогичный баг там невозможен по
конструкции. Тест ниже это подтверждает.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

from src.config import Settings
from src.db import Database
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.subscription_utils import make_group_subscription
from src.telegram_bot import build_dispatcher
from src.vk_bot import build_vk_bot


def _make_settings(db_path: Path) -> MagicMock:
    settings = MagicMock(spec=Settings)
    settings.telegram_bot_token = "123456:FAKE-TOKEN"
    settings.vk_bot_token = "vk_fake_token"
    settings.vk_disable_ssl_verify = False
    settings.admin_telegram_ids = []
    settings.limited_admin_telegram_ids = []
    settings.admin_vk_id = None
    settings.schedule_url = "http://localhost/schedule"
    settings.database_path = db_path
    return settings


class VkScheduleShortcutButtonsTests(unittest.IsolatedAsyncioTestCase):
    USER_ID = 504200420

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_vk_shortcuts.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

        await self.db.upsert_user(platform="vk", user_id=self.USER_ID, username=None, full_name="Тестовый пользователь")
        subscription = make_group_subscription("ИСП-25-1", schedule_id=600)
        await self.db.set_user_subscription("vk", self.USER_ID, **subscription)

        today = datetime.now().date().isoformat()
        snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime.now(),
            days=[
                DaySchedule(
                    date_iso=today,
                    date_label="Сегодня",
                    lessons=[Lesson(number=1, subject="Математика", teacher="Иванов И.И.", classroom="301")],
                )
            ],
        )
        await self.db.save_snapshot(
            "current",
            "test-hash",
            snapshot,
            schedule_id=600,
            group_name="ИСП-25-1",
            source_key=subscription["subscription_key"],
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _build_bot(self):
        parser = MagicMock()
        parser.parse = AsyncMock(side_effect=httpx.HTTPError("stub: no network access in tests"))
        bot = build_vk_bot(
            settings=_make_settings(self.db_path),
            db=self.db,
            parser=parser,
            broadcaster=None,
            group_catalog=None,
            search_catalog=None,
            schedule_jobs=None,
        )
        assert bot is not None

        async def fake_request(method: str, params: dict, version: str | None = None):
            if method == "users.get":
                return {"response": []}
            if method == "messages.send":
                return {"response": [{"peer_id": 0, "message_id": 1}]}
            return {"response": None}

        # bot.api.messages / bot.api.users пересоздаются заново на каждое
        # обращение (это просто тонкие обёртки), но все они держат ссылку на
        # один и тот же bot.api — поэтому мокаем именно точку входа .request,
        # через которую вызовы реально уходят в сеть VK.
        bot.api.request = AsyncMock(side_effect=fake_request)
        return bot

    @staticmethod
    def _get_message_handler(bot):
        for handler in bot.labeler.message_view.handlers:
            if getattr(handler.handler, "__name__", "") == "all_messages_handler":
                return handler.handler
        raise AssertionError("all_messages_handler не зарегистрирован в labeler'е VK-бота")

    @staticmethod
    def _fake_message(user_id: int, text: str) -> SimpleNamespace:
        return SimpleNamespace(peer_id=user_id, from_id=user_id, text=text, action=None, attachments=[])

    @staticmethod
    def _sent_texts(bot) -> list[str]:
        return [
            call.args[1]["message"]
            for call in bot.api.request.call_args_list
            if call.args[0] == "messages.send"
        ]

    async def test_today_shortcut_works_on_a_fresh_bot_process(self) -> None:
        """Свежий процесс бота = пустой peer_modes, ровно как после рестарта."""
        bot = self._build_bot()
        handler = self._get_message_handler(bot)

        await handler(self._fake_message(self.USER_ID, "Расписание на сегодня"))

        sent_text = self._sent_texts(bot)[-1]
        self.assertIn("Математика", sent_text)
        self.assertNotIn("Бот расписания колледжа", sent_text)

    async def test_tomorrow_and_two_days_shortcuts_also_work_on_a_fresh_process(self) -> None:
        bot = self._build_bot()
        handler = self._get_message_handler(bot)

        for text in ("Расписание на завтра", "Расписание на 2 дня"):
            bot.api.request.reset_mock()
            await handler(self._fake_message(self.USER_ID, text))
            sent_text = self._sent_texts(bot)[-1]
            self.assertNotIn("Бот расписания колледжа", sent_text, f"кнопка {text!r} не должна уходить в главное меню")

    async def test_bells_shortcut_works_on_a_fresh_process(self) -> None:
        bot = self._build_bot()
        handler = self._get_message_handler(bot)

        await handler(self._fake_message(self.USER_ID, "Расписание звонков"))

        sent_texts = self._sent_texts(bot)
        self.assertTrue(any("понедельник" in text.lower() or "звонк" in text.lower() for text in sent_texts))

    async def test_missing_snapshot_shows_explanatory_message_not_main_menu(self) -> None:
        """Даже без кэша снимка кнопка не должна тихо возвращать в главное меню."""
        other_user_id = 111222333
        await self.db.upsert_user(platform="vk", user_id=other_user_id, username=None, full_name="Без группы")
        subscription = make_group_subscription("ИСП-99-9", schedule_id=999)
        await self.db.set_user_subscription("vk", other_user_id, **subscription)

        bot = self._build_bot()
        handler = self._get_message_handler(bot)

        await handler(self._fake_message(other_user_id, "Расписание на сегодня"))

        sent_text = self._sent_texts(bot)[-1]
        self.assertIn("Не удалось получить расписание", sent_text)
        self.assertNotIn("Бот расписания колледжа", sent_text)


class TelegramScheduleShortcutButtonsTests(unittest.IsolatedAsyncioTestCase):
    """В TG эти кнопки — inline-кнопки с callback_data, а не reply-текст, и

    обрабатываются глобальным хендлером без state-фильтра, так что баг из
    VK-версии там структурно не воспроизводим. Тест фиксирует это свойство,
    чтобы будущий рефакторинг не привязал хендлер к какому-либо состоянию.
    """

    USER_ID = 504200420
    CHAT_ID = 504200420

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_tg_shortcuts.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

        await self.db.upsert_user(platform="telegram", user_id=self.USER_ID, username=None, full_name="Тестовый пользователь")
        subscription = make_group_subscription("ИСП-25-1", schedule_id=600)
        await self.db.set_user_subscription("telegram", self.USER_ID, **subscription)

        today = datetime.now().date().isoformat()
        snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime.now(),
            days=[
                DaySchedule(
                    date_iso=today,
                    date_label="Сегодня",
                    lessons=[Lesson(number=1, subject="Физика", teacher="Петров П.П.", classroom="202")],
                )
            ],
        )
        await self.db.save_snapshot(
            "current",
            "test-hash",
            snapshot,
            schedule_id=600,
            group_name="ИСП-25-1",
            source_key=subscription["subscription_key"],
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _get_schedule_callback_handler(dispatcher):
        for handler in dispatcher.callback_query.handlers:
            if handler.callback.__name__ == "handle_schedule_callback":
                return handler.callback
        raise AssertionError("handle_schedule_callback не зарегистрирован в dispatcher'е")

    def test_schedule_callback_has_no_state_filter(self) -> None:
        """Хендлер не должен зависеть ни от какого FSM/сессионного состояния —

        именно отсутствие такой зависимости и защищает TG от бага, который был
        найден в VK-боте (см. VkScheduleShortcutButtonsTests)."""
        dispatcher = build_dispatcher(
            settings=_make_settings(self.db_path),
            db=self.db,
            parser=MagicMock(),
            broadcaster=None,
            group_catalog=None,
            search_catalog=None,
            schedule_jobs=None,
        )
        handler_obj = next(
            h for h in dispatcher.callback_query.handlers if h.callback.__name__ == "handle_schedule_callback"
        )
        for filter_obj in handler_obj.filters:
            self.assertNotIn("state", getattr(filter_obj.callback, "__qualname__", "").lower())

    async def test_today_callback_works_without_any_prior_interaction(self) -> None:
        dispatcher = build_dispatcher(
            settings=_make_settings(self.db_path),
            db=self.db,
            parser=MagicMock(),
            broadcaster=None,
            group_catalog=None,
            search_catalog=None,
            schedule_jobs=None,
        )
        handler = self._get_schedule_callback_handler(dispatcher)

        fake_bot = AsyncMock()
        callback = SimpleNamespace(
            data="schedule:today",
            message=SimpleNamespace(chat=SimpleNamespace(id=self.CHAT_ID)),
            from_user=SimpleNamespace(id=self.USER_ID, first_name="Тест", last_name=None, username=None),
            bot=fake_bot,
            answer=AsyncMock(),
        )

        await handler(callback)

        sent_text = fake_bot.send_message.call_args.args[1]
        self.assertIn("Физика", sent_text)


if __name__ == "__main__":
    unittest.main()

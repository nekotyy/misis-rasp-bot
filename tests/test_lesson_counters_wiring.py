from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config import Settings
from src.db import Database
from src.lesson_counters import (
    LessonCounterService,
    LessonCounterSyncResult,
    format_counter_sync_report,
    is_uncounted_lesson,
)
from src.telegram_bot import (
    ADMIN_COUNTER_SYNC_KEYBOARD,
    ADMIN_FULL_ONLY_SECTIONS,
    ADMIN_KEYBOARD,
    ADMIN_KEYBOARD_LIMITED,
    ADMIN_SECTION_KEYBOARDS,
    build_dispatcher,
)
from src.vk_bot import build_vk_bot

GROUP_PAYLOAD = {
    "groups": [
        {
            "schedule_id": 600,
            "group_name": "ИСП-25-1",
            "subjects": [
                {
                    "group_name": "ИСП-25-1",
                    "subject": "Компьютерные сети",
                    "display_name": "Компьютерные сети",
                    "teacher": "Семенов А.В.",
                    "passed": 2,
                    "total": None,
                }
            ],
        }
    ]
}


class LessonCountersReadJsonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()
        self.json_path = Path(self._tmp.name) / "lesson_counters.json"
        self.json_path.write_text(json.dumps(GROUP_PAYLOAD, ensure_ascii=False), encoding="utf-8")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_service_with_path_shows_counters_with_unknown_total(self) -> None:
        service = LessonCounterService(self.db, self.json_path)

        text = await service.format_counters_text(600, group_name="ИСП-25-1")

        self.assertNotIn("не настроен", text)
        self.assertIn("Компьютерные сети", text)

    async def test_service_without_path_cannot_see_json(self) -> None:
        service = LessonCounterService(self.db)

        text = await service.format_counters_text(600, group_name="ИСП-25-1")

        self.assertEqual(text, "Список дисциплин пока не настроен.")


class ConsultationIsNotCountedTests(unittest.IsolatedAsyncioTestCase):
    """"Консульт." / "Консультирующий" — не пара, в счётчиках её быть не должно."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()
        self.json_path = Path(self._tmp.name) / "lesson_counters.json"

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def test_detects_consultation_by_subject_or_teacher(self) -> None:
        self.assertTrue(is_uncounted_lesson("Консульт.", "Консультирующий"))
        self.assertTrue(is_uncounted_lesson("консульт", "Иванов И.И."))
        self.assertTrue(is_uncounted_lesson("Математика", "Консультирующий"))
        self.assertFalse(is_uncounted_lesson("Математика", "Иванов И.И."))

    async def test_auto_increment_ignores_consultation(self) -> None:
        service = LessonCounterService(self.db, self.json_path)

        changed = service.auto_increment_or_create_subject_in_json(
            group_name="ИСП-25-1", schedule_id=600, subject="Консульт.", teacher="Консультирующий", count=1
        )

        self.assertFalse(changed)
        self.assertFalse(self.json_path.exists())

    async def test_auto_increment_still_counts_regular_lesson(self) -> None:
        service = LessonCounterService(self.db, self.json_path)

        service.auto_increment_or_create_subject_in_json(
            group_name="ИСП-25-1", schedule_id=600, subject="Математика", teacher="Иванов И.И.", count=1
        )

        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["groups"][0]["subjects"][0]["passed"], 1)

    async def test_existing_consultation_entries_are_hidden_from_users(self) -> None:
        payload = {
            "groups": [
                {
                    "schedule_id": 600,
                    "group_name": "ИСП-25-1",
                    "subjects": [
                        {"subject": "Консульт.", "teacher": "Консультирующий", "passed": 2, "total": None},
                        {"subject": "Математика", "teacher": "Иванов И.И.", "passed": 4, "total": None},
                    ],
                }
            ]
        }
        self.json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        service = LessonCounterService(self.db, self.json_path)

        text = await service.format_counters_text(600, group_name="ИСП-25-1")

        self.assertIn("Математика", text)
        self.assertNotIn("Консульт", text)


class BotsPassCountersPathToServiceTests(unittest.IsolatedAsyncioTestCase):
    """Боты читают счётчики из JSON на каждый запрос — путь должен быть задан сразу при сборке,
    а не только после первой админской операции со счётчиками."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()
        self.counters_path = Path(self._tmp.name) / "lesson_counters.json"

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _settings(self) -> MagicMock:
        s = MagicMock(spec=Settings)
        s.telegram_bot_token = "123456:FAKE-TOKEN"
        s.vk_bot_token = "vk_fake_token"
        s.vk_disable_ssl_verify = False
        s.admin_telegram_ids = []
        s.limited_admin_telegram_ids = []
        s.admin_vk_id = None
        s.schedule_url = "http://localhost/schedule"
        s.database_path = self.db.path
        s.lesson_counters_path = self.counters_path
        return s

    def _common(self) -> dict:
        return dict(
            settings=self._settings(), db=self.db,
            parser=MagicMock(), broadcaster=MagicMock(),
            group_catalog=MagicMock(), search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )

    async def test_telegram_bot_service_gets_counters_path(self) -> None:
        with patch("src.telegram_bot.LessonCounterService") as service_cls:
            build_dispatcher(**self._common())

        service_cls.assert_called_once_with(self.db, self.counters_path)

    async def test_vk_bot_service_gets_counters_path(self) -> None:
        with patch("src.vk_bot.LessonCounterService") as service_cls:
            build_vk_bot(**self._common())

        service_cls.assert_called_once_with(self.db, self.counters_path)


if __name__ == "__main__":
    unittest.main()


class ManualCounterSyncReportTests(unittest.TestCase):
    def test_empty_result(self) -> None:
        text = format_counter_sync_report(LessonCounterSyncResult(), "2026-09-28")

        self.assertIn("28 сентября 2026 года", text)
        self.assertIn("Нет ни одной группы", text)

    def test_counts_and_failures(self) -> None:
        result = LessonCounterSyncResult(
            processed=["ИСП-25-1", "ИСП-25-2"],
            skipped_already_done=["Э-25"],
            failed=[("ТМ-25-1", "502 Bad Gateway")],
        )

        text = format_counter_sync_report(result, "2026-09-28", html=True)

        self.assertIn("Учтено групп: 2", text)
        self.assertIn("Уже было учтено раньше (пропущено, чтобы не задвоить): 1", text)
        self.assertIn("Ошибок: 1", text)
        self.assertIn("ТМ-25-1: 502 Bad Gateway", text)
        self.assertNotIn("ИСП-25-1", text)

    def test_many_failures_are_truncated(self) -> None:
        result = LessonCounterSyncResult(failed=[(f"Г-{i}", "сбой") for i in range(15)])

        text = format_counter_sync_report(result, "2026-09-28")

        self.assertIn("Ошибок: 15", text)
        self.assertIn("…и ещё 5", text)

    def test_html_escapes_group_names_and_errors(self) -> None:
        result = LessonCounterSyncResult(failed=[("<b>Г</b>", "<script>")])

        text = format_counter_sync_report(result, "2026-09-28", html=True)

        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)


class TelegramAdminMenuStructureTests(unittest.TestCase):
    @staticmethod
    def _callbacks(keyboard) -> list[str]:
        return [button.callback_data for row in keyboard.inline_keyboard for button in row]

    def test_main_menu_is_only_sections_and_close(self) -> None:
        callbacks = self._callbacks(ADMIN_KEYBOARD)

        self.assertEqual(
            callbacks,
            ["admin:sec:monitor", "admin:sec:schedule", "admin:sec:counters", "admin:sec:people",
             "admin:sec:service", "admin:close"],
        )

    def test_manual_count_is_in_counters_section_and_offers_today_and_yesterday(self) -> None:
        self.assertIn("admin:counter_sync", self._callbacks(ADMIN_SECTION_KEYBOARDS["counters"]))
        self.assertEqual(
            self._callbacks(ADMIN_COUNTER_SYNC_KEYBOARD)[:2],
            ["admin:counter_sync:today", "admin:counter_sync:yesterday"],
        )

    def test_every_section_can_go_back_to_menu(self) -> None:
        for name, keyboard in ADMIN_SECTION_KEYBOARDS.items():
            self.assertIn("admin:back", self._callbacks(keyboard), name)

    def test_limited_admin_menu_has_no_full_only_sections(self) -> None:
        callbacks = self._callbacks(ADMIN_KEYBOARD_LIMITED)

        for section in ADMIN_FULL_ONLY_SECTIONS:
            self.assertNotIn(f"admin:sec:{section}", callbacks)
        self.assertIn("admin:sec:monitor", callbacks)

    def test_every_action_is_in_exactly_one_section(self) -> None:
        seen: list[str] = []
        for keyboard in ADMIN_SECTION_KEYBOARDS.values():
            seen += [c for c in self._callbacks(keyboard) if c != "admin:back"]

        self.assertEqual(len(seen), len(set(seen)))
        for expected in ("admin:status", "admin:refresh", "admin:lesson_add", "admin:users", "admin:cleandb"):
            self.assertIn(expected, seen)

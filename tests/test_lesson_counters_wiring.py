from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config import Settings
from src.db import Database
from src.lesson_counters import LessonCounterService, is_uncounted_lesson
from src.telegram_bot import build_dispatcher
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

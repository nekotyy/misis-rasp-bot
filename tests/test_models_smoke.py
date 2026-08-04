from __future__ import annotations

import unittest
from datetime import datetime

from src.models import ChangeSummary, DaySchedule, Lesson, ScheduleSnapshot, UserRecord


class TestModelsSmoke(unittest.TestCase):
    """Smoke-тесты для dataclass-моделей — проверяем что создаются корректно."""

    def test_lesson_creation(self) -> None:
        lesson = Lesson(number=1, subject="Математика", teacher="Иванов И.И.", classroom="301")
        self.assertEqual(lesson.number, 1)
        self.assertEqual(lesson.subject, "Математика")
        self.assertEqual(lesson.teacher, "Иванов И.И.")
        self.assertEqual(lesson.classroom, "301")

    def test_day_schedule_creation(self) -> None:
        lesson = Lesson(number=1, subject="Физика", teacher="Петров П.П.", classroom="201")
        day = DaySchedule(date_label="Понедельник, 5 августа", date_iso="2026-08-05", lessons=[lesson])
        self.assertEqual(len(day.lessons), 1)
        self.assertEqual(day.date_iso, "2026-08-05")

    def test_day_schedule_empty_lessons(self) -> None:
        day = DaySchedule(date_label="Пустой день", date_iso="2026-08-05", lessons=[])
        self.assertEqual(day.lessons, [])

    def test_schedule_snapshot_creation(self) -> None:
        snapshot = ScheduleSnapshot(
            group_name="КИБ-24-1",
            fetched_at=datetime(2026, 8, 5, 12, 0, 0),
            days=[],
        )
        self.assertEqual(snapshot.group_name, "КИБ-24-1")
        self.assertIsInstance(snapshot.fetched_at, datetime)

    def test_user_record_creation(self) -> None:
        user = UserRecord(
            platform="telegram",
            user_id=12345,
            username="test_user",
            full_name="Тест Тестов",
            subscription_type="group",
            subscription_key="600",
            subscription_title="КИБ-24-1",
            subscription_url="http://example.com",
            audience_subscription_key=None,
            audience_subscription_title=None,
            audience_subscription_url=None,
            group_name="КИБ-24-1",
            schedule_id=600,
            is_admin=False,
            is_editor=False,
            homework_notifications_enabled=True,
            delivery_disabled_auto=False,
            created_at="2026-08-01T00:00:00",
            last_seen_at="2026-08-05T00:00:00",
        )
        self.assertEqual(user.platform, "telegram")
        self.assertEqual(user.user_id, 12345)
        self.assertIsNone(user.custom_sticker_file_id)

    def test_user_record_custom_sticker_default(self) -> None:
        user = UserRecord(
            platform="vk", user_id=1, username=None, full_name=None,
            subscription_type=None, subscription_key=None, subscription_title=None,
            subscription_url=None, audience_subscription_key=None,
            audience_subscription_title=None, audience_subscription_url=None,
            group_name=None, schedule_id=None, is_admin=False, is_editor=False,
            homework_notifications_enabled=True, delivery_disabled_auto=False,
            created_at="", last_seen_at="",
        )
        self.assertIsNone(user.custom_sticker_file_id)

    def test_change_summary_creation(self) -> None:
        summary = ChangeSummary(
            changed_dates=["2026-08-05", "2026-08-06"],
            message="Изменения на 2 дня",
            payload={"days": ["2026-08-05", "2026-08-06"]},
        )
        self.assertEqual(len(summary.changed_dates), 2)
        self.assertIsNone(summary.telegram_message)
        self.assertIsNone(summary.vk_message)

    def test_change_summary_with_platform_messages(self) -> None:
        summary = ChangeSummary(
            changed_dates=["2026-08-05"],
            message="Общий текст",
            payload={},
            telegram_message="<b>TG</b> текст",
            vk_message="VK текст",
        )
        self.assertIsNotNone(summary.telegram_message)
        self.assertIsNotNone(summary.vk_message)


if __name__ == "__main__":
    unittest.main()

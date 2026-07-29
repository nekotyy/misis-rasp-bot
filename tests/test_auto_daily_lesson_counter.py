import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.db import Database
from src.lesson_counters import LessonCounterService
from src.message_broker import AutoDailyLessonCounterJob
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.scheduler import ScheduleJobs


class AutoDailyLessonCounterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_auto_counter.db"
        self.json_path = Path(self.temp_dir.name) / "lesson_counters.json"
        self.db = Database(self.db_path)
        await self.db.initialize()

        self.service = LessonCounterService(self.db, lesson_counters_path=self.json_path)

        self.mock_parser = MagicMock()
        self.mock_broadcaster = MagicMock()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_auto_increment_and_idempotency(self):
        target_date = "2026-09-01"

        # Mock schedule snapshot with 2 math lessons and 1 physics lesson on target_date
        lessons = [
            Lesson(number=1, subject="Математика", teacher="Иванов И.И.", classroom="301"),
            Lesson(number=2, subject="Математика", teacher="Иванов И.И.", classroom="301"),
            Lesson(number=3, subject="Физика", teacher="Петров П.П.", classroom="202"),
        ]
        day = DaySchedule(date_label="01.09", date_iso=target_date, lessons=lessons)
        snapshot = ScheduleSnapshot(group_name="ИСП-25-1", fetched_at=MagicMock(), days=[day])

        self.mock_parser.parse = AsyncMock(return_value=(snapshot, "hash123"))

        # Setup ScheduleJobs
        jobs = ScheduleJobs(
            db=self.db,
            parser=self.mock_parser,
            broadcaster=self.mock_broadcaster,
            timezone="Europe/Moscow",
            lesson_counters_enabled=True,
            lesson_counter_service=self.service,
            lesson_counters_path=self.json_path,
        )

        # Mock active sources in DB
        self.db.get_active_sources = AsyncMock(return_value=[
            {"source_type": "group", "schedule_id": 600, "group_name": "ИСП-25-1", "source_title": "ИСП-25-1"}
        ])

        job = AutoDailyLessonCounterJob(target_date_iso=target_date)

        # First run: should process and add 2 math (+2) and 1 physics (+1)
        await jobs.handle_auto_daily_lesson_counter_job(job)

        text = await self.service.format_counters_text(group_name="ИСП-25-1", html=True)
        self.assertIn("Математика", text)
        self.assertIn("Прошло - 2, всего - ##?", text)
        self.assertIn("Прошло - 1, всего - ##?", text)
        self.assertIn("💡 <i>Замечена пара с не указанным итоговым количеством пар (##?)!", text)

        # Second run (simulating retry or control check at 23:50/01:00/05:00): MUST BE IDEMPOTENT (skipped)
        await jobs.handle_auto_daily_lesson_counter_job(job)

        text_after_second_run = await self.service.format_counters_text(group_name="ИСП-25-1", html=True)
        # Values MUST NOT increase on second run
        self.assertIn("Прошло - 2, всего - ##?", text_after_second_run)
        self.assertIn("Прошло - 1, всего - ##?", text_after_second_run)

    def test_reset_group_counters(self):
        self.service.auto_increment_or_create_subject_in_json(
            group_name="ИСП-25-1",
            schedule_id=600,
            subject="Математика",
            teacher="Иванов И.И.",
            count=5,
        )
        reset_success = self.service.reset_group_counters("ИСП-25-1")
        self.assertTrue(reset_success)


if __name__ == "__main__":
    unittest.main()

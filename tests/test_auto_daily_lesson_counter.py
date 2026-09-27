import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.db import Database
from src.lesson_counters import LessonCounterService, sync_lesson_counters_for_date
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

        # Group is configured for lesson counting (JSON is the source of truth for which
        # groups get auto-counted, not the subscriber list — a group can have zero
        # subscribers and still be tracked).
        import json
        self.json_path.write_text(
            json.dumps({"groups": [{"schedule_id": 600, "group_name": "ИСП-25-1", "subjects": []}]}),
            encoding="utf-8",
        )
        # No subscribers at all for this group — auto-count must still process it.
        self.db.get_active_sources = AsyncMock(return_value=[])

        job = AutoDailyLessonCounterJob(target_date_iso=target_date)

        # First run: should process and add 2 math (+2) and 1 physics (+1)
        await jobs.handle_auto_daily_lesson_counter_job(job)

        text = await self.service.format_counters_text(group_name="ИСП-25-1", html=True)
        self.assertIn("Математика", text)
        self.assertIn("Прошло - 2, всего - ##?", text)
        self.assertIn("Прошло - 1, всего - ##?", text)
        self.assertIn("<i>Замечена пара с не указанным итоговым количеством пар (##?).", text)

        # Second run (simulating retry or control check at 23:50/01:00/05:00): MUST BE IDEMPOTENT (skipped)
        await jobs.handle_auto_daily_lesson_counter_job(job)

        text_after_second_run = await self.service.format_counters_text(group_name="ИСП-25-1", html=True)
        # Values MUST NOT increase on second run
        self.assertIn("Прошло - 2, всего - ##?", text_after_second_run)
        self.assertIn("Прошло - 1, всего - ##?", text_after_second_run)

    async def test_group_missing_from_json_but_present_in_site_catalog_is_auto_added(self):
        """Регресс: группа без единой записи в lesson_counters.json (в т.ч. появившаяся уже

        после написания конфига) раньше вообще не попадала в подсчёт — sync шёл только по
        JSON. Теперь она берётся из каталога сайта (таблица groups) и заводится сама."""
        target_date = "2026-09-10"
        lessons = [Lesson(number=1, subject="Химия", teacher="Сидоров С.С.", classroom="105")]
        day = DaySchedule(date_label="10.09", date_iso=target_date, lessons=lessons)
        snapshot = ScheduleSnapshot(group_name="МТО-25", fetched_at=MagicMock(), days=[day])
        self.mock_parser.parse = AsyncMock(return_value=(snapshot, "hash456"))

        await self.db.save_groups([
            {
                "schedule_id": 610,
                "group_name": "МТО-25",
                "department_id": 1,
                "department_code": "IT",
                "department_name": "ИТ",
                "url": "http://asu.sf-misis.ru/rasp/610",
            }
        ])

        # JSON-конфиг пустой — группа МТО-25 в нём не настроена вообще.
        import json
        self.json_path.write_text(json.dumps({"groups": []}), encoding="utf-8")

        result = await sync_lesson_counters_for_date(self.db, self.mock_parser, self.service, target_date)

        self.assertIn("МТО-25", result.processed)
        text = await self.service.format_counters_text(group_name="МТО-25", html=True)
        self.assertIn("Химия", text)
        self.assertIn("Прошло - 1, всего - ##?", text)

    async def test_group_in_both_json_and_catalog_is_processed_once(self):
        """Группа, уже настроенная в JSON, не должна задваиваться из-за совпадения в каталоге сайта."""
        target_date = "2026-09-11"
        lessons = [Lesson(number=1, subject="Химия", teacher="Сидоров С.С.", classroom="105")]
        day = DaySchedule(date_label="11.09", date_iso=target_date, lessons=lessons)
        snapshot = ScheduleSnapshot(group_name="МТО-25", fetched_at=MagicMock(), days=[day])
        self.mock_parser.parse = AsyncMock(return_value=(snapshot, "hash789"))

        await self.db.save_groups([
            {
                "schedule_id": 610,
                "group_name": "МТО-25 (сайт)",
                "department_id": 1,
                "department_code": "IT",
                "department_name": "ИТ",
                "url": "http://asu.sf-misis.ru/rasp/610",
            }
        ])
        import json
        self.json_path.write_text(
            json.dumps({"groups": [{"schedule_id": 610, "group_name": "МТО-25", "subjects": []}]}),
            encoding="utf-8",
        )

        result = await sync_lesson_counters_for_date(self.db, self.mock_parser, self.service, target_date)

        self.assertEqual(result.processed.count("МТО-25"), 1)
        self.mock_parser.parse.assert_awaited_once()

    async def test_catalog_group_without_schedule_id_is_skipped(self):
        """Группы без schedule_id (сайт его ещё не назначил, OCR-only) не подхватываются

        автообнаружением — для них нет способа запросить расписание с сайта."""
        target_date = "2026-09-12"
        self.mock_parser.parse = AsyncMock(side_effect=AssertionError("не должен вызываться"))

        await self.db.save_groups([
            {
                "schedule_id": None,
                "group_name": "РУП-26-1",
                "department_id": 1,
                "department_code": "IT",
                "department_name": "ИТ",
                "url": "http://asu.sf-misis.ru/rasp/610",
            }
        ])
        import json
        self.json_path.write_text(json.dumps({"groups": []}), encoding="utf-8")

        result = await sync_lesson_counters_for_date(self.db, self.mock_parser, self.service, target_date)

        self.assertEqual(result.processed, [])
        self.mock_parser.parse.assert_not_awaited()

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

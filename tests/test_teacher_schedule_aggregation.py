from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.db import Database
from src.lesson_counters import build_teacher_schedule_snapshot
from src.models import DaySchedule, Lesson, ScheduleSnapshot


class TestGetLatestGroupSnapshots(unittest.IsolatedAsyncioTestCase):
    """get_latest_group_snapshots должен отдавать по одному (последнему) снимку на группу."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _snapshot(self, group_name: str, teacher: str) -> ScheduleSnapshot:
        return ScheduleSnapshot(
            group_name=group_name,
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[
                DaySchedule(
                    date_label="14 сентября",
                    date_iso="2026-09-14",
                    lessons=[Lesson(number=1, subject="Матан", teacher=teacher, classroom="301")],
                )
            ],
        )

    async def test_only_latest_current_snapshot_per_group_returned(self) -> None:
        await self.db.save_snapshot(
            "current", "hash_old", self._snapshot("ИСП-25-1", "Старый Т.Т."),
            schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )
        await self.db.save_snapshot(
            "current", "hash_new", self._snapshot("ИСП-25-1", "Новый Н.Н."),
            schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )

        rows = await self.db.get_latest_group_snapshots("current")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshot_hash"], "hash_new")

    async def test_baseline_and_non_group_snapshots_excluded(self) -> None:
        await self.db.save_snapshot(
            "current", "hash_group", self._snapshot("ИСП-25-1", "Иванов И.И."),
            schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )
        await self.db.save_snapshot(
            "daily_baseline", "hash_baseline", self._snapshot("ИСП-25-1", "Иванов И.И."),
            schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )
        await self.db.save_snapshot(
            "current", "hash_teacher", self._snapshot("Иванов И.И.", "Иванов И.И."),
            schedule_id=None, group_name="Иванов И.И.",
            source_type="teacher", source_key="teacher:5", source_title="Иванов И.И.", source_url="http://example.com/prep/5",
        )

        rows = await self.db.get_latest_group_snapshots("current")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshot_hash"], "hash_group")


class TestBuildTeacherScheduleSnapshot(unittest.IsolatedAsyncioTestCase):
    """Личное расписание препода собирается из снимков групп в БД, без обращения к сайту."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_aggregates_matching_lessons_across_groups(self) -> None:
        group_a = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[
                DaySchedule(
                    date_label="14 сентября",
                    date_iso="2026-09-14",
                    lessons=[
                        Lesson(number=1, subject="Математика", teacher="Иванов И.И.", classroom="301"),
                        Lesson(number=2, subject="Физика", teacher="Петров П.П.", classroom="202"),
                    ],
                )
            ],
        )
        group_b = ScheduleSnapshot(
            group_name="КИБ-24-1",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[
                DaySchedule(
                    date_label="15 сентября",
                    date_iso="2026-09-15",
                    lessons=[
                        Lesson(number=1, subject="Английский", teacher="Сидорова С.С.", classroom="101"),
                        # Формат чуть отличается (лишний пробел, OCR-хвост) — должно совпасть нечетким сравнением.
                        Lesson(number=3, subject="Матанализ", teacher="Иванов И. И. (доп.)", classroom="305"),
                    ],
                )
            ],
        )
        await self.db.save_snapshot(
            "current", "hash_a", group_a, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )
        await self.db.save_snapshot(
            "current", "hash_b", group_b, schedule_id=202, group_name="КИБ-24-1",
            source_type="group", source_key="group:202", source_title="КИБ-24-1", source_url="rasp:202",
        )

        snapshot = await build_teacher_schedule_snapshot(self.db, "Иванов И.И.")

        self.assertEqual(snapshot.group_name, "Иванов И.И.")
        self.assertEqual([day.date_iso for day in snapshot.days], ["2026-09-14", "2026-09-15"])

        day1 = snapshot.days[0]
        self.assertEqual(len(day1.lessons), 1)
        self.assertEqual(day1.lessons[0].subject, "Математика")
        self.assertEqual(day1.lessons[0].teacher, "ИСП-25-1")
        self.assertNotIn("Петров", [lesson.teacher for lesson in day1.lessons])

        day2 = snapshot.days[1]
        self.assertEqual(len(day2.lessons), 1)
        self.assertEqual(day2.lessons[0].subject, "Матанализ")
        self.assertEqual(day2.lessons[0].teacher, "КИБ-24-1")

    async def test_lessons_on_same_day_are_sorted_by_number_not_group_order(self) -> None:
        """Регресс: у препода несколько групп в один день — пары шли в порядке обхода
        групп в БД (например 3, 1, 2), а не по возрастанию номера пары."""
        common_day = "2026-09-14"
        group_a = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="14 сентября", date_iso=common_day, lessons=[
                Lesson(number=3, subject="Информационные технологии", teacher="Коврижных О.А.", classroom="303"),
            ])],
        )
        group_b = ScheduleSnapshot(
            group_name="ИСП-25-4",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="14 сентября", date_iso=common_day, lessons=[
                Lesson(number=1, subject="Информационные технологии", teacher="Коврижных О.А.", classroom="303"),
            ])],
        )
        group_c = ScheduleSnapshot(
            group_name="ИСП-25-2",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="14 сентября", date_iso=common_day, lessons=[
                Lesson(number=2, subject="Информационные технологии", teacher="Коврижных О.А.", classroom="303"),
            ])],
        )
        # Порядок сохранения намеренно не совпадает с порядком номеров пар.
        await self.db.save_snapshot(
            "current", "hash_a", group_a, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )
        await self.db.save_snapshot(
            "current", "hash_d", group_b, schedule_id=104, group_name="ИСП-25-4",
            source_type="group", source_key="group:104", source_title="ИСП-25-4", source_url="rasp:104",
        )
        await self.db.save_snapshot(
            "current", "hash_c", group_c, schedule_id=102, group_name="ИСП-25-2",
            source_type="group", source_key="group:102", source_title="ИСП-25-2", source_url="rasp:102",
        )

        snapshot = await build_teacher_schedule_snapshot(self.db, "Коврижных О.А.")

        self.assertEqual(len(snapshot.days), 1)
        numbers = [lesson.number for lesson in snapshot.days[0].lessons]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers, [1, 2, 3])
        groups_in_order = [lesson.teacher for lesson in snapshot.days[0].lessons]
        self.assertEqual(groups_in_order, ["ИСП-25-4", "ИСП-25-2", "ИСП-25-1"])

    async def test_no_matching_lessons_yields_empty_days(self) -> None:
        group_a = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[
                DaySchedule(
                    date_label="14 сентября",
                    date_iso="2026-09-14",
                    lessons=[Lesson(number=1, subject="Математика", teacher="Петров П.П.", classroom="301")],
                )
            ],
        )
        await self.db.save_snapshot(
            "current", "hash_a", group_a, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )

        snapshot = await build_teacher_schedule_snapshot(self.db, "Иванов И.И.")

        self.assertEqual(snapshot.days, [])

    async def test_no_group_snapshots_yet(self) -> None:
        snapshot = await build_teacher_schedule_snapshot(self.db, "Иванов И.И.")
        self.assertEqual(snapshot.days, [])


if __name__ == "__main__":
    unittest.main()

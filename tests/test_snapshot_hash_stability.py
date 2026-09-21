from __future__ import annotations

import unittest
from datetime import datetime

from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.parser import compute_snapshot_hash


class ComputeSnapshotHashStabilityTests(unittest.TestCase):
    """Регресс: у личного расписания препода в один день пара с одинаковым номером

    может идти сразу в нескольких его группах (номер пары внутри дня уникален
    только для одной группы, а не для препода, который ведёт в нескольких). Хеш
    должен зависеть только от содержимого, а не от порядка, в котором эти пары
    пришли на вход (он определяется порядком групп из БД и не гарантирован)."""

    def _snapshot(self, lessons: list[Lesson]) -> ScheduleSnapshot:
        return ScheduleSnapshot(
            group_name="Иванов И.И.",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="14 сентября", date_iso="2026-09-14", lessons=lessons)],
        )

    def test_hash_is_the_same_regardless_of_duplicate_number_order(self) -> None:
        lesson_a = Lesson(number=1, subject="Математика", teacher="ИСП-25-1", classroom="301")
        lesson_b = Lesson(number=1, subject="Физика", teacher="ИСП-25-4", classroom="202")

        hash_1 = compute_snapshot_hash(self._snapshot([lesson_a, lesson_b]))
        hash_2 = compute_snapshot_hash(self._snapshot([lesson_b, lesson_a]))

        self.assertEqual(hash_1, hash_2)

    def test_hash_changes_when_content_actually_differs(self) -> None:
        lesson_a = Lesson(number=1, subject="Математика", teacher="ИСП-25-1", classroom="301")
        lesson_b = Lesson(number=1, subject="Физика", teacher="ИСП-25-4", classroom="202")
        lesson_b_moved = Lesson(number=1, subject="Физика", teacher="ИСП-25-4", classroom="303")

        hash_before = compute_snapshot_hash(self._snapshot([lesson_a, lesson_b]))
        hash_after = compute_snapshot_hash(self._snapshot([lesson_a, lesson_b_moved]))

        self.assertNotEqual(hash_before, hash_after)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.db import Database
from src.models import ChangeSummary, DaySchedule, Lesson, ScheduleSnapshot


class TestSyncSource(unittest.IsolatedAsyncioTestCase):
    """Тесты для _sync_source — центрального цикла синхронизации."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_sync.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

        self.mock_broadcaster = AsyncMock()
        self.mock_broadcaster.broadcast = AsyncMock()

        self.mock_parser = MagicMock()
        self.snapshot = ScheduleSnapshot(
            group_name="КИБ-24-1",
            fetched_at=datetime(2026, 8, 5, 12, 0, 0),
            days=[DaySchedule(date_label="Пн", date_iso="2026-08-05", lessons=[
                Lesson(number=1, subject="Математика", teacher="Иванов", classroom="301"),
            ])],
        )

        self.source = {
            "source_type": "group",
            "source_key": "600",
            "source_title": "КИБ-24-1",
            "source_url": "http://example.com/600",
            "schedule_id": 600,
            "group_name": "КИБ-24-1",
        }

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_first_sync_saves_baseline_no_broadcast(self) -> None:
        """Первый sync — сохраняет baseline, но НЕ рассылает."""
        from src.scheduler import ScheduleJobs
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster
        jobs._parse_source = AsyncMock(return_value=(self.snapshot, "hash_first"))

        await jobs._sync_source(self.source)

        self.mock_broadcaster.broadcast.assert_not_called()
        baseline = await self.db.get_latest_snapshot("daily_baseline", schedule_id=600)
        self.assertIsNotNone(baseline)
        self.assertEqual(baseline["snapshot_hash"], "hash_first")

    async def test_no_change_no_broadcast(self) -> None:
        """Если расписание не изменилось — broadcast не вызывается."""
        from src.scheduler import ScheduleJobs
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster

        # Первый sync — baseline
        jobs._parse_source = AsyncMock(return_value=(self.snapshot, "hash_same"))
        await jobs._sync_source(self.source)

        # Второй sync — тот же hash
        jobs._parse_source = AsyncMock(return_value=(self.snapshot, "hash_same"))
        with patch("src.scheduler.ScheduleComparator") as mock_comp:
            mock_comp.compare.return_value = None  # Нет изменений
            await jobs._sync_source(self.source)

        self.mock_broadcaster.broadcast.assert_not_called()

    async def test_change_triggers_broadcast(self) -> None:
        """Если расписание изменилось — broadcast вызывается."""
        from src.scheduler import ScheduleJobs
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster

        # Первый sync — baseline
        jobs._parse_source = AsyncMock(return_value=(self.snapshot, "hash_v1"))
        await jobs._sync_source(self.source)

        # Второй sync — другой hash + compare возвращает изменение
        changed_snapshot = ScheduleSnapshot(
            group_name="КИБ-24-1",
            fetched_at=datetime(2026, 8, 5, 13, 0, 0),
            days=[DaySchedule(date_label="Пн", date_iso="2026-08-05", lessons=[
                Lesson(number=1, subject="Физика", teacher="Петров", classroom="201"),
            ])],
        )
        jobs._parse_source = AsyncMock(return_value=(changed_snapshot, "hash_v2"))

        change = ChangeSummary(
            changed_dates=["2026-08-05"],
            message="Изменения на понедельник",
            payload={"test": True},
            telegram_message="<b>Изменения</b>",
            vk_message="Изменения",
        )
        with patch("src.scheduler.ScheduleComparator") as mock_comp:
            mock_comp.compare.return_value = change
            await jobs._sync_source(self.source)

        self.mock_broadcaster.broadcast.assert_called_once()
        call_kwargs = self.mock_broadcaster.broadcast.call_args
        self.assertIn("Изменения на понедельник", call_kwargs.args or [call_kwargs[0][0]])

    async def test_background_sync_skips_offline_ocr_group(self) -> None:
        from src.scheduler import ScheduleJobs

        offline_source = {
            **self.source,
            "source_key": "group-pending:киб-24-1",
            "source_url": "",
            "schedule_id": None,
        }
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = MagicMock(get_active_sources=AsyncMock(return_value=[offline_source]))
        jobs.alert_manager = None
        worker = AsyncMock()

        await jobs._run_for_active_sources("sync-current", worker)

        worker.assert_not_awaited()

    async def test_parse_source_teacher_never_hits_site(self) -> None:
        """Синхронизация "подписки на препода" не должна ходить на сайт — только в БД по группам."""
        from src.scheduler import ScheduleJobs

        group_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="14 сентября", date_iso="2026-09-14", lessons=[
                Lesson(number=1, subject="Математика", teacher="Иванов И.И.", classroom="301"),
            ])],
        )
        await self.db.save_snapshot(
            "current", "hash_a", group_snapshot, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )

        teacher_source = {
            "source_type": "teacher",
            "source_key": "teacher:5",
            "source_title": "Иванов И.И.",
            "source_url": "http://example.com/prep/5",
            "schedule_id": None,
            "group_name": None,
        }

        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.parser = MagicMock()
        jobs.parser.parse_from_url = AsyncMock(side_effect=AssertionError("site must not be called for teacher sync"))

        snapshot, snapshot_hash = await jobs._parse_source(teacher_source)

        jobs.parser.parse_from_url.assert_not_called()
        self.assertTrue(snapshot_hash)
        self.assertEqual(len(snapshot.days), 1)
        self.assertEqual(snapshot.days[0].lessons[0].subject, "Математика")
        self.assertEqual(snapshot.days[0].lessons[0].teacher, "ИСП-25-1")

    def test_scheduler_configure_auto_daily_lesson_counter_jobs(self) -> None:
        """Проверяем, что задачи автоподсчета пар регистрируются как корутины с правильными kwargs."""
        from src.scheduler import ScheduleJobs

        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.scheduler = MagicMock()
        jobs.lesson_counters_enabled = True
        jobs.lesson_counter_service = MagicMock()
        jobs.admin_backup_enabled = False
        jobs.save_daily_baseline = AsyncMock()
        jobs.save_daily_baseline_fallback = AsyncMock()
        jobs.sync_current_snapshot = AsyncMock()
        jobs.count_today_lessons = AsyncMock()
        jobs.enqueue_or_run_auto_daily_lesson_counter = AsyncMock()
        jobs.enqueue_or_run_db_cleanup = AsyncMock()
        jobs.group_catalog = None

        jobs.configure()

        # Check all add_job calls
        registered_funcs = [call.args[0] for call in jobs.scheduler.add_job.call_args_list if call.args]
        for func in registered_funcs:
            # None of the registered jobs should be a sync lambda returning an unawaited coroutine
            self.assertFalse(func.__name__ == "<lambda>", "Job must not be an unawaited sync lambda")


class TestApplySnapshotNotifiesAffectedTeachers(unittest.IsolatedAsyncioTestCase):
    """Регресс: изменение группы (в т.ч. из ручной/OCR загрузки) должно сразу же

    пересчитать и, если нужно, разослать подписки "на препода", которые в ней
    ведут — не дожидаясь их отдельного слота в плановой синхронизации.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test_teacher_cascade.db")
        await self.db.initialize()

        self.mock_broadcaster = AsyncMock()
        self.mock_broadcaster.broadcast = AsyncMock()

        await self.db.upsert_user(
            "telegram", 42, "prep", "Иванов И.И.",
            subscription_type="teacher", subscription_key="teacher:5",
            subscription_title="Иванов И.И.", subscription_url="http://example.com/prep/5",
        )

        # ScheduleComparator смотрит только на ближайшие дни от текущей даты, поэтому дата
        # в тесте должна быть реальным "сегодня", а не произвольной фиксированной датой.
        self.today_iso = datetime.now().date().isoformat()

        old_group_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=1, subject="Физика", teacher="Петров П.П.", classroom="202"),
            ])],
        )
        await self.db.save_snapshot(
            "daily_baseline", "hash_old", old_group_snapshot, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )
        await self.db.save_snapshot(
            "current", "hash_old", old_group_snapshot, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )

        old_teacher_snapshot = ScheduleSnapshot(
            group_name="Иванов И.И.",
            fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[],
        )
        await self.db.save_snapshot(
            "daily_baseline", "hash_teacher_old", old_teacher_snapshot, schedule_id=None, group_name="Иванов И.И.",
            source_type="teacher", source_key="teacher:5", source_title="Иванов И.И.", source_url="http://example.com/prep/5",
        )

        self.group_source = {
            "source_type": "group",
            "source_key": "group:101",
            "source_title": "ИСП-25-1",
            "source_url": "rasp:101",
            "schedule_id": 101,
            "group_name": "ИСП-25-1",
        }

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_teacher_matching_new_group_lesson_gets_notified_immediately(self) -> None:
        from src.scheduler import ScheduleJobs

        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster

        new_group_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime(2026, 9, 14, 9, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=1, subject="Математика", teacher="Иванов И.И.", classroom="301"),
            ])],
        )

        await jobs.apply_snapshot(self.group_source, new_group_snapshot, "hash_new")

        self.assertEqual(self.mock_broadcaster.broadcast.await_count, 2)
        notified_keys = {call.kwargs.get("subscription_key") for call in self.mock_broadcaster.broadcast.await_args_list}
        self.assertEqual(notified_keys, {"group:101", "teacher:5"})

        teacher_current = await self.db.get_latest_snapshot("current", source_key="teacher:5")
        self.assertIsNotNone(teacher_current)
        self.assertEqual(teacher_current["content"]["days"][0]["lessons"][0]["subject"], "Математика")
        self.assertEqual(teacher_current["content"]["days"][0]["lessons"][0]["teacher"], "ИСП-25-1")

    async def test_teacher_gains_a_lesson_from_a_different_group_reaching_three_total(self) -> None:
        """Точное подтверждение сценария из чата: у препода было 2 пары (в одной группе),

        другой уже загруженной группе на тот же день добавили ещё одну его пару — стало 3.
        Это работает и без фикса на "замену/удаление" из прошлого коммита: механизм с
        самого начала сканирует НОВЫЙ снимок изменившейся группы и сразу находит там ФИО
        препода — баланс со старым снимком нужен только для обратного случая (пара исчезла)."""
        from src.scheduler import ScheduleJobs

        # У Иванова уже 2 пары — обе в ИСП-25-1 (переопределяем однопарный снимок
        # из общего asyncSetUp).
        two_lesson_group_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1", fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=1, subject="Физика", teacher="Иванов И.И.", classroom="202"),
                Lesson(number=2, subject="Химия", teacher="Иванов И.И.", classroom="203"),
            ])],
        )
        await self.db.save_snapshot(
            "current", "hash_isp2", two_lesson_group_snapshot, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )
        ivanov_two_lessons = ScheduleSnapshot(
            group_name="Иванов И.И.", fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=1, subject="Физика", teacher="ИСП-25-1", classroom="202"),
                Lesson(number=2, subject="Химия", teacher="ИСП-25-1", classroom="203"),
            ])],
        )
        await self.db.save_snapshot(
            "daily_baseline", "hash_teacher_two", ivanov_two_lessons, schedule_id=None, group_name="Иванов И.И.",
            source_type="teacher", source_key="teacher:5", source_title="Иванов И.И.", source_url="http://example.com/prep/5",
        )

        # Другая, уже загруженная группа: на этот день у неё пока пусто, а теперь
        # добавляется пара — и её тоже ведёт Иванов.
        mto_source = {
            "source_type": "group", "source_key": "group:102", "source_title": "МТО-25",
            "source_url": "rasp:102", "schedule_id": 102, "group_name": "МТО-25",
        }
        empty_mto_snapshot = ScheduleSnapshot(group_name="МТО-25", fetched_at=datetime(2026, 9, 14, 8, 0, 0), days=[
            DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[]),
        ])
        await self.db.save_snapshot(
            "daily_baseline", "hash_mto_old", empty_mto_snapshot, schedule_id=102, group_name="МТО-25",
            source_type="group", source_key="group:102", source_title="МТО-25", source_url="rasp:102",
        )

        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster

        new_mto_snapshot = ScheduleSnapshot(
            group_name="МТО-25", fetched_at=datetime(2026, 9, 14, 9, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=3, subject="История", teacher="Иванов И.И.", classroom="404"),
            ])],
        )

        await jobs.apply_snapshot(mto_source, new_mto_snapshot, "hash_mto_new")

        notified_keys = {call.kwargs.get("subscription_key") for call in self.mock_broadcaster.broadcast.await_args_list}
        self.assertIn("teacher:5", notified_keys, "Иванов должен узнать о новой (третьей) паре из другой группы")

        ivanov_current = await self.db.get_latest_snapshot("current", source_key="teacher:5")
        lessons = ivanov_current["content"]["days"][0]["lessons"]
        self.assertEqual(len(lessons), 3, "Итоговое расписание должно содержать все 3 пары — из обеих групп")
        self.assertEqual(sorted(item["number"] for item in lessons), [1, 2, 3])

    async def test_unrelated_teacher_is_not_notified(self) -> None:
        from src.scheduler import ScheduleJobs

        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster

        new_group_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime(2026, 9, 14, 9, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=1, subject="Физика", teacher="Сидоров С.С.", classroom="202"),
            ])],
        )

        await jobs.apply_snapshot(self.group_source, new_group_snapshot, "hash_new2")

        self.assertEqual(self.mock_broadcaster.broadcast.await_count, 1)
        notified_keys = {call.kwargs.get("subscription_key") for call in self.mock_broadcaster.broadcast.await_args_list}
        self.assertEqual(notified_keys, {"group:101"})

    async def test_teacher_replaced_on_an_already_loaded_day_still_gets_notified(self) -> None:
        """Регресс: замену препода на уже загруженный день (например, повторным фото/OCR)

        видел только новый снимок группы — а препод, которого заменили, из него уже пропал,
        так что раньше его личное расписание вообще не пересчитывалось и он не узнавал,
        что его пары больше нет, хотя это тоже реальное изменение из-за той же группы."""
        from src.scheduler import ScheduleJobs

        await self.db.upsert_user(
            "telegram", 43, "prep2", "Петров П.П.",
            subscription_type="teacher", subscription_key="teacher:6",
            subscription_title="Петров П.П.", subscription_url="http://example.com/prep/6",
        )
        old_petrov_snapshot = ScheduleSnapshot(
            group_name="Петров П.П.", fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=1, subject="Физика", teacher="ИСП-25-1", classroom="202"),
                Lesson(number=2, subject="Химия", teacher="ИСП-25-1", classroom="203"),
            ])],
        )
        await self.db.save_snapshot(
            "daily_baseline", "hash_petrov_old", old_petrov_snapshot, schedule_id=None, group_name="Петров П.П.",
            source_type="teacher", source_key="teacher:6", source_title="Петров П.П.", source_url="http://example.com/prep/6",
        )
        # Группа изначально вела 2 пары в этот день, обе у Петрова (baseline/current уже
        # содержат обе — переопределяем то, что положил общий asyncSetUp с одной парой).
        two_lesson_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1", fetched_at=datetime(2026, 9, 14, 8, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=1, subject="Физика", teacher="Петров П.П.", classroom="202"),
                Lesson(number=2, subject="Химия", teacher="Петров П.П.", classroom="203"),
            ])],
        )
        await self.db.save_snapshot(
            "daily_baseline", "hash_old2", two_lesson_snapshot, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )
        await self.db.save_snapshot(
            "current", "hash_old2", two_lesson_snapshot, schedule_id=101, group_name="ИСП-25-1",
            source_type="group", source_key="group:101", source_title="ИСП-25-1", source_url="rasp:101",
        )

        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster

        # Замена: пара №1 теперь у Сидорова, пара №2 у Петрова осталась как была.
        new_group_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1", fetched_at=datetime(2026, 9, 14, 9, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=1, subject="Физика", teacher="Сидоров С.С.", classroom="202"),
                Lesson(number=2, subject="Химия", teacher="Петров П.П.", classroom="203"),
            ])],
        )

        await jobs.apply_snapshot(self.group_source, new_group_snapshot, "hash_new3")

        notified_keys = {call.kwargs.get("subscription_key") for call in self.mock_broadcaster.broadcast.await_args_list}
        self.assertIn("teacher:6", notified_keys, "Петров, которого частично заменили, должен узнать об изменении")

        petrov_current = await self.db.get_latest_snapshot("current", source_key="teacher:6")
        lessons = petrov_current["content"]["days"][0]["lessons"]
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["subject"], "Химия")


class TestTeacherNotifyBatchCoalescing(unittest.IsolatedAsyncioTestCase):
    """Регресс: препод, ведущий в нескольких группах, получал одно растущее

    уведомление на КАЖДУЮ свою группу, обновившуюся в одном проходе плановой
    синхронизации — вместо одного финального с полной картиной. Причина была
    двойная: (1) `_notify_affected_teachers` пересчитывал и слал препода сразу
    же на каждое отдельное изменение группы, не дожидаясь остальных его групп
    в том же проходе; (2) сам препод как активный источник ТОЖЕ получал свой
    независимый "ход" в общем цикле синхронизации, хотя его расписание целиком
    выводится из групп и не может найти ничего нового само по себе.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test_teacher_batch.db")
        await self.db.initialize()

        self.mock_broadcaster = AsyncMock()
        self.mock_broadcaster.broadcast = AsyncMock()
        self.today_iso = datetime.now().date().isoformat()

        # Студенты — иначе группы не попадут в get_active_sources() (он строится
        # из подписок пользователей, а не из самого факта существования группы).
        await self.db.upsert_user(
            "telegram", 1, "student_a", "Студент А",
            subscription_type="group", subscription_key="group:101",
            subscription_title="ИСП-25-1", schedule_id=101, group_name="ИСП-25-1",
        )
        await self.db.upsert_user(
            "telegram", 2, "student_b", "Студент Б",
            subscription_type="group", subscription_key="group:102",
            subscription_title="МТО-25", schedule_id=102, group_name="МТО-25",
        )
        await self.db.upsert_user(
            "telegram", 42, "prep", "Иванов И.И.",
            subscription_type="teacher", subscription_key="teacher:5",
            subscription_title="Иванов И.И.", subscription_url="http://example.com/prep/5",
        )

        empty_teacher_snapshot = ScheduleSnapshot(group_name="Иванов И.И.", fetched_at=datetime(2026, 9, 14, 8, 0, 0), days=[])
        await self.db.save_snapshot(
            "daily_baseline", "hash_teacher_old", empty_teacher_snapshot, schedule_id=None, group_name="Иванов И.И.",
            source_type="teacher", source_key="teacher:5", source_title="Иванов И.И.", source_url="http://example.com/prep/5",
        )

        self.group_a = {
            "source_type": "group", "source_key": "group:101", "source_title": "ИСП-25-1",
            "source_url": "rasp:101", "schedule_id": 101, "group_name": "ИСП-25-1",
        }
        self.group_b = {
            "source_type": "group", "source_key": "group:102", "source_title": "МТО-25",
            "source_url": "rasp:102", "schedule_id": 102, "group_name": "МТО-25",
        }
        for group in (self.group_a, self.group_b):
            empty_snapshot = ScheduleSnapshot(group_name=group["group_name"], fetched_at=datetime(2026, 9, 14, 8, 0, 0), days=[])
            await self.db.save_snapshot(
                "daily_baseline", "hash_empty", empty_snapshot, schedule_id=group["schedule_id"], group_name=group["group_name"],
                source_type="group", source_key=group["source_key"], source_title=group["source_title"], source_url=group["source_url"],
            )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _make_jobs(self):
        from src.scheduler import ScheduleJobs

        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.db = self.db
        jobs.broadcaster = self.mock_broadcaster
        jobs.alert_manager = None
        jobs.request_delay_seconds = 0.0
        jobs.request_jitter_seconds = 0.0
        return jobs

    async def test_teacher_in_two_groups_synced_in_one_pass_gets_one_notification(self) -> None:
        jobs = self._make_jobs()

        snapshot_a = ScheduleSnapshot(
            group_name="ИСП-25-1", fetched_at=datetime(2026, 9, 14, 9, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=1, subject="История России", teacher="Иванов И.И.", classroom="312/2"),
            ])],
        )
        snapshot_b = ScheduleSnapshot(
            group_name="МТО-25", fetched_at=datetime(2026, 9, 14, 9, 0, 0),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today_iso, lessons=[
                Lesson(number=2, subject="История России", teacher="Иванов И.И.", classroom="312/2"),
            ])],
        )
        canned = {101: (snapshot_a, "hash_a"), 102: (snapshot_b, "hash_b")}
        jobs.parser = MagicMock()
        jobs.parser.parse = AsyncMock(side_effect=lambda schedule_id: canned[schedule_id])

        await jobs._run_for_active_sources("sync", jobs._sync_source, skip_source_types={"teacher"})

        teacher_broadcasts = [
            call for call in self.mock_broadcaster.broadcast.await_args_list
            if call.kwargs.get("subscription_key") == "teacher:5"
        ]
        self.assertEqual(len(teacher_broadcasts), 1, "Препод из двух групп в одном проходе должен получить одно уведомление")

        teacher_current = await self.db.get_latest_snapshot("current", source_key="teacher:5")
        lessons = teacher_current["content"]["days"][0]["lessons"]
        self.assertEqual(len(lessons), 2, "Финальное уведомление должно содержать пары из ОБЕИХ групп")

    async def test_teacher_source_skipped_in_its_own_turn_during_sync(self) -> None:
        """Препод как источник не должен получать собственный ход в плановом sync —

        его расписание целиком выводится из групп и обновляется только реактивно."""
        jobs = self._make_jobs()
        real_sync_source = jobs._sync_source
        jobs._sync_source = AsyncMock(wraps=real_sync_source)
        jobs.parser = MagicMock()
        jobs.parser.parse = AsyncMock(return_value=(ScheduleSnapshot(group_name="x", fetched_at=datetime.now(), days=[]), "h"))

        await jobs._run_for_active_sources("sync", jobs._sync_source, skip_source_types={"teacher"})

        synced_types = {call.args[0]["source_type"] for call in jobs._sync_source.await_args_list}
        self.assertNotIn("teacher", synced_types)


if __name__ == "__main__":
    unittest.main()

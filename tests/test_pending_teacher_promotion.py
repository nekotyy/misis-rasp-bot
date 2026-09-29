from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.db import Database
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.schedule_search import ScheduleSearchCatalog, SearchTarget
from src.scheduler import ScheduleJobs

SITE_TEACHERS = [
    ("Боярищев В. В.", "http://asu.sf-misis.ru/raspprep/16"),
    ("Иванова А. И.", "http://asu.sf-misis.ru/raspprep/755"),
    ("Иванова Е. Ю.", "http://asu.sf-misis.ru/raspprep/1012"),
    ("Хребтова Т. В.", "http://asu.sf-misis.ru/raspprep/985"),
]


def _catalog(pairs=SITE_TEACHERS) -> ScheduleSearchCatalog:
    catalog = ScheduleSearchCatalog("http://asu.sf-misis.ru/rasp/600", group_catalog=MagicMock())
    catalog._populate_preps(pairs)
    catalog._preps_loaded = True
    return catalog


class FindSiteTeacherTests(unittest.IsolatedAsyncioTestCase):
    async def test_matches_regardless_of_spaces_between_initials(self) -> None:
        catalog = _catalog()

        for written in ("Боярищев В.В.", "Боярищев В. В.", "боярищев в.в."):
            target = await catalog.find_site_teacher(written)
            self.assertIsNotNone(target, written)
            self.assertEqual(target.url, "http://asu.sf-misis.ru/raspprep/16")

    async def test_same_surname_different_initials_is_not_mixed_up(self) -> None:
        catalog = _catalog()

        first = await catalog.find_site_teacher("Иванова А.И.")
        second = await catalog.find_site_teacher("Иванова Е.Ю.")

        self.assertTrue(first.url.endswith("/755"))
        self.assertTrue(second.url.endswith("/1012"))

    async def test_surname_only_or_wrong_initials_do_not_match(self) -> None:
        catalog = _catalog()

        self.assertIsNone(await catalog.find_site_teacher("Иванова"))
        self.assertIsNone(await catalog.find_site_teacher("Иванова О.О."))
        self.assertIsNone(await catalog.find_site_teacher("Петров П.П."))
        self.assertIsNone(await catalog.find_site_teacher(""))

    async def test_entry_without_url_is_ignored(self) -> None:
        catalog = _catalog([("Боярищев В. В.", "")])

        self.assertIsNone(await catalog.find_site_teacher("Боярищев В.В."))

    async def test_duplicate_full_names_on_site_are_not_guessed(self) -> None:
        catalog = _catalog(
            [
                ("Иванова А. И.", "http://asu.sf-misis.ru/raspprep/755"),
                ("Иванова А. И.", "http://asu.sf-misis.ru/raspprep/999"),
            ]
        )

        self.assertIsNone(await catalog.find_site_teacher("Иванова А.И."))


class RefreshTeachersTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_picks_up_teacher_added_later(self) -> None:
        catalog = _catalog([("Боярищев В. В.", "http://asu.sf-misis.ru/raspprep/16")])
        self.assertIsNone(await catalog.find_site_teacher("Хребтова Т.В."))
        catalog._fetch_pairs = AsyncMock(return_value=SITE_TEACHERS)

        refreshed = await catalog.refresh_teachers()

        self.assertTrue(refreshed)
        self.assertIsNotNone(await catalog.find_site_teacher("Хребтова Т.В."))

    async def test_failed_refresh_keeps_previous_list(self) -> None:
        catalog = _catalog()
        catalog._fetch_pairs = AsyncMock(side_effect=RuntimeError("сайт лежит"))

        refreshed = await catalog.refresh_teachers()

        self.assertFalse(refreshed)
        self.assertIsNotNone(await catalog.find_site_teacher("Боярищев В.В."))

    async def test_empty_site_answer_does_not_wipe_list(self) -> None:
        catalog = _catalog()
        catalog._fetch_pairs = AsyncMock(return_value=[])

        self.assertFalse(await catalog.refresh_teachers())
        self.assertIsNotNone(await catalog.find_site_teacher("Боярищев В.В."))


def _snapshot(subject: str) -> ScheduleSnapshot:
    return ScheduleSnapshot(
        group_name="Боярищев В.В.",
        fetched_at=datetime(2026, 9, 29, 10, 0, 0),
        days=[DaySchedule(date_label="Сегодня", date_iso="2026-09-29", lessons=[
            Lesson(number=1, subject=subject, teacher="Боярищев В.В.", classroom="с-з"),
        ])],
    )


class PendingTeacherDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.initialize()
        self.pending_key = "teacher-pending:боярищев в.в."
        await self.db.upsert_user(
            "vk", 1, None, "Преп",
            subscription_type="teacher", subscription_key=self.pending_key,
            subscription_title="Боярищев В.В.", subscription_url="",
        )
        for snapshot_type in ("current", "daily_baseline"):
            await self.db.save_snapshot(
                snapshot_type, "hash-old", _snapshot("Физкультура"), schedule_id=None,
                group_name="Боярищев В.В.", source_type="teacher", source_key=self.pending_key,
                source_title="Боярищев В.В.", source_url="",
            )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_lists_only_pending_teacher_subscriptions(self) -> None:
        await self.db.upsert_user(
            "vk", 2, None, "Другой",
            subscription_type="teacher", subscription_key="teacher:918", subscription_title="Абилов О. Ю.",
        )
        await self.db.upsert_user(
            "vk", 3, None, "Студент",
            subscription_type="group", subscription_key="group:600", subscription_title="ИСП-25-1", schedule_id=600,
        )

        pending = await self.db.get_pending_teacher_subscribers()

        self.assertEqual(
            pending, [{"subscription_key": self.pending_key, "subscription_title": "Боярищев В.В."}]
        )

    async def test_promote_moves_user_and_copies_latest_snapshots(self) -> None:
        moved = await self.db.promote_pending_teacher_subscription(
            self.pending_key, "teacher:16", "Боярищев В. В.", "http://asu.sf-misis.ru/raspprep/16"
        )

        self.assertEqual(moved, 1)
        user = await self.db.get_user("vk", 1)
        self.assertEqual(user.subscription_key, "teacher:16")
        self.assertEqual(user.subscription_title, "Боярищев В. В.")
        self.assertEqual(user.subscription_url, "http://asu.sf-misis.ru/raspprep/16")
        for snapshot_type in ("current", "daily_baseline"):
            copied = await self.db.get_latest_snapshot(snapshot_type, source_key="teacher:16")
            self.assertIsNotNone(copied, snapshot_type)
            self.assertEqual(copied["snapshot_hash"], "hash-old")
            self.assertEqual(copied["source_title"], "Боярищев В. В.")
        self.assertEqual(await self.db.get_pending_teacher_subscribers(), [])

    async def test_promote_does_not_overwrite_existing_snapshots_of_target(self) -> None:
        await self.db.save_snapshot(
            "daily_baseline", "hash-existing", _snapshot("Информатика"), schedule_id=None,
            group_name="Боярищев В. В.", source_type="teacher", source_key="teacher:16",
            source_title="Боярищев В. В.", source_url="http://asu.sf-misis.ru/raspprep/16",
        )

        await self.db.promote_pending_teacher_subscription(
            self.pending_key, "teacher:16", "Боярищев В. В.", "http://asu.sf-misis.ru/raspprep/16"
        )

        baseline = await self.db.get_latest_snapshot("daily_baseline", source_key="teacher:16")
        self.assertEqual(baseline["snapshot_hash"], "hash-existing")

    async def test_promote_without_subscribers_changes_nothing(self) -> None:
        moved = await self.db.promote_pending_teacher_subscription(
            "teacher-pending:нет такого", "teacher:1", "Никто Н. Н.", "http://x/1"
        )

        self.assertEqual(moved, 0)
        self.assertIsNone(await self.db.get_latest_snapshot("current", source_key="teacher:1"))


class PromotePendingTeachersJobTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _jobs(catalog, pending) -> ScheduleJobs:
        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.search_catalog = catalog
        jobs.db = MagicMock(
            get_pending_teacher_subscribers=AsyncMock(return_value=pending),
            promote_pending_teacher_subscription=AsyncMock(return_value=1),
        )
        return jobs

    async def test_noop_without_search_catalog(self) -> None:
        jobs = self._jobs(None, [])

        await jobs.promote_pending_teacher_subscribers()  # не должно бросать

    async def test_site_is_not_asked_when_nothing_is_pending(self) -> None:
        catalog = MagicMock(refresh_teachers=AsyncMock(), find_site_teacher=AsyncMock())
        jobs = self._jobs(catalog, [])

        await jobs.promote_pending_teacher_subscribers()

        catalog.refresh_teachers.assert_not_awaited()

    async def test_matching_teacher_is_promoted_and_unmatched_is_left(self) -> None:
        found = SearchTarget(kind="teacher", title="Боярищев В. В.", url="http://asu.sf-misis.ru/raspprep/16")
        catalog = MagicMock(
            refresh_teachers=AsyncMock(return_value=True),
            find_site_teacher=AsyncMock(side_effect=lambda title: found if title == "Боярищев В.В." else None),
        )
        pending = [
            {"subscription_key": "teacher-pending:боярищев в.в.", "subscription_title": "Боярищев В.В."},
            {"subscription_key": "teacher-pending:нет в.в.", "subscription_title": "Нет В.В."},
        ]
        jobs = self._jobs(catalog, pending)

        await jobs.promote_pending_teacher_subscribers()

        catalog.refresh_teachers.assert_awaited_once()
        jobs.db.promote_pending_teacher_subscription.assert_awaited_once_with(
            "teacher-pending:боярищев в.в.", "teacher:16", "Боярищев В. В.", "http://asu.sf-misis.ru/raspprep/16"
        )

    async def test_job_is_registered_only_with_search_catalog(self) -> None:
        with_catalog = ScheduleJobs(
            db=MagicMock(), parser=MagicMock(), broadcaster=MagicMock(), timezone="Europe/Moscow",
            search_catalog=MagicMock(),
        )
        without_catalog = ScheduleJobs(
            db=MagicMock(), parser=MagicMock(), broadcaster=MagicMock(), timezone="Europe/Moscow",
        )
        with_catalog.configure()
        without_catalog.configure()

        def registered(jobs: ScheduleJobs) -> list:
            return [j for j in jobs.scheduler.get_jobs() if j.func == jobs.promote_pending_teacher_subscribers]

        self.assertEqual(len(registered(with_catalog)), 1)
        self.assertEqual(registered(without_catalog), [])


if __name__ == "__main__":
    unittest.main()

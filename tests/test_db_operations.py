from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.db import Database
from src.models import DaySchedule, Lesson, ScheduleSnapshot


class TestDatabaseOperations(unittest.IsolatedAsyncioTestCase):
    """Интеграционные тесты для Database — реальный SQLite в tmpdir."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_upsert_and_list_users(self) -> None:
        await self.db.upsert_user("telegram", 100, "alice", "Alice A", subscription_type="group", subscription_key="600", subscription_title="КИБ-24-1")
        await self.db.upsert_user("vk", 200, "bob", "Bob B", subscription_type="teacher", subscription_key="prep_1", subscription_title="Иванов И.И.")
        await self.db.upsert_user("telegram", 300, "charlie", "Charlie C")

        all_users = await self.db.list_users()
        self.assertEqual(len(all_users), 3)

    async def test_list_users_filter_by_platform(self) -> None:
        await self.db.upsert_user("telegram", 100, "alice", "Alice")
        await self.db.upsert_user("vk", 200, "bob", "Bob")

        tg_users = await self.db.list_users(platform="telegram")
        self.assertEqual(len(tg_users), 1)
        self.assertEqual(tg_users[0].platform, "telegram")

        vk_users = await self.db.list_users(platform="vk")
        self.assertEqual(len(vk_users), 1)
        self.assertEqual(vk_users[0].platform, "vk")

    async def test_upsert_user_updates_existing(self) -> None:
        await self.db.upsert_user("telegram", 100, "alice", "Alice Old")
        await self.db.upsert_user("telegram", 100, "alice_new", "Alice New")

        users = await self.db.list_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].username, "alice_new")
        self.assertEqual(users[0].full_name, "Alice New")

    async def test_save_and_get_latest_snapshot(self) -> None:
        snapshot = ScheduleSnapshot(
            group_name="КИБ-24-1",
            fetched_at=datetime(2026, 8, 5, 12, 0, 0),
            days=[
                DaySchedule(
                    date_label="Понедельник",
                    date_iso="2026-08-05",
                    lessons=[Lesson(number=1, subject="Математика", teacher="Иванов И.И.", classroom="301")],
                ),
            ],
        )
        await self.db.save_snapshot("current", "hash_abc", snapshot, 600, "КИБ-24-1")

        latest = await self.db.get_latest_snapshot("current", schedule_id=600)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["snapshot_hash"], "hash_abc")

    async def test_get_latest_snapshot_returns_none_when_empty(self) -> None:
        latest = await self.db.get_latest_snapshot("current", schedule_id=999)
        self.assertIsNone(latest)

    async def test_save_multiple_snapshots_latest_is_newest(self) -> None:
        for i, hash_val in enumerate(["hash_1", "hash_2", "hash_3"]):
            snapshot = ScheduleSnapshot(
                group_name="Группа",
                fetched_at=datetime(2026, 8, 5, 12, i, 0),
                days=[],
            )
            await self.db.save_snapshot("current", hash_val, snapshot, 600, "Группа")

        latest = await self.db.get_latest_snapshot("current", schedule_id=600)
        self.assertEqual(latest["snapshot_hash"], "hash_3")

    async def test_get_active_sources(self) -> None:
        await self.db.upsert_user("telegram", 1, "u1", "U1", subscription_type="group", subscription_key="600", subscription_title="КИБ-24-1", schedule_id=600)
        await self.db.upsert_user("telegram", 2, "u2", "U2", subscription_type="group", subscription_key="600", subscription_title="КИБ-24-1", schedule_id=600)
        await self.db.upsert_user("vk", 3, "u3", "U3", subscription_type="teacher", subscription_key="prep_1", subscription_title="Иванов", subscription_url="http://example.com/prep/1")

        sources = await self.db.get_active_sources()
        self.assertGreaterEqual(len(sources), 2)

        keys = {s["source_key"] for s in sources}
        self.assertIn("600", keys)
        self.assertIn("prep_1", keys)

    async def test_get_active_sources_empty_db(self) -> None:
        sources = await self.db.get_active_sources()
        self.assertEqual(sources, [])

    async def test_upsert_user_preserves_subscription_on_none(self) -> None:
        """COALESCE должен сохранять старую подписку если новая = None."""
        await self.db.upsert_user("telegram", 100, "alice", "Alice", subscription_type="group", subscription_key="600", subscription_title="КИБ-24-1")
        await self.db.upsert_user("telegram", 100, "alice", "Alice")  # без подписки

        users = await self.db.list_users()
        self.assertEqual(users[0].subscription_key, "600")


if __name__ == "__main__":
    unittest.main()

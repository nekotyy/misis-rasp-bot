from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.db import Database
from src.models import DaySchedule, Lesson, ScheduleSnapshot


class TestMetricsContract(unittest.IsolatedAsyncioTestCase):
    """Contract-тесты: collect_metrics возвращает правильную структуру."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_metrics.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

        # Добавим тестовых данных
        await self.db.upsert_user("telegram", 1, "alice", "Alice", subscription_type="group", subscription_key="600", subscription_title="КИБ-24-1", schedule_id=600)
        await self.db.upsert_user("vk", 2, "bob", "Bob", subscription_type="teacher", subscription_key="prep_1", subscription_title="Иванов")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_collect_metrics_returns_required_keys(self) -> None:
        from web_configurator.metrics import collect_metrics

        metrics = await collect_metrics(
            self.db_path,
            rabbitmq_url="",
            telegram_token="",
            vk_token="",
            started_at=datetime(2026, 8, 5, 0, 0, 0),
        )

        # Верхний уровень
        required_keys = {"uptime_seconds", "users", "user_rows", "services", "schedule", "delivery", "lesson_counters", "extra"}
        self.assertTrue(required_keys.issubset(metrics.keys()), f"Missing keys: {required_keys - metrics.keys()}")

    async def test_users_section_structure(self) -> None:
        from web_configurator.metrics import collect_metrics

        metrics = await collect_metrics(
            self.db_path, rabbitmq_url="", telegram_token="", vk_token="",
            started_at=datetime(2026, 8, 5),
        )

        users = metrics["users"]
        for key in ("total", "telegram", "vk", "new_7d", "old", "teachers", "groups"):
            self.assertIn(key, users, f"Missing users.{key}")
            self.assertIsInstance(users[key], int, f"users.{key} should be int")

        self.assertEqual(users["total"], 2)
        self.assertEqual(users["telegram"], 1)
        self.assertEqual(users["vk"], 1)

    async def test_services_section_structure(self) -> None:
        from web_configurator.metrics import collect_metrics

        metrics = await collect_metrics(
            self.db_path, rabbitmq_url="", telegram_token="", vk_token="",
            started_at=datetime(2026, 8, 5),
        )

        services = metrics["services"]
        for svc_name in ("telegram", "vk", "rabbitmq"):
            self.assertIn(svc_name, services, f"Missing services.{svc_name}")
            svc = services[svc_name]
            self.assertIn("ok", svc)
            self.assertIn("label", svc)
            self.assertIsInstance(svc["ok"], bool)

    async def test_delivery_section_structure(self) -> None:
        from web_configurator.metrics import collect_metrics

        metrics = await collect_metrics(
            self.db_path, rabbitmq_url="", telegram_token="", vk_token="",
            started_at=datetime(2026, 8, 5),
        )

        delivery = metrics["delivery"]
        self.assertIn("today", delivery)
        self.assertIn("total", delivery)

    async def test_schedule_section_structure(self) -> None:
        from web_configurator.metrics import collect_metrics

        metrics = await collect_metrics(
            self.db_path, rabbitmq_url="", telegram_token="", vk_token="",
            started_at=datetime(2026, 8, 5),
        )

        schedule = metrics["schedule"]
        for key in ("latest_parse", "latest_change", "changes", "active_groups_total", "active_groups"):
            self.assertIn(key, schedule, f"Missing schedule.{key}")

    async def test_user_rows_structure(self) -> None:
        from web_configurator.metrics import collect_metrics

        metrics = await collect_metrics(
            self.db_path, rabbitmq_url="", telegram_token="", vk_token="",
            started_at=datetime(2026, 8, 5),
        )

        self.assertEqual(len(metrics["user_rows"]), 2)
        row = metrics["user_rows"][0]
        for key in ("platform", "user_id", "username", "full_name", "subscription_title", "is_new"):
            self.assertIn(key, row, f"Missing user_rows[].{key}")

    async def test_services_all_disabled_when_empty_tokens(self) -> None:
        from web_configurator.metrics import collect_metrics

        metrics = await collect_metrics(
            self.db_path, rabbitmq_url="", telegram_token="", vk_token="",
            started_at=datetime(2026, 8, 5),
        )

        for svc_name in ("telegram", "vk", "rabbitmq"):
            self.assertFalse(metrics["services"][svc_name]["ok"],
                             f"{svc_name} should be not ok with empty token")


if __name__ == "__main__":
    unittest.main()

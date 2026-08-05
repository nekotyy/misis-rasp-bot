from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from src.db import Database
from src.models import UserRecord
from src.notifier import Broadcaster


def _make_user(platform: str, user_id: int, **kwargs) -> UserRecord:
    defaults = dict(
        username=None, full_name=None, subscription_type="group",
        subscription_key="600", subscription_title="КИБ-24-1",
        subscription_url=None, audience_subscription_key=None,
        audience_subscription_title=None, audience_subscription_url=None,
        group_name="КИБ-24-1", schedule_id=600, is_admin=False,
        is_editor=False, homework_notifications_enabled=True,
        delivery_disabled_auto=False, created_at="2026-01-01T00:00:00",
        last_seen_at="2026-08-05T00:00:00",
    )
    defaults.update(kwargs)
    return UserRecord(platform=platform, user_id=user_id, **defaults)


class TestBroadcasterResilience(unittest.IsolatedAsyncioTestCase):
    """Тесты на устойчивость Broadcaster — один падающий юзер не убивает рассылку."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_notifier.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

        # Создаём пользователей
        for i in range(1, 4):
            await self.db.upsert_user(
                "telegram", i, f"user{i}", f"User {i}",
                subscription_type="group", subscription_key="600",
                subscription_title="КИБ-24-1", schedule_id=600,
            )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_broadcast_continues_after_send_error(self) -> None:
        """Если отправка одному юзеру падает, рассылка продолжается."""
        mock_tg_bot = AsyncMock()
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("User blocked bot")
            msg = MagicMock()
            msg.message_id = call_count
            return msg

        mock_tg_bot.send_message = AsyncMock(side_effect=side_effect)

        broadcaster = Broadcaster(
            db=self.db,
            telegram_bot=mock_tg_bot,
            vk_bot=None,
            broker=None,
        )

        progress = await broadcaster.broadcast(
            "Тестовое сообщение",
            subscription_key="600",
        )

        # Все 3 юзера должны быть обработаны
        self.assertEqual(mock_tg_bot.send_message.call_count, 3)

    async def test_broadcast_without_bots_returns_empty_progress(self) -> None:
        """Broadcast без ботов не крашится, возвращает пустой прогресс."""
        broadcaster = Broadcaster(db=self.db, telegram_bot=None, vk_bot=None, broker=None)
        progress = await broadcaster.broadcast("Тест")
        self.assertEqual(progress.success_count, 0)


class TestBroadcasterInstantiation(unittest.TestCase):
    """Smoke-тесты на создание Broadcaster."""

    def test_creates_with_no_bots(self) -> None:
        db = MagicMock()
        broadcaster = Broadcaster(db=db)
        self.assertIsNone(broadcaster.telegram_bot)
        self.assertIsNone(broadcaster.vk_bot)

    def test_creates_with_all_params(self) -> None:
        broadcaster = Broadcaster(
            db=MagicMock(),
            telegram_bot=MagicMock(),
            vk_bot=MagicMock(),
            admin_telegram_id=100,
            admin_vk_id=200,
            broker=MagicMock(),
        )
        self.assertEqual(broadcaster.admin_telegram_id, 100)
        self.assertEqual(broadcaster.admin_vk_id, 200)


if __name__ == "__main__":
    unittest.main()

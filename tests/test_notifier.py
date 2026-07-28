import unittest
from unittest.mock import AsyncMock, MagicMock
from src.notifier import Broadcaster, CAMPAIGN_ADMIN_BROADCAST


class BroadcasterTargetPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_target_platform_all(self):
        mock_db = MagicMock()
        mock_db.auto_disable_undeliverable_telegram_users = AsyncMock(return_value=0)
        mock_db.get_users_for_notifications = AsyncMock(return_value=[])
        broadcaster = Broadcaster(db=mock_db, telegram_bot=MagicMock(), vk_bot=MagicMock())

        prog = await broadcaster.broadcast("Test message", target_platform="all", target_audience="all")

        self.assertTrue(prog.is_finished)
        mock_db.get_users_for_notifications.assert_any_call("telegram", schedule_id=None, subscription_key=None, target_audience="all")
        mock_db.get_users_for_notifications.assert_any_call("vk", schedule_id=None, subscription_key=None, target_audience="all")

    async def test_broadcast_target_platform_telegram(self):
        mock_db = MagicMock()
        mock_db.get_users_for_notifications = AsyncMock(return_value=[])
        broadcaster = Broadcaster(db=mock_db, telegram_bot=MagicMock(), vk_bot=MagicMock())

        prog = await broadcaster.broadcast("TG message", target_platform="telegram", target_audience="teachers", campaign_type=CAMPAIGN_ADMIN_BROADCAST)

        self.assertTrue(prog.is_finished)
        mock_db.get_users_for_notifications.assert_called_once_with("telegram", schedule_id=None, subscription_key=None, target_audience="teachers")

    async def test_broadcast_target_platform_vk(self):
        mock_db = MagicMock()
        mock_db.get_users_for_notifications = AsyncMock(return_value=[])
        broadcaster = Broadcaster(db=mock_db, telegram_bot=MagicMock(), vk_bot=MagicMock())

        prog = await broadcaster.broadcast("VK message", target_platform="vk", target_audience="students", campaign_type=CAMPAIGN_ADMIN_BROADCAST)

        self.assertTrue(prog.is_finished)
        mock_db.get_users_for_notifications.assert_called_once_with("vk", schedule_id=None, subscription_key=None, target_audience="students")


if __name__ == "__main__":
    unittest.main()

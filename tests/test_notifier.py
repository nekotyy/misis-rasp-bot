import unittest
from unittest.mock import AsyncMock, MagicMock
from src.notifier import Broadcaster, CAMPAIGN_ADMIN_BROADCAST


class BroadcasterTargetPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_target_platform_all(self):
        broadcaster = Broadcaster(db=MagicMock())
        broadcaster._broadcast_telegram = AsyncMock()
        broadcaster._broadcast_vk = AsyncMock()

        await broadcaster.broadcast("Test message", target_platform="all", target_audience="all")

        broadcaster._broadcast_telegram.assert_awaited_once_with(
            "Test message", schedule_id=None, subscription_key=None, campaign_type="notification", target_audience="all"
        )
        broadcaster._broadcast_vk.assert_awaited_once_with(
            "Test message", schedule_id=None, subscription_key=None, campaign_type="notification", target_audience="all"
        )

    async def test_broadcast_target_platform_telegram(self):
        broadcaster = Broadcaster(db=MagicMock())
        broadcaster._broadcast_telegram = AsyncMock()
        broadcaster._broadcast_vk = AsyncMock()

        await broadcaster.broadcast("TG message", target_platform="telegram", target_audience="teachers", campaign_type=CAMPAIGN_ADMIN_BROADCAST)

        broadcaster._broadcast_telegram.assert_awaited_once_with(
            "TG message", schedule_id=None, subscription_key=None, campaign_type=CAMPAIGN_ADMIN_BROADCAST, target_audience="teachers"
        )
        broadcaster._broadcast_vk.assert_not_awaited()

    async def test_broadcast_target_platform_vk(self):
        broadcaster = Broadcaster(db=MagicMock())
        broadcaster._broadcast_telegram = AsyncMock()
        broadcaster._broadcast_vk = AsyncMock()

        await broadcaster.broadcast("VK message", target_platform="vk", target_audience="students", campaign_type=CAMPAIGN_ADMIN_BROADCAST)

        broadcaster._broadcast_vk.assert_awaited_once_with(
            "VK message", schedule_id=None, subscription_key=None, campaign_type=CAMPAIGN_ADMIN_BROADCAST, target_audience="students"
        )
        broadcaster._broadcast_telegram.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

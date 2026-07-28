import unittest
from unittest.mock import AsyncMock, MagicMock
from src.notifier import Broadcaster, CAMPAIGN_ADMIN_BROADCAST


class BroadcasterTargetPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_target_platform_all(self):
        broadcaster = Broadcaster(db=MagicMock())
        broadcaster._broadcast_telegram = AsyncMock()
        broadcaster._broadcast_vk = AsyncMock()

        await broadcaster.broadcast("Test message", target_platform="all")

        broadcaster._broadcast_telegram.assert_awaited_once_with(
            "Test message", schedule_id=None, subscription_key=None, campaign_type="notification"
        )
        broadcaster._broadcast_vk.assert_awaited_once_with(
            "Test message", schedule_id=None, subscription_key=None, campaign_type="notification"
        )

    async def test_broadcast_target_platform_telegram(self):
        broadcaster = Broadcaster(db=MagicMock())
        broadcaster._broadcast_telegram = AsyncMock()
        broadcaster._broadcast_vk = AsyncMock()

        await broadcaster.broadcast("TG message", target_platform="telegram", campaign_type=CAMPAIGN_ADMIN_BROADCAST)

        broadcaster._broadcast_telegram.assert_awaited_once_with(
            "TG message", schedule_id=None, subscription_key=None, campaign_type=CAMPAIGN_ADMIN_BROADCAST
        )
        broadcaster._broadcast_vk.assert_not_awaited()

    async def test_broadcast_target_platform_vk(self):
        broadcaster = Broadcaster(db=MagicMock())
        broadcaster._broadcast_telegram = AsyncMock()
        broadcaster._broadcast_vk = AsyncMock()

        await broadcaster.broadcast("VK message", target_platform="vk", campaign_type=CAMPAIGN_ADMIN_BROADCAST)

        broadcaster._broadcast_vk.assert_awaited_once_with(
            "VK message", schedule_id=None, subscription_key=None, campaign_type=CAMPAIGN_ADMIN_BROADCAST
        )
        broadcaster._broadcast_telegram.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

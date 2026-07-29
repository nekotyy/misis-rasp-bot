import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aiogram import Dispatcher
from vkbottle.bot import Bot as VkBot

from src.config import Settings
from src.db import Database
from src.telegram_bot import build_dispatcher
from src.vk_bot import build_vk_bot


class BotInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_init.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

        self.mock_settings = MagicMock(spec=Settings)
        self.mock_settings.telegram_bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        self.mock_settings.vk_bot_token = "vk_test_token_1234567890"
        self.mock_settings.vk_disable_ssl_verify = True
        self.mock_settings.admin_telegram_ids = [100001]
        self.mock_settings.limited_admin_telegram_ids = []
        self.mock_settings.admin_vk_id = 200002
        self.mock_settings.schedule_url = "http://localhost/schedule"
        self.mock_settings.database_path = self.db_path

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_build_dispatcher_returns_valid_dispatcher(self):
        dispatcher = build_dispatcher(
            settings=self.mock_settings,
            db=self.db,
            parser=MagicMock(),
            broadcaster=MagicMock(),
            group_catalog=MagicMock(),
            search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )
        self.assertIsNotNone(dispatcher, "build_dispatcher returned None instead of Dispatcher instance!")
        self.assertIsInstance(dispatcher, Dispatcher, "build_dispatcher must return an aiogram.Dispatcher instance!")

    async def test_build_vk_bot_returns_valid_bot(self):
        vk_bot = build_vk_bot(
            settings=self.mock_settings,
            db=self.db,
            parser=MagicMock(),
            broadcaster=MagicMock(),
            group_catalog=MagicMock(),
            search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )
        self.assertIsNotNone(vk_bot, "build_vk_bot returned None when vk_bot_token was set!")
        self.assertIsInstance(vk_bot, VkBot, "build_vk_bot must return a vkbottle.bot.Bot instance!")


if __name__ == "__main__":
    unittest.main()

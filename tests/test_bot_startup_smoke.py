from __future__ import annotations

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


class TestBotStartupErrors(unittest.IsolatedAsyncioTestCase):
    """Тесты запуска ботов с невалидными/пустыми параметрами — не должно крашиться."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_startup.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_settings(self, **overrides) -> MagicMock:
        s = MagicMock(spec=Settings)
        s.telegram_bot_token = overrides.get("telegram_bot_token", "123456:FAKE-TOKEN")
        s.vk_bot_token = overrides.get("vk_bot_token", "vk_fake_token")
        s.vk_disable_ssl_verify = overrides.get("vk_disable_ssl_verify", False)
        s.admin_telegram_ids = overrides.get("admin_telegram_ids", [])
        s.limited_admin_telegram_ids = overrides.get("limited_admin_telegram_ids", [])
        s.admin_vk_id = overrides.get("admin_vk_id", None)
        s.schedule_url = overrides.get("schedule_url", "http://localhost/schedule")
        s.database_path = self.db_path
        return s

    async def test_dispatcher_with_empty_admin_ids(self) -> None:
        """Dispatcher создаётся без админов — не крашится."""
        settings = self._make_settings(admin_telegram_ids=[], admin_vk_id=None)
        dp = build_dispatcher(
            settings=settings, db=self.db,
            parser=MagicMock(), broadcaster=MagicMock(),
            group_catalog=MagicMock(), search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )
        self.assertIsInstance(dp, Dispatcher)

    async def test_dispatcher_with_multiple_admins(self) -> None:
        """Dispatcher с несколькими админами."""
        settings = self._make_settings(admin_telegram_ids=[111, 222, 333])
        dp = build_dispatcher(
            settings=settings, db=self.db,
            parser=MagicMock(), broadcaster=MagicMock(),
            group_catalog=MagicMock(), search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )
        self.assertIsInstance(dp, Dispatcher)

    async def test_dispatcher_without_optional_deps(self) -> None:
        """Dispatcher без broadcaster/catalog/search — не крашится."""
        settings = self._make_settings()
        dp = build_dispatcher(
            settings=settings, db=self.db,
            parser=MagicMock(),
            broadcaster=None,
            group_catalog=None,
            search_catalog=None,
            schedule_jobs=None,
        )
        self.assertIsInstance(dp, Dispatcher)

    async def test_vk_bot_with_empty_token(self) -> None:
        """VK бот с пустым токеном — возвращает None или не крашится."""
        settings = self._make_settings(vk_bot_token="")
        result = build_vk_bot(
            settings=settings, db=self.db,
            parser=MagicMock(), broadcaster=MagicMock(),
            group_catalog=MagicMock(), search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )
        # Пустой токен может вернуть None или пустой бот — главное не упасть
        # Если бот создаётся всё равно, это ОК (vkbottle не валидирует при создании)

    async def test_vk_bot_with_ssl_verify_disabled(self) -> None:
        """VK бот с отключённой SSL проверкой."""
        settings = self._make_settings(vk_disable_ssl_verify=True)
        result = build_vk_bot(
            settings=settings, db=self.db,
            parser=MagicMock(), broadcaster=MagicMock(),
            group_catalog=MagicMock(), search_catalog=MagicMock(),
            schedule_jobs=MagicMock(),
        )
        self.assertIsNotNone(result)

    async def test_vk_bot_without_optional_deps(self) -> None:
        """VK бот без необязательных зависимостей."""
        settings = self._make_settings()
        result = build_vk_bot(
            settings=settings, db=self.db,
            parser=MagicMock(),
            broadcaster=None,
            group_catalog=None,
            search_catalog=None,
            schedule_jobs=None,
        )
        # Главное — не крашится
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from src.db import Database
from src.telegram_bot import build_admin_status_text
from src.vk_bot import build_vk_admin_status_text


class TestAdminStatusAndStats(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_misis.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_get_group_user_stats_with_chats_and_personal(self) -> None:
        await self.db.upsert_user("telegram", 1001, "tg_user1", "TG User 1")
        await self.db.set_user_subscription("telegram", 1001, "group", "ИСП-25-1", "ИСП-25-1", 101)

        await self.db.upsert_user("telegram", 1002, "tg_user2", "TG User 2")
        await self.db.set_user_subscription("telegram", 1002, "group", "ИСП-25-1", "ИСП-25-1", 101)

        await self.db.upsert_user("telegram", -100123456789, "chat1", "TG Chat 1")
        await self.db.set_user_subscription("telegram", -100123456789, "group", "ИСП-25-1", "ИСП-25-1", 101)

        await self.db.upsert_user("vk", 2001, "vk_user1", "VK User 1")
        await self.db.set_user_subscription("vk", 2001, "group", "ИСП-25-1", "ИСП-25-1", 101)

        await self.db.upsert_user("vk", 2000000005, "vk_chat1", "VK Chat 1")
        await self.db.set_user_subscription("vk", 2000000005, "group", "ИСП-25-1", "ИСП-25-1", 101)

        await self.db.upsert_user("telegram", 1003, "tg_user3", "TG User 3")
        await self.db.set_user_subscription("telegram", 1003, "group", "МТО-25", "МТО-25", 102)

        stats = await self.db.get_group_user_stats()
        self.assertEqual(len(stats), 2)

        isp_stat = next(s for s in stats if s["group_name"] == "ИСП-25-1")
        self.assertEqual(isp_stat["users_count"], 5)
        self.assertEqual(isp_stat["personal_count"], 3)
        self.assertEqual(isp_stat["chat_count"], 2)
        self.assertEqual(isp_stat["tg_chat_count"], 1)
        self.assertEqual(isp_stat["vk_chat_count"], 1)

        mto_stat = next(s for s in stats if s["group_name"] == "МТО-25")
        self.assertEqual(mto_stat["users_count"], 1)
        self.assertEqual(mto_stat["personal_count"], 1)
        self.assertEqual(mto_stat["chat_count"], 0)

    async def test_telegram_admin_status_formatting(self) -> None:
        await self.db.upsert_user("telegram", 1001, "tg_user1", "TG User 1")
        await self.db.set_user_subscription("telegram", 1001, "group", "ИСП-25-1", "ИСП-25-1", 101)
        await self.db.upsert_user("telegram", -100123456789, "chat1", "TG Group Chat")
        await self.db.set_user_subscription("telegram", -100123456789, "group", "ИСП-25-1", "ИСП-25-1", 101)
        await self.db.upsert_user("vk", 2001, "vk_user1", "VK User 1")
        await self.db.set_user_subscription("vk", 2001, "group", "ИСП-25-1", "ИСП-25-1", 101)
        await self.db.upsert_user("vk", 2000000001, "vk_chat1", "VK Conversation")
        await self.db.set_user_subscription("vk", 2000000001, "group", "ИСП-25-1", "ИСП-25-1", 101)

        status_text = await build_admin_status_text(self.db)

        self.assertIn("Пользователей: <b>4</b>.", status_text)
        self.assertIn("Пользователей с VK: <b>2</b>.", status_text)
        self.assertIn("Пользователей с TG: <b>2</b>.", status_text)
        self.assertIn("Личных пользователей: <b>2</b>", status_text)
        self.assertIn("Всего бесед и групп: <b>2</b>.", status_text)
        self.assertIn("Групповых чатов в Telegram: <b>1</b>.", status_text)
        self.assertIn("Бесед ВКонтакте: <b>1</b>.", status_text)
        self.assertIn("ИСП-25-1", status_text)
        self.assertIn("───────────────────────────", status_text)

    async def test_vk_admin_status_formatting(self) -> None:
        await self.db.upsert_user("telegram", 1001, "tg_user1", "TG User 1")
        await self.db.set_user_subscription("telegram", 1001, "group", "ИСП-25-1", "ИСП-25-1", 101)
        await self.db.upsert_user("telegram", -100123456789, "chat1", "TG Group Chat")
        await self.db.set_user_subscription("telegram", -100123456789, "group", "ИСП-25-1", "ИСП-25-1", 101)
        await self.db.upsert_user("vk", 2001, "vk_user1", "VK User 1")
        await self.db.set_user_subscription("vk", 2001, "group", "ИСП-25-1", "ИСП-25-1", 101)

        status_text = await build_vk_admin_status_text(self.db)

        self.assertIn("Пользователей: 3.", status_text)
        self.assertIn("Пользователей с VK: 1.", status_text)
        self.assertIn("Пользователей с TG: 2.", status_text)
        self.assertIn("Личных пользователей: 2 (TG: 1, VK: 1).", status_text)
        self.assertIn("Всего бесед и групп: 1.", status_text)
        self.assertIn("Групповых чатов в Telegram: 1.", status_text)
        self.assertIn("Бесед ВКонтакте: 0.", status_text)
        self.assertIn("ИСП-25-1", status_text)
        self.assertIn("───────────────────────────", status_text)


if __name__ == "__main__":
    unittest.main()

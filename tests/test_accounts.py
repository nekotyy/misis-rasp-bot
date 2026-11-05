import tempfile
import unittest
from pathlib import Path

import aiosqlite

from src.account_service import AccountService
from src.db import Database


class AccountServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test_accounts.db"
        self.db = Database(self.path)
        await self.db.initialize()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_get_or_create_account_creates_identity_and_migrates_existing_user_sub(self):
        # 1. Сначала пользователь создаётся через старый бот
        await self.db.upsert_user("telegram", 101, "tg_user", "Telegram User")
        await self.db.set_user_subscription(
            "telegram", 101, "group", "group:600", "ИСП-25-1", "http://asu/rasp/600", "ИСП-25-1", 600,
        )

        async with aiosqlite.connect(self.path) as conn:
            account = await AccountService.get_or_create_account(conn, "telegram", 101)
            self.assertIsNotNone(account)
            self.assertEqual(len(account.identities), 1)
            self.assertEqual(account.identities[0].platform, "telegram")
            self.assertEqual(account.identities[0].user_id, 101)

            # Проверяем, что основная группа перенеслась в account_subscriptions
            self.assertIsNotNone(account.primary_subscription)
            self.assertEqual(account.primary_subscription.subscription_key, "group:600")
            self.assertTrue(account.primary_subscription.is_primary)

    async def test_multiple_subscriptions_and_primary_switch(self):
        async with aiosqlite.connect(self.path) as conn:
            account = await AccountService.get_or_create_account(conn, "telegram", 102)

            # Добавляем первую группу (становится основной)
            sub1 = await AccountService.add_subscription(
                conn, account.id, subscription_type="group", subscription_key="group:600",
                subscription_title="ИСП-25-1", subscription_url="url1", group_name="ИСП-25-1",
                schedule_id=600, is_primary=True,
            )
            self.assertTrue(sub1.is_primary)

            # Добавляем вторую группу (дополнительная)
            sub2 = await AccountService.add_subscription(
                conn, account.id, subscription_type="group", subscription_key="group:700",
                subscription_title="МТО-26", subscription_url="url2", group_name="МТО-26",
                schedule_id=700, is_primary=False,
            )
            self.assertFalse(sub2.is_primary)

            acc_updated = await AccountService.get_account(conn, account.id)
            self.assertEqual(len(acc_updated.subscriptions), 2)
            self.assertEqual(acc_updated.primary_subscription.subscription_key, "group:600")

            # Переключаем основную группу на вторую
            await AccountService.update_subscription(conn, account.id, sub2.id, is_primary=True)
            acc_switched = await AccountService.get_account(conn, account.id)
            self.assertEqual(acc_switched.primary_subscription.subscription_key, "group:700")

    async def test_linked_accounts_sync_primary_subscription_and_preferences(self):
        # Пользователь в Telegram
        await self.db.upsert_user("telegram", 201, "tg_john", "John TG")
        await self.db.set_user_subscription(
            "telegram", 201, "group", "group:600", "ИСП-25-1", "http://asu/rasp/600", "ИСП-25-1", 600,
        )

        # Пользователь в VK
        await self.db.upsert_user("vk", 301, "vk_john", "John VK")
        await self.db.set_user_subscription(
            "vk", 301, "group", "group:700", "МТО-26", "http://asu/rasp/700", "МТО-26", 700,
        )

        async with aiosqlite.connect(self.path) as conn:
            tg_acc = await AccountService.get_or_create_account(conn, "telegram", 201)
            vk_acc = await AccountService.get_or_create_account(conn, "vk", 301)
            self.assertNotEqual(tg_acc.id, vk_acc.id)

            # Создаём запрос на связывание из Telegram
            raw_token, _ = await AccountService.create_link_request(conn, tg_acc.id, "telegram", 201)

            # Подтверждаем связывание из VK (с выбором Telegram-группы как основной)
            success = await AccountService.confirm_link(conn, raw_token, "vk", 301, primary_choice="initiator")
            self.assertTrue(success)
            await conn.commit()

        # После связывания оба бота ссылаются на один аккаунт
        async with aiosqlite.connect(self.path) as conn:
            acc_by_tg = await AccountService.get_account_by_identity(conn, "telegram", 201)
            acc_by_vk = await AccountService.get_account_by_identity(conn, "vk", 301)
            self.assertEqual(acc_by_tg.id, acc_by_vk.id)
            self.assertEqual(len(acc_by_tg.identities), 2)
            self.assertEqual(acc_by_tg.primary_subscription.subscription_key, "group:600")

        # Проверяем, что в users для VK обновилась основная подписка на общую
        vk_user = await self.db.get_user("vk", 301)
        self.assertEqual(vk_user.subscription_key, "group:600")

        # Теперь меняем группу в VK-боте — изменение синхронизируется и в Telegram!
        await self.db.set_user_subscription(
            "vk", 301, "group", "group:800", "ЭВМ-24", "http://asu/rasp/800", "ЭВМ-24", 800,
        )

        tg_user_after = await self.db.get_user("telegram", 201)
        self.assertEqual(tg_user_after.subscription_key, "group:800")

    async def test_active_sources_and_notifications_include_additional_groups(self):
        # Пользователь 1 подписан на группу 600 как основную
        await self.db.upsert_user("telegram", 401, "user401", "User 401")
        await self.db.set_user_subscription(
            "telegram", 401, "group", "group:600", "ИСП-25-1", "url600", "ИСП-25-1", 600,
        )

        # Добавляем ему дополнительную группу 700 через account_subscriptions
        async with aiosqlite.connect(self.path) as conn:
            acc = await AccountService.get_account_by_identity(conn, "telegram", 401)
            await AccountService.add_subscription(
                conn, acc.id, subscription_type="group", subscription_key="group:700",
                subscription_title="МТО-26", subscription_url="url700", group_name="МТО-26",
                schedule_id=700, is_primary=False,
            )
            await conn.commit()

        # 1. get_active_sources включает и 600, и 700!
        sources = await self.db.get_active_sources()
        source_keys = {s["source_key"] for s in sources}
        self.assertIn("group:600", source_keys)
        self.assertIn("group:700", source_keys)

        # 2. get_users_for_notifications для группы 700 возвращает user 401
        recipients_700 = await self.db.get_users_for_notifications("telegram", schedule_id=700, subscription_key="group:700")
        self.assertEqual(len(recipients_700), 1)
        self.assertEqual(recipients_700[0].user_id, 401)

    async def test_notification_deduplication_for_linked_account(self):
        # Пользователь привязал Telegram (501) и VK (601)
        await self.db.upsert_user("telegram", 501, "u501", "User 501")
        await self.db.set_user_subscription(
            "telegram", 501, "group", "group:600", "ИСП-25-1", "url", "ИСП-25-1", 600,
        )
        await self.db.upsert_user("vk", 601, "u601", "User 601")
        await self.db.set_user_subscription(
            "vk", 601, "group", "group:600", "ИСП-25-1", "url", "ИСП-25-1", 600,
        )

        async with aiosqlite.connect(self.path) as conn:
            tg_acc = await AccountService.get_or_create_account(conn, "telegram", 501)
            raw_token, _ = await AccountService.create_link_request(conn, tg_acc.id, "telegram", 501)
            await AccountService.confirm_link(conn, raw_token, "vk", 601)
            await conn.commit()

        # Запрос получателей для Telegram возвращает ровно одну запись
        tg_recipients = await self.db.get_users_for_notifications("telegram", schedule_id=600, subscription_key="group:600")
        self.assertEqual(len(tg_recipients), 1)
        self.assertEqual(tg_recipients[0].user_id, 501)

        # Запрос получателей для VK возвращает ровно одну запись
        vk_recipients = await self.db.get_users_for_notifications("vk", schedule_id=600, subscription_key="group:600")
        self.assertEqual(len(vk_recipients), 1)
        self.assertEqual(vk_recipients[0].user_id, 601)

    async def test_unlink_identity_splits_cleanly_and_preserves_data(self):
        await self.db.upsert_user("telegram", 701, "u701", "User 701")
        await self.db.set_user_subscription(
            "telegram", 701, "group", "group:600", "ИСП-25-1", "url", "ИСП-25-1", 600,
        )
        await self.db.upsert_user("vk", 801, "u801", "User 801")
        await self.db.set_user_subscription(
            "vk", 801, "group", "group:600", "ИСП-25-1", "url", "ИСП-25-1", 600,
        )

        async with aiosqlite.connect(self.path) as conn:
            tg_acc = await AccountService.get_or_create_account(conn, "telegram", 701)
            raw_token, _ = await AccountService.create_link_request(conn, tg_acc.id, "telegram", 701)
            await AccountService.confirm_link(conn, raw_token, "vk", 801)
            await conn.commit()

            # Отвязываем VK
            new_acc_id = await AccountService.unlink_identity(conn, tg_acc.id, "vk")
            await conn.commit()

            old_acc = await AccountService.get_account(conn, tg_acc.id)
            new_acc = await AccountService.get_account(conn, new_acc_id)

            self.assertEqual(len(old_acc.identities), 1)
            self.assertEqual(old_acc.identities[0].platform, "telegram")

            self.assertEqual(len(new_acc.identities), 1)
            self.assertEqual(new_acc.identities[0].platform, "vk")
            self.assertEqual(new_acc.primary_subscription.subscription_key, "group:600")


if __name__ == "__main__":
    unittest.main()

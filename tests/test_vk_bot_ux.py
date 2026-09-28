"""Регрессы UX VK-бота: беседы, упоминания бота, навигация по админке, ошибки."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.config import Settings
from src.db import Database
from src.subscription_utils import make_group_subscription
from src.vk_bot import USER_ERROR_TEXT, build_vk_bot, strip_vk_bot_mention

ADMIN_ID = 111
USER_ID = 222
CHAT_PEER = 2_000_000_005


def _settings(db_path: Path) -> MagicMock:
    settings = MagicMock(spec=Settings)
    settings.telegram_bot_token = "123456:FAKE-TOKEN"
    settings.vk_bot_token = "vk_fake_token"
    settings.vk_disable_ssl_verify = False
    settings.admin_telegram_ids = []
    settings.limited_admin_telegram_ids = []
    settings.admin_vk_id = ADMIN_ID
    settings.schedule_url = "http://localhost/schedule"
    settings.database_path = db_path
    settings.lesson_counters_enabled = False
    settings.lesson_counters_path = db_path.parent / "lesson_counters.json"
    settings.ocr_timeout_seconds = 180.0
    return settings


class StripMentionTests(unittest.TestCase):
    def test_mention_before_button_label_is_removed(self) -> None:
        self.assertEqual(strip_vk_bot_mention("[club237526231|@misisrasp] Расписание"), "Расписание")
        self.assertEqual(strip_vk_bot_mention("[public1|Бот], /startgroup"), "/startgroup")
        self.assertEqual(strip_vk_bot_mention("@club237526231 Помощь"), "Помощь")

    def test_plain_text_is_unchanged(self) -> None:
        self.assertEqual(strip_vk_bot_mention("  ИСП-25-1 "), "ИСП-25-1")
        self.assertEqual(strip_vk_bot_mention(None), "")


class VkBotUxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "vk_ux.db"
        self.db = Database(self.db_path)
        await self.db.initialize()
        for user_id in (ADMIN_ID, USER_ID):
            await self.db.upsert_user(platform="vk", user_id=user_id, username=None, full_name="Тест")
            await self.db.set_user_subscription("vk", user_id, **make_group_subscription("ИСП-25-1", 600))

        self.group_catalog = MagicMock()
        self.group_catalog.find_group = AsyncMock(return_value=SimpleNamespace(group_name="ИСП-25-1", schedule_id=600))
        self.search_catalog = MagicMock()
        self.search_catalog.find = AsyncMock(return_value=None)
        self.bot = build_vk_bot(
            settings=_settings(self.db_path),
            db=self.db,
            parser=MagicMock(),
            broadcaster=None,
            group_catalog=self.group_catalog,
            search_catalog=self.search_catalog,
            schedule_jobs=None,
        )

        async def fake_request(method: str, params: dict, version: str | None = None):
            if method == "users.get":
                return {"response": []}
            if method == "messages.getConversationMembers":
                return {"response": {"count": 1, "items": [{"member_id": USER_ID, "is_admin": True}]}}
            if method == "messages.send":
                return {"response": 1}
            return {"response": None}

        self.bot.api.request = AsyncMock(side_effect=fake_request)
        self.handler = next(
            item.handler for item in self.bot.labeler.message_view.handlers if item.handler.__name__ == "all_messages_handler"
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def send(self, text: str, *, user_id: int = USER_ID, peer_id: int | None = None) -> None:
        await self.handler(SimpleNamespace(peer_id=peer_id or user_id, from_id=user_id, text=text, action=None, attachments=[]))

    def sent(self) -> list[dict]:
        return [call.args[1] for call in self.bot.api.request.await_args_list if call.args[0] == "messages.send"]

    def last_screen(self) -> tuple[str, list[str]]:
        params = self.sent()[-1]
        keyboard = json.loads(params["keyboard"]) if params.get("keyboard") else {"buttons": []}
        return params["message"], [button["action"]["label"] for row in keyboard["buttons"] for button in row]

    async def test_chat_chatter_is_ignored(self) -> None:
        await self.send("привет всем, кто идёт на пары?", peer_id=CHAT_PEER)
        self.assertEqual(self.sent(), [])

    async def test_chat_setup_saves_subscription_for_the_chat(self) -> None:
        await self.send("[club1|@bot] /startgroup", peer_id=CHAT_PEER)
        self.assertIn("Быстрая настройка беседы", self.sent()[-1]["message"])

        await self.send("ИСП-25-1", peer_id=CHAT_PEER)

        chat = await self.db.get_user("vk", CHAT_PEER)
        self.assertIsNotNone(chat, "у беседы должна появиться своя запись — иначе подписка не сохраняется")
        self.assertEqual(chat.subscription_title, "ИСП-25-1")
        self.assertIn("Беседа подписана", self.sent()[-1]["message"])
        users = await self.db.get_users_for_notifications("vk", schedule_id=600)
        self.assertIn(CHAT_PEER, [user.user_id for user in users])

    async def test_admin_button_works_from_search_mode(self) -> None:
        await self.send("Найти расписание", user_id=ADMIN_ID)
        await self.send("Админка", user_id=ADMIN_ID)
        text, labels = self.last_screen()
        self.assertIn("Админ-панель", text)
        self.search_catalog.find.assert_not_awaited()

    async def test_search_prompt_has_back_button(self) -> None:
        await self.send("Найти расписание")
        _, labels = self.last_screen()
        self.assertEqual(labels, ["Назад в меню"])

    async def test_back_from_status_returns_to_admin_not_user_menu(self) -> None:
        await self.send("Админка", user_id=ADMIN_ID)
        await self.send("Ошибки за день", user_id=ADMIN_ID)
        _, labels = self.last_screen()
        self.assertIn("Назад в админку", labels)
        await self.send("Назад в админку", user_id=ADMIN_ID)
        text, _ = self.last_screen()
        self.assertIn("Админ-панель", text)

    async def test_mention_prefixed_button_is_recognized(self) -> None:
        await self.send("[club1|@bot] Дополнительно")
        text, _ = self.last_screen()
        self.assertIn("Дополнительно", text)

    async def test_update_failure_notifies_user_with_friendly_text(self) -> None:
        update = {"object": {"message": {"peer_id": USER_ID, "from_id": USER_ID, "text": "x"}}}
        await self.bot.report_update_failure(update, RuntimeError("boom"))
        self.assertEqual(self.sent()[-1]["message"], USER_ERROR_TEXT)

    async def test_profile_refresh_failure_does_not_block_reply(self) -> None:
        original = self.bot.api.request.side_effect

        async def failing_users_get(method, params, version=None):
            if method == "users.get":
                raise RuntimeError("VK users.get down")
            return await original(method, params, version)

        self.bot.api.request.side_effect = failing_users_get
        await self.send("Дополнительно", user_id=333)
        self.assertTrue(self.sent(), "бот должен ответить, даже если профиль не обновился")


if __name__ == "__main__":
    unittest.main()

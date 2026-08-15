import re
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.telegram_bot import (
    format_help_commands_text,
    format_help_group_setup_text,
    format_help_main_text,
    format_help_notifications_text,
    format_help_personal_setup_text,
    format_help_personalization_text,
    resolve_subscription_input,
)
from src.telegram_bot import (
    is_group_setup_command as is_group_setup_tg,
)
from src.vk_bot import (
    is_group_setup_command as is_group_setup_vk,
)
from src.vk_bot import (
    vk_help_commands_text,
    vk_help_group_setup_text,
    vk_help_notifications_text,
    vk_help_personal_setup_text,
    vk_help_personalization_text,
)


class GroupSetupAndHelpTests(unittest.IsolatedAsyncioTestCase):
    EMOJI_PATTERN = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE,
    )

    def test_telegram_wiki_texts_no_emojis(self):
        texts = [
            format_help_main_text(),
            format_help_group_setup_text(),
            format_help_personal_setup_text(),
            format_help_notifications_text(),
            format_help_personalization_text(),
            format_help_commands_text(),
        ]
        for text in texts:
            self.assertFalse(
                bool(self.EMOJI_PATTERN.search(text)),
                f"Text contains emojis: {text}",
            )
            self.assertNotIn("👥", text)
            self.assertNotIn("👤", text)
            self.assertNotIn("❓", text)

    def test_vk_wiki_texts_no_emojis(self):
        texts = [
            vk_help_group_setup_text(),
            vk_help_personal_setup_text(),
            vk_help_notifications_text(),
            vk_help_personalization_text(),
            vk_help_commands_text(),
        ]
        for text in texts:
            self.assertFalse(
                bool(self.EMOJI_PATTERN.search(text)),
                f"Text contains emojis: {text}",
            )
            self.assertNotIn("👥", text)
            self.assertNotIn("👤", text)
            self.assertNotIn("❓", text)

    def test_wiki_content(self):
        group_text = format_help_group_setup_text()
        self.assertIn("/startgroup", group_text)
        self.assertIn("Telegram", group_text)
        self.assertIn("ВКонтакте", group_text)
        self.assertIn("vk.ru/app6441755_-237526231", group_text)
        self.assertNotIn("https://vk.ru/app6441755_-237526231", group_text)
        self.assertNotIn("Разрешать добавлять сообщество в беседы", group_text)

        vk_group_text = vk_help_group_setup_text()
        self.assertIn("/startgroup", vk_group_text)
        self.assertIn("Telegram", vk_group_text)
        self.assertIn("ВКонтакте", vk_group_text)
        self.assertIn("vk.ru/app6441755_-237526231", vk_group_text)
        self.assertNotIn("https://vk.ru/app6441755_-237526231", vk_group_text)
        self.assertNotIn("Разрешать добавлять сообщество в беседы", vk_group_text)

        cmd_text = format_help_commands_text()
        self.assertIn("/start", cmd_text)
        self.assertIn("/rasp", cmd_text)

    def test_is_group_setup_command_strict(self):
        valid_variations = [
            "/startgroup",
            "/group",
            "startgroup",
            "group",
            "Группа",
            "группа",
            "ГРУППА",
            "Настройка группы",
            "настройка группы",
            "НАСТРОЙКА ГРУППЫ",
            "Настройки группы",
            "настройки группы",
            "Настроить группу",
            "настроить группу",
            "  настройка   группы  ",
            "/startgroup@misis_bot",
            "Группа ИСП-25-1",
            "Настройка группы: ИСП-25-1",
        ]
        for var in valid_variations:
            self.assertTrue(is_group_setup_vk(var), f"VK failed on: {var}")
            self.assertTrue(is_group_setup_tg(var), f"TG failed on: {var}")

        invalid_variations = [
            "Привет",
            "ИСП-25-1",
            "Расписание",
            "Дополнительно",
            "Помощь",
        ]
        for var in invalid_variations:
            self.assertFalse(is_group_setup_vk(var), f"VK false positive on: {var}")
            self.assertFalse(is_group_setup_tg(var), f"TG false positive on: {var}")

    async def test_resolve_subscription_input_network_error(self):
        mock_catalog = MagicMock()
        mock_catalog.find_group = AsyncMock(side_effect=Exception("Network down"))
        sub_data, error_text = await resolve_subscription_input(
            "ИСП-25-1",
            target_group_catalog=mock_catalog,
        )
        self.assertIsNone(sub_data)
        self.assertIn("Сайт расписания колледжа сейчас недоступен", error_text)


if __name__ == "__main__":
    unittest.main()

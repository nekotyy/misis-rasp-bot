from __future__ import annotations

import json
import unittest

from src.vk_bot import vk_help_main_keyboard


class TestVkHelpMainKeyboard(unittest.TestCase):
    """Тесты для vk_help_main_keyboard — главное меню помощи VK."""

    def test_returns_valid_json_string(self) -> None:
        result = vk_help_main_keyboard()
        self.assertIsInstance(result, str)
        parsed = json.loads(result)
        self.assertIsInstance(parsed, dict)

    def test_has_buttons_key(self) -> None:
        parsed = json.loads(vk_help_main_keyboard())
        self.assertIn("buttons", parsed)
        self.assertIsInstance(parsed["buttons"], list)
        self.assertGreater(len(parsed["buttons"]), 0, "Клавиатура без кнопок")

    def test_buttons_have_actions(self) -> None:
        parsed = json.loads(vk_help_main_keyboard())
        for row in parsed["buttons"]:
            self.assertIsInstance(row, list)
            for btn in row:
                self.assertIn("action", btn)
                action = btn["action"]
                self.assertIn("type", action)
                self.assertIn("label", action)
                self.assertTrue(action["label"].strip(), "Пустой текст кнопки")

    def test_has_one_row_attribute(self) -> None:
        parsed = json.loads(vk_help_main_keyboard())
        self.assertIn("one_time", parsed)

    def test_inline_attribute(self) -> None:
        parsed = json.loads(vk_help_main_keyboard())
        # VK keyboard может быть inline или обычной
        self.assertIn("inline", parsed)


if __name__ == "__main__":
    unittest.main()

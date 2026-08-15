from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from src.vk_bot import (
    build_vk_subscription_settings_keyboard,
    make_vk_keyboard,
    vk_help_main_keyboard,
)


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
        self.assertIn("inline", parsed)


class TestVkSettingsKeyboard(unittest.TestCase):
    """Тесты для build_vk_subscription_settings_keyboard — меню настроек VK."""

    def _extract_labels(self, keyboard_json: str) -> list[str]:
        parsed = json.loads(keyboard_json)
        labels: list[str] = []
        for row in parsed.get("buttons", []):
            for btn in row:
                labels.append(btn["action"]["label"])
        return labels

    def test_help_button_in_settings_keyboard_default(self) -> None:
        kb_json = build_vk_subscription_settings_keyboard(None)
        labels = self._extract_labels(kb_json)
        self.assertIn("Помощь", labels)
        self.assertIn("Пройденные пары", labels)
        self.assertIn("Персонализация", labels)
        self.assertIn("О проекте", labels)
        self.assertIn("Назад в меню", labels)

    def test_help_button_in_settings_keyboard_student(self) -> None:
        user = SimpleNamespace(
            homework_notifications_enabled=True,
            subscription_key="ISP-25-1",
            subscription_type="group",
            audience_subscription_key=None,
        )
        kb_json = build_vk_subscription_settings_keyboard(user)
        labels = self._extract_labels(kb_json)
        self.assertIn("Помощь", labels)
        self.assertIn("Отписаться от группы", labels)
        self.assertIn("Отключить уведомления", labels)

    def test_help_button_in_settings_keyboard_teacher(self) -> None:
        user = SimpleNamespace(
            homework_notifications_enabled=False,
            subscription_key="Ivanov",
            subscription_type="teacher",
            audience_subscription_key="101",
        )
        kb_json = build_vk_subscription_settings_keyboard(user)
        labels = self._extract_labels(kb_json)
        self.assertIn("Помощь", labels)
        self.assertIn("Включить уведомления", labels)
        self.assertIn("Изменить кабинет", labels)
        self.assertIn("Убрать кабинет", labels)
        self.assertIn("Отписаться от группы", labels)


if __name__ == "__main__":
    unittest.main()


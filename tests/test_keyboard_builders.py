from __future__ import annotations

import unittest

from aiogram.types import InlineKeyboardMarkup

from src.telegram_bot import (
    build_admin_broadcast_preview_keyboard,
    build_help_main_keyboard,
    format_user_profile_link,
)


class TestBuildHelpMainKeyboard(unittest.TestCase):
    """Тесты для build_help_main_keyboard — главная клавиатура помощи."""

    def test_returns_inline_keyboard(self) -> None:
        kb = build_help_main_keyboard()
        self.assertIsInstance(kb, InlineKeyboardMarkup)

    def test_has_expected_buttons(self) -> None:
        kb = build_help_main_keyboard()
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        self.assertGreaterEqual(len(all_buttons), 4, "Должно быть минимум 4 кнопки помощи")

    def test_all_buttons_have_callback_data(self) -> None:
        kb = build_help_main_keyboard()
        for row in kb.inline_keyboard:
            for btn in row:
                self.assertIsNotNone(btn.callback_data, f"Кнопка '{btn.text}' без callback_data")
                self.assertTrue(btn.callback_data.startswith("help:") or btn.callback_data.startswith("menu:"),
                                f"Неожиданный callback: {btn.callback_data}")

    def test_buttons_have_nonempty_text(self) -> None:
        kb = build_help_main_keyboard()
        for row in kb.inline_keyboard:
            for btn in row:
                self.assertTrue(btn.text.strip(), "Пустой текст кнопки")


class TestBuildAdminBroadcastPreviewKeyboard(unittest.TestCase):
    """Тесты для build_admin_broadcast_preview_keyboard — клавиатура рассылки."""

    def test_default_platform_all(self) -> None:
        kb = build_admin_broadcast_preview_keyboard()
        all_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        # По умолчанию "Везде" должна быть с галочкой
        self.assertTrue(any("✅" in t and "Везде" in t for t in all_texts),
                        f"Не найдена активная кнопка 'Везде': {all_texts}")

    def test_platform_telegram(self) -> None:
        kb = build_admin_broadcast_preview_keyboard(target_platform="telegram")
        all_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("✅" in t and "ТГ" in t for t in all_texts))

    def test_platform_vk(self) -> None:
        kb = build_admin_broadcast_preview_keyboard(target_platform="vk")
        all_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("✅" in t and "ВК" in t for t in all_texts))

    def test_all_buttons_have_callback_data(self) -> None:
        kb = build_admin_broadcast_preview_keyboard()
        for row in kb.inline_keyboard:
            for btn in row:
                self.assertIsNotNone(btn.callback_data, f"Кнопка '{btn.text}' без callback_data")


class TestFormatUserProfileLink(unittest.TestCase):
    """Тесты для format_user_profile_link — форматирование ссылки на профиль."""

    def test_telegram_with_username_html(self) -> None:
        result = format_user_profile_link("telegram", 12345, "alice")
        self.assertIn("t.me/alice", result)
        self.assertIn("12345", result)
        self.assertIn("<a ", result)  # HTML link

    def test_telegram_with_at_prefix(self) -> None:
        result = format_user_profile_link("telegram", 12345, "@alice")
        self.assertIn("t.me/alice", result)
        self.assertNotIn("@@", result)

    def test_telegram_without_username(self) -> None:
        result = format_user_profile_link("telegram", 12345, None)
        self.assertIn("12345", result)

    def test_telegram_no_html(self) -> None:
        result = format_user_profile_link("telegram", 12345, "alice", html=False)
        self.assertNotIn("<a ", result)
        self.assertIn("t.me/alice", result)

    def test_vk_user(self) -> None:
        result = format_user_profile_link("vk", 67890)
        self.assertIn("67890", result)

    def test_none_user_id(self) -> None:
        result = format_user_profile_link("telegram", None)
        self.assertEqual(result, "неизвестно")

    def test_case_insensitive_platform(self) -> None:
        result = format_user_profile_link("Telegram", 12345, "alice")
        self.assertIn("t.me/alice", result)


class TestAdminKeyboards(unittest.TestCase):
    """Тесты для админских клавиатур и навигации."""

    def test_admin_keyboards_structure(self) -> None:
        from src.telegram_bot import (
            ADMIN_BACK_KEYBOARD,
            ADMIN_DAILY_ERRORS_KEYBOARD,
            ADMIN_KEYBOARD,
            ADMIN_KEYBOARD_LIMITED,
            ADMIN_STATUS_KEYBOARD,
        )

        # ADMIN_KEYBOARD
        full_callbacks = [btn.callback_data for row in ADMIN_KEYBOARD.inline_keyboard for btn in row]
        self.assertIn("admin:status", full_callbacks)
        self.assertIn("admin:daily_errors", full_callbacks)
        self.assertIn("admin:download_db", full_callbacks)
        self.assertIn("admin:download_counters", full_callbacks)
        self.assertIn("admin:close", full_callbacks)

        # ADMIN_KEYBOARD_LIMITED
        limited_callbacks = [btn.callback_data for row in ADMIN_KEYBOARD_LIMITED.inline_keyboard for btn in row]
        self.assertIn("admin:status", limited_callbacks)
        self.assertIn("admin:daily_errors", limited_callbacks)

        # ADMIN_BACK_KEYBOARD
        back_callbacks = [btn.callback_data for row in ADMIN_BACK_KEYBOARD.inline_keyboard for btn in row]
        self.assertEqual(back_callbacks, ["admin:back"])

        # ADMIN_STATUS_KEYBOARD
        status_callbacks = [btn.callback_data for row in ADMIN_STATUS_KEYBOARD.inline_keyboard for btn in row]
        self.assertIn("admin:daily_errors", status_callbacks)
        self.assertIn("admin:status", status_callbacks)
        self.assertIn("admin:back", status_callbacks)

        # ADMIN_DAILY_ERRORS_KEYBOARD
        daily_errors_callbacks = [btn.callback_data for row in ADMIN_DAILY_ERRORS_KEYBOARD.inline_keyboard for btn in row]
        self.assertIn("admin:status", daily_errors_callbacks)
        self.assertIn("admin:back", daily_errors_callbacks)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.telegram_bot import (
    ADMIN_KEYBOARD,
    ADMIN_OCR_INPUT_KEYBOARD,
    ADMIN_OCR_PREVIEW_KEYBOARD,
    format_admin_ocr_prompt,
)
from src.vk_bot import _best_vk_photo_url, format_vk_ocr_prompt


class TelegramOcrKeyboardTests(unittest.TestCase):
    """Кнопки импорта с фото должны быть на месте — их потеря ломает единственный вход в фичу."""

    def test_admin_keyboard_has_ocr_button(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_KEYBOARD.inline_keyboard for button in row]
        self.assertIn("admin:ocr_import", callbacks)

    def test_input_keyboard_can_cancel(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_OCR_INPUT_KEYBOARD.inline_keyboard for button in row]
        self.assertEqual(callbacks, ["admin:ocr_cancel"])

    def test_preview_keyboard_offers_both_apply_modes(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_OCR_PREVIEW_KEYBOARD.inline_keyboard for button in row]
        self.assertEqual(callbacks, ["admin:ocr_confirm", "admin:ocr_confirm_silent", "admin:ocr_cancel"])

    def test_prompt_mentions_requirements(self) -> None:
        prompt = format_admin_ocr_prompt()
        self.assertIn("группы", prompt)
        self.assertIn("подтверждение", prompt)

    def test_prompt_shows_error(self) -> None:
        self.assertIn("Файл слишком большой", format_admin_ocr_prompt("Файл слишком большой"))


class VkOcrHelpersTests(unittest.TestCase):
    def test_prompt_is_plain_text(self) -> None:
        prompt = format_vk_ocr_prompt()
        self.assertNotIn("<b>", prompt)
        self.assertIn("фото", prompt)

    def test_best_photo_url_picks_largest(self) -> None:
        photo = MagicMock(
            sizes=[
                MagicMock(url="small.jpg", width=100, height=100),
                MagicMock(url="large.jpg", width=1200, height=900),
                MagicMock(url="medium.jpg", width=600, height=400),
            ]
        )
        self.assertEqual(_best_vk_photo_url(photo), "large.jpg")

    def test_best_photo_url_without_sizes(self) -> None:
        self.assertEqual(_best_vk_photo_url(MagicMock(sizes=[])), "")


if __name__ == "__main__":
    unittest.main()

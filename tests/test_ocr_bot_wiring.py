from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.telegram_bot import (
    ADMIN_KEYBOARD,
    ADMIN_OCR_INPUT_KEYBOARD,
    ADMIN_OCR_PREVIEW_KEYBOARD,
    ADMIN_OCR_SUMMARY_INPUT_KEYBOARD,
    ADMIN_OCR_SUMMARY_PREVIEW_KEYBOARD,
    format_admin_ocr_prompt,
    format_admin_ocr_summary_prompt,
)
from src.vk_bot import (
    _best_vk_photo_url,
    _collect_vk_image_urls,
    format_vk_ocr_prompt,
    format_vk_ocr_summary_prompt,
)


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


class TelegramOcrSummaryKeyboardTests(unittest.TestCase):
    """Сводный режим (один день, много групп) — отдельная ветка от обычного OCR-импорта."""

    def test_admin_keyboard_has_summary_ocr_button(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_KEYBOARD.inline_keyboard for button in row]
        self.assertIn("admin:ocr_summary_import", callbacks)

    def test_input_keyboard_can_cancel(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_OCR_SUMMARY_INPUT_KEYBOARD.inline_keyboard for button in row]
        self.assertEqual(callbacks, ["admin:ocr_summary_cancel"])

    def test_preview_keyboard_offers_both_apply_modes(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_OCR_SUMMARY_PREVIEW_KEYBOARD.inline_keyboard for button in row]
        self.assertEqual(
            callbacks, ["admin:ocr_summary_confirm", "admin:ocr_summary_confirm_silent", "admin:ocr_summary_cancel"]
        )

    def test_summary_callbacks_are_distinct_from_single_group_ones(self) -> None:
        single = {button.callback_data for row in ADMIN_OCR_PREVIEW_KEYBOARD.inline_keyboard for button in row}
        summary = {button.callback_data for row in ADMIN_OCR_SUMMARY_PREVIEW_KEYBOARD.inline_keyboard for button in row}
        self.assertEqual(single & summary, set())

    def test_prompt_explains_the_difference_from_single_group_mode(self) -> None:
        prompt = format_admin_ocr_summary_prompt()
        self.assertIn("нескольких групп", prompt)
        self.assertIn("Расписание с фото", prompt)

    def test_prompt_shows_error(self) -> None:
        self.assertIn("Файл слишком большой", format_admin_ocr_summary_prompt("Файл слишком большой"))


class VkOcrHelpersTests(unittest.TestCase):
    def test_prompt_is_plain_text(self) -> None:
        prompt = format_vk_ocr_prompt()
        self.assertNotIn("<b>", prompt)
        self.assertIn("фото", prompt)

    def test_summary_prompt_is_plain_text_and_mentions_difference(self) -> None:
        prompt = format_vk_ocr_summary_prompt()
        self.assertNotIn("<b>", prompt)
        self.assertIn("нескольких групп", prompt)

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

    def test_collect_urls_from_several_photo_attachments(self) -> None:
        """В VK альбом — это одно сообщение с несколькими attachments, а не несколько сообщений, как в Telegram."""
        message = MagicMock()
        message.attachments = [
            MagicMock(photo=MagicMock(sizes=[MagicMock(url="a.jpg", width=100, height=100)]), doc=None),
            MagicMock(photo=MagicMock(sizes=[MagicMock(url="b.jpg", width=200, height=200)]), doc=None),
        ]
        self.assertEqual(_collect_vk_image_urls(message), ["a.jpg", "b.jpg"])

    def test_collect_urls_mixes_photos_and_image_documents(self) -> None:
        message = MagicMock()
        message.attachments = [
            MagicMock(photo=MagicMock(sizes=[MagicMock(url="a.jpg", width=100, height=100)]), doc=None),
            MagicMock(photo=None, doc=MagicMock(ext="png", url="b.png")),
        ]
        self.assertEqual(_collect_vk_image_urls(message), ["a.jpg", "b.png"])

    def test_collect_urls_skips_non_image_documents(self) -> None:
        message = MagicMock()
        message.attachments = [
            MagicMock(photo=None, doc=MagicMock(ext="pdf", url="c.pdf")),
        ]
        self.assertEqual(_collect_vk_image_urls(message), [])

    def test_collect_urls_empty_without_attachments(self) -> None:
        message = MagicMock()
        message.attachments = []
        self.assertEqual(_collect_vk_image_urls(message), [])


if __name__ == "__main__":
    unittest.main()

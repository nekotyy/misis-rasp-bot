"""Фото от админа не должно проваливаться в тишину.

Регрессия: чтобы бот отреагировал на фото, требовалось сначала нажать кнопку в
админ-панели. Без неё хендлер молча выходил — со стороны это выглядело как
полностью сломанный бот: кинул фото и ничего не произошло.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.telegram_bot import format_admin_ocr_prompt, is_ocr_photo_candidate
from src.vk_bot import _has_image_attachment, format_vk_ocr_prompt


def vk_message(*, photo=None, doc_ext: str | None = None, empty: bool = False) -> MagicMock:
    message = MagicMock()
    if empty:
        message.attachments = []
        return message
    attachment = MagicMock()
    attachment.photo = photo
    if doc_ext is None:
        attachment.doc = None
    else:
        attachment.doc = MagicMock(ext=doc_ext)
    message.attachments = [attachment]
    return message


class TelegramEntryTests(unittest.TestCase):
    def test_admin_in_private_chat_is_handled(self) -> None:
        self.assertTrue(is_ocr_photo_candidate("private", is_admin=True))

    def test_non_admin_is_ignored(self) -> None:
        self.assertFalse(is_ocr_photo_candidate("private", is_admin=False))

    def test_group_chats_are_ignored(self) -> None:
        self.assertFalse(is_ocr_photo_candidate("group", is_admin=True))
        self.assertFalse(is_ocr_photo_candidate("supergroup", is_admin=True))

    def test_button_press_is_not_required(self) -> None:
        """Ключевая регрессия: решение не зависит от нажатой кнопки."""
        self.assertTrue(is_ocr_photo_candidate("private", is_admin=True))


class VkEntryTests(unittest.TestCase):
    def test_photo_attachment_detected(self) -> None:
        self.assertTrue(_has_image_attachment(vk_message(photo=MagicMock())))

    def test_image_document_detected(self) -> None:
        for ext in ("jpg", "JPEG", "png", "webp", "bmp"):
            self.assertTrue(_has_image_attachment(vk_message(doc_ext=ext)), ext)

    def test_non_image_document_ignored(self) -> None:
        for ext in ("pdf", "docx", "zip", "json"):
            self.assertFalse(_has_image_attachment(vk_message(doc_ext=ext)), ext)

    def test_no_attachments(self) -> None:
        self.assertFalse(_has_image_attachment(vk_message(empty=True)))

    def test_message_without_attachments_attribute(self) -> None:
        message = MagicMock()
        message.attachments = None
        self.assertFalse(_has_image_attachment(message))


class ErrorMessageTests(unittest.TestCase):
    """Любой сбой должен доходить до админа текстом, а не тишиной."""

    def test_telegram_prompt_shows_error(self) -> None:
        prompt = format_admin_ocr_prompt("Внутренняя ошибка: RuntimeError: движок умер")

        self.assertIn("RuntimeError", prompt)
        self.assertIn("движок умер", prompt)

    def test_telegram_prompt_escapes_html(self) -> None:
        prompt = format_admin_ocr_prompt("сломалось <tag> & прочее")

        self.assertIn("&lt;tag&gt;", prompt)
        self.assertNotIn("<tag>", prompt)

    def test_vk_prompt_shows_error_without_html(self) -> None:
        prompt = format_vk_ocr_prompt("Внутренняя ошибка: TimeoutError")

        self.assertIn("TimeoutError", prompt)
        self.assertNotIn("<b>", prompt)

    def test_prompts_without_error_stay_clean(self) -> None:
        self.assertNotIn("ошибка", format_admin_ocr_prompt().lower())
        self.assertNotIn("ошибка", format_vk_ocr_prompt().lower())


if __name__ == "__main__":
    unittest.main()

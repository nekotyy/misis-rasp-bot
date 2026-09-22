from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.ocr_import import format_admin_gemini_status
from src.ocr_schedule import SUMMARY_RECOGNITION_PROMPT
from src.telegram_bot import (
    ADMIN_KEYBOARD,
    ADMIN_OCR_INPUT_KEYBOARD,
    ADMIN_OCR_JSON_INPUT_KEYBOARD,
    ADMIN_OCR_JSON_PREVIEW_KEYBOARD,
    ADMIN_OCR_MENU_KEYBOARD,
    ADMIN_OCR_PREVIEW_KEYBOARD,
    ADMIN_OCR_SUMMARY_INPUT_KEYBOARD,
    ADMIN_OCR_SUMMARY_PREVIEW_KEYBOARD,
    format_admin_ocr_json_prompt,
    format_admin_ocr_prompt,
    format_admin_ocr_summary_add_more_prompt,
    format_admin_ocr_summary_prompt,
)
from src.vk_bot import (
    _best_vk_photo_url,
    _collect_vk_image_urls,
    format_vk_ocr_json_prompt,
    format_vk_ocr_prompt,
    format_vk_ocr_summary_add_more_prompt,
    format_vk_ocr_summary_prompt,
    vk_admin_keyboard_rows,
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
            callbacks,
            [
                "admin:ocr_summary_confirm",
                "admin:ocr_summary_confirm_silent",
                "admin:ocr_summary_add_more",
                "admin:ocr_summary_cancel",
            ],
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

    def test_add_more_prompt_mentions_queued_count(self) -> None:
        prompt = format_admin_ocr_summary_add_more_prompt(2)
        self.assertIn("2", prompt)

    def test_add_more_prompt_without_queue_is_clean(self) -> None:
        prompt = format_admin_ocr_summary_add_more_prompt(0)
        self.assertNotIn("Уже загружено", prompt)


class TelegramOcrJsonImportTests(unittest.TestCase):
    """Резервный путь: JSON, распознанный вручную другой нейросетью, минуя Gemini."""

    def test_admin_keyboard_has_json_import_button(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_KEYBOARD.inline_keyboard for button in row]
        self.assertIn("admin:ocr_json_import", callbacks)

    def test_ocr_menu_keyboard_has_all_three_modes(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_OCR_MENU_KEYBOARD.inline_keyboard for button in row]
        self.assertEqual(
            callbacks,
            ["admin:ocr_import", "admin:ocr_summary_import", "admin:ocr_json_import"],
        )

    def test_input_keyboard_can_cancel(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_OCR_JSON_INPUT_KEYBOARD.inline_keyboard for button in row]
        self.assertEqual(callbacks, ["admin:ocr_summary_cancel"])

    def test_preview_keyboard_offers_both_apply_modes_but_no_add_more(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_OCR_JSON_PREVIEW_KEYBOARD.inline_keyboard for button in row]
        self.assertEqual(
            callbacks,
            ["admin:ocr_summary_confirm", "admin:ocr_summary_confirm_silent", "admin:ocr_summary_cancel"],
        )
        self.assertNotIn("admin:ocr_summary_add_more", callbacks)

    def test_prompt_includes_the_copyable_summary_prompt(self) -> None:
        prompt = format_admin_ocr_json_prompt()
        self.assertIn(SUMMARY_RECOGNITION_PROMPT, prompt)
        self.assertIn("<code>", prompt)

    def test_prompt_shows_error(self) -> None:
        self.assertIn("Пустой JSON", format_admin_ocr_json_prompt("Пустой JSON."))


class VkOcrHelpersTests(unittest.TestCase):
    def test_prompt_is_plain_text(self) -> None:
        prompt = format_vk_ocr_prompt()
        self.assertNotIn("<b>", prompt)
        self.assertIn("фото", prompt)

    def test_summary_prompt_is_plain_text_and_mentions_difference(self) -> None:
        prompt = format_vk_ocr_summary_prompt()
        self.assertNotIn("<b>", prompt)
        self.assertIn("нескольких групп", prompt)

    def test_summary_add_more_prompt_mentions_queued_count(self) -> None:
        prompt = format_vk_ocr_summary_add_more_prompt(3)
        self.assertNotIn("<b>", prompt)
        self.assertIn("3", prompt)

    def test_json_prompt_is_plain_text_and_includes_the_copyable_prompt(self) -> None:
        prompt = format_vk_ocr_json_prompt()
        self.assertNotIn("<b>", prompt)
        self.assertIn(SUMMARY_RECOGNITION_PROMPT, prompt)

    def test_json_prompt_shows_error(self) -> None:
        self.assertIn("Пустой JSON", format_vk_ocr_json_prompt("Пустой JSON."))

    def test_admin_keyboard_has_json_import_button_within_vk_limits(self) -> None:
        rows = vk_admin_keyboard_rows()
        labels = {label for row in rows for label in row}
        self.assertIn("Импорт OCR JSON", labels)
        for row in rows:
            self.assertLessEqual(len(row), 5)

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


class AdminGeminiPanelTests(unittest.TestCase):
    """Экран «Управление Gemini» прямо в боте — отдельный от веб-дашборда вход."""

    def test_telegram_admin_keyboard_has_gemini_button(self) -> None:
        callbacks = [button.callback_data for row in ADMIN_KEYBOARD.inline_keyboard for button in row]
        self.assertIn("admin:gemini_status", callbacks)

    def test_vk_admin_keyboard_has_gemini_button(self) -> None:
        labels = {label for row in vk_admin_keyboard_rows() for label in row}
        self.assertIn("Управление Gemini", labels)

    def test_reports_unconfigured_engine(self) -> None:
        service = MagicMock(engine=None)
        text = format_admin_gemini_status(service)
        self.assertIn("не настроен", text)

    def test_reports_none_service(self) -> None:
        text = format_admin_gemini_status(None)
        self.assertIn("не настроен", text)

    def _make_service(self) -> MagicMock:
        engine = MagicMock()
        engine.live_status.return_value = {
            "model_configured": "gemini-3.8-flash",
            "account_status": "AVAILABLE",
            "account_status_description": "Account is authorized",
            "cookies_configured": True,
            "proxy_configured": False,
            "session_active": True,
            "build_label": "v1",
            "session_id": "sess-1",
            "usage_info": {"used": 10},
            "quotas": {"limit": 100},
            "abuse_status": {"flags": 0},
        }
        service = MagicMock(engine=engine)
        service.diagnostics.return_value = {
            "ready": True,
            "remote_ip": "1.2.3.4",
            "route_http_status": 200,
            "doh_url": "",
            "last_success_at": "17.09 12:00",
            "last_available_at": "2026-09-17T12:00:00",
            "last_failure_at": "",
        }
        return service

    def test_html_variant_renders_account_status_and_usage(self) -> None:
        text = format_admin_gemini_status(self._make_service(), html=True)
        self.assertIn("<b>", text)
        self.assertIn("AVAILABLE", text)
        self.assertIn("gemini-3.8-flash", text)
        self.assertIn("used: 10", text)

    def test_plain_variant_has_no_html_tags(self) -> None:
        text = format_admin_gemini_status(self._make_service(), html=False)
        self.assertNotIn("<b>", text)
        self.assertNotIn("<code>", text)
        self.assertIn("AVAILABLE", text)

    def test_shows_failure_details_when_present(self) -> None:
        service = self._make_service()
        service.diagnostics.return_value.update(
            {
                "last_failure_at": "17.09 11:00",
                "failure_code": "auth",
                "failure_title": "ошибка авторизации cookies",
                "failure_action": "обновить __Secure-1PSID и __Secure-1PSIDTS в .env",
                "failure_retryable": False,
                "consecutive_failures": 3,
            }
        )
        text = format_admin_gemini_status(service, html=False)
        self.assertIn("ошибка авторизации cookies", text)
        self.assertIn("[auth]", text)
        self.assertIn("3", text)

    def test_no_failures_reports_clean(self) -> None:
        text = format_admin_gemini_status(self._make_service(), html=False)
        self.assertIn("Сбоев не зафиксировано", text)


if __name__ == "__main__":
    unittest.main()

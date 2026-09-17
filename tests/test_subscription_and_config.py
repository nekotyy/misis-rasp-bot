import os
import unittest
from unittest.mock import patch

from src.config import Settings
from src.schedule_search import SearchTarget
from src.subscription_utils import (
    make_audience_subscription,
    make_group_subscription,
    make_teacher_subscription,
    subscription_caption,
)


class SubscriptionAndConfigTests(unittest.TestCase):
    def test_settings_from_env(self):
        env_vars = {
            "SCHEDULE_URL": "http://test-url.local/rasp",
            "APP_TIMEZONE": "Europe/Moscow",
            "ADMIN_TELEGRAM_ID": "123456789,987654321",
            "ADMIN_VK_ID": "555444",
            "RABBITMQ_URL": "amqp://guest:guest@localhost:5672/",
            "LESSON_COUNTERS_ENABLED": "true",
            "WEB_COOKIE_SECURE": "false",
            "GEMINI_DOH_URL": "https://xbox-dns.ru/dns-query",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            settings = Settings.from_env()
            self.assertEqual(settings.schedule_url, "http://test-url.local/rasp")
            self.assertEqual(settings.admin_telegram_id, 123456789)
            self.assertEqual(settings.admin_telegram_ids, [123456789, 987654321])
            self.assertEqual(settings.admin_vk_id, 555444)
            self.assertTrue(settings.lesson_counters_enabled)
            self.assertFalse(settings.web_cookie_secure)
            self.assertEqual(settings.gemini_doh_url, "https://xbox-dns.ru/dns-query")

    def test_gemini_doh_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.gemini_doh_url, "")

    def test_subscription_utils_helpers(self):
        group_sub = make_group_subscription("ИСП-25-1", schedule_id=600)
        self.assertEqual(group_sub["subscription_type"], "group")
        self.assertEqual(group_sub["subscription_title"], "ИСП-25-1")
        pending_sub = make_group_subscription("МТО-26", schedule_id=None)
        self.assertEqual(pending_sub["subscription_key"], "group-pending:мто-26")
        self.assertEqual(pending_sub["subscription_url"], "")

        teacher_target = SearchTarget(
            kind="teacher",
            title="Иванов И.И.",
            url="http://test-url.local/prep/123",
        )
        teacher_sub = make_teacher_subscription(teacher_target)
        self.assertEqual(teacher_sub["subscription_type"], "teacher")
        self.assertIn("Иванов", teacher_sub["subscription_title"])

        aud_target = SearchTarget(
            kind="audience",
            title="каб. 301",
            url="http://test-url.local/aud/301",
        )
        aud_sub = make_audience_subscription(aud_target)
        self.assertIn("301", aud_sub["audience_subscription_title"])

        caption = subscription_caption("group", "ИСП-25-1")
        self.assertIn("ИСП-25-1", caption)


if __name__ == "__main__":
    unittest.main()

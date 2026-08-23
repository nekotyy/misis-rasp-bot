from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.db import Database
from src.system_status import (
    BOT_VERSION,
    SystemAlertManager,
    check_database_status,
    check_rabbitmq_status,
    check_schedule_site,
    check_web_dashboard_status,
    format_bytes,
    format_daily_errors_report,
    format_uptime,
    get_bot_version,
    get_memory_usage_mb,
)


class TestSystemStatus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_status.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_get_bot_version(self) -> None:
        version = get_bot_version()
        self.assertIsInstance(version, str)
        self.assertTrue(len(version) > 0)
        self.assertEqual(version, BOT_VERSION)

    def test_format_uptime(self) -> None:
        start_time = datetime.now() - timedelta(days=2, hours=3, minutes=15, seconds=10)
        uptime_str = format_uptime(start_time)
        self.assertIn("2 дн.", uptime_str)
        self.assertIn("3 ч.", uptime_str)
        self.assertIn("15 мин.", uptime_str)

        start_time_mins = datetime.now() - timedelta(minutes=5)
        uptime_str_mins = format_uptime(start_time_mins)
        self.assertIn("5 мин.", uptime_str_mins)

    def test_format_bytes(self) -> None:
        self.assertEqual(format_bytes(500), "500 Б")
        self.assertEqual(format_bytes(1024), "1.0 КБ")
        self.assertEqual(format_bytes(1024 * 1024 * 5), "5.00 МБ")

    def test_get_memory_usage_mb(self) -> None:
        mem = get_memory_usage_mb()
        self.assertIsInstance(mem, float)
        self.assertGreaterEqual(mem, 0.0)

    async def test_check_database_status(self) -> None:
        status = await check_database_status(self.db)
        self.assertTrue(status["ok"])
        self.assertIn("Б", status["size_formatted"])

    async def test_check_schedule_site_success(self) -> None:
        mock_response = httpx.Response(200, request=httpx.Request("GET", "http://test.local"))
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_response)):
            status = await check_schedule_site("http://test.local")
            self.assertTrue(status["ok"])
            self.assertEqual(status["status_code"], 200)
            self.assertGreaterEqual(status["latency_ms"], 0)

    async def test_check_schedule_site_failure(self) -> None:
        mock_response = httpx.Response(500, request=httpx.Request("GET", "http://test.local"))
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_response)):
            status = await check_schedule_site("http://test.local")
            self.assertFalse(status["ok"])
            self.assertEqual(status["status_code"], 500)

    async def test_check_schedule_site_exception(self) -> None:
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.ConnectError("Connection refused"))):
            status = await check_schedule_site("http://test.local")
            self.assertFalse(status["ok"])
            self.assertIn("ConnectError", status["error"])

    async def test_check_rabbitmq_status(self) -> None:
        status_empty = await check_rabbitmq_status("")
        self.assertFalse(status_empty["ok"])
        self.assertEqual(status_empty["label"], "disabled")

    async def test_check_web_dashboard_status(self) -> None:
        status_zero = await check_web_dashboard_status(0)
        self.assertFalse(status_zero["ok"])

    async def test_alert_manager_transitions_and_cooldown(self) -> None:
        mock_broadcaster = AsyncMock()
        alert_manager = SystemAlertManager(
            db=self.db,
            broadcaster=mock_broadcaster,
            cooldown_seconds=100.0,
        )

        # 1. First failure -> triggers alert
        await alert_manager.report_component_status(
            component="schedule_site",
            ok=False,
            error_message="HTTP 503 Service Unavailable",
            details="Backend unreachable",
        )
        self.assertEqual(mock_broadcaster.notify_admins.call_count, 1)

        # 2. Repeated failure within cooldown -> does NOT trigger another alert
        await alert_manager.report_component_status(
            component="schedule_site",
            ok=False,
            error_message="HTTP 503 Service Unavailable",
        )
        self.assertEqual(mock_broadcaster.notify_admins.call_count, 1)

        # 3. Recovery -> triggers recovery alert
        await alert_manager.report_component_status(
            component="schedule_site",
            ok=True,
        )
        self.assertEqual(mock_broadcaster.notify_admins.call_count, 2)

        # 4. Repeated success -> no alert
        await alert_manager.report_component_status(
            component="schedule_site",
            ok=True,
        )
        self.assertEqual(mock_broadcaster.notify_admins.call_count, 2)

        # 5. Check DB logged error
        errors = await self.db.get_daily_errors()
        self.assertGreaterEqual(len(errors), 1)
        self.assertEqual(errors[0]["component"], "schedule_site")

    async def test_format_daily_errors_report(self) -> None:
        # Empty report
        empty_html = await format_daily_errors_report(self.db, html=True)
        self.assertIn("не зафиксировано", empty_html)

        # Log some errors
        await self.db.log_system_error("schedule_site", "http_500", "500 Internal Server Error", "test details")
        await self.db.record_delivery_event(
            campaign_type="test_campaign",
            platform="telegram",
            user_id=12345,
            via_broker=False,
            status="failed",
            error_text="TelegramForbiddenError",
        )

        html_report = await format_daily_errors_report(self.db, html=True)
        self.assertIn("Ошибки и сбои за сегодня", html_report)
        self.assertIn("500 Internal Server Error", html_report)
        self.assertIn("TelegramForbiddenError", html_report)

        plain_report = await format_daily_errors_report(self.db, html=False)
        self.assertNotIn("<b>", plain_report)
        self.assertIn("500 Internal Server Error", plain_report)
        self.assertIn("TelegramForbiddenError", plain_report)


if __name__ == "__main__":
    unittest.main()

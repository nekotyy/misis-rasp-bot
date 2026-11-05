import re
import unittest

import aiosqlite

from src.account_schema import ACCOUNTS_SCHEMA_V1
from src.miniapp_stats import (
    collect_miniapp_stats,
    format_duration,
    format_miniapp_stats_report,
    record_telemetry_event,
    record_telemetry_ping,
    record_telemetry_session,
)

EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff]|"
    r"[\u2600-\u27bf]|"
    r"[\u2300-\u23ff]|"
    r"[\u2b50-\u2b55]|"
    r"[\u203c\u2049\u2122\u2139\u2194-\u2199\u21a9-\u21aa]|"
    r"[\u25aa-\u25ab\u25b6\u25c0\u25fb-\u25fe]"
)


class MiniappStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        await self.db.executescript(ACCOUNTS_SCHEMA_V1)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_duration_formatting(self):
        self.assertEqual(format_duration(0), "0 сек")
        self.assertEqual(format_duration(45), "45 сек")
        self.assertEqual(format_duration(60), "1 мин")
        self.assertEqual(format_duration(125), "2 мин 5 сек")
        self.assertEqual(format_duration(3665), "1 ч 1 мин")

    async def test_telemetry_recording_and_collection(self):
        # Пользователь 1: Telegram, сессия 1
        await record_telemetry_session(
            self.db,
            session_id="s1",
            platform="telegram",
            user_id=111,
            account_id="acc1",
            theme="dark",
            scale=100,
            active_group="ИСП-25-1",
            last_page="/schedule",
        )
        await record_telemetry_ping(
            self.db,
            session_id="s1",
            duration_seconds=120,
            active_group="ИСП-25-1",
            current_page="/history",
        )
        await record_telemetry_event(
            self.db,
            session_id="s1",
            platform="telegram",
            user_id=111,
            account_id="acc1",
            event_type="page_view",
            event_data={"page": "/history"},
        )

        # Пользователь 2: VK, сессия 2
        await record_telemetry_session(
            self.db,
            session_id="s2",
            platform="vk",
            user_id=222,
            account_id="acc2",
            theme="light",
            scale=110,
            active_group="БПИ-22-1",
            last_page="/profile",
        )
        await record_telemetry_ping(
            self.db,
            session_id="s2",
            duration_seconds=60,
            active_group="БПИ-22-1",
            current_page="/profile",
        )

        report = await collect_miniapp_stats(self.db)
        self.assertEqual(report.total_unique_users, 2)
        self.assertEqual(report.total_tg_unique, 1)
        self.assertEqual(report.total_vk_unique, 1)
        self.assertEqual(report.dau_today, 2)
        self.assertEqual(report.wau_7d, 2)
        self.assertEqual(report.mau_30d, 2)
        self.assertEqual(report.total_sessions, 2)
        self.assertEqual(report.active_online_15m, 2)
        self.assertGreaterEqual(report.avg_duration_seconds, 60)
        self.assertEqual(report.dark_theme_count, 1)
        self.assertEqual(report.light_theme_count, 1)

    def test_report_formatting_strictly_zero_emojis(self):
        # Проверяем форматирование отчета без эмодзи
        from src.miniapp_stats import MiniappStatsReport

        mock_report = MiniappStatsReport(
            generated_at="05.11.2026 22:50 (МСК)",
            total_unique_users=42,
            total_tg_unique=30,
            total_vk_unique=12,
            dau_today=15,
            wau_7d=35,
            mau_30d=42,
            total_sessions=150,
            sessions_today=25,
            sessions_7d=90,
            sessions_30d=150,
            active_online_15m=4,
            avg_duration_seconds=185,
            median_duration_seconds=120,
            total_time_spent_seconds=27750,
            peak_hour="14:00 - 15:00",
            peak_hour_sessions=28,
            top_active_hours=[("14:00 - 15:00", 28), ("12:00 - 13:00", 22)],
            peak_day_of_week="Среда",
            peak_day_sessions=45,
            page_views_schedule=210,
            page_views_history=95,
            page_views_profile=40,
            top_groups=[("ИСП-25-1", 120), ("БПИ-22-1", 80)],
            dark_theme_count=35,
            light_theme_count=7,
            scale_100_count=40,
            scale_other_count=2,
        )

        for is_html in (True, False):
            text = format_miniapp_stats_report(mock_report, html=is_html)
            self.assertIn("Статистика Mini App", text)
            self.assertIn("DAU", text)
            self.assertIn("WAU", text)
            self.assertIn("MAU", text)
            self.assertIn("Пики активности", text)
            self.assertIn("14:00 - 15:00", text)
            self.assertIn("ИСП-25-1", text)

            # Проверяем строгое отсутствие любых эмодзи
            emojis_found = EMOJI_PATTERN.findall(text)
            self.assertEqual(
                emojis_found,
                [],
                f"Отчет содержит эмодзи, что запрещено: {emojis_found}",
            )


if __name__ == "__main__":
    unittest.main()

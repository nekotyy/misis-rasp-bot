import unittest
from unittest.mock import AsyncMock, MagicMock

from src.models import UserRecord
from src.notifier import Broadcaster, BroadcastProgress
from src.telegram_bot import format_broadcast_progress_status, generate_progress_bar


class BroadcastProgressFormattingTests(unittest.TestCase):
    def test_generate_progress_bar_formatting(self):
        # Check exact length of 10 blocks + percentage string
        self.assertEqual(generate_progress_bar(0), "[░░░░░░░░░░] 0%")
        self.assertEqual(generate_progress_bar(50), "[█████░░░░░] 50%")
        self.assertEqual(generate_progress_bar(100), "[██████████] 100%")

        # Boundary checks
        self.assertEqual(generate_progress_bar(-10), "[░░░░░░░░░░] 0%")
        self.assertEqual(generate_progress_bar(150), "[██████████] 100%")

    def test_smoke_broadcast_status_report_formatting(self):
        progress_in_flight = BroadcastProgress(
            target_platform="all",
            target_audience="students",
            started_at="29.07.2026 01:20:00",
            total_users=100,
            processed_count=45,
            success_count=40,
            failed_count=5,
            is_finished=False,
        )

        text_html = format_broadcast_progress_status("Тестовое расписание", progress_in_flight, html=True)
        self.assertIn("<b>Рассылка выполняется...</b>", text_html)
        self.assertIn("Тестовое расписание", text_html)
        self.assertIn("45 / 100 (45%)", text_html)
        self.assertIn("<code>[████░░░░░░] 45%</code>", text_html)

        progress_finished = BroadcastProgress(
            target_platform="telegram",
            target_audience="teachers",
            started_at="29.07.2026 01:20:00",
            total_users=50,
            processed_count=50,
            success_count=48,
            failed_count=2,
            is_finished=True,
            finished_at="29.07.2026 01:20:05",
        )

        text_finished = format_broadcast_progress_status("Важная новость", progress_finished, html=True)
        self.assertIn("<b>Рассылка завершена</b>", text_finished)
        self.assertIn("Важная новость", text_finished)
        self.assertIn("Успешно доставлено:</b> 48", text_finished)
        self.assertIn("Ошибки доставки:</b> 2", text_finished)
        self.assertIn("Процент успеха:</b> 96%", text_finished)


class BroadcastProgressNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_progress_callback_invocation(self):
        mock_db = MagicMock()
        mock_db.auto_disable_undeliverable_telegram_users = AsyncMock(return_value=0)
        mock_db.get_user = AsyncMock(return_value=None)
        user1 = UserRecord("telegram", 101, "u1", "U1", "group", "group:1", "G1", None, None, None, None, "G1", 1, False, False, True, False, "t", "t")
        user2 = UserRecord("telegram", 102, "u2", "U2", "group", "group:1", "G1", None, None, None, None, "G1", 1, False, False, True, False, "t", "t")
        mock_db.get_users_for_notifications = AsyncMock(return_value=[user1, user2])
        mock_db.record_delivery_event = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        broadcaster = Broadcaster(db=mock_db, telegram_bot=mock_bot)

        reports: list[BroadcastProgress] = []

        async def callback(p: BroadcastProgress):
            reports.append(
                BroadcastProgress(
                    target_platform=p.target_platform,
                    target_audience=p.target_audience,
                    started_at=p.started_at,
                    total_users=p.total_users,
                    processed_count=p.processed_count,
                    success_count=p.success_count,
                    failed_count=p.failed_count,
                    is_finished=p.is_finished,
                    finished_at=p.finished_at,
                )
            )

        final_prog = await broadcaster.broadcast(
            "Текст анонса",
            target_platform="telegram",
            target_audience="all",
            progress_callback=callback,
        )

        self.assertTrue(final_prog.is_finished)
        self.assertEqual(final_prog.total_users, 2)
        self.assertEqual(final_prog.processed_count, 2)
        self.assertEqual(final_prog.success_count, 2)

        self.assertGreater(len(reports), 0)
        self.assertTrue(reports[-1].is_finished)
        self.assertEqual(reports[-1].processed_count, 2)


if __name__ == "__main__":
    unittest.main()

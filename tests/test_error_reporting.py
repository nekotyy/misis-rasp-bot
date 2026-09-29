from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.error_reporting import AdminErrorReporter, AdminLogHandler, ErrorReport, install_error_reporting


def _raise(message: str) -> Exception:
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


class AdminErrorReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_exception_report_contains_details_and_traceback(self) -> None:
        notify = AsyncMock()
        reporter = AdminErrorReporter(notify)

        await reporter.report_exception("VK-бот", _raise("boom <tag>"), details=[("Чат", "42")])

        html_text, plain_text = notify.await_args.args
        self.assertIn("Сбой: VK-бот", html_text)
        self.assertIn("Чат: <b>42</b>", html_text)
        self.assertIn("boom &lt;tag&gt;", html_text)  # HTML экранирован — Telegram не отклонит сообщение
        self.assertIn("Traceback", plain_text)

    async def test_repeated_error_is_collapsed_and_counted(self) -> None:
        notify = AsyncMock()
        reporter = AdminErrorReporter(notify, dedup_window_seconds=0.2)
        error = _raise("same")

        for _ in range(4):
            await reporter.report_exception("VK-бот", error)
        self.assertEqual(notify.await_count, 1)

        await asyncio.sleep(0.25)
        await reporter.report_exception("VK-бот", error)
        self.assertEqual(notify.await_count, 2)
        self.assertIn("Повторялась ещё 3 раз", notify.await_args.args[0])

    async def test_hourly_budget_limits_reports(self) -> None:
        notify = AsyncMock()
        reporter = AdminErrorReporter(notify, max_reports_per_hour=2)
        for index in range(5):
            await reporter.report(ErrorReport(source="x", summary=f"разная ошибка {index}"))
        self.assertEqual(notify.await_count, 2)

    async def test_delivery_failure_does_not_raise(self) -> None:
        reporter = AdminErrorReporter(AsyncMock(side_effect=RuntimeError("telegram down")))
        await reporter.report_exception("VK-бот", _raise("boom"))

    async def test_muted_reporter_sends_nothing(self) -> None:
        notify = AsyncMock()
        reporter = AdminErrorReporter(notify)
        reporter.attach_loop(asyncio.get_running_loop())
        reporter.mute()

        await reporter.report_exception("VK-бот", _raise("boom"))
        reporter.report_in_background(ErrorReport(source="x", summary="в фоне"))
        await asyncio.sleep(0)

        notify.assert_not_awaited()

    async def test_without_notifier_nothing_happens(self) -> None:
        reporter = AdminErrorReporter()
        await reporter.report_exception("VK-бот", _raise("boom"))


class AdminLogHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.notify = AsyncMock()
        self.reporter = AdminErrorReporter(self.notify)
        self.handler = install_error_reporting(self.reporter, asyncio.get_running_loop())
        self.logger = logging.getLogger("tests.error_reporting")

    async def asyncTearDown(self) -> None:
        logging.getLogger().removeHandler(self.handler)

    async def _drain(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        if self.reporter._tasks:
            await asyncio.gather(*self.reporter._tasks)

    async def test_error_log_is_forwarded_to_admin(self) -> None:
        self.logger.error("Задача планировщика упала: %s", "boom")
        await self._drain()
        self.notify.assert_awaited_once()
        self.assertIn("Задача планировщика упала: boom", self.notify.await_args.args[1])

    async def test_warning_and_marked_records_are_not_forwarded(self) -> None:
        self.logger.warning("просто предупреждение")
        self.logger.error("уже отправлено алертом", extra={"skip_admin_report": True})
        await self._drain()
        self.notify.assert_not_awaited()

    async def test_cancelled_task_log_is_not_forwarded(self) -> None:
        try:
            raise asyncio.CancelledError
        except asyncio.CancelledError:
            self.logger.exception("Job raised an exception")
        await self._drain()
        self.notify.assert_not_awaited()

    async def test_transient_aiogram_polling_network_errors_are_not_forwarded(self) -> None:
        polling_logger = logging.getLogger("aiogram.dispatcher")
        for error_type, text in (
            ("TelegramNetworkError", "HTTP Client says - ServerDisconnectedError: Server disconnected"),
            ("TelegramNetworkError", "HTTP Client says - ClientConnectorError: Temporary failure in name resolution"),
            ("TelegramServerError", "Bad Gateway"),
        ):
            polling_logger.error("Failed to fetch updates - %s: %s", error_type, text)
        await self._drain()
        self.notify.assert_not_awaited()

    async def test_non_network_aiogram_polling_errors_are_still_forwarded(self) -> None:
        """Неверный токен или второй экземпляр бота — не «шум», об этом админ должен узнать."""
        polling_logger = logging.getLogger("aiogram.dispatcher")
        polling_logger.error("Failed to fetch updates - %s: %s", "TelegramUnauthorizedError", "Unauthorized")
        polling_logger.error("Что-то другое в диспетчере")
        await self._drain()
        self.assertEqual(self.notify.await_count, 2)
        self.assertIn("TelegramUnauthorizedError", self.notify.await_args_list[0].args[1])

    async def test_log_from_worker_thread_is_forwarded(self) -> None:
        await asyncio.to_thread(self.logger.error, "ошибка из потока")
        await self._drain()
        self.notify.assert_awaited_once()

    async def test_only_one_handler_is_installed(self) -> None:
        install_error_reporting(self.reporter, asyncio.get_running_loop())
        handlers = [item for item in logging.getLogger().handlers if isinstance(item, AdminLogHandler)]
        self.assertEqual(len(handlers), 1)
        self.handler = handlers[0]


class ShutdownMutesReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_stops_admin_reports(self) -> None:
        from src.main import shutdown

        notify = AsyncMock()
        reporter = AdminErrorReporter(notify)
        jobs = MagicMock()
        broadcaster = MagicMock(stop=AsyncMock(), telegram_bot=None, vk_bot=None)

        await shutdown(jobs, broadcaster, reporter)
        await reporter.report(ErrorReport(source="aiogram", summary="Server disconnected"))

        jobs.scheduler.shutdown.assert_called_once()
        notify.assert_not_awaited()


class RestoreLoggingTests(unittest.TestCase):
    def test_library_loggers_are_reenabled_after_alembic(self) -> None:
        import logging.config

        import aiogram  # noqa: F401 — создаёт свои логгеры до fileConfig, как в проде

        from src.main import restore_logging

        root = logging.getLogger()
        saved_level, saved_handlers = root.level, list(root.handlers)
        try:
            logging.config.fileConfig("alembic.ini")
            restore_logging()
            for name in ("aiogram.dispatcher", "aiogram.event", "asyncio", "src.vk_runtime"):
                self.assertFalse(logging.getLogger(name).disabled, name)
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)


if __name__ == "__main__":
    unittest.main()

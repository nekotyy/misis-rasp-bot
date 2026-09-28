from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock

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

    async def test_log_from_worker_thread_is_forwarded(self) -> None:
        await asyncio.to_thread(self.logger.error, "ошибка из потока")
        await self._drain()
        self.notify.assert_awaited_once()

    async def test_only_one_handler_is_installed(self) -> None:
        install_error_reporting(self.reporter, asyncio.get_running_loop())
        handlers = [item for item in logging.getLogger().handlers if isinstance(item, AdminLogHandler)]
        self.assertEqual(len(handlers), 1)
        self.handler = handlers[0]


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

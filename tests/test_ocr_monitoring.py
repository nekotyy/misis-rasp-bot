"""Тесты наблюдаемости импорта расписания с фото.

Прогрев, индикатор прогресса, строка статуса, отчёты в мониторинг и поведение
при сбоях. Без этого распознавание — чёрный ящик: админ видит «Распознаю...»
и не понимает, живо оно или наебнулось.
"""

from __future__ import annotations

import asyncio
import pathlib
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.ocr_import import (
    HEARTBEAT_INTERVAL_SECONDS,
    OCR_STAGE_PARSE,
    OCR_STAGE_PREVIEW,
    OCR_STAGE_RECOGNIZE,
    OCR_STAGES,
    OcrScheduleImporter,
    format_progress_bar,
)
from src.ocr_schedule import OcrEngineError
from src.system_status import COMPONENT_TITLES, check_ocr_status

RECOGNIZED_TEXT = """ИСП-25-1
01.09.2026 г.
Пара Дисциплина Преподаватель Ауд.
1 Операционные системы и среды Кубанева Е.А. 301
"""

ACTIVE_SOURCE = {
    "source_type": "group",
    "source_key": "group:600",
    "source_title": "ИСП-25-1",
    "source_url": "http://example.com/rasp/600",
    "schedule_id": 600,
    "group_name": "ИСП-25-1",
}


class FakeEngine:
    name = "fake"

    def __init__(self, *, available: bool = True, warm_error: str = "", recognize_error: str = "") -> None:
        self._available = available
        self._warm_error = warm_error
        self._recognize_error = recognize_error
        self.warm_calls = 0

    def availability(self) -> tuple[bool, str]:
        return (True, "fake 1.0") if self._available else (False, "Движок не установлен.")

    def warm_up(self) -> None:
        self.warm_calls += 1
        if self._warm_error:
            raise RuntimeError(self._warm_error)

    def recognize(self, image_bytes: bytes) -> str:
        if self._recognize_error:
            raise OcrEngineError(self._recognize_error)
        return RECOGNIZED_TEXT


def make_importer(*, engine: FakeEngine | None = None, alerts: MagicMock | None = None, enabled: bool = True):
    db = MagicMock()
    db.get_active_sources = AsyncMock(return_value=[ACTIVE_SOURCE])
    db.get_latest_snapshot = AsyncMock(return_value=None)
    return OcrScheduleImporter(
        db,
        MagicMock(apply_manual_snapshot=AsyncMock(return_value=None)),
        None,
        engine=engine or FakeEngine(),
        enabled=enabled,
        alert_manager=alerts,
    )


class ProgressBarTests(unittest.TestCase):
    def test_bar_reflects_percent(self) -> None:
        self.assertIn("0%", format_progress_bar("Старт", 0))
        self.assertIn("100%", format_progress_bar("Готово", 100))

    def test_bar_has_fixed_width(self) -> None:
        for percent in (0, 17, 50, 99, 100):
            bar = format_progress_bar("Этап", percent).splitlines()[0]
            blocks = bar.split(" ")[0]
            self.assertEqual(len(blocks), 12, f"Сбилась ширина на {percent}%")

    def test_bar_clamps_out_of_range(self) -> None:
        self.assertIn("0%", format_progress_bar("Этап", -50))
        self.assertIn("100%", format_progress_bar("Этап", 500))

    def test_bar_shows_stage(self) -> None:
        self.assertIn("Распознаю текст", format_progress_bar(OCR_STAGE_RECOGNIZE, 35))

    def test_stages_are_ordered_and_bounded(self) -> None:
        percents = [percent for _stage, percent in OCR_STAGES]
        self.assertEqual(percents, sorted(percents))
        self.assertTrue(all(0 < p < 100 for p in percents), "Проценты этапов вне диапазона")


class WarmUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_warm_up_sets_flag_and_reports_ok(self) -> None:
        alerts = MagicMock(report_component_status=AsyncMock())
        importer = make_importer(alerts=alerts)

        await importer.warm_up()

        self.assertTrue(importer.is_warm)
        self.assertEqual(importer.last_error, "")
        alerts.report_component_status.assert_awaited_once()
        self.assertEqual(alerts.report_component_status.await_args.args[0], "ocr")
        self.assertTrue(alerts.report_component_status.await_args.args[1])

    async def test_warm_up_failure_reports_down(self) -> None:
        alerts = MagicMock(report_component_status=AsyncMock())
        importer = make_importer(engine=FakeEngine(warm_error="нет моделей"), alerts=alerts)

        await importer.warm_up()

        self.assertFalse(importer.is_warm)
        self.assertIn("нет моделей", importer.last_error)
        self.assertFalse(alerts.report_component_status.await_args.args[1])

    async def test_warm_up_survives_broken_alert_manager(self) -> None:
        alerts = MagicMock(report_component_status=AsyncMock(side_effect=RuntimeError("мониторинг лёг")))
        importer = make_importer(alerts=alerts)

        await importer.warm_up()

        self.assertTrue(importer.is_warm, "Сбой мониторинга не должен мешать прогреву")

    async def test_warm_up_skipped_when_engine_unavailable(self) -> None:
        engine = FakeEngine(available=False)
        importer = make_importer(engine=engine)

        await importer.warm_up()

        self.assertEqual(engine.warm_calls, 0)
        self.assertFalse(importer.is_warm)


class StatusLineTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_shows_red(self) -> None:
        line = make_importer(enabled=False).status_line(html=False)
        self.assertTrue(line.startswith("🔴"))
        self.assertIn("отключён", line)

    def test_cold_engine_shows_yellow(self) -> None:
        line = make_importer().status_line(html=False)
        self.assertTrue(line.startswith("🟡"))
        self.assertIn("греются", line)

    async def test_warm_engine_shows_green(self) -> None:
        importer = make_importer()
        await importer.warm_up()

        line = importer.status_line(html=False)

        self.assertTrue(line.startswith("🟢"))
        self.assertIn("fake", line)

    async def test_last_error_shows_yellow(self) -> None:
        importer = make_importer()
        await importer.warm_up()
        importer.last_error = "таймаут распознавания"

        line = importer.status_line(html=False)

        self.assertTrue(line.startswith("🟡"))
        self.assertIn("таймаут распознавания", line)

    async def test_html_variant_is_escaped(self) -> None:
        importer = make_importer()
        await importer.warm_up()
        importer.last_error = "<битый> тег"

        line = importer.status_line(html=True)

        self.assertIn("<b>", line)
        self.assertIn("&lt;битый&gt;", line)
        self.assertNotIn("<битый>", line)


class ProgressReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_draft_reports_stages_in_order(self) -> None:
        importer = make_importer()
        await importer.warm_up()
        seen: list[tuple[str, int]] = []

        async def progress(stage: str, percent: int) -> None:
            seen.append((stage, percent))

        await importer.build_draft(b"image", progress=progress)

        stages = [stage for stage, _ in seen]
        self.assertIn(OCR_STAGE_RECOGNIZE, stages)
        self.assertIn(OCR_STAGE_PARSE, stages)
        self.assertIn(OCR_STAGE_PREVIEW, stages)
        percents = [percent for _, percent in seen]
        self.assertEqual(percents, sorted(percents), "Проценты не растут монотонно")

    async def test_broken_progress_callback_does_not_break_import(self) -> None:
        importer = make_importer()

        async def progress(stage: str, percent: int) -> None:
            raise RuntimeError("телеграм отвалился")

        draft = await importer.build_draft(b"image", progress=progress)

        self.assertTrue(draft.can_apply)

    async def test_build_draft_without_progress_callback(self) -> None:
        draft = await make_importer().build_draft(b"image")
        self.assertTrue(draft.can_apply)


class FailureReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_recognition_failure_is_reported_and_raised(self) -> None:
        alerts = MagicMock(report_component_status=AsyncMock())
        importer = make_importer(engine=FakeEngine(recognize_error="движок умер"), alerts=alerts)

        with self.assertRaises(OcrEngineError):
            await importer.build_draft(b"image")

        self.assertIn("движок умер", importer.last_error)
        self.assertFalse(alerts.report_component_status.await_args.args[1])

    async def test_success_clears_error_and_stamps_time(self) -> None:
        alerts = MagicMock(report_component_status=AsyncMock())
        importer = make_importer(alerts=alerts)
        importer.last_error = "старая ошибка"

        await importer.build_draft(b"image")

        self.assertEqual(importer.last_error, "")
        self.assertTrue(importer.last_success_at)
        self.assertTrue(alerts.report_component_status.await_args.args[1])


class SystemStatusTests(unittest.IsolatedAsyncioTestCase):
    def test_component_is_registered(self) -> None:
        self.assertIn("ocr", COMPONENT_TITLES)

    async def test_check_reports_missing_importer(self) -> None:
        status = await check_ocr_status(None)
        self.assertFalse(status["ok"])
        self.assertFalse(status["ready"])

    async def test_check_reports_cold_engine(self) -> None:
        status = await check_ocr_status(make_importer())
        self.assertTrue(status["ok"])
        self.assertFalse(status["ready"])

    async def test_check_reports_ready_engine(self) -> None:
        importer = make_importer()
        await importer.warm_up()

        status = await check_ocr_status(importer)

        self.assertTrue(status["ok"])
        self.assertTrue(status["ready"])
        self.assertEqual(status["engine"], "fake")

    async def test_check_reports_unavailable_engine(self) -> None:
        status = await check_ocr_status(make_importer(enabled=False))
        self.assertFalse(status["ok"])
        self.assertIn("отключён", status["error"])

    async def test_check_survives_broken_importer(self) -> None:
        broken = MagicMock()
        broken.availability = MagicMock(side_effect=RuntimeError("всё плохо"))

        status = await check_ocr_status(broken)

        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    """Распознавание идёт минуты: индикатор обязан показывать, что он жив."""

    async def test_heartbeat_updates_during_long_recognition(self) -> None:
        import src.ocr_import as ocr_import

        original = ocr_import.HEARTBEAT_INTERVAL_SECONDS
        ocr_import.HEARTBEAT_INTERVAL_SECONDS = 0.01

        class SlowEngine(FakeEngine):
            def recognize(self, image_bytes: bytes) -> str:
                import time as _time

                _time.sleep(0.12)
                return RECOGNIZED_TEXT

        try:
            importer = make_importer(engine=SlowEngine())
            seen: list[str] = []

            async def progress(stage: str, percent: int) -> None:
                seen.append(stage)

            await importer.build_draft(b"image", progress=progress)
        finally:
            ocr_import.HEARTBEAT_INTERVAL_SECONDS = original

        ticks = [stage for stage in seen if "с)" in stage]
        self.assertTrue(ticks, f"Пульс не сработал, этапы: {seen}")
        self.assertIn(OCR_STAGE_RECOGNIZE, ticks[0])

    async def test_heartbeat_stops_after_recognition(self) -> None:
        importer = make_importer()
        seen: list[str] = []

        async def progress(stage: str, percent: int) -> None:
            seen.append(stage)

        await importer.build_draft(b"image", progress=progress)
        before = len(seen)
        await asyncio.sleep(0.05)

        self.assertEqual(len(seen), before, "Пульс продолжился после завершения")

    def test_heartbeat_interval_is_sane(self) -> None:
        self.assertGreater(HEARTBEAT_INTERVAL_SECONDS, 0)
        self.assertLessEqual(HEARTBEAT_INTERVAL_SECONDS, 30)


class EngineTuningTests(unittest.TestCase):
    """Уменьшение кадра теряет пары: на 1600 из четырёх распознаётся три."""

    def test_default_image_side_keeps_accuracy(self) -> None:
        from src.ocr_schedule import MAX_DETECTION_SIDE, EasyOcrEngine

        self.assertEqual(MAX_DETECTION_SIDE, 2000)
        self.assertEqual(EasyOcrEngine().max_image_side, 2000)

    def test_image_side_has_lower_bound(self) -> None:
        from src.ocr_schedule import EasyOcrEngine

        self.assertGreaterEqual(EasyOcrEngine(max_image_side=10).max_image_side, 600)

    def test_threads_are_configurable(self) -> None:
        from src.ocr_schedule import EasyOcrEngine

        self.assertEqual(EasyOcrEngine(threads=4).threads, 4)
        self.assertEqual(EasyOcrEngine(threads=-5).threads, 0)

    def test_threads_unlimited_by_default(self) -> None:
        """Потоки — главный фактор скорости: 2 потока вдвое медленнее всех ядер."""
        from src.ocr_schedule import EasyOcrEngine

        self.assertEqual(EasyOcrEngine().threads, 0)

    def test_factory_passes_tuning(self) -> None:
        from src.ocr_schedule import build_ocr_engine

        engine = build_ocr_engine(engine="easyocr", threads=3, max_image_side=1800)
        self.assertEqual(engine.threads, 3)
        self.assertEqual(engine.max_image_side, 1800)


class LoggingRestoreTests(unittest.TestCase):
    """alembic глушит логи: без восстановления бот работает вслепую."""

    def test_restore_logging_reenables_info(self) -> None:
        import logging as std_logging

        from src.main import restore_logging

        root = std_logging.getLogger()
        original_level = root.level
        try:
            root.setLevel(std_logging.WARNING)
            std_logging.getLogger("src").disabled = True

            restore_logging()

            self.assertEqual(root.level, std_logging.INFO)
            self.assertFalse(std_logging.getLogger("src").disabled)
        finally:
            root.setLevel(original_level)

    def test_restore_logging_adds_handler_when_missing(self) -> None:
        import logging as std_logging

        from src.main import restore_logging

        root = std_logging.getLogger()
        saved = list(root.handlers)
        try:
            root.handlers = []
            restore_logging()
            self.assertTrue(root.handlers, "Обработчик не добавлен")
        finally:
            root.handlers = saved


class ReadyNoticeThrottleTests(unittest.TestCase):
    """При крешлупе уведомление о готовности не должно спамиться."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.marker = pathlib.Path(self._tmp.name) / "runtime" / ".ocr_ready_notified"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_start_notifies(self) -> None:
        from src.main import should_notify_ocr_ready

        self.assertTrue(should_notify_ocr_ready(self.marker, 12))

    def test_restart_within_window_is_silent(self) -> None:
        from src.main import should_notify_ocr_ready

        self.assertTrue(should_notify_ocr_ready(self.marker, 12))
        for _ in range(5):
            self.assertFalse(should_notify_ocr_ready(self.marker, 12))

    def test_zero_interval_always_notifies(self) -> None:
        from src.main import should_notify_ocr_ready

        self.assertTrue(should_notify_ocr_ready(self.marker, 0))
        self.assertTrue(should_notify_ocr_ready(self.marker, 0))

    def test_expired_marker_notifies_again(self) -> None:
        import os

        from src.main import should_notify_ocr_ready

        self.assertTrue(should_notify_ocr_ready(self.marker, 12))
        old = time.time() - 13 * 3600
        os.utime(self.marker, (old, old))

        self.assertTrue(should_notify_ocr_ready(self.marker, 12))

    def test_unwritable_path_does_not_crash(self) -> None:
        from src.main import should_notify_ocr_ready

        broken = pathlib.Path("/proc/definitely/not/writable/.marker")
        self.assertIsInstance(should_notify_ocr_ready(broken, 12), bool)

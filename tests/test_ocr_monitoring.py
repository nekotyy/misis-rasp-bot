"""Тесты наблюдаемости импорта расписания с фото.

Прогрев, индикатор прогресса, строка статуса, отчёты в мониторинг и поведение
при сбоях. Без этого распознавание — чёрный ящик: админ видит «Распознаю...»
и не понимает, живо оно или наебнулось.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gemini_webapi.exceptions import TemporarilyBlockedError, UsageLimitExceededError

from src.ocr_import import (
    HEARTBEAT_INTERVAL_SECONDS,
    OCR_STAGE_PARSE,
    OCR_STAGE_PREVIEW,
    OCR_STAGE_RECOGNIZE,
    OCR_STAGES,
    OcrScheduleImporter,
    format_progress_bar,
)
from src.ocr_schedule import GeminiOcrEngine, OcrEngineError, classify_gemini_failure
from src.system_status import COMPONENT_TITLES, check_ocr_status

RECOGNIZED_TEXT = json.dumps(
    {
        "group_name": "ИСП-25-1",
        "days": [
            {
                "date_iso": "2026-09-01",
                "lessons": [
                    {"number": 1, "subject": "Операционные системы и среды", "teacher": "Кубанева Е.А.", "classroom": "301"},
                ],
            }
        ],
    },
    ensure_ascii=False,
)

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

    async def warm_up(self) -> None:
        self.warm_calls += 1
        if self._warm_error:
            raise RuntimeError(self._warm_error)

    async def recognize(self, images: list[bytes]) -> str:
        if self._recognize_error:
            raise OcrEngineError(self._recognize_error)
        return RECOGNIZED_TEXT

    def diagnostics(self) -> dict[str, str | int | bool]:
        return {
            "doh_enabled": True,
            "remote_ip": "87.228.47.194",
            "route_http_status": 200,
            "account_status": "AVAILABLE",
        }


class GeminiFailureClassificationTests(unittest.TestCase):
    def test_geo_rejection_is_non_retryable(self) -> None:
        result = classify_gemini_failure("Account status: LOCATION_REJECTED - unsupported country/region")

        self.assertEqual(result.code, "geo")
        self.assertFalse(result.retryable)

    def test_ip_429_is_distinct_from_quota(self) -> None:
        result = classify_gemini_failure(TemporarilyBlockedError("HTTP 429: IP address temporarily flagged"))

        self.assertEqual(result.code, "ip_block")
        self.assertTrue(result.retryable)

    def test_usage_limit_has_own_category(self) -> None:
        result = classify_gemini_failure(UsageLimitExceededError("Usage limit exceeded"))

        self.assertEqual(result.code, "quota")
        self.assertTrue(result.retryable)

    def test_auth_and_network_categories(self) -> None:
        self.assertEqual(classify_gemini_failure("cookies have expired").code, "auth")
        self.assertEqual(classify_gemini_failure("connection refused").code, "network")
        self.assertEqual(classify_gemini_failure("Gemini не завершил распознавание за 60 с.").code, "timeout")


class GeminiRouteProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_uses_doh_and_records_real_remote_ip(self) -> None:
        response = MagicMock(status_code=200, primary_ip="87.228.47.202")
        session = MagicMock(get=AsyncMock(return_value=response), close=AsyncMock())
        engine = GeminiOcrEngine(doh_url="https://xbox-dns.ru/dns-query")

        with patch("src.ocr_schedule.CurlAsyncSession", return_value=session) as session_factory:
            await engine._probe_route()

        session_factory.assert_called_once_with(
            doh_url="https://xbox-dns.ru/dns-query",
            proxy=None,
        )
        self.assertEqual(engine.diagnostics()["remote_ip"], "87.228.47.202")
        self.assertEqual(engine.diagnostics()["route_http_status"], 200)
        session.close.assert_awaited_once()

    async def test_probe_classifies_http_429_before_authorization(self) -> None:
        response = MagicMock(status_code=429, primary_ip="87.228.47.194")
        session = MagicMock(get=AsyncMock(return_value=response), close=AsyncMock())
        engine = GeminiOcrEngine(doh_url="https://xbox-dns.ru/dns-query")

        with (
            patch("src.ocr_schedule.CurlAsyncSession", return_value=session),
            self.assertRaisesRegex(OcrEngineError, "HTTP 429"),
        ):
            await engine._probe_route()

        session.close.assert_awaited_once()


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

    async def test_geo_failure_shows_red_reason_code(self) -> None:
        importer = make_importer(engine=FakeEngine(warm_error="LOCATION_REJECTED country/region"))

        await importer.warm_up()
        line = importer.status_line(html=False)

        self.assertTrue(line.startswith("🔴"))
        self.assertIn("географическое ограничение", line)
        self.assertIn("[geo]", line)

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

        await importer.build_draft([b"image"], progress=progress)

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

        draft = await importer.build_draft([b"image"], progress=progress)

        self.assertTrue(draft.can_apply)

    async def test_build_draft_without_progress_callback(self) -> None:
        draft = await make_importer().build_draft([b"image"])
        self.assertTrue(draft.can_apply)


class FailureReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_recognition_failure_is_reported_and_raised(self) -> None:
        alerts = MagicMock(report_component_status=AsyncMock())
        importer = make_importer(engine=FakeEngine(recognize_error="движок умер"), alerts=alerts)

        with self.assertRaises(OcrEngineError):
            await importer.build_draft([b"image"])

        self.assertIn("движок умер", importer.last_error)
        self.assertFalse(alerts.report_component_status.await_args.args[1])
        self.assertEqual(importer.last_failure_code, "unknown")
        self.assertEqual(importer.consecutive_failures, 1)
        self.assertIn("Gemini IP: 87.228.47.194", alerts.report_component_status.await_args.kwargs["details"])

    async def test_success_clears_error_and_stamps_time(self) -> None:
        alerts = MagicMock(report_component_status=AsyncMock())
        importer = make_importer(alerts=alerts)
        importer.last_error = "старая ошибка"

        await importer.build_draft([b"image"])

        self.assertEqual(importer.last_error, "")
        self.assertTrue(importer.last_success_at)
        self.assertEqual(importer.consecutive_failures, 0)
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

    async def test_check_reports_classified_runtime_failure(self) -> None:
        importer = make_importer(engine=FakeEngine(warm_error="LOCATION_REJECTED country/region"))
        await importer.warm_up()

        status = await check_ocr_status(importer)

        self.assertFalse(status["ok"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["diagnostics"]["failure_code"], "geo")
        self.assertEqual(status["diagnostics"]["remote_ip"], "87.228.47.194")

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
            async def recognize(self, images: list[bytes]) -> str:
                await asyncio.sleep(0.12)
                return RECOGNIZED_TEXT

        try:
            importer = make_importer(engine=SlowEngine())
            seen: list[str] = []

            async def progress(stage: str, percent: int) -> None:
                seen.append(stage)

            await importer.build_draft([b"image"], progress=progress)
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

        await importer.build_draft([b"image"], progress=progress)
        before = len(seen)
        await asyncio.sleep(0.05)

        self.assertEqual(len(seen), before, "Пульс продолжился после завершения")

    def test_heartbeat_interval_is_sane(self) -> None:
        self.assertGreater(HEARTBEAT_INTERVAL_SECONDS, 0)
        self.assertLessEqual(HEARTBEAT_INTERVAL_SECONDS, 30)


class ImageExtensionTests(unittest.TestCase):
    """Без настоящего расширения файл уходит в Gemini как text/plain и картинка не распознаётся."""

    def test_jpeg_signature(self) -> None:
        from src.ocr_schedule import _guess_image_extension

        self.assertEqual(_guess_image_extension(b"\xff\xd8\xff\xe0rest"), ".jpg")

    def test_png_signature(self) -> None:
        from src.ocr_schedule import _guess_image_extension

        self.assertEqual(_guess_image_extension(b"\x89PNG\r\n\x1a\nrest"), ".png")

    def test_webp_signature(self) -> None:
        from src.ocr_schedule import _guess_image_extension

        self.assertEqual(_guess_image_extension(b"RIFF????WEBPrest"), ".webp")

    def test_unknown_signature_defaults_to_jpg(self) -> None:
        from src.ocr_schedule import _guess_image_extension

        self.assertEqual(_guess_image_extension(b"not an image"), ".jpg")


class GeminiEngineRecognizeTests(unittest.IsolatedAsyncioTestCase):
    """Регрессия: `BytesIO` без имени файла уходит в Gemini как text/plain, и картинка не распознаётся."""

    async def test_recognize_uploads_a_real_file_with_image_extension(self) -> None:
        import os
        from unittest.mock import patch

        from src.ocr_schedule import (
            OCR_GEM_DESCRIPTION,
            OCR_GEM_SYSTEM_PROMPT,
            GeminiOcrEngine,
        )

        captured: dict[str, object] = {}

        class FakeGeminiClient:
            async def init(self, **kwargs) -> None:
                captured["init"] = kwargs
                return None

            def resolve_model(self, name):
                captured.setdefault("resolved_names", []).append(name)
                return "resolved-flash"

            async def fetch_gems(self):
                jar = MagicMock()
                jar.get = MagicMock(
                    side_effect=lambda **kwargs: MagicMock(
                        id="ocr-gem",
                        prompt=OCR_GEM_SYSTEM_PROMPT,
                        description=OCR_GEM_DESCRIPTION,
                    )
                    if kwargs.get("name")
                    else None
                )
                return jar

            async def generate_content(self, prompt, files=None, model=None, gem=None, temporary=False):
                path = files[0]
                captured["path"] = path
                captured["model"] = model
                captured["gem"] = gem
                captured["temporary"] = temporary
                with open(path, "rb") as opened:
                    captured["content"] = opened.read()
                return MagicMock(text='{"group_name": "", "days": []}')

        with patch("src.ocr_schedule.GeminiClient", return_value=FakeGeminiClient()):
            engine = GeminiOcrEngine(secure_1psid="a", secure_1psidts="b")
            engine._probe_route = AsyncMock()
            await engine.recognize([b"\xff\xd8\xff\xe0fake-jpeg-bytes"])

        self.assertIsInstance(captured["path"], str)
        self.assertTrue(str(captured["path"]).endswith(".jpg"))
        self.assertEqual(captured["content"], b"\xff\xd8\xff\xe0fake-jpeg-bytes")
        self.assertTrue(captured["init"]["auto_refresh"])
        self.assertEqual(captured["resolved_names"], ["flash", "pro"])
        self.assertEqual(captured["model"], "resolved-flash")
        self.assertEqual(captured["gem"], "ocr-gem")
        self.assertTrue(captured["temporary"])
        self.assertFalse(os.path.exists(str(captured["path"])), "Временный файл должен удаляться после запроса")

    async def test_recognize_uploads_several_photos_in_one_request(self) -> None:
        """Несколько фото (например, части одной таблицы) уходят в Gemini одним запросом."""
        import os
        from unittest.mock import patch

        from src.ocr_schedule import (
            OCR_GEM_DESCRIPTION,
            OCR_GEM_SYSTEM_PROMPT,
            GeminiOcrEngine,
        )

        captured: dict[str, object] = {}

        class FakeGeminiClient:
            async def init(self, **kwargs) -> None:
                return None

            def resolve_model(self, name):
                return "resolved-flash"

            async def fetch_gems(self):
                jar = MagicMock()
                jar.get = MagicMock(
                    side_effect=lambda **kwargs: MagicMock(
                        id="ocr-gem",
                        prompt=OCR_GEM_SYSTEM_PROMPT,
                        description=OCR_GEM_DESCRIPTION,
                    )
                    if kwargs.get("name")
                    else None
                )
                return jar

            async def generate_content(self, prompt, files=None, model=None, gem=None, temporary=False):
                captured["paths"] = list(files)
                contents = []
                for path in files:
                    with open(path, "rb") as opened:
                        contents.append(opened.read())
                captured["contents"] = contents
                return MagicMock(text='{"group_name": "", "days": []}')

        images = [b"\xff\xd8\xff\xe0first", b"\x89PNG\r\n\x1a\nsecond"]
        with patch("src.ocr_schedule.GeminiClient", return_value=FakeGeminiClient()):
            engine = GeminiOcrEngine(secure_1psid="a", secure_1psidts="b")
            engine._probe_route = AsyncMock()
            await engine.recognize(images)

        paths = captured["paths"]
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].endswith(".jpg"))
        self.assertTrue(paths[1].endswith(".png"))
        self.assertEqual(captured["contents"], images)
        for path in paths:
            self.assertFalse(os.path.exists(path), "Временные файлы должны удаляться после запроса")

    def test_update_env_file_replaces_secrets_and_preserves_other_settings(self) -> None:
        import tempfile

        from src.ocr_schedule import update_env_file

        with tempfile.TemporaryDirectory() as directory:
            env_path = pathlib.Path(directory) / ".env"
            env_path.write_text("OCR_ENABLED=true\nGEMINI_SECURE_1PSIDTS=old\n", encoding="utf-8")

            update_env_file(
                env_path,
                {
                    "GEMINI_SECURE_1PSIDTS": "new",
                    "GEMINI_OCR_GEM_ID": "gem-123",
                },
            )

            content = env_path.read_text(encoding="utf-8")
            self.assertIn("OCR_ENABLED=true", content)
            self.assertIn("GEMINI_SECURE_1PSIDTS=new", content)
            self.assertIn("GEMINI_OCR_GEM_ID=gem-123", content)
            self.assertNotIn("=old", content)

    async def test_non_json_flash_response_retries_once_with_pro(self) -> None:
        from unittest.mock import patch

        from src.ocr_schedule import (
            OCR_GEM_DESCRIPTION,
            OCR_GEM_SYSTEM_PROMPT,
            GeminiOcrEngine,
        )

        flash = MagicMock(model_id="flash-id")
        pro = MagicMock(model_id="pro-id")
        client = MagicMock()
        client.init = AsyncMock()
        client.account_status = 1000
        client.close = AsyncMock()
        client.resolve_model = MagicMock(side_effect=lambda name: pro if name == "pro" else flash)
        jar = MagicMock()
        jar.get = MagicMock(
            side_effect=lambda **kwargs: MagicMock(
                id="ocr-gem",
                prompt=OCR_GEM_SYSTEM_PROMPT,
                description=OCR_GEM_DESCRIPTION,
            )
            if kwargs.get("name")
            else None
        )
        client.fetch_gems = AsyncMock(return_value=jar)
        client.generate_content = AsyncMock(
            side_effect=[
                MagicMock(text="Я не могу обработать изображение."),
                MagicMock(text='{"group_name": "ИСП-25-1", "days": []}'),
            ]
        )

        with patch("src.ocr_schedule.GeminiClient", return_value=client):
            engine = GeminiOcrEngine(secure_1psid="a", secure_1psidts="b")
            engine._probe_route = AsyncMock()
            result = await engine.recognize([b"\xff\xd8\xffimage"])

        self.assertIn("ИСП-25-1", result)
        self.assertEqual(client.generate_content.await_count, 2)
        self.assertIs(client.generate_content.await_args_list[0].kwargs["model"], flash)
        self.assertIs(client.generate_content.await_args_list[1].kwargs["model"], pro)

    async def test_recognize_rejects_empty_image_list(self) -> None:
        from src.ocr_schedule import GeminiOcrEngine

        engine = GeminiOcrEngine(secure_1psid="a", secure_1psidts="b")
        with self.assertRaises(OcrEngineError):
            await engine.recognize([])

    async def test_recognize_rejects_too_many_images(self) -> None:
        from src.ocr_schedule import MAX_OCR_IMAGES, GeminiOcrEngine

        engine = GeminiOcrEngine(secure_1psid="a", secure_1psidts="b")
        with self.assertRaises(OcrEngineError):
            await engine.recognize([b"x"] * (MAX_OCR_IMAGES + 1))


class EngineFactoryTests(unittest.TestCase):
    def test_factory_builds_gemini_engine(self) -> None:
        from src.ocr_schedule import GeminiOcrEngine, build_ocr_engine

        engine = build_ocr_engine(secure_1psid="psid", secure_1psidts="psidts", model="gemini-pro")
        self.assertIsInstance(engine, GeminiOcrEngine)
        self.assertEqual(engine.model, "gemini-pro")
        self.assertEqual(engine.doh_url, "https://xbox-dns.ru/dns-query")
        available, _ = engine.availability()
        self.assertTrue(available)

    def test_gemini_session_gets_its_own_doh(self) -> None:
        import importlib
        from unittest.mock import patch

        from src.ocr_schedule import configure_gemini_doh

        module = importlib.import_module("gemini_webapi.utils.get_access_token")
        original = module.AsyncSession
        session = object()
        try:
            with patch("src.ocr_schedule.CurlAsyncSession", return_value=session) as session_class:
                configure_gemini_doh("https://xbox-dns.ru/dns-query")
                self.assertIs(module.AsyncSession(verify=True), session)
            session_class.assert_called_once_with(
                verify=True,
                doh_url="https://xbox-dns.ru/dns-query",
            )
        finally:
            module.AsyncSession = original

    def test_factory_reports_missing_cookies(self) -> None:
        from src.ocr_schedule import build_ocr_engine

        engine = build_ocr_engine()
        available, message = engine.availability()
        self.assertFalse(available)
        self.assertIn("куки", message.lower())


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

    def test_restore_logging_reenables_existing_src_children(self) -> None:
        import logging as std_logging

        from src.main import restore_logging

        child = std_logging.getLogger("src.ocr_schedule")
        child.disabled = True

        restore_logging()

        self.assertFalse(child.disabled)

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

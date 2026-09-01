from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.ocr_import import OcrScheduleImporter, build_ocr_importer, format_ocr_preview
from src.ocr_schedule import OcrEngineError

RECOGNIZED_TEXT = """ИСП-25-1
01.09.2026 г.
Пара Дисциплина Преподаватель Ауд.
1 Операционные системы и среды Кубанева Е.А. 301
2 Физическая культура Кузьминова И.Н. с-3
"""

ACTIVE_SOURCE = {
    "source_type": "group",
    "source_key": "group:600",
    "source_title": "ИСП-25-1",
    "source_url": "http://example.com/rasp/600",
    "schedule_id": 600,
    "group_name": "ИСП-25-1",
    "users_count": 12,
}


class FakeEngine:
    """Движок-заглушка: отдаёт заранее заданный текст вместо запуска Tesseract."""

    def __init__(self, text: str = RECOGNIZED_TEXT, available: bool = True) -> None:
        self.text = text
        self._available = available

    def availability(self) -> tuple[bool, str]:
        if self._available:
            return True, "fake-tesseract 5.0"
        return False, "Tesseract не найден."

    def recognize(self, image_bytes: bytes) -> str:
        if not image_bytes:
            raise OcrEngineError("Пустое изображение.")
        return self.text


def make_db(sources: list[dict] | None = None, latest_snapshot: dict | None = None) -> MagicMock:
    db = MagicMock()
    db.get_active_sources = AsyncMock(return_value=sources if sources is not None else [ACTIVE_SOURCE])
    db.get_latest_snapshot = AsyncMock(return_value=latest_snapshot)
    return db


def make_importer(
    *,
    db: MagicMock | None = None,
    jobs: MagicMock | None = None,
    catalog: MagicMock | None = None,
    engine: FakeEngine | None = None,
    enabled: bool = True,
) -> OcrScheduleImporter:
    return OcrScheduleImporter(
        db if db is not None else make_db(),
        jobs if jobs is not None else MagicMock(apply_manual_snapshot=AsyncMock(return_value=None)),
        catalog,
        engine=engine or FakeEngine(),
        enabled=enabled,
    )


class AvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_importer_reports_reason(self) -> None:
        importer = make_importer(enabled=False)
        available, message = importer.availability()

        self.assertFalse(available)
        self.assertIn("отключён", message)

    def test_missing_engine_reports_reason(self) -> None:
        importer = OcrScheduleImporter(make_db(), MagicMock(), None, engine=None)
        available, message = importer.availability()

        self.assertFalse(available)
        self.assertIn("не настроен", message)

    def test_missing_scheduler_reports_reason(self) -> None:
        importer = OcrScheduleImporter(make_db(), None, None, engine=FakeEngine())
        available, message = importer.availability()

        self.assertFalse(available)
        self.assertIn("Планировщик", message)

    def test_available_when_engine_ready(self) -> None:
        available, message = make_importer().availability()

        self.assertTrue(available)
        self.assertIn("fake-tesseract", message)

    async def test_build_draft_refuses_when_unavailable(self) -> None:
        importer = make_importer(enabled=False)
        with self.assertRaises(OcrEngineError):
            await importer.build_draft(b"image")


class ResolveSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_from_active_sources(self) -> None:
        importer = make_importer()
        source, error = await importer._resolve_source("ИСП-25-1")

        self.assertEqual(error, "")
        self.assertEqual(source["schedule_id"], 600)

    async def test_resolution_is_case_and_dash_insensitive(self) -> None:
        importer = make_importer()
        source, error = await importer._resolve_source("исп - 25 - 1")

        self.assertEqual(error, "")
        self.assertIsNotNone(source)

    async def test_falls_back_to_group_catalog(self) -> None:
        catalog = MagicMock()
        catalog.find_group = AsyncMock(
            return_value=MagicMock(schedule_id=701, group_name="ИСП-25-2", url="http://example.com/rasp/701")
        )
        importer = make_importer(db=make_db(sources=[]), catalog=catalog)

        source, error = await importer._resolve_source("ИСП-25-2")

        self.assertEqual(error, "")
        self.assertEqual(source["schedule_id"], 701)
        self.assertEqual(source["source_key"], "group:701")

    async def test_survives_broken_catalog(self) -> None:
        catalog = MagicMock()
        catalog.find_group = AsyncMock(side_effect=RuntimeError("сайт лежит"))
        importer = make_importer(db=make_db(sources=[]), catalog=catalog)

        source, error = await importer._resolve_source("ИСП-25-9")

        self.assertIsNone(source)
        self.assertIn("не найдена", error)

    async def test_unknown_group_lists_known_ones(self) -> None:
        importer = make_importer()
        source, error = await importer._resolve_source("ЮРИ-30-7")

        self.assertIsNone(source)
        self.assertIn("ИСП-25-1", error)

    async def test_unreadable_group_name_reports_error(self) -> None:
        importer = make_importer()
        source, error = await importer._resolve_source("Неизвестная группа")

        self.assertIsNone(source)
        self.assertIn("название группы", error)


class BuildDraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_applicable_draft(self) -> None:
        importer = make_importer()
        draft = await importer.build_draft(b"image-bytes")

        self.assertTrue(draft.can_apply)
        self.assertEqual(draft.source["schedule_id"], 600)
        self.assertEqual(draft.result.lessons_count, 2)
        self.assertEqual(draft.raw_text, RECOGNIZED_TEXT)

    async def test_draft_merges_with_stored_snapshot(self) -> None:
        stored = {
            "content": {
                "group_name": "ИСП-25-1",
                "days": [
                    {
                        "date_iso": "2026-08-31",
                        "date_label": "31.08.2026",
                        "lessons": [
                            {"number": 1, "subject": "История", "teacher": "Сидоров С.С.", "classroom": "105"}
                        ],
                    }
                ],
            }
        }
        importer = make_importer(db=make_db(latest_snapshot=stored))
        draft = await importer.build_draft(b"image-bytes")

        dates = [day.date_iso for day in draft.snapshot.days]
        self.assertIn("2026-08-31", dates)
        self.assertIn("2026-09-01", dates)
        self.assertEqual(draft.merge.kept_dates, ["2026-08-31"])

    async def test_draft_without_known_group_cannot_apply(self) -> None:
        importer = make_importer(db=make_db(sources=[]))
        draft = await importer.build_draft(b"image-bytes")

        self.assertFalse(draft.can_apply)
        self.assertIn("не найдена", draft.source_error)


class ApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_delegates_to_schedule_jobs(self) -> None:
        jobs = MagicMock()
        jobs.apply_manual_snapshot = AsyncMock(return_value=None)
        importer = make_importer(jobs=jobs)
        draft = await importer.build_draft(b"image-bytes")

        applied, report = await importer.apply(draft)

        self.assertTrue(applied)
        jobs.apply_manual_snapshot.assert_awaited_once()
        call = jobs.apply_manual_snapshot.await_args
        self.assertEqual(call.args[0]["schedule_id"], 600)
        self.assertIsInstance(call.args[1], ScheduleSnapshot)
        self.assertTrue(call.kwargs["notify"])
        self.assertIn("Отличий от эталона нет", report)

    async def test_apply_without_notify_passes_flag(self) -> None:
        jobs = MagicMock()
        jobs.apply_manual_snapshot = AsyncMock(return_value=None)
        importer = make_importer(jobs=jobs)
        draft = await importer.build_draft(b"image-bytes")

        await importer.apply(draft, notify=False)

        self.assertFalse(jobs.apply_manual_snapshot.await_args.kwargs["notify"])

    async def test_apply_reports_broadcast(self) -> None:
        change = MagicMock(changed_dates=["2026-09-01"])
        jobs = MagicMock()
        jobs.apply_manual_snapshot = AsyncMock(return_value=change)
        importer = make_importer(jobs=jobs)
        draft = await importer.build_draft(b"image-bytes")

        applied, report = await importer.apply(draft)

        self.assertTrue(applied)
        self.assertIn("разосланы", report)

    async def test_apply_refuses_without_source(self) -> None:
        importer = make_importer(db=make_db(sources=[]))
        draft = await importer.build_draft(b"image-bytes")

        applied, report = await importer.apply(draft)

        self.assertFalse(applied)
        self.assertIn("не найдена", report)

    async def test_apply_reports_scheduler_failure(self) -> None:
        jobs = MagicMock()
        jobs.apply_manual_snapshot = AsyncMock(side_effect=RuntimeError("БД недоступна"))
        importer = make_importer(jobs=jobs)
        draft = await importer.build_draft(b"image-bytes")

        applied, report = await importer.apply(draft)

        self.assertFalse(applied)
        self.assertIn("БД недоступна", report)


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_contains_key_sections(self) -> None:
        importer = make_importer()
        draft = await importer.build_draft(b"image-bytes")

        preview = format_ocr_preview(draft, html=True)

        self.assertIn("ИСП-25-1", preview)
        self.assertIn("Уверенность", preview)
        self.assertIn("Операционные системы и среды", preview)
        self.assertIn("Что будет сохранено", preview)
        self.assertIn("<b>", preview)

    async def test_plain_preview_has_no_html(self) -> None:
        importer = make_importer()
        draft = await importer.build_draft(b"image-bytes")

        preview = format_ocr_preview(draft, html=False)

        self.assertNotIn("<b>", preview)
        self.assertIn("Уверенность", preview)

    async def test_preview_respects_max_length(self) -> None:
        importer = make_importer()
        draft = await importer.build_draft(b"image-bytes")

        preview = format_ocr_preview(draft, html=False, max_length=120)

        self.assertLessEqual(len(preview), 120)
        self.assertTrue(preview.endswith("…"))

    async def test_preview_not_truncated_when_short_enough(self) -> None:
        importer = make_importer()
        draft = await importer.build_draft(b"image-bytes")

        full = format_ocr_preview(draft, html=False)
        self.assertEqual(format_ocr_preview(draft, html=False, max_length=10_000), full)

    async def test_preview_explains_blocked_import(self) -> None:
        importer = make_importer(db=make_db(sources=[]))
        draft = await importer.build_draft(b"image-bytes")

        preview = format_ocr_preview(draft, html=False)

        self.assertIn("Источник не определён", preview)
        self.assertIn("Импорт недоступен", preview)


class BuildImporterTests(unittest.TestCase):
    def test_build_ocr_importer_reads_settings(self) -> None:
        settings = MagicMock(
            ocr_enabled=True,
            ocr_tesseract_cmd="/usr/bin/tesseract",
            ocr_languages="rus",
            ocr_psm=4,
            ocr_timeout_seconds=15.0,
            ocr_min_confidence=0.7,
            ocr_fuzzy_threshold=0.9,
        )
        importer = build_ocr_importer(settings, make_db(), MagicMock(), None)

        self.assertTrue(importer.enabled)
        self.assertEqual(importer.engine.command, "/usr/bin/tesseract")
        self.assertEqual(importer.engine.languages, "rus")
        self.assertEqual(importer.engine.psm, 4)
        self.assertEqual(importer.parser.fuzzy_threshold, 0.9)
        self.assertEqual(importer.parser.min_confidence, 0.7)


class ManualSnapshotPipelineTests(unittest.IsolatedAsyncioTestCase):
    """Ручной снимок должен идти тем же путём, что и снимок с сайта."""

    async def test_apply_manual_snapshot_uses_shared_apply(self) -> None:
        from src.parser import compute_snapshot_hash
        from src.scheduler import ScheduleJobs

        jobs = ScheduleJobs.__new__(ScheduleJobs)
        jobs.apply_snapshot = AsyncMock(return_value=None)
        snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime(2026, 9, 1, 10, 0, 0),
            days=[
                DaySchedule(
                    date_label="01.09.2026",
                    date_iso="2026-09-01",
                    lessons=[Lesson(number=1, subject="Математика", teacher="Иванов И.И.", classroom="301")],
                )
            ],
        )

        await jobs.apply_manual_snapshot(ACTIVE_SOURCE, snapshot, notify=False)

        jobs.apply_snapshot.assert_awaited_once()
        args = jobs.apply_snapshot.await_args
        self.assertEqual(args.args[2], compute_snapshot_hash(snapshot))
        self.assertFalse(args.kwargs["notify"])


if __name__ == "__main__":
    unittest.main()

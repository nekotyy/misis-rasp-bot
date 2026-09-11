"""Сводное расписание: один день сразу на много групп (в отличие от обычного OCR — одна группа, много дней)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.ocr_import import (
    OcrScheduleImporter,
    build_ocr_importer,
    format_ocr_summary_preview,
)
from src.ocr_schedule import SUMMARY_RECOGNITION_PROMPT, OcrScheduleParser

SAMPLE_SUMMARY = json.dumps(
    {
        "date_iso": "2026-09-07",
        "groups": [
            {
                "group_name": "МТО-26",
                "lessons": [
                    {"number": 1, "subject": "Обществознание", "teacher": "Полупанова И.И.", "classroom": "208"},
                    {"number": 2, "subject": "География", "teacher": "Киреева Л.В.", "classroom": "407"},
                ],
            },
            {
                "group_name": "ИСП-25-3",
                "lessons": [
                    {"number": 2, "subject": "Основы алгоритмизации и прогр.", "teacher": "Коренькова Т.Н.", "classroom": "511/2М"},
                    {"number": 3, "subject": "Физическая культура", "teacher": "Кузьминова И.Н.", "classroom": "С-3"},
                    {"number": 4, "subject": "Компьютерные сети", "teacher": "Семенов А.В.", "classroom": "302"},
                ],
            },
        ],
    },
    ensure_ascii=False,
)

ACTIVE_SOURCE_ISP = {
    "source_type": "group",
    "source_key": "group:602",
    "source_title": "ИСП-25-3",
    "source_url": "http://example.com/rasp/602",
    "schedule_id": 602,
    "group_name": "ИСП-25-3",
}


class ParseSummaryTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = OcrScheduleParser()

    def test_parses_date_and_groups(self) -> None:
        result = self.parser.parse_summary_text(SAMPLE_SUMMARY)

        self.assertEqual(result.date_iso, "2026-09-07")
        self.assertEqual(len(result.groups), 2)
        self.assertEqual(result.lessons_count, 5)
        self.assertTrue(result.is_valid)

    def test_groups_sorted_by_name(self) -> None:
        result = self.parser.parse_summary_text(SAMPLE_SUMMARY)
        names = [group.group_name for group in result.groups]
        self.assertEqual(names, sorted(names))

    def test_invalid_json_produces_error(self) -> None:
        result = self.parser.parse_summary_text("не json")
        self.assertFalse(result.is_valid)
        self.assertTrue(result.errors)

    def test_empty_groups_list_is_error(self) -> None:
        result = self.parser.parse_summary_text(json.dumps({"date_iso": "2026-09-07", "groups": []}))
        self.assertFalse(result.is_valid)
        self.assertIn("не найдено ни одной группы", result.errors[0].message)

    def test_bad_date_reports_error(self) -> None:
        payload = json.dumps(
            {"date_iso": "не дата", "groups": [{"group_name": "МТО-26", "lessons": []}]}
        )
        result = self.parser.parse_summary_text(payload, now=datetime(2026, 9, 10))
        self.assertTrue(any("дату" in issue.message for issue in result.errors))

    def test_group_with_no_lessons_is_kept(self) -> None:
        payload = json.dumps({"date_iso": "2026-09-07", "groups": [{"group_name": "ТТО-24", "lessons": []}]})
        result = self.parser.parse_summary_text(payload)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].lessons, [])

    def test_duplicate_group_name_keeps_first_and_warns(self) -> None:
        payload = json.dumps(
            {
                "date_iso": "2026-09-07",
                "groups": [
                    {"group_name": "МТО-26", "lessons": [{"number": 1, "subject": "А", "teacher": "", "classroom": ""}]},
                    {"group_name": "мто-26", "lessons": [{"number": 1, "subject": "Б", "teacher": "", "classroom": ""}]},
                ],
            }
        )
        result = self.parser.parse_summary_text(payload)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].lessons[0].subject, "А")
        self.assertTrue(any("несколько раз" in issue.message for issue in result.warnings))

    def test_duplicate_lesson_number_within_group_keeps_first(self) -> None:
        payload = json.dumps(
            {
                "date_iso": "2026-09-07",
                "groups": [
                    {
                        "group_name": "МТО-26",
                        "lessons": [
                            {"number": 1, "subject": "Первая", "teacher": "", "classroom": ""},
                            {"number": 1, "subject": "Вторая", "teacher": "", "classroom": ""},
                        ],
                    }
                ],
            }
        )
        result = self.parser.parse_summary_text(payload)
        self.assertEqual(len(result.groups[0].lessons), 1)
        self.assertEqual(result.groups[0].lessons[0].subject, "Первая")

    def test_lesson_without_subject_is_skipped(self) -> None:
        payload = json.dumps(
            {
                "date_iso": "2026-09-07",
                "groups": [
                    {"group_name": "МТО-26", "lessons": [{"number": 1, "subject": "", "teacher": "Иванов", "classroom": "101"}]}
                ],
            }
        )
        result = self.parser.parse_summary_text(payload)
        self.assertEqual(result.groups[0].lessons, [])
        self.assertTrue(result.skipped_lines)

    def test_group_without_name_is_skipped(self) -> None:
        payload = json.dumps({"date_iso": "2026-09-07", "groups": [{"group_name": "", "lessons": []}]})
        result = self.parser.parse_summary_text(payload)
        self.assertEqual(result.groups, [])


class EngineUsesSummaryPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_recognize_summary_image_sends_summary_prompt(self) -> None:
        engine = MagicMock()
        engine.recognize = AsyncMock(return_value=SAMPLE_SUMMARY)
        parser = OcrScheduleParser(engine)

        await parser.recognize_summary_image([b"image"])

        engine.recognize.assert_awaited_once_with([b"image"], prompt=SUMMARY_RECOGNITION_PROMPT)


class FakeEngine:
    name = "fake"

    def __init__(self, text: str = SAMPLE_SUMMARY) -> None:
        self.text = text

    def availability(self) -> tuple[bool, str]:
        return True, "fake-gemini 1.0"

    async def warm_up(self) -> None:
        return None

    async def recognize(self, images: list[bytes], *, prompt: str | None = None) -> str:
        return self.text


def make_db(sources: list[dict] | None = None, latest_snapshot: dict | None = None) -> MagicMock:
    db = MagicMock()
    db.get_active_sources = AsyncMock(return_value=sources if sources is not None else [ACTIVE_SOURCE_ISP])
    db.get_latest_snapshot = AsyncMock(return_value=latest_snapshot)
    db.add_pending_groups = AsyncMock()
    return db


def make_importer(*, db: MagicMock | None = None, jobs: MagicMock | None = None, engine: FakeEngine | None = None) -> OcrScheduleImporter:
    return OcrScheduleImporter(
        db if db is not None else make_db(),
        jobs if jobs is not None else MagicMock(apply_manual_snapshot=AsyncMock(return_value=None)),
        None,
        engine=engine or FakeEngine(),
    )


class BuildSummaryDraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_known_and_new_offline_group(self) -> None:
        importer = make_importer()
        draft = await importer.build_summary_draft([b"image"])

        self.assertEqual(len(draft.result.groups), 2)
        resolved_names = {r.group_lessons.group_name for r in draft.resolved}
        self.assertEqual(resolved_names, {"ИСП-25-3", "МТО-26"})
        self.assertEqual(draft.unresolved, [])
        offline = next(r for r in draft.resolved if r.group_lessons.group_name == "МТО-26")
        self.assertEqual(offline.source["source_key"], "group-pending:мто-26")
        self.assertTrue(draft.can_apply)

    async def test_resolved_group_merges_with_existing_snapshot(self) -> None:
        stored = {
            "content": {
                "group_name": "ИСП-25-3",
                "days": [
                    {
                        "date_iso": "2026-08-31",
                        "date_label": "31.08.2026",
                        "lessons": [{"number": 1, "subject": "История", "teacher": "", "classroom": ""}],
                    }
                ],
            }
        }
        importer = make_importer(db=make_db(latest_snapshot=stored))
        draft = await importer.build_summary_draft([b"image"])

        resolved = next(r for r in draft.resolved if r.group_lessons.group_name == "ИСП-25-3")
        dates = [day.date_iso for day in resolved.merge.snapshot.days]
        self.assertIn("2026-08-31", dates)
        self.assertIn("2026-09-07", dates)

    async def test_no_matching_sources_creates_offline_sources(self) -> None:
        importer = make_importer(db=make_db(sources=[]))
        draft = await importer.build_summary_draft([b"image"])

        self.assertEqual(len(draft.resolved), 2)
        self.assertTrue(draft.can_apply)
        self.assertTrue(all(r.source["schedule_id"] is None for r in draft.resolved))


class ApplySummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_applies_resolved_and_offline_groups(self) -> None:
        jobs = MagicMock()
        jobs.apply_manual_snapshot = AsyncMock(return_value=None)
        importer = make_importer(jobs=jobs)
        draft = await importer.build_summary_draft([b"image"])

        applied, report = await importer.apply_summary(draft)

        self.assertTrue(applied)
        self.assertEqual(jobs.apply_manual_snapshot.await_count, 2)
        self.assertIn("2 из 2", report)

    async def test_refuses_when_nothing_resolved(self) -> None:
        importer = make_importer(db=make_db(sources=[]))
        draft = await importer.build_summary_draft([b"image"])
        for resolution in draft.resolutions:
            resolution.source = None
            resolution.merge = None

        applied, report = await importer.apply_summary(draft)

        self.assertFalse(applied)

    async def test_reports_partial_failure(self) -> None:
        jobs = MagicMock()
        jobs.apply_manual_snapshot = AsyncMock(side_effect=RuntimeError("БД недоступна"))
        importer = make_importer(jobs=jobs)
        draft = await importer.build_summary_draft([b"image"])

        applied, report = await importer.apply_summary(draft)

        self.assertFalse(applied)
        self.assertIn("БД недоступна", report)


class FormatSummaryPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_lists_resolved_and_offline_groups(self) -> None:
        importer = make_importer()
        draft = await importer.build_summary_draft([b"image"])

        preview = format_ocr_summary_preview(draft, html=False)

        self.assertIn("ИСП-25-3", preview)
        self.assertIn("МТО-26", preview)
        self.assertIn("Будут обновлены", preview)
        self.assertNotIn("Источник не найден", preview)

    async def test_html_preview_has_bold_tags(self) -> None:
        importer = make_importer()
        draft = await importer.build_summary_draft([b"image"])

        preview = format_ocr_summary_preview(draft, html=True)
        self.assertIn("<b>", preview)


class BuildOcrImporterHasSummaryModeTests(unittest.TestCase):
    def test_build_ocr_importer_still_works(self) -> None:
        settings = MagicMock(gemini_secure_1psid="a", gemini_secure_1psidts="b")
        importer = build_ocr_importer(settings, make_db(), MagicMock(), None)
        self.assertTrue(hasattr(importer, "build_summary_draft"))
        self.assertTrue(hasattr(importer, "apply_summary"))


if __name__ == "__main__":
    unittest.main()

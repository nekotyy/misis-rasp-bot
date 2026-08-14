import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from web_configurator.lesson_editor import (
    apply_imported_lessons_config,
    format_import_preview,
    load_lesson_config,
    parse_imported_json_payload,
    save_lesson_config,
    validate_lesson_config,
)


class LessonEditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "lesson_counters.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_lesson_editor_crud_and_validation(self):
        initial_data = {
            "groups": [
                {
                    "group_name": "ИСП-25-1",
                    "schedule_id": 600,
                    "subjects": [
                        {"subject": "Математика", "teacher": "Иванов И.И.", "passed": 10, "total": 50},
                    ],
                }
            ]
        }
        save_lesson_config(self.config_path, initial_data)
        loaded = load_lesson_config(self.config_path)
        self.assertEqual(len(loaded["groups"]), 1)

        group_mock = MagicMock()
        group_mock.group_name = "ИСП-25-1"
        mock_catalog = MagicMock()
        mock_catalog.find_group = AsyncMock(return_value=group_mock)
        mock_catalog.get_by_schedule_id = AsyncMock(return_value=group_mock)

        lesson_mock = MagicMock()
        lesson_mock.subject = "Математика"
        lesson_mock.teacher = "Иванов И.И."
        day_mock = MagicMock(lessons=[lesson_mock])

        mock_parser = MagicMock()
        mock_parser.fetch_html = AsyncMock(return_value="<html></html>")
        mock_parser.parse_html = MagicMock(return_value=MagicMock(days=[day_mock]))
        mock_parser.parse = AsyncMock(return_value=(MagicMock(days=[day_mock]), "hash"))

        config, errs = await validate_lesson_config(loaded, group_catalog=mock_catalog, parser=mock_parser)
        self.assertIsInstance(config, dict)
        self.assertIsInstance(errs, list)

        # Test imported json parsing and formatting
        raw_json = json.dumps(initial_data)
        parsed, parse_err = parse_imported_json_payload(raw_json)
        self.assertIsNotNone(parsed, f"JSON parse error: {parse_err}")
        self.assertIn("groups", parsed)

        preview_text, total_groups, total_subjects = await format_import_preview(parsed, group_catalog=mock_catalog)
        self.assertIn("ИСП-25-1", preview_text)
        self.assertEqual(total_groups, 1)

        applied_config, num_g, num_s = await apply_imported_lessons_config(parsed, loaded, group_catalog=mock_catalog)
        self.assertEqual(len(applied_config["groups"]), 1)

    def test_atomic_write_json_helper(self):
        from src.lesson_counters import _atomic_write_json
        test_path = Path(self.temp_dir.name) / "subdir" / "atomic.json"
        data = {"test": 123, "text": "тест"}
        self.assertTrue(_atomic_write_json(test_path, data))
        self.assertTrue(test_path.exists())
        self.assertEqual(json.loads(test_path.read_text(encoding="utf-8")), data)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import AsyncMock, MagicMock

from web_configurator.lesson_editor import (
    apply_imported_lessons_config,
    format_import_preview,
    parse_imported_json_payload,
)


class LessonImportTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_imported_json_payload_valid(self):
        json_data = """{
  "groups": [
    {
      "schedule_id": 600,
      "group_name": "ИСП-25-1",
      "subjects": [
        {
          "subject": "Литература",
          "teacher": "Волошина Н. В.",
          "passed": 10,
          "total": 62
        }
      ]
    }
  ]
}"""
        parsed, error = parse_imported_json_payload(json_data)
        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["groups"]), 1)
        self.assertEqual(parsed["groups"][0]["schedule_id"], 600)
        self.assertEqual(parsed["groups"][0]["group_name"], "ИСП-25-1")
        self.assertEqual(len(parsed["groups"][0]["subjects"]), 1)
        self.assertEqual(parsed["groups"][0]["subjects"][0]["subject"], "Литература")

    def test_parse_imported_json_payload_invalid(self):
        parsed, error = parse_imported_json_payload("not a json")
        self.assertIsNone(parsed)
        self.assertIn("Ошибка парсинга", error)

    async def test_format_import_preview(self):
        parsed = {
            "groups": [
                {
                    "schedule_id": 600,
                    "group_name": "ИСП-25-1",
                    "subjects": [
                        {"subject": "Математика", "teacher": "Иванов И. И.", "passed": 5, "total": 30}
                    ]
                }
            ]
        }
        catalog = MagicMock()
        catalog.get_by_schedule_id = AsyncMock(return_value=None)

        preview_html, total_g, total_s = await format_import_preview(parsed, catalog, html=True)
        self.assertEqual(total_g, 1)
        self.assertEqual(total_s, 1)
        self.assertIn("ИСП-25-1", preview_html)
        self.assertIn("Математика", preview_html)

    async def test_apply_imported_lessons_config(self):
        parsed = {
            "groups": [
                {
                    "schedule_id": 600,
                    "group_name": "ИСП-25-1",
                    "subjects": [
                        {"subject": "Математика", "teacher": "Иванов И. И.", "passed": 10, "total": 40}
                    ]
                }
            ]
        }
        catalog = MagicMock()
        catalog_g = MagicMock()
        catalog_g.schedule_id = 600
        catalog_g.group_name = "ИСП-25-1"
        catalog.get_by_schedule_id = AsyncMock(return_value=catalog_g)

        current_payload = {"groups": []}
        updated, total_g, total_s = await apply_imported_lessons_config(parsed, current_payload, catalog)

        self.assertEqual(total_g, 1)
        self.assertEqual(total_s, 1)
        self.assertEqual(len(updated["groups"]), 1)
        self.assertEqual(updated["groups"][0]["schedule_id"], 600)
        self.assertEqual(updated["groups"][0]["subjects"][0]["subject"], "Математика")
        self.assertEqual(updated["groups"][0]["subjects"][0]["passed"], 10)


if __name__ == "__main__":
    unittest.main()

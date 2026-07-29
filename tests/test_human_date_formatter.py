import unittest

from src.schedule_service import format_human_date


class HumanDateFormatterTests(unittest.TestCase):
    def test_format_human_date(self):
        self.assertEqual(format_human_date("2026-09-01"), "1 сентября 2026 года")
        self.assertEqual(format_human_date("01.09.2026"), "1 сентября 2026 года")
        self.assertEqual(format_human_date("15.10"), "15 октября 2026 года")
        self.assertEqual(format_human_date(""), "")
        self.assertEqual(format_human_date(None), "")


if __name__ == "__main__":
    unittest.main()

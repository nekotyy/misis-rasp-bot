from __future__ import annotations

import unittest

from src.config import _parse_int_list, _env_bool


class TestParseIntList(unittest.TestCase):
    """Тесты для _parse_int_list — парсинг списков ID из env."""

    def test_normal_input(self) -> None:
        self.assertEqual(_parse_int_list("1,2,3"), [1, 2, 3])

    def test_single_value(self) -> None:
        self.assertEqual(_parse_int_list("42"), [42])

    def test_with_spaces(self) -> None:
        self.assertEqual(_parse_int_list(" 1 , 2 , 3 "), [1, 2, 3])

    def test_empty_string(self) -> None:
        self.assertEqual(_parse_int_list(""), [])

    def test_only_commas(self) -> None:
        self.assertEqual(_parse_int_list(",,,"), [])

    def test_mixed_valid_and_invalid(self) -> None:
        self.assertEqual(_parse_int_list("1,abc,3,def,5"), [1, 3, 5])

    def test_all_invalid(self) -> None:
        self.assertEqual(_parse_int_list("abc,def,ghi"), [])

    def test_negative_numbers(self) -> None:
        self.assertEqual(_parse_int_list("-1,2,-3"), [-1, 2, -3])

    def test_trailing_comma(self) -> None:
        self.assertEqual(_parse_int_list("1,2,"), [1, 2])

    def test_leading_comma(self) -> None:
        self.assertEqual(_parse_int_list(",1,2"), [1, 2])

    def test_float_values_skipped(self) -> None:
        self.assertEqual(_parse_int_list("1,2.5,3"), [1, 3])


class TestEnvBool(unittest.TestCase):
    """Тесты для _env_bool — парсинг булевых значений."""

    def test_true_values(self) -> None:
        import os
        for val in ("1", "true", "yes", "on", "да", "вкл", "TRUE", "Yes", "ON"):
            os.environ["_TEST_BOOL"] = val
            self.assertTrue(_env_bool("_TEST_BOOL", default=False), f"Failed for {val!r}")

    def test_false_values(self) -> None:
        import os
        for val in ("0", "false", "no", "off", "нет", "выкл", "anything_else"):
            os.environ["_TEST_BOOL"] = val
            self.assertFalse(_env_bool("_TEST_BOOL", default=True), f"Failed for {val!r}")

    def test_empty_returns_default_true(self) -> None:
        import os
        os.environ["_TEST_BOOL"] = ""
        self.assertTrue(_env_bool("_TEST_BOOL", default=True))

    def test_empty_returns_default_false(self) -> None:
        import os
        os.environ["_TEST_BOOL"] = ""
        self.assertFalse(_env_bool("_TEST_BOOL", default=False))

    def test_missing_env_returns_default(self) -> None:
        import os
        os.environ.pop("_TEST_BOOL_MISSING", None)
        self.assertTrue(_env_bool("_TEST_BOOL_MISSING", default=True))
        self.assertFalse(_env_bool("_TEST_BOOL_MISSING", default=False))

    def tearDown(self) -> None:
        import os
        os.environ.pop("_TEST_BOOL", None)


if __name__ == "__main__":
    unittest.main()

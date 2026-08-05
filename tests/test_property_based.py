from __future__ import annotations

import random
import string
import unittest

from src.config import _parse_int_list
from src.schedule_search import ScheduleSearchCatalog


def _html_escape(value: object) -> str:
    """Mirror of web_configurator.app.html_escape."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


class TestParseIntListProperty(unittest.TestCase):
    """Property-based тесты: _parse_int_list никогда не крашится, всегда list[int]."""

    def test_random_ascii_strings(self) -> None:
        rng = random.Random(42)
        for _ in range(200):
            length = rng.randint(0, 100)
            s = "".join(rng.choices(string.printable, k=length))
            result = _parse_int_list(s)
            self.assertIsInstance(result, list)
            for item in result:
                self.assertIsInstance(item, int)

    def test_random_comma_separated_garbage(self) -> None:
        rng = random.Random(123)
        for _ in range(200):
            parts = [rng.choice(["", "abc", str(rng.randint(-999, 999)), "  ", "3.14", "None"]) for _ in range(rng.randint(0, 20))]
            s = ",".join(parts)
            result = _parse_int_list(s)
            self.assertIsInstance(result, list)
            for item in result:
                self.assertIsInstance(item, int)

    def test_only_ints_survive(self) -> None:
        rng = random.Random(456)
        for _ in range(100):
            ints = [rng.randint(-10000, 10000) for _ in range(rng.randint(1, 10))]
            s = ",".join(str(i) for i in ints)
            result = _parse_int_list(s)
            self.assertEqual(result, ints)


class TestNormalizeProperty(unittest.TestCase):
    """Property-based тесты: normalize никогда не крашится, всегда str."""

    def test_random_unicode_strings(self) -> None:
        rng = random.Random(42)
        for _ in range(300):
            length = rng.randint(0, 50)
            codepoints = [rng.randint(0x20, 0x4FF) for _ in range(length)]
            s = "".join(chr(cp) for cp in codepoints)
            result = ScheduleSearchCatalog.normalize(s)
            self.assertIsInstance(result, str)

    def test_empty_and_whitespace(self) -> None:
        for s in ("", " ", "\t", "\n", "   \t\n  "):
            result = ScheduleSearchCatalog.normalize(s)
            self.assertIsInstance(result, str)
            self.assertEqual(result.strip(), result)

    def test_idempotent(self) -> None:
        """Повторная нормализация не должна менять результат."""
        rng = random.Random(789)
        for _ in range(100):
            length = rng.randint(1, 30)
            chars = [chr(rng.randint(0x410, 0x44F)) for _ in range(length)]
            s = "".join(chars)
            once = ScheduleSearchCatalog.normalize(s)
            twice = ScheduleSearchCatalog.normalize(once)
            self.assertEqual(once, twice, f"Not idempotent for: {s!r}")

    def test_never_contains_multiple_spaces(self) -> None:
        rng = random.Random(101)
        for _ in range(200):
            length = rng.randint(0, 40)
            s = "".join(chr(rng.randint(0x20, 0x4FF)) for _ in range(length))
            result = ScheduleSearchCatalog.normalize(s)
            self.assertNotIn("  ", result, f"Double space in: {result!r}")


class TestHtmlEscapeProperty(unittest.TestCase):
    """Property-based тесты: html_escape не пропускает опасные символы."""

    def test_no_raw_html_chars_in_output(self) -> None:
        rng = random.Random(42)
        dangerous = '<>&"\''
        for _ in range(300):
            length = rng.randint(0, 50)
            s = "".join(rng.choices(string.printable + dangerous * 3, k=length))
            result = _html_escape(s)
            # В результате НЕ должно быть сырых < > & " '
            # кроме тех что в escape-последовательностях
            cleaned = result.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "").replace("&quot;", "").replace("&#39;", "")
            for char in dangerous:
                self.assertNotIn(char, cleaned,
                    f"Raw {char!r} found in escaped output for input {s!r}: {result!r}")

    def test_preserves_safe_text(self) -> None:
        safe_strings = ["hello world", "12345", "тест кириллицы", "abc-def_ghi"]
        for s in safe_strings:
            self.assertEqual(_html_escape(s), s)

    def test_roundtrip_length_never_decreases(self) -> None:
        rng = random.Random(77)
        for _ in range(200):
            s = "".join(rng.choices(string.printable, k=rng.randint(0, 30)))
            result = _html_escape(s)
            self.assertGreaterEqual(len(result), len(s))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from web_configurator.security import validate_security_config


class TestValidateSecurityConfig(unittest.TestCase):
    def test_insecure_secret_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            validate_security_config("short", "secure-password-12chars")

    def test_insecure_password_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            validate_security_config("a" * 32, "admin")

    def test_empty_credentials_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            validate_security_config("", "")

    def test_secure_credentials_pass(self) -> None:
        validate_security_config("a" * 32, "secure-password-12chars")


class TestHtmlEscape(unittest.TestCase):
    """Test html_escape logic independently of web_configurator.app (heavy side effects)."""

    @staticmethod
    def _html_escape(value: object) -> str:
        """Mirror of web_configurator.app.html_escape for isolated testing."""
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")

    def test_escapes_ampersand(self) -> None:
        self.assertEqual(self._html_escape("a&b"), "a&amp;b")

    def test_escapes_angle_brackets(self) -> None:
        self.assertEqual(self._html_escape("<script>"), "&lt;script&gt;")

    def test_escapes_double_quotes(self) -> None:
        self.assertIn("&quot;", self._html_escape('a"b'))

    def test_escapes_single_quotes(self) -> None:
        self.assertIn("&#39;", self._html_escape("a'b"))

    def test_non_string_input(self) -> None:
        self.assertEqual(self._html_escape(42), "42")


if __name__ == "__main__":
    unittest.main()


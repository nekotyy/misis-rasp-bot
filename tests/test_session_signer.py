from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from web_configurator.security import SessionSigner


class TestSessionSigner(unittest.TestCase):
    """Тесты для SessionSigner — ядра авторизации веб-админки."""

    def setUp(self) -> None:
        self.signer = SessionSigner("test-secret-key-at-least-32-chars-long!")

    def test_sign_returns_nonempty_string(self) -> None:
        token = self.signer.sign("admin")
        self.assertIsInstance(token, str)
        self.assertGreater(len(token), 0)

    def test_sign_contains_dot_separator(self) -> None:
        token = self.signer.sign("admin")
        self.assertIn(".", token)

    def test_unsign_valid_token(self) -> None:
        token = self.signer.sign("admin")
        login = self.signer.unsign(token)
        self.assertEqual(login, "admin")

    def test_unsign_different_users(self) -> None:
        for login in ("admin", "editor", "viewer", "тестовый_юзер"):
            token = self.signer.sign(login)
            self.assertEqual(self.signer.unsign(token), login)

    def test_unsign_tampered_signature_returns_none(self) -> None:
        token = self.signer.sign("admin")
        data, sig = token.rsplit(".", 1)
        tampered = f"{data}.{'0' * len(sig)}"
        self.assertIsNone(self.signer.unsign(tampered))

    def test_unsign_tampered_data_returns_none(self) -> None:
        token = self.signer.sign("admin")
        tampered = "AAAA" + token[4:]
        self.assertIsNone(self.signer.unsign(tampered))

    def test_unsign_wrong_secret_returns_none(self) -> None:
        token = self.signer.sign("admin")
        other_signer = SessionSigner("other-secret-key-at-least-32-chars-long!")
        self.assertIsNone(other_signer.unsign(token))

    def test_unsign_none_returns_none(self) -> None:
        self.assertIsNone(self.signer.unsign(None))

    def test_unsign_empty_string_returns_none(self) -> None:
        self.assertIsNone(self.signer.unsign(""))

    def test_unsign_no_dot_returns_none(self) -> None:
        self.assertIsNone(self.signer.unsign("nodothere"))

    def test_unsign_garbage_returns_none(self) -> None:
        self.assertIsNone(self.signer.unsign("abc.def"))

    def test_unsign_expired_token_returns_none(self) -> None:
        token = self.signer.sign("admin")
        # Перематываем время на 2 часа вперёд — токен с max_age=1 секунда должен истечь
        with patch("web_configurator.security.time") as mock_time:
            mock_time.time.return_value = time.time() + 7200
            self.assertIsNone(self.signer.unsign(token, max_age_seconds=1))

    def test_unsign_respects_max_age(self) -> None:
        token = self.signer.sign("admin")
        # С большим max_age должен работать
        self.assertEqual(self.signer.unsign(token, max_age_seconds=3600), "admin")
        # С перемоткой времени — нет
        with patch("web_configurator.security.time") as mock_time:
            mock_time.time.return_value = time.time() + 7200
            self.assertIsNone(self.signer.unsign(token, max_age_seconds=1))

    def test_sign_produces_unique_tokens(self) -> None:
        token1 = self.signer.sign("admin")
        token2 = self.signer.sign("admin")
        self.assertNotEqual(token1, token2, "Nonce should make tokens unique")

    def test_unsign_fallback_secret(self) -> None:
        """SessionSigner с пустым секретом использует фоллбэк."""
        signer = SessionSigner("")
        token = signer.sign("test")
        self.assertEqual(signer.unsign(token), "test")


if __name__ == "__main__":
    unittest.main()

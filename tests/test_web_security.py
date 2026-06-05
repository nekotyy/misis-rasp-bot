from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_configurator.security import LoginRateLimiter, verify_password


class LoginRateLimiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "guard.db"
        self.limiter = LoginRateLimiter(self.db_path)
        self.keys = ["ip:test", "device:test"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_blocks_after_three_failures(self) -> None:
        self.assertFalse(self.limiter.is_blocked(self.keys))

        self.limiter.register_failure(self.keys)
        self.limiter.register_failure(self.keys)
        self.assertFalse(self.limiter.is_blocked(self.keys))

        self.limiter.register_failure(self.keys)
        self.assertTrue(self.limiter.is_blocked(self.keys))

    def test_block_persists_across_instances(self) -> None:
        for _ in range(3):
            self.limiter.register_failure(self.keys)

        reloaded = LoginRateLimiter(self.db_path)
        self.assertTrue(reloaded.is_blocked(self.keys))

    def test_reset_clears_guard_state(self) -> None:
        for _ in range(3):
            self.limiter.register_failure(self.keys)

        self.limiter.reset(self.keys)

        self.assertFalse(self.limiter.is_blocked(self.keys))

    def test_record_attempt_writes_audit_row(self) -> None:
        self.limiter.record_attempt(
            requested_login="admin",
            ip="127.0.0.1",
            user_agent="test-agent",
            device_id_hash="device-hash",
            fingerprint_hash="fingerprint-hash",
            outcome="failed",
            reason="invalid_credentials",
        )

        connection = self.limiter._connect()
        try:
            row = connection.execute(
                """
                SELECT requested_login, ip, outcome, reason
                FROM web_login_attempts
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["requested_login"], "admin")
        self.assertEqual(row["ip"], "127.0.0.1")
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["reason"], "invalid_credentials")


class PasswordHashTests(unittest.TestCase):
    def test_verify_password_rejects_malformed_hash(self) -> None:
        self.assertFalse(verify_password("secret", "broken"))


if __name__ == "__main__":
    unittest.main()

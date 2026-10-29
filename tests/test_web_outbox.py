from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.db import Database
from src.outbox_store import OutboxStore
from web_configurator import app as web


class WebOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_broadcast_persists_all_recipients_without_requiring_live_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "web.db")
            await db.initialize()
            for platform, user_id in (("telegram", 1), ("vk", 2)):
                await db.upsert_user(platform, user_id, None, "Студент", subscription_type="group", subscription_key="group:1")
            with patch.object(web, "get_db", AsyncMock(return_value=db)):
                self.assertEqual(await web.enqueue_broadcast("Важное сообщение"), 2)
            self.assertEqual(await OutboxStore(db).counts(), {"pending": 2})

    def test_localhost_login_keeps_cookie_policy_and_protected_broadcast_requires_login(self):
        store = MagicMock()
        store.authenticate.return_value = SimpleNamespace(login="test-admin")
        store.get_user.return_value = None
        guard = MagicMock()
        guard.is_blocked.return_value = False
        with (patch.object(web, "auth_store", store), patch.object(web, "login_rate_limiter", guard),
              patch.object(web, "record_login_attempt"), TestClient(web.app, base_url="http://localhost") as client):
            self.assertEqual(client.get("/login").status_code, 200)
            blocked = client.post("/control/broadcast", data={"message": "x"}, headers={"Origin": "http://localhost"}, follow_redirects=False)
            self.assertEqual(blocked.status_code, 303)
            self.assertEqual(blocked.headers["location"], "/login")
            response = client.post("/login", data={"login": "test-admin", "password": "test-password"},
                                   headers={"Origin": "http://localhost"}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            session_cookie = next(item for item in response.headers.get_list("set-cookie") if item.startswith(web.SESSION_COOKIE_NAME + "="))
            self.assertIn("HttpOnly", session_cookie)
            self.assertIn("SameSite=strict", session_cookie)
            self.assertNotIn("; Secure", session_cookie)

    def test_failed_and_blocked_login_still_use_guard_and_audit(self):
        store = MagicMock()
        store.authenticate.return_value = None
        guard = MagicMock()
        guard.is_blocked.return_value = False
        with (patch.object(web, "auth_store", store), patch.object(web, "login_rate_limiter", guard),
              patch.object(web, "record_login_attempt") as audit, TestClient(web.app, base_url="http://localhost") as client):
            params = {"data": {"login": "test-admin", "password": "invalid"}, "headers": {"Origin": "http://localhost"}, "follow_redirects": False}
            self.assertEqual(client.post("/login", **params).status_code, 303)
            guard.register_failure.assert_called_once()
            self.assertEqual(audit.call_args.kwargs["outcome"], "failed")
            store.authenticate.reset_mock()
            guard.is_blocked.return_value = True
            self.assertEqual(client.post("/login", **params).status_code, 303)
            store.authenticate.assert_not_called()
            self.assertEqual(audit.call_args.kwargs["outcome"], "blocked")

    def test_cross_origin_broadcast_is_rejected_before_queueing(self):
        with (patch.object(web, "enqueue_broadcast", AsyncMock()) as enqueue,
              TestClient(web.app, base_url="http://localhost") as client):
            response = client.post("/control/broadcast", data={"message": "x"}, headers={"Origin": "https://foreign.example"})
            self.assertEqual(response.status_code, 403)
            enqueue.assert_not_awaited()

"""Тесты для REST API миниаппа Telegram и VK."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from miniapp_api.app import app
from src.db import Database
from src.models import DaySchedule, Lesson
from src.schedule_archive import ScheduleDayArchive


def make_telegram_init_data(
    user_id: int,
    username: str,
    bot_token: str,
    auth_date: int | None = None,
) -> str:
    if auth_date is None:
        auth_date = int(datetime.now(UTC).timestamp())
    user_json = json.dumps(
        {"id": user_id, "first_name": "Test", "last_name": "User", "username": username},
        separators=(",", ":"),
    )
    data = {
        "auth_date": str(auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": user_json,
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    hash_val = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    data["hash"] = hash_val
    return urllib.parse.urlencode(data)


def make_vk_launch_params(user_id: int, app_id: int, client_secret: str) -> str:
    vk_params = {
        "vk_app_id": str(app_id),
        "vk_are_notifications_enabled": "1",
        "vk_is_app_user": "1",
        "vk_platform": "mobile_web",
        "vk_user_id": str(user_id),
    }
    sorted_query = urllib.parse.urlencode(sorted(vk_params.items()))
    hash_bytes = hmac.new(client_secret.encode("utf-8"), sorted_query.encode("utf-8"), hashlib.sha256).digest()
    sign = base64.b64encode(hash_bytes).decode("utf-8").rstrip("=").replace("+", "-").replace("/", "_")
    vk_params["sign"] = sign
    return urllib.parse.urlencode(vk_params)


class MiniappApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_miniapp.db"
        self.bot_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
        self.vk_secret = "secret1234567890"

        self.env_patch = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.db_path),
                "TELEGRAM_BOT_TOKEN": self.bot_token,
                "VK_APP_SECRET": self.vk_secret,
                "APP_TIMEZONE": "Europe/Moscow",
            },
        )
        self.env_patch.start()

        db = Database(self.db_path)
        await db.initialize()

    async def asyncTearDown(self):
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def test_telegram_auth_success_and_tampered_rejection(self):
        client = TestClient(app)
        init_data = make_telegram_init_data(101, "tg_user", self.bot_token)

        resp = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": init_data})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("token", data)
        self.assertEqual(data["platform"], "telegram")
        self.assertEqual(data["user_id"], 101)

        # Поддельная подпись должна отклоняться
        tampered = init_data.replace("hash=", "hash=deadbeef")
        resp_tampered = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": tampered})
        self.assertEqual(resp_tampered.status_code, 401)

        # Просроченная подпись должна отклоняться
        old_ts = int((datetime.now(UTC) - timedelta(days=2)).timestamp())
        expired = make_telegram_init_data(101, "tg_user", self.bot_token, auth_date=old_ts)
        resp_expired = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": expired})
        self.assertEqual(resp_expired.status_code, 401)

    def test_vk_auth_success_and_tampered_rejection(self):
        client = TestClient(app)
        launch_params = make_vk_launch_params(202, 55555, self.vk_secret)

        resp = client.post("/api/miniapp/v1/auth/vk", json={"launch_params": launch_params})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("token", data)
        self.assertEqual(data["platform"], "vk")
        self.assertEqual(data["user_id"], 202)

        # Поддельная подпись должна отклоняться
        tampered = launch_params.replace("sign=", "sign=invalid_sign")
        resp_tampered = client.post("/api/miniapp/v1/auth/vk", json={"launch_params": tampered})
        self.assertEqual(resp_tampered.status_code, 401)

    def test_vk_auth_with_vk_miniapp_secret_env(self):
        miniapp_secret = "custom_miniapp_secret_777"
        with patch.dict(os.environ, {"VK_MINIAPP_SECRET": miniapp_secret, "VK_APP_SECRET": ""}):
            client = TestClient(app)
            params = make_vk_launch_params(203, 55555, miniapp_secret)
            resp = client.post("/api/miniapp/v1/auth/vk", json={"launch_params": params})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["user_id"], 203)


    def test_dev_mode_auth(self):
        with patch.dict(os.environ, {"MINIAPP_DEV_MODE": "1"}):
            client = TestClient(app)
            tg_resp = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": "dev:999:dev_tester"})
            self.assertEqual(tg_resp.status_code, 200)
            self.assertEqual(tg_resp.json()["user_id"], 999)

            vk_resp = client.post("/api/miniapp/v1/auth/vk", json={"launch_params": "dev:888"})
            self.assertEqual(vk_resp.status_code, 200)
            self.assertEqual(vk_resp.json()["user_id"], 888)

    def test_session_authorization_and_logout(self):
        client = TestClient(app)
        init_data = make_telegram_init_data(303, "logout_user", self.bot_token)
        auth_resp = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": init_data})
        token = auth_resp.json()["token"]

        headers = {"Authorization": f"Bearer {token}"}

        # Доступ к /me успешен
        me_resp = client.get("/api/miniapp/v1/me", headers=headers)
        self.assertEqual(me_resp.status_code, 200)
        profile = me_resp.json()
        self.assertEqual(profile["current_identity"]["user_id"], 303)
        self.assertEqual(profile["current_identity"]["platform"], "telegram")

        # Отзыв сессии (logout)
        logout_resp = client.post("/api/miniapp/v1/auth/logout", headers=headers)
        self.assertEqual(logout_resp.status_code, 200)

        # Повторный доступ возвращает 401
        me_after = client.get("/api/miniapp/v1/me", headers=headers)
        self.assertEqual(me_after.status_code, 401)

    def test_preferences_update(self):
        client = TestClient(app)
        init_data = make_telegram_init_data(404, "pref_user", self.bot_token)
        auth_resp = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": init_data})
        headers = {"Authorization": f"Bearer {auth_resp.json()['token']}"}

        patch_resp = client.patch(
            "/api/miniapp/v1/me/preferences",
            headers=headers,
            json={
                "theme": "dark",
                "accent": "emerald",
                "density": "compact",
                "reduced_motion": True,
                "notifications_enabled": False,
            },
        )
        self.assertEqual(patch_resp.status_code, 200)
        data = patch_resp.json()
        pref = data["preferences"]
        self.assertEqual(pref["theme"], "dark")
        self.assertEqual(pref["accent"], "emerald")
        self.assertEqual(pref["density"], "compact")
        self.assertTrue(pref["reduced_motion"])
        self.assertFalse(pref["notifications_enabled"])

    def test_subscriptions_crud(self):
        client = TestClient(app)
        init_data = make_telegram_init_data(505, "sub_user", self.bot_token)
        auth_resp = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": init_data})
        headers = {"Authorization": f"Bearer {auth_resp.json()['token']}"}

        # 1. Добавление подписки
        add_resp = client.post(
            "/api/miniapp/v1/me/subscriptions",
            headers=headers,
            json={
                "group_name": "БВТ-21-1",
                "schedule_id": 105,
                "is_primary": True,
                "university_subgroup": 1,
            },
        )
        self.assertEqual(add_resp.status_code, 200)
        sub_data = add_resp.json()
        sub_id = sub_data["id"]
        self.assertEqual(sub_data["group_name"], "БВТ-21-1")
        self.assertEqual(sub_data["schedule_id"], 105)
        self.assertTrue(sub_data["is_primary"])
        self.assertEqual(sub_data["university_subgroup"], 1)

        # 2. Обновление подписки
        patch_sub = client.patch(
            f"/api/miniapp/v1/me/subscriptions/{sub_id}",
            headers=headers,
            json={"university_subgroup": 2, "notifications_enabled": False},
        )
        self.assertEqual(patch_sub.status_code, 200)
        updated_sub = patch_sub.json()
        self.assertEqual(updated_sub["university_subgroup"], 2)
        self.assertFalse(updated_sub["notifications_enabled"])

        # 3. Удаление подписки
        del_resp = client.delete(f"/api/miniapp/v1/me/subscriptions/{sub_id}", headers=headers)
        self.assertEqual(del_resp.status_code, 200)

        # Проверка удаления
        me_resp = client.get("/api/miniapp/v1/me", headers=headers)
        self.assertEqual(len(me_resp.json()["subscriptions"]), 0)

    async def test_schedule_3_days_with_diff(self):
        client = TestClient(app)
        init_data = make_telegram_init_data(606, "sched_user", self.bot_token)
        auth_resp = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": init_data})
        headers = {"Authorization": f"Bearer {auth_resp.json()['token']}"}

        # Добавим подписку
        add_resp = client.post(
            "/api/miniapp/v1/me/subscriptions",
            headers=headers,
            json={"group_name": "БВТ-20-1", "schedule_id": 100, "is_primary": True},
        )
        sub_id = add_resp.json()["id"]

        today_dt = datetime.now(UTC).date()
        today_iso = today_dt.isoformat()

        # Сохраним в архив опору и текущий день с заменой аудитории
        archive = ScheduleDayArchive(self.db_path)
        ref_day = DaySchedule(
            date_label="Понедельник",
            date_iso=today_iso,
            lessons=[Lesson(number=1, subject="Физика", teacher="Иванов И.И.", classroom="101")],
        )
        # Опора дня (day_start)
        now_utc = datetime.now(UTC)
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO schedule_day_references
                   (source_key, date_iso, week_variant, reference_kind, content_json, completeness, origin, observed_at, recorded_at)
                   VALUES (?, ?, 0, 'day_start', ?, 'complete', 'baseline', ?, ?)""",
                (
                    "group:100",
                    today_iso,
                    json.dumps(ref_day.to_dict(), ensure_ascii=False),
                    now_utc.isoformat(),
                    now_utc.isoformat(),
                ),
            )
            await db.commit()

        # Текущий день в архиве с изменённой аудиторией
        cur_day = DaySchedule(
            date_label="Понедельник",
            date_iso=today_iso,
            lessons=[Lesson(number=1, subject="Физика", teacher="Иванов И.И.", classroom="202")],
        )
        await archive.save_day(
            "group:100",
            "group",
            cur_day,
            observed_at=now_utc,
            origin="current",
            completeness="complete",
        )

        sched_resp = client.get(f"/api/miniapp/v1/schedule?subscription_id={sub_id}", headers=headers)
        self.assertEqual(sched_resp.status_code, 200)
        sched_data = sched_resp.json()
        self.assertEqual(len(sched_data["days"]), 3)
        today_entry = next(d for d in sched_data["days"] if d["date"] == today_iso)
        self.assertEqual(today_entry["status"], "available")
        self.assertEqual(len(today_entry["lessons"]), 1)
        lesson_item = today_entry["lessons"][0]
        self.assertEqual(lesson_item["status"], "changed")
        self.assertIsNotNone(lesson_item["change"])
        self.assertEqual(lesson_item["change"]["kind"], "classroom")
        self.assertEqual(lesson_item["change"]["before"]["classroom"], "101")
        self.assertEqual(lesson_item["change"]["after"]["classroom"], "202")

    async def test_history_endpoint(self):
        client = TestClient(app)
        init_data = make_telegram_init_data(707, "hist_user", self.bot_token)
        auth_resp = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": init_data})
        headers = {"Authorization": f"Bearer {auth_resp.json()['token']}"}

        add_resp = client.post(
            "/api/miniapp/v1/me/subscriptions",
            headers=headers,
            json={"group_name": "БВТ-20-2", "schedule_id": 200, "is_primary": True},
        )
        sub_id = add_resp.json()["id"]

        hist_resp = client.get(f"/api/miniapp/v1/history?subscription_id={sub_id}", headers=headers)
        self.assertEqual(hist_resp.status_code, 200)
        hist_data = hist_resp.json()
        self.assertIn("week_start", hist_data)
        self.assertIn("bounds", hist_data)
        self.assertIn("has_previous_week", hist_data)
        self.assertIn("has_next_week", hist_data)
        self.assertIsInstance(hist_data["days"], list)

    def test_account_linking_and_unlinking(self):
        client = TestClient(app)

        # 1. Telegram пользователь
        tg_init = make_telegram_init_data(801, "tg_linker", self.bot_token)
        tg_auth = client.post("/api/miniapp/v1/auth/telegram", json={"init_data": tg_init}).json()
        tg_headers = {"Authorization": f"Bearer {tg_auth['token']}"}

        # Добавим TG подписку
        client.post(
            "/api/miniapp/v1/me/subscriptions",
            headers=tg_headers,
            json={"group_name": "TG-GROUP", "schedule_id": 801, "is_primary": True},
        )

        # 2. VK пользователь
        vk_params = make_vk_launch_params(802, 55555, self.vk_secret)
        vk_auth = client.post("/api/miniapp/v1/auth/vk", json={"launch_params": vk_params}).json()
        vk_headers = {"Authorization": f"Bearer {vk_auth['token']}"}

        # 3. TG инициирует связывание
        link_resp = client.post("/api/miniapp/v1/account-links", headers=tg_headers)
        self.assertEqual(link_resp.status_code, 200)
        link_token = link_resp.json()["token"]

        # 4. VK подтверждает связывание
        confirm_resp = client.post(
            "/api/miniapp/v1/account-links/confirm",
            headers=vk_headers,
            json={"token": link_token, "primary_choice": "initiator"},
        )
        self.assertEqual(confirm_resp.status_code, 200)

        # 5. Проверяем, что в профиле TG теперь 2 идентичности
        tg_profile = client.get("/api/miniapp/v1/me", headers=tg_headers).json()
        self.assertEqual(len(tg_profile["identities"]), 2)
        platforms = {i["platform"] for i in tg_profile["identities"]}
        self.assertEqual(platforms, {"telegram", "vk"})

        # 6. Отвязываем VK
        unlink_resp = client.delete("/api/miniapp/v1/me/identities/vk", headers=tg_headers)
        self.assertEqual(unlink_resp.status_code, 200)

        # После отвязки у TG осталась одна идентичность
        tg_profile_after = client.get("/api/miniapp/v1/me", headers=tg_headers).json()
        self.assertEqual(len(tg_profile_after["identities"]), 1)
        self.assertEqual(tg_profile_after["identities"][0]["platform"], "telegram")

    def test_unauthorized_endpoints(self):
        client = TestClient(app)
        self.assertEqual(client.get("/api/miniapp/v1/me").status_code, 401)
        self.assertEqual(client.get("/api/miniapp/v1/schedule").status_code, 401)
        self.assertEqual(client.get("/api/miniapp/v1/me", headers={"Authorization": "Bearer fake_token"}).status_code, 401)

    async def test_group_search(self):
        client = TestClient(app)
        # Сохраним снапшот с группой БВТ-21-01
        import aiosqlite

        now_iso = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """INSERT INTO schedule_snapshots
                   (snapshot_type, snapshot_hash, content_json, schedule_id, group_name, source_key, source_title, source_url, fetched_at, created_at)
                   VALUES ('current', 'hash123', '{}', 991, 'БВТ-21-01', 'group:991', 'БВТ-21-01', 'rasp:991', ?, ?)""",
                (now_iso, now_iso),
            )
            await conn.commit()

        resp = client.get("/api/miniapp/v1/groups?query=БВТ")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()
        self.assertTrue(any(g["group_name"] == "БВТ-21-01" for g in items))

    def test_subscription_access_control(self):
        client = TestClient(app)
        user1_init = make_telegram_init_data(911, "user_1", self.bot_token)
        user2_init = make_telegram_init_data(922, "user_2", self.bot_token)

        h1 = {"Authorization": f"Bearer {client.post('/api/miniapp/v1/auth/telegram', json={'init_data': user1_init}).json()['token']}"}
        h2 = {"Authorization": f"Bearer {client.post('/api/miniapp/v1/auth/telegram', json={'init_data': user2_init}).json()['token']}"}

        # User 1 добавляет подписку
        sub_id = client.post(
            "/api/miniapp/v1/me/subscriptions",
            headers=h1,
            json={"group_name": "U1-GRP", "schedule_id": 911, "is_primary": True},
        ).json()["id"]

        # User 2 пытается получить расписание по sub_id User 1 -> 404
        self.assertEqual(client.get(f"/api/miniapp/v1/schedule?subscription_id={sub_id}", headers=h2).status_code, 404)
        # User 2 пытается изменить подписку User 1 -> 404
        self.assertEqual(client.patch(f"/api/miniapp/v1/me/subscriptions/{sub_id}", headers=h2, json={"is_primary": False}).status_code, 404)
        # User 2 пытается удалить подписку User 1 -> 404
        self.assertEqual(client.delete(f"/api/miniapp/v1/me/subscriptions/{sub_id}", headers=h2).status_code, 404)

    def test_account_linking_invalid_token(self):
        client = TestClient(app)
        user_init = make_telegram_init_data(933, "user_3", self.bot_token)
        headers = {"Authorization": f"Bearer {client.post('/api/miniapp/v1/auth/telegram', json={'init_data': user_init}).json()['token']}"}

        resp = client.post("/api/miniapp/v1/account-links/confirm", headers=headers, json={"token": "invalid_or_expired_token"})
        self.assertEqual(resp.status_code, 400)

    def test_unlink_last_identity_fails(self):
        client = TestClient(app)
        user_init = make_telegram_init_data(944, "user_4", self.bot_token)
        headers = {"Authorization": f"Bearer {client.post('/api/miniapp/v1/auth/telegram', json={'init_data': user_init}).json()['token']}"}

        # Попытка отвязать единственную платформу
        resp = client.delete("/api/miniapp/v1/me/identities/telegram", headers=headers)
        self.assertEqual(resp.status_code, 400)

    def test_health_endpoints(self):
        client = TestClient(app)
        resp1 = client.get("/health")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.json(), {"status": "ok"})

        resp2 = client.get("/api/health")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json(), {"status": "ok"})

    def test_avatar_endpoint_and_cache_invalidation(self):
        client = TestClient(app)

        # При отсутствии фото возвращается SVG с инициалами и заголовками валидации
        svg_resp = client.get("/api/miniapp/v1/avatar/telegram/99999")
        self.assertEqual(svg_resp.status_code, 200)
        self.assertIn("image/svg+xml", svg_resp.headers["content-type"])
        self.assertIn("<svg", svg_resp.text)
        self.assertIn("Cache-Control", svg_resp.headers)

        # Проверка кэширования и 304 Not Modified при наличии ETag
        from datetime import UTC, datetime

        from miniapp_api.app import _AVATAR_CACHE, _build_profile_response, _get_avatar_url
        from src.account_service import Account, AccountIdentity

        test_uid = 777777
        cache_key = f"telegram:{test_uid}"
        now_ts = datetime.now(UTC).timestamp()
        _AVATAR_CACHE[cache_key] = (now_ts, "unique_avatar_1", b"fake_jpeg_data", "image/jpeg")

        # 1. Запрос с пустым / несовпадающим ETag возвращает 200 и контент
        resp = client.get(f"/api/miniapp/v1/avatar/telegram/{test_uid}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"fake_jpeg_data")
        self.assertEqual(resp.headers.get("etag"), '"unique_avatar_1"')
        self.assertIn("no-cache", resp.headers.get("cache-control", ""))

        # 2. Запрос с If-None-Match возвращает 304 Not Modified
        resp_304 = client.get(
            f"/api/miniapp/v1/avatar/telegram/{test_uid}",
            headers={"if-none-match": '"unique_avatar_1"'},
        )
        self.assertEqual(resp_304.status_code, 304)
        self.assertEqual(len(resp_304.content), 0)

        # 3. URL аватара в профиле содержит версию ?v=unique_avatar_1
        avatar_url = _get_avatar_url("telegram", test_uid)
        self.assertIn("?v=unique_avatar_1", avatar_url)

        ident = AccountIdentity(
            account_id="acc-test",
            platform="telegram",
            user_id=test_uid,
            username="test_user",
            full_name="User Test",
            created_at="2026-11-05T00:00:00Z",
            last_seen_at="2026-11-05T00:00:00Z",
        )
        acc = Account(
            id="acc-test",
            theme="dark",
            accent="blue",
            density="comfortable",
            reduced_motion=False,
            notifications_enabled=True,
            created_at="2026-11-05T00:00:00Z",
            updated_at="2026-11-05T00:00:00Z",
            identities=(ident,),
            subscriptions=(),
        )
        prof = _build_profile_response(acc, ident)
        self.assertIn("?v=unique_avatar_1", prof.current_identity.photo_url)

        # Очистка кэша
        _AVATAR_CACHE.pop(cache_key, None)


if __name__ == "__main__":
    unittest.main()

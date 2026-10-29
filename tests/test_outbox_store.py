from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from src.db import Database
from src.outbox_store import OutboxStore, retention_cutoff


class OutboxStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")
        await self.db.initialize()
        self.store = OutboxStore(self.db)
        self.now = datetime(2026, 10, 29, tzinfo=UTC).timestamp()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def put(self, key, peer="1", **kwargs):
        return await self.store.enqueue(key, "telegram", peer, "sendMessage", {"chat_id": int(peer), "text": key}, now=self.now, **kwargs)

    async def test_saved_request_and_idempotency_survive_new_store_instance(self):
        await self.put("one", metadata={"campaign_type": "notification"})
        restarted = OutboxStore(Database(self.db.path))
        duplicate = await restarted.enqueue("one", "telegram", "1", "sendMessage", {"text": "changed"}, now=self.now)
        self.assertEqual(json.loads(duplicate["payload_json"])["text"], "one")
        self.assertEqual(await restarted.counts(), {"pending": 1})

    async def test_batch_rolls_back_all_parts_when_one_cannot_be_saved(self):
        items = [
            {"delivery_id": "first", "platform": "telegram", "peer_key": "1", "method": "sendMessage", "payload": {"text": "first"}},
            {"delivery_id": "bad", "platform": "telegram", "peer_key": "1", "method": "sendMessage", "payload": {"bad": object()}},
        ]
        with self.assertRaises(TypeError):
            await self.store.enqueue_many(items, now=self.now)
        self.assertIsNone(await self.store.get("first"))
        self.assertEqual(await self.store.counts(), {})

    async def test_fifo_blocks_only_the_same_peer(self):
        await self.put("first")
        await self.put("second")
        await self.put("other", "2")
        self.assertEqual(await self.store.reserve_due("telegram", now=self.now), ["first", "other"])
        self.assertIsNone(await self.store.claim("second", "worker", now=self.now))
        self.assertIsNotNone(await self.store.claim("other", "worker", now=self.now))
        self.assertIsNotNone(await self.store.claim("first", "owner", now=self.now))
        await self.store.finish("first", "owner", result=True, now=self.now)
        self.assertIsNotNone(await self.store.claim("second", "worker", now=self.now))

    async def test_two_workers_cannot_claim_one_delivery(self):
        await self.put("one")
        claimed = await asyncio.gather(
            self.store.claim("one", "a", now=self.now), self.store.claim("one", "b", now=self.now),
        )
        self.assertEqual(sum(row is not None for row in claimed), 1)

    async def test_expired_lease_is_recovered_and_old_worker_cannot_finish(self):
        await self.put("one")
        await self.store.claim("one", "old", now=self.now)
        self.assertIsNone(await self.store.claim("one", "new", now=self.now + 119))
        new = await self.store.claim("one", "new", now=self.now + 121)
        self.assertEqual(new["attempts"], 2)
        self.assertFalse(await self.store.finish("one", "old", result="stale", now=self.now + 122))
        self.assertTrue(await self.store.finish("one", "new", result="ok", now=self.now + 122))
        self.assertEqual(json.loads((await self.store.get("one"))["result_json"]), "ok")

    async def test_heartbeat_extends_owned_lease_only(self):
        await self.put("one")
        await self.store.claim("one", "owner", now=self.now)
        self.assertFalse(await self.store.renew("one", "stranger", now=self.now + 100))
        self.assertTrue(await self.store.renew("one", "owner", now=self.now + 100))
        self.assertIsNone(await self.store.claim("one", "new", now=self.now + 200))

    async def test_hour_outage_does_not_exhaust_five_attempts(self):
        await self.put("one")
        stamp = self.now
        for attempt in range(15):
            owner = str(attempt)
            row = await self.store.claim("one", owner, now=stamp)
            self.assertIsNotNone(row)
            await self.store.finish("one", owner, error={"type": "network"}, retry_delay=300, now=stamp)
            self.assertEqual(await self.store.reserve_due("telegram", now=stamp + 299), [])
            stamp += 300
        restarted = OutboxStore(self.db)
        row = await restarted.claim("one", "recovered", now=stamp)
        self.assertGreater(row["attempts"], 5)
        self.assertGreater(stamp - self.now, 3600)
        await restarted.finish("one", "recovered", result=42, now=stamp)
        self.assertEqual((await restarted.get("one"))["status"], "sent")

    async def test_expired_request_releases_later_messages(self):
        await self.put("expired", ttl=1)
        await self.put("next")
        self.assertEqual(await self.store.reserve_due("telegram", now=self.now + 2), ["next"])
        self.assertEqual((await self.store.get("expired"))["status"], "expired")

    async def test_failed_publication_is_retried_after_delay(self):
        await self.put("one")
        self.assertEqual(await self.store.reserve_due("telegram", now=self.now), ["one"])
        self.assertEqual(await self.store.reserve_due("telegram", now=self.now + 1), [])
        await self.store.publication_failed("one", now=self.now)
        self.assertEqual(await self.store.reserve_due("telegram", now=self.now + 5), ["one"])

    async def test_cancelled_worker_releases_record_for_restart(self):
        await self.put("one")
        await self.store.claim("one", "owner", now=self.now)
        await self.store.release("one", "stranger")
        self.assertEqual((await self.store.get("one"))["status"], "processing")
        await self.store.release("one", "owner")
        self.assertEqual((await self.store.get("one"))["status"], "pending")
        self.assertIsNotNone(await self.store.claim("one", "new", now=self.now))

    async def test_cleanup_deletes_only_records_older_than_three_calendar_months(self):
        cutoff = retention_cutoff(self.now)
        await self.store.enqueue("old", "telegram", "1", "sendMessage", {}, now=cutoff - 1)
        await self.store.enqueue("boundary", "telegram", "1", "sendMessage", {}, now=cutoff)
        await self.put("fresh")
        await self.db.upsert_user("telegram", 99, None, "Не удалять")
        removed = await self.store.purge(now=self.now)
        self.assertEqual([row["delivery_id"] for row in removed], ["old"])
        self.assertIsNotNone(await self.store.get("boundary"))
        self.assertIsNotNone(await self.store.get("fresh"))
        self.assertIsNotNone(await self.db.get_user("telegram", 99))

    async def test_cleanup_does_not_delete_active_worker_record(self):
        await self.store.enqueue("old", "telegram", "1", "sendMessage", {}, now=retention_cutoff(self.now) - 1, ttl=200 * 86400)
        await self.store.claim("old", "owner", now=self.now)
        self.assertEqual(await self.store.purge(now=self.now + 1), [])
        self.assertEqual(len(await self.store.purge(now=self.now + 121)), 1)

    def test_three_month_cutoff_handles_short_month_and_leap_year(self):
        for year, expected in ((2026, 28), (2024, 29)):
            now = datetime(year, 5, 31, 12, tzinfo=UTC)
            cutoff = datetime.fromtimestamp(retention_cutoff(now.timestamp()), tz=UTC)
            self.assertEqual(cutoff, datetime(year, 2, expected, 12, tzinfo=UTC))

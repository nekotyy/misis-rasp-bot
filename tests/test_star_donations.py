import tempfile
import unittest
from pathlib import Path
from src.db import Database


class StarDonationsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_bot.db"
        self.db = Database(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_record_and_get_star_donation(self):
        donation_id = await self.db.record_star_donation(
            user_id=123456789,
            username="testuser",
            full_name="Test User",
            stars=50,
            charge_id="tx_test_charge_123",
        )
        self.assertIsInstance(donation_id, int)
        self.assertGreater(donation_id, 0)

        donation = await self.db.get_star_donation(donation_id)
        self.assertIsNotNone(donation)
        self.assertEqual(donation["id"], donation_id)
        self.assertEqual(donation["user_id"], 123456789)
        self.assertEqual(donation["username"], "testuser")
        self.assertEqual(donation["full_name"], "Test User")
        self.assertEqual(donation["stars"], 50)
        self.assertEqual(donation["charge_id"], "tx_test_charge_123")
        self.assertFalse(donation["refunded"])
        self.assertIsNone(donation["refunded_at"])

    async def test_refund_star_donation(self):
        donation_id = await self.db.record_star_donation(
            user_id=987654321,
            username="donor",
            full_name="Donor User",
            stars=100,
            charge_id="tx_test_charge_456",
        )

        refund_success = await self.db.refund_star_donation(donation_id)
        self.assertTrue(refund_success)

        donation = await self.db.get_star_donation(donation_id)
        self.assertTrue(donation["refunded"])
        self.assertIsNotNone(donation["refunded_at"])

        # Second refund attempt should return False
        refund_again = await self.db.refund_star_donation(donation_id)
        self.assertFalse(refund_again)


if __name__ == "__main__":
    unittest.main()

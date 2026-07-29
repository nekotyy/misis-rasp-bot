import unittest
from dataclasses import asdict

from src.message_broker import (
    DatabaseCleanupJob,
    DatabaseCleanupJobBroker,
    LessonCounterJob,
    LessonCounterJobBroker,
    OutboundMessage,
    RabbitMQBroker,
)


class MessageBrokerTests(unittest.TestCase):
    def test_outbound_message_dataclass(self):
        msg = OutboundMessage(
            platform="telegram",
            user_id=12345,
            text="Тестовое сообщение",
            campaign_type="notification",
        )
        self.assertEqual(msg.platform, "telegram")
        self.assertEqual(msg.user_id, 12345)
        self.assertEqual(msg.attempt, 1)

        d = asdict(msg)
        self.assertEqual(d["user_id"], 12345)
        self.assertEqual(d["text"], "Тестовое сообщение")

    def test_lesson_counter_job_dataclass(self):
        job = LessonCounterJob(schedule_id=600)
        self.assertEqual(job.schedule_id, 600)
        self.assertEqual(job.attempt, 1)
        self.assertEqual(job.max_attempts, 8)

    def test_database_cleanup_job_dataclass(self):
        job = DatabaseCleanupJob(days=90)
        self.assertEqual(job.days, 90)
        self.assertEqual(job.attempt, 1)

    def test_broker_disabled_when_url_empty(self):
        broker = RabbitMQBroker(url="", queue_name="test_queue")
        self.assertFalse(broker.enabled)

        lc_broker = LessonCounterJobBroker(url="   ", queue_name="test_queue")
        self.assertFalse(lc_broker.enabled)

        cleanup_broker = DatabaseCleanupJobBroker(url="", queue_name="test_queue")
        self.assertFalse(cleanup_broker.enabled)


if __name__ == "__main__":
    unittest.main()

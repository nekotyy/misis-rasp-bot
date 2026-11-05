from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.deploy_production import (
    Deployer,
    DeploymentError,
    Settings,
    schema_fingerprint,
    validate_compose,
    validate_sha,
    write_release,
)
from scripts.deploy_request import service_command
from scripts.deploy_ssh import deployment_command

OLD = "a" * 40
NEW = "b" * 40


class FakeDeployer(Deployer):
    """Exercise deployment transactions without invoking Git or Docker."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.events = []
        self.head = OLD
        self.latest = NEW
        self.local_changes = ""
        self.failure = ""
        self.health_calls = 0
        self.schema_change = False
        self.override_broker = False

    def git(self, *args):
        self.events.append(("git", *args))
        if args[0] == "status":
            return self.local_changes
        if args[0] == "rev-parse":
            return self.head
        if args[:2] == ("switch", "--detach"):
            self.head = args[2]
        return ""

    def latest_main(self):
        return self.latest

    def run(self, *args, **kwargs):
        self.events.append(tuple(args))
        if args[:2] == ("docker", "build") and self.failure == "build":
            raise DeploymentError("build failed")
        return ""

    def compose(self, *args, source=None, release=None):
        self.events.append(("compose", *args))
        if args[0] == "config":
            config = compose_config(self.settings)
            if source is not None and self.override_broker:
                config["services"]["rabbitmq"]["hostname"] = "changed-node"
            return json.dumps(config)
        if args[0] == "stop" and self.failure == "stop":
            raise DeploymentError("partial stop failed")
        return ""

    def current_images(self):
        return {"bot": "old-bot:rollback", "web": "old-web:rollback"}

    def backup(self, directory):
        if self.failure == "backup":
            raise DeploymentError("backup failed")
        return super().backup(directory)

    def wait_healthy(self):
        self.health_calls += 1
        if self.failure == "health" and self.health_calls == 1:
            with contextlib.closing(sqlite3.connect(self.settings.repo / "runtime/bot.db")) as db:
                db.execute("INSERT INTO messages VALUES ('delivered after deployment')")
                if self.schema_change:
                    db.execute("CREATE TABLE new_schema (id INTEGER)")
                db.commit()
            raise DeploymentError("unhealthy new image")


def compose_config(settings):
    runtime = {"type": "bind", "source": str(settings.repo / "runtime"), "target": "/app/runtime"}
    env = {"type": "bind", "source": str(settings.repo / ".env"), "target": "/app/config/.env"}
    return {"services": {
        "bot": {"volumes": [runtime.copy(), env]},
        "web": {"volumes": [runtime.copy()], "ports": [{"host_ip": "127.0.0.1", "target": 8080}]},
        "rabbitmq": {"hostname": "preserved-node", "ports": [{"host_ip": "127.0.0.1", "target": 5672}]},
    }}


class ProductionDeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.settings = Settings(
            repo=root / "repo", state=root / "state", backups=root / "backups",
            host_override=root / "host.json", release_override=root / "release.json", min_free_bytes=0,
        )
        (self.settings.repo / "runtime/backups").mkdir(parents=True)
        self.settings.state.mkdir()
        (self.settings.repo / ".env").write_text("WEB_CONFIG_SECRET=preserved-secret\n")
        (self.settings.repo / "runtime/lesson_counters.json").write_text('{"counter": 42}')
        (self.settings.repo / "runtime/bot.vk_polling.json").write_text('{"ts": "123"}')
        (self.settings.repo / "runtime/backups/old.db").write_text("old backup must remain")
        with contextlib.closing(sqlite3.connect(self.settings.repo / "runtime/bot.db")) as db:
            db.execute("CREATE TABLE messages (text TEXT)")
            db.execute("INSERT INTO messages VALUES ('before deployment')")
            db.commit()
        write_release(self.settings.release_override, {"bot": "old-bot:latest", "web": "old-web:latest"})
        self.deployer = FakeDeployer(self.settings)

    def test_forced_command_rejects_shells_short_shas_and_injection(self):
        for command in ("", "bash", "deploy main", "deploy " + NEW[:7], "deploy " + NEW + "; id", "deploy " + NEW + "\n", "deploy " + NEW.upper()):
            with self.subTest(command=command), self.assertRaises(ValueError):
                deployment_command(command)
        self.assertEqual(deployment_command("deploy " + NEW), ["/usr/bin/sudo", "-n", "/usr/local/sbin/misis-deploy-request", NEW])
        for value in ("main", "-" * 40, NEW + " --force"):
            with self.assertRaises(DeploymentError):
                validate_sha(value)

    def test_deployment_runs_as_a_fixed_systemd_service_without_a_client_stdio_pipe(self):
        command = service_command(NEW)
        self.assertEqual(command[-2:], ["/usr/local/sbin/misis-deploy", NEW])
        self.assertIn("--wait", command)
        self.assertNotIn("--pipe", command)
        with self.assertRaises(ValueError):
            service_command(NEW + " --property=ExecStart=/bin/bash")

    def test_rejects_runtime_changes_and_public_ports(self):
        config = compose_config(self.settings)
        validate_compose(config, self.settings)
        config["services"]["web"]["ports"][0]["host_ip"] = "0.0.0.0"
        with self.assertRaises(DeploymentError):
            validate_compose(config, self.settings)
        config = compose_config(self.settings)
        config["services"]["bot"]["volumes"][0]["source"] = str(self.settings.repo / "empty-runtime")
        with self.assertRaises(DeploymentError):
            validate_compose(config, self.settings)

    def test_backup_includes_committed_wal_cursor_and_configuration_without_nesting_old_backups(self):
        database = self.settings.repo / "runtime/bot.db"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO messages VALUES ('committed in WAL')")
            connection.commit()
            self.deployer.backup(self.settings.backups / "snapshot")
        with contextlib.closing(sqlite3.connect(self.settings.backups / "snapshot/bot.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        with tarfile.open(self.settings.backups / "snapshot/runtime.tar.gz") as archive:
            names = archive.getnames()
            self.assertIn("runtime/bot.db", names)
            self.assertIn("runtime/bot.vk_polling.json", names)
            self.assertIn(".env", names)
            self.assertNotIn("runtime/bot.db-wal", names)
            self.assertNotIn("runtime/backups/old.db", names)
        self.assertTrue((self.settings.repo / "runtime/backups/old.db").exists())

    def test_superseded_commit_and_dirty_checkout_never_stop_services(self):
        self.deployer.latest = "c" * 40
        self.deployer.deploy(NEW)
        self.assertFalse(any(e[0] == "compose" for e in self.deployer.events))
        self.deployer.local_changes = " M docker-compose.yml"
        with self.assertRaises(DeploymentError):
            self.deployer.deploy(NEW)
        self.assertFalse(any(e[:2] == ("compose", "stop") for e in self.deployer.events))

    def test_build_failure_leaves_running_services_untouched(self):
        self.deployer.failure = "build"
        with self.assertRaises(DeploymentError):
            self.deployer.deploy(NEW)
        self.assertEqual(self.deployer.head, OLD)
        self.assertFalse(any(e[:2] == ("compose", "stop") for e in self.deployer.events))

    def test_rabbitmq_infrastructure_change_requires_manual_deployment(self):
        self.deployer.override_broker = True
        with self.assertRaisesRegex(DeploymentError, "RabbitMQ"):
            self.deployer.deploy(NEW)
        self.assertFalse(any(e[:2] == ("compose", "stop") for e in self.deployer.events))

    def test_backup_or_partial_stop_failure_restarts_original_services(self):
        for failure in ("backup", "stop"):
            with self.subTest(failure=failure):
                deployer = FakeDeployer(self.settings)
                deployer.failure = failure
                with self.assertRaises(DeploymentError):
                    deployer.deploy(NEW)
                self.assertIn(("compose", "start", "bot", "web"), deployer.events)
                self.assertEqual(deployer.head, OLD)

    def test_unhealthy_release_rolls_back_code_but_keeps_new_messages(self):
        self.deployer.failure = "health"
        with self.assertRaisesRegex(DeploymentError, "preserved current data"):
            self.deployer.deploy(NEW)
        self.assertEqual(self.deployer.head, OLD)
        self.assertEqual(json.loads(self.settings.release_override.read_text())["services"]["bot"]["image"], "old-bot:rollback")
        with contextlib.closing(sqlite3.connect(self.settings.repo / "runtime/bot.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(self.deployer.health_calls, 2)

    def test_schema_change_never_restores_old_database_or_old_code(self):
        original = schema_fingerprint(self.settings.repo / "runtime/bot.db")
        self.deployer.failure = "health"
        self.deployer.schema_change = True
        with self.assertRaisesRegex(DeploymentError, "schema change"):
            self.deployer.deploy(NEW)
        self.assertEqual(self.deployer.head, NEW)
        self.assertNotEqual(schema_fingerprint(self.settings.repo / "runtime/bot.db"), original)
        with contextlib.closing(sqlite3.connect(self.settings.repo / "runtime/bot.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)

    def test_success_records_sha_and_preserves_runtime_files(self):
        self.deployer.deploy(NEW)
        self.assertEqual((self.settings.state / "current-sha").read_text().strip(), NEW)
        self.assertEqual(self.deployer.head, NEW)
        self.assertEqual((self.settings.repo / "runtime/lesson_counters.json").read_text(), '{"counter": 42}')
        self.assertTrue((self.settings.repo / "runtime/backups/old.db").exists())
        self.assertEqual(len(list(self.settings.backups.glob("*/runtime.tar.gz"))), 1)

    def test_readiness_can_read_telegram_startup_from_stderr_without_logging_user_payloads(self):
        result = subprocess.CompletedProcess([], 0, "VK payload", "Run polling for bot")
        with patch("scripts.deploy_production.subprocess.run", return_value=result):
            logs = Deployer(self.settings).run("docker", "logs", "misis-rasp-bot")
        self.assertIn("Run polling for bot", logs)
        self.assertNotIn("VK payload", (self.settings.state / "deploy.log").read_text())


if __name__ == "__main__":
    unittest.main()

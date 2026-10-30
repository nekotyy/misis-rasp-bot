#!/usr/bin/python3
"""Deploy an exact, CI-tested main revision. Install a reviewed copy outside Git as root."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

APPLICATIONS = ("bot", "web")
CONTAINERS = ("misis-rasp-bot", "misis-web-configurator", "misis-rabbitmq")
LOGIN_PROBE = """import os, httpx
with httpx.Client(base_url='http://127.0.0.1:8080', timeout=8,
                  headers={'Origin':'http://127.0.0.1:8080'}, follow_redirects=False) as c:
    assert c.get('/login').status_code == 200
    r = c.post('/login', data={'login':os.environ.get('WEB_SUPERUSER_LOGIN','admin'),
                              'password':os.environ['WEB_SUPERUSER_PASSWORD']})
    assert r.status_code == 303 and r.headers.get('location') == '/'
    assert c.get('/').status_code == 200
    assert c.get('/docs').status_code == 404
    assert c.post('/logout').status_code == 303
"""


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    repo: Path = Path("/opt/misis-rasp-bot")
    state: Path = Path("/var/lib/misis-deploy")
    backups: Path = Path("/var/backups/misis-rasp-bot")
    host_override: Path = Path("/etc/misis-rasp-bot/compose.override.yml")
    release_override: Path = Path("/etc/misis-rasp-bot/release.override.yml")
    health_timeout: int = 180
    min_free_bytes: int = 4 * 1024**3


def validate_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise DeploymentError("Expected exactly one full lowercase Git commit SHA")
    return value


def schema_fingerprint(database: Path) -> str:
    with contextlib.closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)) as connection:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def write_release(path: Path, images: dict[str, str]) -> None:
    # JSON is also valid YAML; json.dumps prevents image names from injecting YAML keys.
    data = {"services": {service: {"image": images[service]} for service in APPLICATIONS}}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_compose(config: dict, settings: Settings) -> None:
    services = config["services"]
    for service in APPLICATIONS:
        volumes = services[service].get("volumes", [])
        runtime = [v for v in volumes if v.get("target") == "/app/runtime"]
        if (len(runtime) != 1 or runtime[0].get("type") != "bind" or runtime[0].get("read_only")
                or Path(runtime[0]["source"]) != settings.repo / "runtime"):
            raise DeploymentError("The production runtime bind mount must be preserved")
    env_mount = [v for v in services["bot"].get("volumes", []) if v.get("target") == "/app/config/.env"]
    if len(env_mount) != 1 or env_mount[0].get("type") != "bind" or Path(env_mount[0]["source"]) != settings.repo / ".env":
        raise DeploymentError("The production environment bind mount must be preserved")
    for service in ("bot", "web", "rabbitmq"):
        for port in services[service].get("ports", []):
            if port.get("host_ip") not in ("127.0.0.1", "::1"):
                raise DeploymentError("Automatic deployment cannot expose internal ports")


class Deployer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.log = settings.state / "deploy.log"

    def run(self, *args: str, cwd: Path | None = None, timeout: int = 120) -> str:
        # Command output, including resolved Compose secrets, stays in a root-only log.
        with self.log.open("a", encoding="utf-8") as log:
            log.write(f"\n{datetime.now(UTC).isoformat()} {args[0]}\n")
            try:
                result = subprocess.run(args, cwd=cwd or self.settings.repo, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise DeploymentError(f"{args[0]} timed out; details: {self.log}") from exc
            sensitive = args[:2] in (("docker", "logs"), ("docker", "inspect")) or "config" in args
            if not sensitive:
                log.write(result.stdout)
                log.write(result.stderr)
        if result.returncode:
            raise DeploymentError(f"{args[0]} failed (exit {result.returncode}); details: {self.log}")
        return (result.stdout + result.stderr if args[:2] == ("docker", "logs") else result.stdout).strip()

    def git(self, *args: str) -> str:
        return self.run("git", *args)

    def compose(self, *args: str, source: Path | None = None, release: Path | None = None) -> str:
        s = self.settings
        return self.run(
            "docker", "compose", "--project-name", "misis-rasp-bot", "--project-directory", str(s.repo),
            "--env-file", str(s.repo / ".env"), "-f", str((source or s.repo) / "docker-compose.yml"),
            "-f", str(s.host_override), "-f", str(release or s.release_override), *args,
        )

    def latest_main(self) -> str:
        self.git("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        return self.git("rev-parse", "refs/remotes/origin/main")

    def backup(self, directory: Path) -> str:
        s = self.settings
        directory.mkdir(mode=0o700, parents=True)
        database = s.repo / "runtime/bot.db"
        snapshot = directory / "bot.db"
        with contextlib.closing(sqlite3.connect(database, timeout=30)) as source, contextlib.closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DeploymentError("SQLite backup integrity check failed")
        snapshot.chmod(0o600)
        with tarfile.open(directory / "runtime.tar.gz", "w:gz", compresslevel=1) as archive:
            for path in sorted((s.repo / "runtime").iterdir()):
                if path.name not in ("backups", "bot.db", "bot.db-wal", "bot.db-shm"):
                    archive.add(path, arcname=f"runtime/{path.name}")
            archive.add(snapshot, arcname="runtime/bot.db")
            archive.add(s.repo / ".env", arcname=".env")
            archive.add(s.release_override, arcname="release.override.yml")
        return schema_fingerprint(snapshot)

    def current_images(self) -> dict[str, str]:
        images = {}
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ").lower()
        for service, container in zip(APPLICATIONS, CONTAINERS[:2], strict=True):
            image_id = self.run("docker", "inspect", "--format", "{{.Image}}", container)
            tag = f"misis-rasp-bot-rollback-{service}:{stamp}"
            self.run("docker", "image", "tag", image_id, tag)
            images[service] = tag
        return images

    def wait_healthy(self) -> None:
        deadline = time.monotonic() + self.settings.health_timeout
        while time.monotonic() < deadline:
            try:
                containers = json.loads(self.run("docker", "inspect", *CONTAINERS))
                running = all(c["State"]["Status"] == "running" for c in containers)
                broker_ready = containers[2]["State"].get("Health", {}).get("Status") == "healthy"
                env = dict(entry.split("=", 1) for entry in containers[0]["Config"]["Env"])
                logs = self.run("docker", "logs", "misis-rasp-bot")
                telegram_ready = not env.get("TELEGRAM_BOT_TOKEN") or "Run polling for bot" in logs
                vk_ready = not env.get("VK_BOT_TOKEN") or "groups.getLongPollServer" in logs
                if running and broker_ready and telegram_ready and vk_ready and "TelegramConflictError" not in logs:
                    self.run("docker", "exec", "misis-web-configurator", "python", "-c", LOGIN_PROBE, timeout=40)
                    return
            except (DeploymentError, json.JSONDecodeError, KeyError, IndexError):
                pass
            time.sleep(3)
        raise DeploymentError("Production readiness checks did not pass")

    def deploy(self, sha: str) -> None:
        validate_sha(sha)
        s = self.settings
        if self.git("status", "--porcelain", "--untracked-files=all"):
            raise DeploymentError("Production checkout has local changes; refusing to overwrite them")
        if self.latest_main() != sha:
            print("SKIPPED: a newer main revision superseded this CI run", flush=True)
            return
        previous = self.git("rev-parse", "HEAD")
        if previous == sha and (s.state / "current-sha").exists() and (s.state / "current-sha").read_text().strip() == sha:
            self.wait_healthy()
            print(f"Already deployed: {sha}", flush=True)
            return
        self.git("merge-base", "--is-ancestor", previous, sha)
        if shutil.disk_usage(s.repo).free < s.min_free_bytes:
            raise DeploymentError("Not enough free disk space; running services were left unchanged")
        builds = s.state / "builds"
        builds.mkdir(mode=0o700, exist_ok=True)
        worktree = Path(tempfile.mkdtemp(prefix="release-", dir=builds))
        self.git("worktree", "add", "--detach", str(worktree), sha)
        try:
            image = f"misis-rasp-bot-deploy:{sha}"
            print(f"Building tested revision {sha}; running services stay available", flush=True)
            self.run("docker", "build", "--pull", "--tag", image, str(worktree), timeout=1200)
            candidate = worktree / "release.override.json"
            write_release(candidate, dict.fromkeys(APPLICATIONS, image))
            before = json.loads(self.compose("config", "--format", "json"))
            after = json.loads(self.compose("config", "--format", "json", source=worktree, release=candidate))
            validate_compose(after, s)
            if before["services"]["rabbitmq"] != after["services"]["rabbitmq"]:
                raise DeploymentError("RabbitMQ configuration changes require a separate infrastructure deployment")
            if self.latest_main() != sha:
                print("SKIPPED: main changed during the build; running services stay available", flush=True)
                return
            candidate.unlink()
            old_images = self.current_images()
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup = s.backups / f"{stamp}-{sha[:12]}"
            old_schema = ""
            activated = False
            print("Stopping application writers and creating a consistent backup", flush=True)
            try:
                self.compose("stop", "--timeout", "60", *APPLICATIONS)
                old_schema = self.backup(backup)
                (backup / "deployment.json").write_text(json.dumps({"previous": previous, "target": sha, "images": old_images}) + "\n")
                self.git("switch", "--detach", sha)
                write_release(s.release_override, dict.fromkeys(APPLICATIONS, image))
                activated = True
                self.compose("up", "--detach", "--no-deps", "--no-build", "--force-recreate", *APPLICATIONS)
                self.wait_healthy()
            except Exception as error:
                if activated:
                    self.compose("stop", "--timeout", "60", *APPLICATIONS)
                    if schema_fingerprint(s.repo / "runtime/bot.db") != old_schema:
                        # Restoring an older SQLite could replay delivered messages or lose new writes.
                        self.compose("up", "--detach", "--no-deps", "--no-build", *APPLICATIONS)
                        raise DeploymentError(f"Readiness failed after a schema change; preserved new data and images. Backup: {backup}") from error
                    self.git("switch", "--detach", previous)
                    write_release(s.release_override, old_images)
                    self.compose("up", "--detach", "--no-deps", "--no-build", "--force-recreate", *APPLICATIONS)
                    self.wait_healthy()
                    raise DeploymentError(f"Readiness failed; restored previous code and images, preserved current data. Backup: {backup}") from error
                self.compose("start", *APPLICATIONS)
                raise
            (s.state / "current-sha").write_text(sha + "\n")
            print(f"DEPLOYED {sha}; readiness OK; backup: {backup}", flush=True)
        finally:
            # Only our temporary worktree is removed; runtime, databases and backups are retained.
            if (worktree / "release.override.json").exists():
                (worktree / "release.override.json").unlink()
            self.git("worktree", "remove", str(worktree))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise DeploymentError("Expected one commit SHA")
    sha = validate_sha(argv[0])
    if os.name != "posix" or os.geteuid() != 0:
        raise DeploymentError("The trusted deployment entrypoint must run as root on Linux")
    import fcntl

    os.umask(0o077)
    settings = Settings()
    settings.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (settings.state / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("Another deployment is already in progress") from exc
        Deployer(settings).deploy(sha)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (DeploymentError, OSError) as exc:
        print(f"DEPLOY FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

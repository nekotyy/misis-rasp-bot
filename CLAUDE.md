# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`misis-rasp-bot` is an async Python 3.12+ Telegram/VK bot that publishes MISIS college class schedules and homework
notifications, plus a FastAPI web dashboard for administration and monitoring. Docs and commit messages in this repo
are Russian; match that when writing user-facing text or commit messages.

## Commands

Package manager is `uv`. Always use `--frozen` (don't let `uv` rewrite `uv.lock`).

```bash
uv sync --frozen                                                              # install deps

uv run --frozen -m src.main                                                   # run bots + background jobs
uv run --frozen uvicorn web_configurator.app:app --host 127.0.0.1 --port 8080 --reload   # run web dashboard

uv run --frozen ruff check src web_configurator tests scripts                 # lint (required before every commit)
uv run --frozen ruff check --fix src web_configurator tests scripts           # lint autofix

uv run --frozen python -m compileall -q src web_configurator tests scripts    # syntax check

uv run --frozen python -m unittest discover -s tests -p "test_*.py" -v        # full test suite
uv run --frozen python -m unittest tests.test_schedule_service -v             # single test file
uv run --frozen python -m unittest tests.test_schedule_service.ClassName.test_method -v  # single test case

docker compose config                                                         # validate compose (if Docker/env/workflow files changed)
```

Every code change (new function or changed business logic) requires a matching test in `tests/` and a clean run of
the lint + compileall + unittest pipeline above before committing.

## Architecture

### Two independent processes, one SQLite database

- `src/main.py` — the bot process. Boots `Database`, `GroupCatalog`, `ScheduleParser`, `Broadcaster`,
  `ScheduleJobs` (APScheduler), then runs Telegram polling and VK polling as supervised background tasks
  (`start_telegram_polling`/`start_vk_polling`, auto-restarting via `run_forever`) plus RabbitMQ consumers.
  Telegram/VK can each be individually absent (missing token just logs a warning); the rest of the system keeps
  running.
- `web_configurator/app.py` — a separate FastAPI process (own `uvicorn` entrypoint) serving the admin dashboard.
  It reads/writes the same SQLite file but is not required for the bots to function.
- Both process share `src/config.py::Settings` (loaded from `.env` via `from_env()`) and `src/db.py::Database`
  (a single class wrapping `aiosqlite`, one method per query/mutation — this is the source of truth for the schema,
  not a separate models layer).

### Data flow

User message (Telegram/VK) → `src/telegram_bot.py` / `src/vk_bot.py` handler → `Database` (users, subscriptions) →
`src/scheduler.py` (`ScheduleJobs`, APScheduler cron jobs) periodically drives `src/parser.py` (`ScheduleParser`,
scrapes `SCHEDULE_URL`) → new snapshot saved via `Database.save_snapshot` → diffed against the previous snapshot →
on change, a `change_events` row is written and `src/notifier.py::Broadcaster` delivers it, either through RabbitMQ
(`src/message_broker.py`) or a direct-send fallback if RabbitMQ is unavailable. A nightly job in `lesson_counters.py`
tallies elapsed lessons per group/subject into `lesson_counter_events`.

### OCR photo import is a second source feeding the same pipeline

When the schedule site is down, an admin can upload a photo of the schedule instead.
`src/ocr_schedule.py` turns an image into a plain `ScheduleSnapshot` (Tesseract for recognition, then a
pure `parse_text` layer that normalizes OCR artifacts, filters noise, snaps values to a vocabulary of known
subjects/teachers/rooms harvested from past snapshots, and scores confidence). `src/ocr_import.py` resolves
the target source, merges the recognized days into the last known snapshot by `date_iso`, renders the
admin preview, and applies. Delivery is not reimplemented: `ScheduleJobs.apply_manual_snapshot` calls the
same `apply_snapshot` used by site sync, so hashing, snapshot saving, diffing and broadcasting are shared.
Keep it that way — an OCR-specific notification path would drift from the site path. `parse_text` is pure
and must stay testable without a Tesseract binary installed.

### Bot handlers are large, single-file, per-platform

`src/telegram_bot.py` (~4100 lines) and `src/vk_bot.py` (~2700 lines) each build their own dispatcher
(`build_dispatcher` / `build_vk_bot`) and independently implement the full user + admin UX (keyboards, FSM/state,
schedule search, admin broadcast, group binding) against the shared `Database`/`Broadcaster`/`ScheduleJobs`
services. Behavior is expected to stay consistent between the two platforms — a UX change on one side usually needs
a matching change on the other. `src/schedule_search.py` (group/teacher/room lookup) and `src/group_catalog.py`
(schedule_id catalog) are shared search/lookup utilities used by both.

### Migrations: Alembic is authoritative, but bootstraps around legacy DBs

`src/db_migrations.py::apply_migrations` runs on every bot startup before `Database.initialize()`. It detects a
pre-existing SQLite file with user tables but no `alembic_version` table and stamps it to `head` before upgrading,
so older/manually-created databases don't try to replay migrations against tables that already exist. Migration
scripts live in `migrations/versions/`; add new ones there rather than hand-editing schema in `src/db.py`.

### RabbitMQ is optional, not load-bearing

`src/message_broker.py` defines several broker classes (`RabbitMQBroker`, `LessonCounterJobBroker`,
`DatabaseCleanupJobBroker`, `AutoDailyLessonCounterJobBroker`), one per queue (`RABBITMQ_QUEUE`,
`LESSON_COUNTERS_QUEUE`, `DB_CLEANUP_QUEUE`, `AUTO_DAILY_LESSON_COUNTER_QUEUE`). Every consumer start in
`src/main.py` is wrapped so an AMQP/connection failure logs, reports through `SystemAlertManager`
(`src/system_status.py`), and falls back to direct/synchronous delivery rather than crashing the process. When
touching broker code, preserve this fallback — the bot must keep working with `RABBITMQ_URL` unset or RabbitMQ down.

### Web dashboard auth/security (`web_configurator/security.py`)

`WebAuthStore` holds a hardcoded superuser (`WEB_SUPERUSER_LOGIN`/`WEB_SUPERUSER_PASSWORD`) alongside DB-backed
`web_users` with a fixed permission set (`ALL_PERMISSIONS`). There's a login guard
(`LOGIN_GUARD_MAX_ATTEMPTS` = 3, `LOGIN_GUARD_BLOCK_SECONDS` = 6h) and explicit denylists of insecure default
secrets/passwords (`INSECURE_SECRET_VALUES`, `INSECURE_SUPERUSER_PASSWORDS`) that are checked at startup —
`localhost`/`127.0.0.1`/`::1` get relaxed cookie/secure requirements for local dev, everything else must run behind
HTTPS. Do not weaken this hardening (login guard, session cookie policy, audit logging of login attempts) without
being asked; see `AGENTS.md` section 7 for the full list of things not to touch casually.

### Storage layout

- SQLite: `bot.db` locally, `runtime/bot.db` in Docker. Table inventory lives in `src/db.py`, not documented
  separately — read that file for the schema.
- `storage/lesson_counters.json` (or `runtime/lesson_counters.json` in Docker) is an editable config for lesson
  counters, loaded via `LessonCounterService.load_config_file` and synced into the DB at startup when
  `LESSON_COUNTERS_ENABLED=true`.
- `runtime/` holds all Docker-container working state (DB, JSON, attachments) — never delete or reformat it without
  an explicit request.

### Config

All runtime configuration is env vars parsed once in `src/config.py::Settings.from_env()` — there is no separate
settings file to edit. Any new env var needs a default there and should be documented in `.env.example` and the
README's "Переменные окружения" section if it's user-facing.

## Repo-specific rules (see `AGENTS.md` for the full Russian-language version)

- Default working branch is `development`; commit messages are Russian, imperative/perfective form
  ("добавил...", "починил...", "настроил...").
- Prefer a minimal, targeted fix over a broad refactor unless the task explicitly calls for restructuring.
- If a change should trigger a new GitHub Release, bump `project.version` in `pyproject.toml` before merging to
  `main` (CI auto-creates a release from that version on push to `main`; it won't recreate an existing tag).
- Never weaken web dashboard security, lose data in `runtime/`/SQLite, or drop bot keyboards/buttons/fallback flows
  without being asked.

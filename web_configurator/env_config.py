from __future__ import annotations

from pathlib import Path


EDITABLE_ENV_KEYS = [
    "APP_TIMEZONE",
    "SCHEDULE_REQUEST_DELAY_SECONDS",
    "SCHEDULE_REQUEST_JITTER_SECONDS",
    "SCHEDULE_URL",
    "DATABASE_PATH",
    "ATTACHMENTS_PATH",
    "VK_DISABLE_SSL_VERIFY",
    "RABBITMQ_URL",
    "RABBITMQ_QUEUE",
    "RABBITMQ_PREFETCH_COUNT",
    "LESSON_COUNTERS_ENABLED",
    "LESSON_COUNTERS_PATH",
    "LESSON_COUNTERS_QUEUE",
]


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return {key: values.get(key, "") for key in EDITABLE_ENV_KEYS}


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    allowed_updates = {key: str(value).strip() for key, value in updates.items() if key in EDITABLE_ENV_KEYS}
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key, _ = stripped.split("=", 1)
        key = key.strip()
        if key in allowed_updates:
            output.append(f"{key}={allowed_updates[key]}")
            seen.add(key)
        else:
            output.append(line)

    for key in EDITABLE_ENV_KEYS:
        if key in allowed_updates and key not in seen:
            output.append(f"{key}={allowed_updates[key]}")

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")

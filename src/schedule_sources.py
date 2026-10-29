"""Адреса источников и совместимые с существующей БД идентификаторы."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

UNIVERSITY_SCHEDULE_URL = "http://r.sf-misis.ru"
UNIVERSITY_BELLS_TEXT = "\n".join(
    (
        "Звонки СТИ НИТУ МИСИС (университет)",
        "",
        "1 пара: 09:00–10:30",
        "2 пара: 10:40–12:10",
        "3 пара: 12:40–14:10",
        "4 пара: 14:20–15:50",
        "5 пара: 16:00–17:30",
        "6 пара: 18:30–20:00",
        "7 пара: 20:10–21:40",
    )
)


def university_kind(url: str) -> str | None:
    match = re.fullmatch(r"/(group|teacher|room)/[1-9]\d*(?:/[12])?/?", urlsplit(url).path)
    return match[1] if match else None


def schedule_numeric_id(url: str) -> int | None:
    parts = urlsplit(url).path.strip("/").split("/")
    if university_kind(url):
        # Отрицательные ID отведены университету: старые group:<id>, SQLite,
        # счетчики, OCR и подписки колледжа не требуют миграции или перенумерации.
        return -int(parts[1])
    return int(parts[-1]) if parts[-1].isdigit() else None


def source_key_from_url(kind: str, url: str) -> str:
    source_id = schedule_numeric_id(url)
    return f"{kind}:{source_id}" if source_id is not None else f"{kind}:{url}"


def parse_university_directory(text: str, kind: str) -> list[dict]:
    """Сайт отдаёт JSON в JS-обёртке `var database = {...};`, без исполнения JS."""
    payload = text.strip()
    if payload.startswith("var database"):
        payload = payload.split("=", 1)[1].strip().removesuffix(";")
    data = json.loads(payload)
    rows = data[kind]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("value"), int)
        or row["value"] <= 0
        or not isinstance(row.get("content"), str)
        or not row["content"].strip()
        for row in rows
    ):
        raise ValueError(f"Некорректный каталог университета: {kind}")
    return rows

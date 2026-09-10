"""Добавляет группы в каталог без ID — например, увиденные на фото, а не на сайте.

Нужен, когда название группы уже известно (с фото сводного расписания,
от студентов и т.п.), а сайт расписания недоступен и не может подтвердить
её номер (schedule_id). Такая группа становится видна поиску и OCR-импорту
сразу, а как только сайт снова заработает и `GroupCatalog` сходит за полным
списком, запись автоматически заменится настоящей — с реальным ID
(`Database.save_groups` при каждой успешной загрузке с сайта сносит и
переливает каталог целиком).

Формат входного JSON — как у `storage/groups_2026-09-07.json`:

    {"courses": {"1": ["МТО-26", "МЧМ-26", ...], "2": [...]}}

Запуск внутри контейнера:

    docker compose exec bot uv run --frozen python -m scripts.seed_pending_groups \
        storage/groups_2026-09-07.json --course 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from src.config import Settings
from src.db import Database

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("seed_pending_groups")


def collect_group_names(payload: dict, courses: list[str] | None) -> list[str]:
    all_courses = payload.get("courses")
    if not isinstance(all_courses, dict):
        raise ValueError("В файле нет объекта 'courses' с группами по курсам.")

    selected = courses or sorted(all_courses)
    names: list[str] = []
    for course in selected:
        if course not in all_courses:
            raise ValueError(f"В файле нет курса {course!r}. Доступные: {', '.join(sorted(all_courses))}.")
        names.extend(str(name).strip() for name in all_courses[course] if str(name).strip())
    return names


async def run(payload: dict, *, courses: list[str] | None, dry_run: bool) -> int:
    names = collect_group_names(payload, courses)
    if not names:
        logger.error("Не нашлось ни одной группы для добавления.")
        return 1

    logger.info("Группы без ID (%s шт.): %s", len(names), ", ".join(names))
    if dry_run:
        logger.info("Пробный запуск: ничего не сохранено.")
        return 0

    settings = Settings.from_env()
    db = Database(settings.database_path)
    await db.initialize()

    before = {group["group_name"] for group in await db.get_all_groups()}
    await db.add_pending_groups(names)
    after = await db.get_all_groups()

    added = sorted({group["group_name"] for group in after} - before)
    skipped = sorted(name for name in names if name not in added)
    if added:
        logger.info("Добавлено новых: %s", ", ".join(added))
    if skipped:
        logger.info("Уже были в каталоге (пропущены): %s", ", ".join(skipped))
    return 0


def main() -> int:
    argument_parser = argparse.ArgumentParser(description="Добавление групп без ID в каталог.")
    argument_parser.add_argument("path", nargs="?", help="Путь к JSON-файлу. Без него читает stdin.")
    argument_parser.add_argument(
        "--course",
        action="append",
        dest="courses",
        help="Курс из файла (можно несколько раз). Без указания — все курсы из файла.",
    )
    argument_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет добавлено, и ничего не менять.",
    )
    args = argument_parser.parse_args()

    raw = Path(args.path).read_text(encoding="utf-8") if args.path else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Файл не является корректным JSON: %s", exc)
        return 1

    try:
        return asyncio.run(run(payload, courses=args.courses, dry_run=args.dry_run))
    except ValueError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

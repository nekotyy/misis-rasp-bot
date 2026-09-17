"""Распознаёт локальные фото расписания через Gemini и импортирует их.

Примеры внутри Docker-контейнера:

    uv run --frozen python -m scripts.import_schedule_photos /tmp/day.jpg --apply
    uv run --frozen python -m scripts.import_schedule_photos /tmp/page*.jpg --summary --apply

По умолчанию выводит только предпросмотр. ``--apply`` сохраняет снимок без
рассылки, а ``--notify`` дополнительно разрешает рассылку подписчикам.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
from src.notifier import Broadcaster
from src.ocr_import import (
    build_ocr_importer,
    format_ocr_preview,
    format_ocr_summary_preview,
)
from src.ocr_schedule import OcrEngineError
from src.parser import ScheduleParser
from src.scheduler import ScheduleJobs

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("import_schedule_photos")


def read_images(paths: list[Path]) -> list[bytes]:
    if not paths:
        raise ValueError("Не указано ни одного изображения.")
    images: list[bytes] = []
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Файл не найден: {path}")
        data = path.read_bytes()
        if not data:
            raise ValueError(f"Файл пуст: {path}")
        images.append(data)
    return images


async def run(paths: list[Path], *, summary: bool, apply: bool, notify: bool) -> int:
    settings = Settings.from_env()
    db = Database(settings.database_path)
    await db.initialize()
    group_catalog = GroupCatalog(settings.schedule_url, db=db)
    broadcaster = Broadcaster(
        db=db,
        telegram_bot=None,
        admin_telegram_id=settings.admin_telegram_id,
        admin_vk_id=settings.admin_vk_id,
        broker=None,
    )
    jobs = ScheduleJobs.__new__(ScheduleJobs)
    jobs.db = db
    jobs.parser = ScheduleParser(settings.schedule_url)
    jobs.broadcaster = broadcaster
    importer = build_ocr_importer(settings, db, jobs, group_catalog)
    images = read_images(paths)

    if summary:
        draft = await importer.build_summary_draft(images)
        print(format_ocr_summary_preview(draft, html=False, max_length=0))
        if not apply:
            logger.info("Предпросмотр завершён: ничего не сохранено.")
            return 0
        success, report = await importer.apply_summary(draft, notify=notify)
    else:
        draft = await importer.build_draft(images)
        print(format_ocr_preview(draft, html=False, max_length=0))
        if not apply:
            logger.info("Предпросмотр завершён: ничего не сохранено.")
            return 0
        success, report = await importer.apply(draft, notify=notify)

    logger.info("%s", report)
    return 0 if success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Импорт расписания с локальных фотографий через Gemini.")
    parser.add_argument("paths", nargs="+", type=Path, help="Фото одного расписания или части общей таблицы.")
    parser.add_argument("--summary", action="store_true", help="На фото один день сразу для нескольких групп.")
    parser.add_argument("--apply", action="store_true", help="Сохранить распознанное расписание.")
    parser.add_argument("--notify", action="store_true", help="Разослать изменения подписчикам (требует --apply).")
    args = parser.parse_args()
    if args.notify and not args.apply:
        parser.error("--notify можно использовать только вместе с --apply")
    try:
        return asyncio.run(run(args.paths, summary=args.summary, apply=args.apply, notify=args.notify))
    except (OcrEngineError, OSError, ValueError) as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

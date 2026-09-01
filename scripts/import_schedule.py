"""Ручная заливка расписания из JSON, минуя распознавание фото.

Нужен, когда сайт расписания недоступен, а OCR по каким-то причинам не работает:
расписание вбивается руками один раз и дальше идёт по тому же конвейеру, что и
данные с сайта — то же хеширование, то же сохранение снимка, то же сравнение с
эталоном и та же рассылка.

Формат входного JSON:

    {
      "group": "ИСП-25-1",
      "days": [
        {
          "date": "01.09.2026",
          "lessons": [
            {"number": 1, "subject": "Физика", "teacher": "Иванов И.И.", "classroom": "301"}
          ]
        },
        {"date": "03.09.2026", "lessons": []}
      ]
    }

День с пустым списком пар означает «пар нет» и затирает этот день в расписании.
Дни, которых нет в файле, остаются нетронутыми.

Запуск внутри контейнера:

    docker compose exec bot uv run --frozen python -m scripts.import_schedule /app/runtime/schedule.json

По умолчанию снимок только сохраняется. Рассылка подписчикам включается флагом
--notify: это видимое всем действие, поэтому оно не должно происходить случайно.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from src.config import Settings
from src.db import Database
from src.group_catalog import GroupCatalog
from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.notifier import Broadcaster
from src.ocr_import import OcrScheduleImporter
from src.ocr_schedule import merge_ocr_days
from src.parser import ScheduleParser
from src.schedule_service import ScheduleFormatter
from src.scheduler import ScheduleJobs

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("import_schedule")


def parse_date(raw: str) -> tuple[str, str]:
    """Принимает 01.09.2026 или 2026-09-01, возвращает (date_iso, date_label)."""
    text = str(raw).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            parsed = datetime.strptime(text, fmt)  # noqa: DTZ007 - дата без времени
        except ValueError:
            continue
        return parsed.date().isoformat(), parsed.strftime("%d.%m.%Y")
    raise ValueError(f"Не понимаю дату {raw!r}. Ожидаю 01.09.2026 или 2026-09-01.")


def build_snapshot(payload: dict) -> ScheduleSnapshot:
    group_name = str(payload.get("group") or "").strip()
    if not group_name:
        raise ValueError("В файле не указано поле 'group' с названием группы.")

    raw_days = payload.get("days")
    if not isinstance(raw_days, list) or not raw_days:
        raise ValueError("В файле нет списка 'days' с днями расписания.")

    days: list[DaySchedule] = []
    for raw_day in raw_days:
        date_iso, date_label = parse_date(raw_day.get("date", ""))
        lessons: list[Lesson] = []
        for raw_lesson in raw_day.get("lessons", []) or []:
            number = int(raw_lesson["number"])
            subject = str(raw_lesson.get("subject") or "").strip()
            if not subject:
                raise ValueError(f"{date_label}, пара {number}: пустая дисциплина.")
            lessons.append(
                Lesson(
                    number=number,
                    subject=subject,
                    teacher=str(raw_lesson.get("teacher") or "").strip(),
                    classroom=str(raw_lesson.get("classroom") or "").strip(),
                )
            )
        numbers = [lesson.number for lesson in lessons]
        if len(numbers) != len(set(numbers)):
            raise ValueError(f"{date_label}: номера пар повторяются.")
        lessons.sort(key=lambda item: item.number)
        days.append(DaySchedule(date_label=date_label, date_iso=date_iso, lessons=lessons))

    days.sort(key=lambda day: day.date_iso)
    return ScheduleSnapshot(group_name=group_name, fetched_at=datetime.now(), days=days)


async def run(payload: dict, *, notify: bool, dry_run: bool) -> int:
    snapshot = build_snapshot(payload)

    print("Расписание к заливке:")
    for day in snapshot.days:
        print(ScheduleFormatter.format_day_plain(day))
        print()

    settings = Settings.from_env()
    db = Database(settings.database_path)
    await db.initialize()

    group_catalog = GroupCatalog(settings.schedule_url)
    parser = ScheduleParser(settings.schedule_url)
    broadcaster = Broadcaster(
        db=db,
        telegram_bot=None,
        admin_telegram_id=settings.admin_telegram_id,
        admin_vk_id=settings.admin_vk_id,
        broker=None,
    )
    jobs = ScheduleJobs.__new__(ScheduleJobs)
    jobs.db = db
    jobs.parser = parser
    jobs.broadcaster = broadcaster

    importer = OcrScheduleImporter(db, jobs, group_catalog, engine=None)
    source, error = await importer.resolve_source_by_group(snapshot.group_name)
    if source is None:
        logger.error("Группа не найдена: %s", error)
        return 1
    logger.info("Источник: %s (schedule_id=%s)", source.get("source_title"), source.get("schedule_id"))

    stored = await db.get_latest_snapshot(
        "current",
        schedule_id=source.get("schedule_id"),
        source_key=source.get("source_key"),
    )
    merged = merge_ocr_days(stored.get("content") if stored else None, snapshot)
    if merged.replaced_dates:
        logger.info("Обновятся дни: %s", ", ".join(merged.replaced_dates))
    if merged.added_dates:
        logger.info("Добавятся дни: %s", ", ".join(merged.added_dates))
    if merged.kept_dates:
        logger.info("Останутся без изменений: %s дн.", len(merged.kept_dates))

    if dry_run:
        logger.info("Пробный запуск: ничего не сохранено.")
        return 0

    change = await jobs.apply_manual_snapshot(source, merged.snapshot, notify=notify)
    if change is None:
        logger.info("Снимок сохранён. Отличий от эталона нет, рассылки не было.")
    elif notify:
        logger.info("Снимок сохранён, изменения разосланы (%s дн.).", len(change.changed_dates))
    else:
        logger.info("Снимок сохранён без рассылки (%s дн. изменений).", len(change.changed_dates))
    return 0


def main() -> int:
    argument_parser = argparse.ArgumentParser(description="Ручная заливка расписания из JSON.")
    argument_parser.add_argument("path", nargs="?", help="Путь к JSON-файлу. Без него читает stdin.")
    argument_parser.add_argument(
        "--notify",
        action="store_true",
        help="Разослать изменения подписчикам. По умолчанию снимок только сохраняется.",
    )
    argument_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет сделано, и ничего не менять.",
    )
    args = argument_parser.parse_args()

    raw = Path(args.path).read_text(encoding="utf-8") if args.path else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Файл не является корректным JSON: %s", exc)
        return 1

    try:
        return asyncio.run(run(payload, notify=args.notify, dry_run=args.dry_run))
    except ValueError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Просмотр сохранённого расписания за календарную неделю."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from src.miniapp_calendar import history_bounds, week_start
from src.models import DaySchedule
from src.schedule_archive import ScheduleDayArchive


def parse_history_date(text: str, today: date) -> date:
    try:
        selected = datetime.strptime(text.strip(), "%d.%m.%Y").date()
    except ValueError:
        selected = date.fromisoformat(text.strip())
    first, _ = history_bounds(today)
    if not first <= selected <= today:
        raise ValueError("Дата вне доступного периода")
    return week_start(selected)


async def history_week(archive: ScheduleDayArchive, key: str, title: str, monday: date, today: date) -> tuple[str, bool, bool]:
    first, last = history_bounds(today)
    monday = week_start(monday)
    if not week_start(first) <= monday <= week_start(today):
        raise ValueError("Неделя вне доступного периода")
    records = await archive.get_days(key, max(monday, first), min(monday + timedelta(days=5), today))
    by_date: dict[str, list[dict]] = {}
    for record in records:
        by_date.setdefault(record["date_iso"], []).append(record)
    weekdays = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота")
    lines = [f"История расписания: {title}", f"Неделя: {monday:%d.%m.%Y} — {monday + timedelta(days=5):%d.%m.%Y}"]
    for offset, name in enumerate(weekdays):
        current = monday + timedelta(days=offset)
        lines.append(f"\n{name}, {current:%d.%m.%Y}")
        if current < first or current > last:
            lines.append("За пределами доступной истории.")
            continue
        entries = by_date.get(current.isoformat(), [])
        if not entries:
            lines.append("Расписание за этот день не сохранено.")
            continue
        for entry in entries:
            day = DaySchedule.from_dict(entry["content"])
            if entry["week_variant"]:
                lines.append("Числитель:" if entry["week_variant"] == 1 else "Знаменатель:")
            if day.lessons:
                for lesson in sorted(day.lessons, key=lambda item: item.number):
                    lines.append(f"{lesson.number}. {lesson.subject} — {lesson.teacher}, ауд. {lesson.classroom}")
            else:
                lines.append("Пар нет." if entry["completeness"] == "complete" else "Пары не сохранены.")
    return "\n".join(lines), monday > week_start(first), monday < week_start(today)

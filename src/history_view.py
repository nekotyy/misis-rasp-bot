"""Просмотр сохранённого расписания за календарную неделю."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape

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


async def history_week(
    archive: ScheduleDayArchive,
    key: str,
    title: str,
    monday: date,
    today: date,
    *,
    html: bool = False,
) -> tuple[str, bool, bool]:
    first, last = history_bounds(today)
    monday = week_start(monday)
    if not week_start(first) <= monday <= week_start(today):
        raise ValueError("Неделя вне доступного периода")
    records = await archive.get_days(key, max(monday, first), min(monday + timedelta(days=5), today))
    by_date: dict[str, list[dict]] = {}
    for record in records:
        by_date.setdefault(record["date_iso"], []).append(record)
    weekdays = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота")
    title_line = f"<b>История расписания:</b> {escape(title)}" if html else f"История расписания: {title}"
    lines = [title_line, f"Неделя: {monday:%d.%m.%Y} — {monday + timedelta(days=5):%d.%m.%Y}"]
    for offset, name in enumerate(weekdays):
        current = monday + timedelta(days=offset)
        day_header = f"<b>{name}, {current:%d.%m.%Y}</b>" if html else f"{name}, {current:%d.%m.%Y}"
        lines.append(f"\n{day_header}")
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
                var_label = "Числитель:" if entry["week_variant"] == 1 else "Знаменатель:"
                lines.append(f"<b>{var_label}</b>" if html else var_label)
            if day.lessons:
                for lesson in sorted(day.lessons, key=lambda item: item.number):
                    if html:
                        lines.append(
                            f"<b>{lesson.number}.</b> в <b>{escape(lesson.classroom)}</b> "
                            f"по <b>{escape(lesson.subject)}</b> у <b>{escape(lesson.teacher)}</b>"
                        )
                    else:
                        lines.append(
                            f"{lesson.number}. в {lesson.classroom} "
                            f"по {lesson.subject} у {lesson.teacher}"
                        )
            else:
                lines.append("Пар нет." if entry["completeness"] == "complete" else "Пары не сохранены.")
    return "\n".join(lines), monday > week_start(first), monday < week_start(today)

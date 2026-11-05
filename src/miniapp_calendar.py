"""Календарные границы расписания и истории миниаппа."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def today_in_timezone(timezone: str, *, now: datetime | None = None) -> date:
    """Возвращает текущую дату в заданной зоне; переданный момент должен иметь зону."""
    zone = ZoneInfo(timezone)
    if now is None:
        return datetime.now(zone).date()
    if now.utcoffset() is None:
        raise ValueError("Время должно содержать часовой пояс")
    return now.astimezone(zone).date()


def subtract_calendar_months(day: date, months: int) -> date:
    """Вычитает календарные месяцы, ограничивая день длиной итогового месяца."""
    if months < 0:
        raise ValueError("Число месяцев не может быть отрицательным")
    month_index = day.year * 12 + day.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def schedule_dates(today: date) -> tuple[date, date, date]:
    """Три последовательных календарных дня, включая выходные."""
    return today, today + timedelta(days=1), today + timedelta(days=2)


def history_bounds(today: date) -> tuple[date, date]:
    """Включительные границы истории; сегодняшний день в неё не входит."""
    return subtract_calendar_months(today, 3), today - timedelta(days=1)


def week_start(day: date) -> date:
    """Понедельник недели, в которой находится дата."""
    return day - timedelta(days=day.weekday())


def history_week_days(today: date, monday: date) -> tuple[date, ...]:
    """Дни недели от понедельника до субботы в хронологическом порядке."""
    if monday.weekday() != 0:
        raise ValueError("Начало недели должно приходиться на понедельник")
    first, _ = history_bounds(today)
    if monday < week_start(first):
        return ()
    return tuple(monday + timedelta(days=offset) for offset in range(6))


def default_history_week(today: date) -> date:
    """Возвращает понедельник текущей недели."""
    return week_start(today)

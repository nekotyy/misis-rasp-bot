from __future__ import annotations

from datetime import datetime
from html import escape

from src.models import ChangeSummary, DaySchedule, Lesson, ScheduleSnapshot


class ScheduleFormatter:
    @staticmethod
    def format_day(day: DaySchedule) -> str:
        if not day.lessons:
            return f"{day.date_label}\nПар нет."
        lines = [day.date_label]
        for lesson in sorted(day.lessons, key=lambda item: item.number):
            lines.append(
                f"{lesson.number}. {lesson.subject} | {lesson.teacher} | ауд. {lesson.classroom}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_day_card(day: DaySchedule, title: str) -> str:
        safe_title = escape(title)
        safe_label = escape(day.date_label)
        if not day.lessons:
            return f"Расписание на {safe_label}\n\nПар нет."

        lines = [f"Расписание на {safe_label}", ""]
        for lesson in sorted(day.lessons, key=lambda item: item.number):
            lines.append(
                f"<b>{lesson.number}.</b> в <b>{escape(lesson.classroom)}</b> "
                f"по <b>{escape(lesson.subject)}</b> у <b>{escape(lesson.teacher)}</b>"
            )
        return "\n".join(lines)

    @staticmethod
    def format_range(days: list[DaySchedule], title: str | None = None) -> str:
        blocks = [ScheduleFormatter.format_day(day) for day in days]
        if title:
            return f"{title}\n\n" + "\n\n".join(blocks)
        return "\n\n".join(blocks)


class ScheduleComparator:
    @staticmethod
    def compare(previous: dict | None, current: ScheduleSnapshot) -> ChangeSummary | None:
        if previous is None:
            return None

        prev_days = {day["date_iso"]: day for day in previous["content"]["days"]}
        current_days = {day.date_iso: day for day in current.days}

        changed_dates: list[str] = []
        payload: dict[str, list[str]] = {}

        for date_iso, day in current_days.items():
            prev_day = prev_days.get(date_iso)
            diff_lines = ScheduleComparator._diff_day(prev_day, day)
            if diff_lines:
                changed_dates.append(day.date_label)
                payload[day.date_label] = diff_lines

        for date_iso, prev_day in prev_days.items():
            if date_iso not in current_days:
                label = prev_day["date_label"]
                changed_dates.append(label)
                payload[label] = ["Расписание на эту дату пропало с сайта."]

        if not changed_dates:
            return None

        message_lines = ["Обнаружены изменения в расписании:"]
        for date_label in changed_dates:
            message_lines.append("")
            message_lines.append(f"{date_label}")
            message_lines.extend(payload[date_label])

        return ChangeSummary(
            changed_dates=changed_dates,
            message="\n".join(message_lines),
            payload=payload,
        )

    @staticmethod
    def _diff_day(prev_day: dict | None, current_day: DaySchedule) -> list[str]:
        current_map = {
            lesson.number: (lesson.subject, lesson.teacher, lesson.classroom)
            for lesson in current_day.lessons
        }

        if prev_day is None:
            if current_map:
                return [f"Добавлены пары: {', '.join(str(number) for number in sorted(current_map))}"]
            return ["Появился пустой день."]

        prev_map = {
            lesson["number"]: (lesson["subject"], lesson["teacher"], lesson["classroom"])
            for lesson in prev_day["lessons"]
        }
        all_numbers = sorted(set(prev_map) | set(current_map))

        changes: list[str] = []
        for number in all_numbers:
            before = prev_map.get(number)
            after = current_map.get(number)
            if before == after:
                continue
            if before is None and after is not None:
                changes.append(f"Добавлена {number} пара: {after[0]} | {after[1]} | ауд. {after[2]}")
                continue
            if before is not None and after is None:
                changes.append(f"Убрана {number} пара: {before[0]} | {before[1]} | ауд. {before[2]}")
                continue
            changes.append(
                "Изменена "
                f"{number} пара: {before[0]} -> {after[0]}, {before[1]} -> {after[1]}, ауд. {before[2]} -> {after[2]}"
            )
        return changes


def filter_days(snapshot: ScheduleSnapshot, days_count: int) -> list[DaySchedule]:
    today = datetime.now().date().isoformat()
    future_days = [day for day in snapshot.days if day.date_iso >= today]
    return future_days[:days_count]


def get_day_by_offset(snapshot: ScheduleSnapshot, offset: int) -> DaySchedule | None:
    days = filter_days(snapshot, offset + 1)
    if len(days) <= offset:
        return None
    return days[offset]


def get_day_by_offset_from_content(content: dict, offset: int) -> DaySchedule | None:
    today = datetime.now().date().isoformat()
    future_days = [day for day in content.get("days", []) if day.get("date_iso", "") >= today]
    if len(future_days) <= offset:
        return None
    day = future_days[offset]
    return DaySchedule(
        date_label=day["date_label"],
        date_iso=day["date_iso"],
        lessons=[
            Lesson(
                number=lesson["number"],
                subject=lesson["subject"],
                teacher=lesson["teacher"],
                classroom=lesson["classroom"],
            )
            for lesson in day.get("lessons", [])
        ],
    )

from __future__ import annotations

from datetime import datetime, timedelta
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
    def format_day_plain(day: DaySchedule) -> str:
        if not day.lessons:
            return f"Расписание на {day.date_label}\n\nПар нет."
        lines = [f"Расписание на {day.date_label}", ""]
        for lesson in sorted(day.lessons, key=lambda item: item.number):
            lines.append(f"{lesson.number}. в {lesson.classroom} по {lesson.subject} у {lesson.teacher}")
        return "\n".join(lines)

    @staticmethod
    def format_range(days: list[DaySchedule], title: str | None = None) -> str:
        blocks = [ScheduleFormatter.format_day(day) for day in days]
        if title:
            return f"{title}\n\n" + "\n\n".join(blocks)
        return "\n\n".join(blocks)

    @staticmethod
    def format_search_snapshot(title: str, content: dict) -> str:
        lines = [f"Расписание для {title}", ""]
        days = content.get("days", [])
        added_any = False
        for day in days:
            lessons = sorted(day.get("lessons", []), key=lambda item: item["number"])
            if not lessons:
                continue
            added_any = True
            lines.append(day.get("date_iso") or day.get("date_label", ""))
            for lesson in lessons:
                lines.append(
                    f"{lesson['number']} в {lesson['classroom']} "
                    f"по {lesson['subject']} у {lesson['teacher']}"
                )
            lines.append("")
        if not added_any:
            lines.append("Пар нет.")
        elif lines[-1] == "":
            lines.pop()
        return "\n".join(lines)


class ScheduleComparator:
    @staticmethod
    def compare(previous: dict | None, current: ScheduleSnapshot) -> ChangeSummary | None:
        if previous is None:
            return None

        prev_days = {day["date_iso"]: day for day in previous["content"]["days"]}
        current_days = {day.date_iso: day for day in current.days}
        today = datetime.now().date()
        days_to_check = 3 if today.weekday() == 5 else 2
        allowed_dates = {
            (today + timedelta(days=offset)).isoformat()
            for offset in range(days_to_check)
        }

        changed_dates: list[str] = []
        payload: dict[str, str] = {}
        changed_days: list[DaySchedule] = []

        for date_iso, day in current_days.items():
            if date_iso not in allowed_dates:
                continue
            if not day.lessons:
                continue
            prev_day = prev_days.get(date_iso)
            if ScheduleComparator._day_changed(prev_day, day):
                changed_dates.append(day.date_label)
                payload[day.date_label] = ScheduleFormatter.format_day_plain(day)
                changed_days.append(day)

        if not changed_dates:
            return None

        plain_blocks = ["Обнаружены изменения в расписании!"]
        telegram_blocks = ["<b>Обнаружены изменения в расписании!</b>"]
        for day in changed_days:
            plain_blocks.extend(["", payload[day.date_label]])
            telegram_blocks.extend(["", ScheduleFormatter.format_day_card(day, day.date_label)])

        return ChangeSummary(
            changed_dates=changed_dates,
            message="\n".join(plain_blocks),
            payload=payload,
            telegram_message="\n".join(telegram_blocks),
            vk_message="\n".join(plain_blocks),
        )

    @staticmethod
    def _day_changed(prev_day: dict | None, current_day: DaySchedule) -> bool:
        current_map = {
            lesson.number: (lesson.subject, lesson.teacher, lesson.classroom)
            for lesson in current_day.lessons
        }

        if prev_day is None:
            return bool(current_map)

        prev_map = {
            lesson["number"]: (lesson["subject"], lesson["teacher"], lesson["classroom"])
            for lesson in prev_day["lessons"]
        }
        if not current_map:
            return False
        return prev_map != current_map


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

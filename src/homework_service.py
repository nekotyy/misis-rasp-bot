from __future__ import annotations

from datetime import datetime
from html import escape

from src.models import HomeworkAttachment

SUBJECTS: list[dict[str, str]] = [
    {"key": "literature", "subject": "Литература", "teacher": "Волошина Н. В."},
    {"key": "english", "subject": "Иностранный язык", "teacher": "Травкина Е. А."},
    {"key": "math", "subject": "Математика", "teacher": "Набережных И. А."},
    {"key": "history", "subject": "История", "teacher": "Цымлянская В. С."},
    {"key": "pe", "subject": "Физическая культура", "teacher": "Кузьминова И. Н."},
    {"key": "safety", "subject": "Основы без-сти и защита Родины", "teacher": "Абрюкин В. И."},
    {"key": "project", "subject": "Индивидуальный проект", "teacher": "Коренев А. М."},
    {"key": "informatics", "subject": "Информатика", "teacher": "Спицына О. И."},
    {"key": "physics", "subject": "Физика", "teacher": "Амельчакова Е. А."},
    {"key": "social", "subject": "Обществознание", "teacher": "Слободенюк Н. В."},
    {"key": "chemistry", "subject": "Химия", "teacher": "Умеренкова Т. И."},
    {"key": "biology", "subject": "Биология", "teacher": "Киреева Л. В."},
]

SUBJECTS_BY_KEY = {item["key"]: item for item in SUBJECTS}

MONTHS_RU = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def get_subject(subject_key: str) -> dict[str, str] | None:
    return SUBJECTS_BY_KEY.get(subject_key)


def format_homework_message(entry: dict) -> str:
    created_at = datetime.fromisoformat(entry["created_at"])
    author = escape(entry["created_by_name"] or "Неизвестный пользователь")
    subject = escape(entry["subject"])
    teacher = escape(entry["teacher"])
    text = escape(entry["text"])
    date_label = f"{created_at.day} {MONTHS_RU[created_at.month]} {created_at.year} года {created_at:%H:%M}"
    return (
        f"<b>{subject} — {teacher}</b> | "
        f"<tg-spoiler>№{entry['id']}</tg-spoiler> | "
        f"<tg-spoiler>{author}</tg-spoiler>\n"
        "———\n"
        f"<blockquote>{text}</blockquote>\n"
        "———\n"
        f"<tg-spoiler><b>{date_label}</b></tg-spoiler>"
    )


def format_homework_preview(
    subject_name: str,
    teacher_name: str,
    text: str,
    attachments: list[HomeworkAttachment],
    created_by_name: str,
) -> str:
    attachments_text = (
        f"\n\nВложений: <b>{len(attachments)}</b>"
        if attachments
        else "\n\nВложений пока нет."
    )
    return (
        f"<b>{escape(subject_name)} — {escape(teacher_name)}</b> | "
        "<tg-spoiler>предпросмотр</tg-spoiler> | "
        f"<tg-spoiler>{escape(created_by_name)}</tg-spoiler>\n"
        "———\n"
        f"<blockquote>{escape(text)}</blockquote>\n"
        "———\n"
        "<tg-spoiler><b>Будет сохранено после подтверждения</b></tg-spoiler>"
        f"{attachments_text}"
    )

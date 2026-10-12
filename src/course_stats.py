"""Статистика по курсам колледжа.

Курс определяется по году набора, зашитому в названии группы: «ИСП-25-1» или «ТЭС-26» —
это набор 2025/2026 годов. Учебный год начинается 1 сентября, поэтому осенью 2026 года
группы «-26» учатся на 1 курсе, «-25» — на 2-м и так далее до 4-го. Всё, что не попало
в 1–4 курсы (выпустившиеся группы, ещё не набранные, названия нестандартного вида),
в курсы не входит и выводится отдельным списком «не учтено».

Студентами здесь называются личные аккаунты бота (Telegram и VK) с подпиской на группу:
реестра настоящих студентов у бота нет. Беседы и групповые чаты считаются отдельно,
потому что одна беседа — это одна запись, а не один человек.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape
from typing import TYPE_CHECKING, Any

from src.group_catalog import GroupCatalog

if TYPE_CHECKING:
    from src.db import Database

COURSES_COUNT = 4
# Учебный год в колледже начинается 1 сентября.
ACADEMIC_YEAR_START_MONTH = 9
# Начиная с этого id VK-диалог — беседа, а не личный аккаунт.
VK_CHAT_ID_OFFSET = 2_000_000_000
MAX_IGNORED_IN_TEXT = 10

# «исп-25-1», «тэс-26», «мп-м-25»: буквы направления, год набора из двух цифр, номер группы по желанию.
_GROUP_NAME_RE = re.compile(r"^[а-я]+(?:-[а-я]+)*-(\d{2})(?:-\d{1,2})?$")
_DIGITS_RE = re.compile(r"(\d+)")


def current_academic_year(today: date) -> int:
    """Год начала текущего учебного года: с 1 сентября — текущий, до него — прошлый."""
    return today.year if today.month >= ACADEMIC_YEAR_START_MONTH else today.year - 1


def parse_admission_year(group_name: str) -> int | None:
    """Год набора из названия группы (25 -> 2025) или None, если название не стандартного вида."""
    match = _GROUP_NAME_RE.match(_compact_group_name(group_name))
    if match is None:
        return None
    return 2000 + int(match.group(1))


def course_of_group(group_name: str, today: date | None = None) -> int | None:
    """Номер курса 1..4 на сегодня; None — группа не учится сейчас или название не разобрать."""
    admission_year = parse_admission_year(group_name)
    if admission_year is None:
        return None
    course = current_academic_year(today or datetime.now().date()) - admission_year + 1
    return course if 1 <= course <= COURSES_COUNT else None


def is_chat(platform: str, user_id: int) -> bool:
    """Беседа/групповой чат (а не личный аккаунт) — по тому же правилу, что и в остальной админке."""
    return (platform == "telegram" and user_id < 0) or (platform == "vk" and user_id >= VK_CHAT_ID_OFFSET)


def _compact_group_name(name: str) -> str:
    return GroupCatalog.normalize(name).replace(" ", "")


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 группа, 2 группы, 5 групп."""
    if count % 100 in range(11, 15):
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(count % 10, many)


def _natural_key(name: str) -> list[str | int]:
    return [int(part) if part.isdigit() else part for part in _DIGITS_RE.split(GroupCatalog.normalize(name))]


@dataclass(slots=True)
class Audience:
    """Сколько личных аккаунтов и бесед на каждой платформе."""

    tg_personal: int = 0
    vk_personal: int = 0
    tg_chats: int = 0
    vk_chats: int = 0

    @property
    def personal(self) -> int:
        return self.tg_personal + self.vk_personal

    @property
    def chats(self) -> int:
        return self.tg_chats + self.vk_chats

    def add(self, platform: str, user_id: int) -> None:
        chat = is_chat(platform, user_id)
        if platform == "telegram":
            if chat:
                self.tg_chats += 1
            else:
                self.tg_personal += 1
        elif platform == "vk":
            if chat:
                self.vk_chats += 1
            else:
                self.vk_personal += 1

    def merge(self, other: Audience) -> None:
        self.tg_personal += other.tg_personal
        self.vk_personal += other.vk_personal
        self.tg_chats += other.tg_chats
        self.vk_chats += other.vk_chats


@dataclass(slots=True)
class GroupLine:
    name: str
    audience: Audience = field(default_factory=Audience)


@dataclass(slots=True)
class CourseStats:
    course: int
    admission_year: int
    groups: list[GroupLine] = field(default_factory=list)

    @property
    def audience(self) -> Audience:
        total = Audience()
        for group in self.groups:
            total.merge(group.audience)
        return total

    @property
    def groups_with_audience(self) -> int:
        return sum(1 for group in self.groups if group.audience.personal or group.audience.chats)


@dataclass(slots=True)
class TeacherStats:
    known: int = 0
    with_subscribers: int = 0
    audience: Audience = field(default_factory=Audience)


@dataclass(slots=True)
class CourseStatsReport:
    today: date
    academic_year_start: int
    courses: list[CourseStats]
    teachers: TeacherStats
    ignored_groups: list[GroupLine]

    @property
    def academic_year_label(self) -> str:
        return f"{self.academic_year_start}/{self.academic_year_start + 1}"

    @property
    def courses_audience(self) -> Audience:
        total = Audience()
        for course in self.courses:
            total.merge(course.audience)
        return total

    @property
    def courses_groups(self) -> int:
        return sum(len(course.groups) for course in self.courses)

    @property
    def everyone_personal(self) -> int:
        return self.courses_audience.personal + self.teachers.audience.personal


def build_course_stats(
    group_names: list[str],
    subscriptions: list[dict[str, Any]],
    *,
    known_teachers: int = 0,
    today: date | None = None,
) -> CourseStatsReport:
    """Собирает отчёт по курсам.

    `group_names` — каталог групп (сайт плюс подтверждённые OCR); к нему добавляются группы,
    на которые есть подписки, чтобы статистика не пустела, пока каталог не загрузился.
    """
    today = today or datetime.now().date()
    academic_year = current_academic_year(today)

    display_names: dict[str, str] = {}
    for name in group_names:
        clean = " ".join(str(name or "").split())
        if clean:
            display_names.setdefault(GroupCatalog.normalize(clean), clean)

    audiences: dict[str, Audience] = {}
    teacher_audience = Audience()
    teacher_keys: set[str] = set()
    for row in subscriptions:
        platform = str(row.get("platform") or "")
        user_id = int(row.get("user_id") or 0)
        subscription_type = row.get("subscription_type")
        title = " ".join(str(row.get("subscription_title") or row.get("group_name") or "").split())
        if subscription_type == "teacher":
            teacher_audience.add(platform, user_id)
            teacher_keys.add(str(row.get("subscription_key") or title))
        elif subscription_type == "group" and title:
            key = GroupCatalog.normalize(title)
            display_names.setdefault(key, title)
            audiences.setdefault(key, Audience()).add(platform, user_id)

    courses = [
        CourseStats(course=number, admission_year=academic_year - number + 1)
        for number in range(1, COURSES_COUNT + 1)
    ]
    ignored: list[GroupLine] = []
    for key, name in display_names.items():
        line = GroupLine(name=name, audience=audiences.get(key, Audience()))
        course = course_of_group(name, today)
        if course is None:
            ignored.append(line)
        else:
            courses[course - 1].groups.append(line)

    for course in courses:
        course.groups.sort(key=lambda line: _natural_key(line.name))
    ignored.sort(key=lambda line: _natural_key(line.name))

    return CourseStatsReport(
        today=today,
        academic_year_start=academic_year,
        courses=courses,
        teachers=TeacherStats(
            known=known_teachers,
            with_subscribers=len(teacher_keys),
            audience=teacher_audience,
        ),
        ignored_groups=ignored,
    )


async def collect_course_stats(db: Database, today: date | None = None) -> CourseStatsReport:
    catalog = await db.get_all_groups()
    subscriptions = await db.get_subscription_rows()
    teacher_targets = await db.get_search_targets("teacher")
    known_teachers = len({str(item.get("url") or item.get("title") or "") for item in teacher_targets} - {""})
    return build_course_stats(
        [str(item["group_name"]) for item in catalog],
        subscriptions,
        known_teachers=known_teachers,
        today=today,
    )


def format_course_stats_report(report: CourseStatsReport, *, html: bool) -> str:
    """Текст для админки в боте: HTML для Telegram, обычный текст для VK."""

    def bold(text: object) -> str:
        return f"<b>{text}</b>" if html else str(text)

    def safe(text: str) -> str:
        return escape(text) if html else text

    def platforms(audience: Audience, *, chats: bool = False) -> str:
        tg, vk = (audience.tg_chats, audience.vk_chats) if chats else (audience.tg_personal, audience.vk_personal)
        return f"TG {tg} / VK {vk}"

    separator = "───────────────────────────"
    lines = [
        bold("Статистика по курсам"),
        f"Учебный год {report.academic_year_label}, данные на {report.today.strftime('%d.%m.%Y')}.",
        separator,
    ]
    for course in report.courses:
        audience = course.audience
        lines.append(
            f"{bold(f'{course.course} курс')} (набор {course.admission_year}): "
            f"{bold(audience.personal)} студ. ({platforms(audience)})"
        )
        lines.append(
            f"   групп: {len(course.groups)} (с подписчиками: {course.groups_with_audience})"
            f" · бесед: {audience.chats}"
        )
    total = report.courses_audience
    lines += [
        separator,
        f"{bold('Итого за 4 курса')}: {bold(total.personal)} студ. ({platforms(total)})",
        f"   групп: {report.courses_groups} · бесед: {total.chats} ({platforms(total, chats=True)})",
        separator,
    ]
    teachers = report.teachers
    teacher_followers = teachers.audience.personal
    lines += [
        f"{bold('Преподаватели')}: {bold(teacher_followers)} "
        f"{_plural(teacher_followers, 'подписчик', 'подписчика', 'подписчиков')} ({platforms(teachers.audience)})",
        f"   в справочнике сайта: {teachers.known} · с подписчиками: {teachers.with_subscribers}"
        f" · бесед: {teachers.audience.chats}",
        separator,
        f"{bold('Всего (4 курса + преподаватели)')}: {bold(report.everyone_personal)}",
    ]
    if report.ignored_groups:
        ignored_users = sum(line.audience.personal for line in report.ignored_groups)
        shown = ", ".join(safe(line.name) for line in report.ignored_groups[:MAX_IGNORED_IN_TEXT])
        extra = len(report.ignored_groups) - MAX_IGNORED_IN_TEXT
        if extra > 0:
            shown += f" и ещё {extra}"
        ignored_count = len(report.ignored_groups)
        lines += [
            separator,
            f"Не учтено: {ignored_count} {_plural(ignored_count, 'группа', 'группы', 'групп')}"
            f" ({ignored_users} {_plural(ignored_users, 'подписчик', 'подписчика', 'подписчиков')}) —"
            f" выпустились, ещё не набраны или название нестандартное: {shown}.",
        ]
    lines += [
        separator,
        "Студенты — личные аккаунты бота с подпиской на группу. Курс считается по году набора"
        " в названии группы: осенью 2026 года «-26» — 1 курс, «-25» — 2 курс и так далее.",
    ]
    return "\n".join(lines)

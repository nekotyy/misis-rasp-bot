from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.schedule_sources import UNIVERSITY_SCHEDULE_URL, university_kind

logger = logging.getLogger(__name__)

# Пауза между источниками при ручном перепарсинге из админки. Плановая синхронизация
# ждёт 12–18 с, а ручная раньше дёргала сайт ~100 раз подряд без паузы, добивая и без
# того нестабильный сайт колледжа.
MANUAL_REFRESH_PAUSE_SECONDS = 1.5

MONTHS = {
    "января": "01",
    "февраля": "02",
    "марта": "03",
    "апреля": "04",
    "мая": "05",
    "июня": "06",
    "июля": "07",
    "августа": "08",
    "сентября": "09",
    "октября": "10",
    "ноября": "11",
    "декабря": "12",
}


def compute_snapshot_hash(snapshot: ScheduleSnapshot) -> str:
    """Каноничный хеш снимка расписания.

    Используется и парсером сайта, и импортом из фото (OCR), чтобы одинаковое
    расписание из разных источников давало одинаковый хеш.
    """
    normalized_parts: list[str] = [snapshot.group_name]
    for day in snapshot.days:
        normalized_parts.append(day.date_iso)
        if day.week_variant in (1, 2):
            normalized_parts.append(f"week_variant:{day.week_variant}")
        # Сортировка по полному кортежу, а не только по номеру: у личного расписания
        # препода несколько групп могут иметь пару с одинаковым номером в один день,
        # и без этого порядок (а значит и хеш) при одинаковом содержимом зависел бы
        # от порядка, в котором группы вернула БД — не гарантированного между вызовами.
        for lesson in sorted(
            day.lessons,
            key=lambda item: (item.number, item.subject, item.teacher, item.classroom),
        ):
            normalized_parts.append(
                "|".join(
                    [
                        str(lesson.number),
                        lesson.subject,
                        lesson.teacher,
                        lesson.classroom,
                    ]
                )
            )
    payload = "\n".join(normalized_parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ScheduleParser:
    def __init__(
        self,
        schedule_url: str,
        timeout: float = 20.0,
        request_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        university_url: str = UNIVERSITY_SCHEDULE_URL,
    ) -> None:
        self.schedule_base_url = schedule_url.rstrip("/")
        if self.schedule_base_url.rsplit("/", 1)[-1].isdigit():
            self.schedule_base_url = self.schedule_base_url.rsplit("/", 1)[0]
        self.timeout = timeout
        self.request_retries = max(1, request_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.university_url = university_url.rstrip("/")

    def build_schedule_url(self, schedule_id: int | None) -> str:
        if schedule_id is None:
            raise ValueError("schedule_id is required to build a schedule URL")
        if schedule_id < 0:
            if not self.university_url:
                raise ValueError("Источник расписания университета отключён")
            return f"{self.university_url}/group/{-schedule_id}"
        return f"{self.schedule_base_url}/{schedule_id}"

    @property
    def site_root_url(self) -> str:
        """Корень сайта (например, http://asu.sf-misis.ru/), а не /rasp без ID.

        /rasp сам по себе не существующая страница — это только префикс, к которому
        всегда приписывается конкретный schedule_id (build_schedule_url). Проверка
        живости сайта должна ходить сюда, а не на /rasp, иначе она будет получать 404
        даже когда сайт полностью работает, и слать ложные тревоги о падении.
        """
        parts = urlsplit(self.schedule_base_url)
        return f"{parts.scheme}://{parts.netloc}/"

    async def fetch_html(self, schedule_id: int | None) -> str:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await self._get_with_retry(client, self.build_schedule_url(schedule_id))
            response.encoding = "utf-8"
            return response.text

    async def fetch_html_from_url(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await self._get_with_retry(client, url)
            response.encoding = "utf-8"
            return response.text

    async def _get_with_retry(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        last_exc: httpx.HTTPError | None = None
        for attempt in range(1, self.request_retries + 1):
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_exc = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                # 4xx (кроме 429) повтором не лечится — страницы просто нет.
                if attempt >= self.request_retries or (status is not None and 400 <= status < 500 and status != 429):
                    break
                logger.warning("Ошибка загрузки %s (попытка %s/%s): %s", url, attempt, self.request_retries, exc)
                await asyncio.sleep(self.retry_backoff_seconds * attempt)
        assert last_exc is not None
        raise last_exc

    async def parse(self, schedule_id: int | None) -> tuple[ScheduleSnapshot, str]:
        if schedule_id is not None and schedule_id < 0:
            return await self.parse_from_url(self.build_schedule_url(schedule_id))
        html = await self.fetch_html(schedule_id)
        snapshot = self.parse_html(html)
        return snapshot, self.compute_hash(snapshot)

    async def parse_from_url(self, url: str) -> tuple[ScheduleSnapshot, str]:
        html = await self.fetch_html_from_url(url)
        soup = BeautifulSoup(html, "html.parser")
        if university_kind(url) and soup.select_one("#schedule-table") is None:
            raise ValueError("Страница университета не содержит таблицу расписания")
        if soup.select_one("#schedule-table") is not None:
            reference = datetime.now()
            snapshot = self._parse_university_html(soup, reference)
            active = soup.select_one("#schedule-title .search-button-active[onclick]")
            match = re.search(r"/([12])['\"]", active.get("onclick", "")) if active else None
            if match is None:
                raise ValueError("Не удалось определить числитель/знаменатель расписания университета")
            other = 3 - int(match[1])
            other_url = re.sub(r"/[12]$", "", url.rstrip("/")) + f"/{other}"
            other_html = await self.fetch_html_from_url(other_url)
            other_soup = BeautifulSoup(other_html, "html.parser")
            next_week = self._parse_university_html(other_soup, reference)
            if {day.date_iso for day in snapshot.days} & {day.date_iso for day in next_week.days}:
                raise ValueError("Сайт вернул одинаковую неделю для числителя и знаменателя")
            snapshot.days.extend(next_week.days)
            snapshot.days.sort(key=lambda day: day.date_iso)
            return snapshot, self.compute_hash(snapshot)
        snapshot = self.parse_html(html)
        return snapshot, self.compute_hash(snapshot)

    def parse_html(self, html: str) -> ScheduleSnapshot:
        soup = BeautifulSoup(html, "html.parser")
        if soup.select_one("#schedule-table") is not None:
            return self._parse_university_html(soup, datetime.now())
        title = soup.find("div", id="titleF")
        group_name = title.get_text(strip=True) if title else "Неизвестная группа"

        day_nodes = soup.select("div.titleDate")
        days: list[DaySchedule] = []
        for day_node in day_nodes:
            label = day_node.get_text(" ", strip=True)
            next_rasp = day_node.find_next_sibling("div", class_="rasp")
            if next_rasp is None:
                continue

            rows = next_rasp.select("table tr")[1:]
            lessons: list[Lesson] = []
            for row in rows:
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
                if len(cells) < 4 or not any(cells):
                    continue
                try:
                    lesson_number = int(cells[0])
                except ValueError:
                    continue
                lessons.append(
                    Lesson(
                        number=lesson_number,
                        subject=cells[1],
                        teacher=cells[2],
                        classroom=cells[3],
                    )
                )

            days.append(
                DaySchedule(
                    date_label=label,
                    date_iso=self._date_label_to_iso(label),
                    lessons=lessons,
                )
            )

        return ScheduleSnapshot(group_name=group_name, fetched_at=datetime.now(), days=days)

    async def parse_university_variant(self, url: str, variant: int, target_date: str) -> ScheduleSnapshot:
        if variant not in (1, 2):
            raise ValueError("Вариант недели должен быть 1 или 2")
        reference = datetime.fromisoformat(target_date)
        url = re.sub(r"/[12]$", "", url.rstrip("/")) + f"/{variant}"
        html = await self.fetch_html_from_url(url)
        soup = BeautifulSoup(html, "html.parser")
        active = soup.select_one("#schedule-title .search-button-active[onclick]")
        if active is None or not re.search(rf"/{variant}['\"]", active.get("onclick", "")):
            raise ValueError("Сайт не вернул выбранный вариант недели")
        return self._parse_university_html(soup, reference, align_current_week=True)

    def _parse_university_html(
        self, soup: BeautifulSoup, reference: datetime, *, align_current_week: bool = False,
    ) -> ScheduleSnapshot:
        table = soup.select_one("#schedule-table")
        title = soup.select_one("#title-value")
        info = soup.select_one("#schedule-title .misc-schedule > span")
        if table is None or title is None or info is None:
            raise ValueError("Неполная страница расписания университета")
        text = " ".join(info.get_text(" ", strip=True).split())
        parity = re.search(r"На этой неделе учимся по (числителю|знаменателю).*показано по (числителю|знаменателю)", text)
        if parity is None:
            raise ValueError("Неизвестная неделя расписания университета")
        monday = reference.date() - timedelta(days=reference.weekday())
        if not align_current_week and parity[1] != parity[2]:
            monday += timedelta(days=7)
        weekdays = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
        numbers = [int(node.get_text(strip=True)) for node in table.select(".table-lesson-num")]
        if not numbers or len(set(numbers)) != len(numbers) or any(number <= 0 for number in numbers):
            raise ValueError("Не удалось прочитать номера пар университета")
        days: list[DaySchedule] = []
        for row in table.select("tr"):
            weekday = row.select_one(".table-weekdays")
            if weekday is None:
                continue
            name = weekday.get_text(" ", strip=True).casefold()
            if name not in weekdays:
                raise ValueError(f"Неизвестный день недели: {name}")
            cells = row.find_all("td", recursive=False)
            if len(cells) != len(numbers):
                raise ValueError("Изменилась структура таблицы расписания университета")
            lessons: list[Lesson] = []
            for number, cell in zip(numbers, cells, strict=True):
                for block in cell.select(".table-subgroup-item") or [cell]:
                    def value(selector: str, block=block) -> str:
                        node = block.select_one(selector)
                        return " ".join(node.get_text(" ", strip=True).split()) if node else ""

                    subject = value(".table-subject")
                    if not subject:
                        if "table-empty" not in cell.get("class", []) and block.select_one(".schedule-subgroup-empty") is None:
                            raise ValueError("Не удалось прочитать занятие университета")
                        continue
                    details = [value(".table-lessontype"), value(".table-sg-name")]
                    suffix = ", ".join(item for item in details if item)
                    lessons.append(
                        Lesson(
                            number=number,
                            subject=f"{subject} ({suffix})" if suffix else subject,
                            teacher=value(".table-teacher"),
                            classroom=value(".table-room"),
                        )
                    )
            day = monday + timedelta(days=weekdays.index(name))
            variants = {"числителю": 1, "знаменателю": 2}
            days.append(DaySchedule(day.isoformat(), day.isoformat(), lessons,
                                    week_variant=variants[parity[2]], current_week_variant=variants[parity[1]]))
        if len(days) != 7 or len({day.date_iso for day in days}) != 7:
            raise ValueError("Неполная неделя расписания университета")
        group_name = title.get_text(" ", strip=True).removeprefix("Расписание для ")
        return ScheduleSnapshot(group_name, reference, days)

    def compute_hash(self, snapshot: ScheduleSnapshot) -> str:
        return compute_snapshot_hash(snapshot)

    def _date_label_to_iso(self, label: str, now: datetime | None = None) -> str:
        parts = label.lower().replace(",", " ").split()
        if len(parts) < 2:
            return label
        day = parts[0].zfill(2)
        month = MONTHS.get(parts[1], "01")

        explicit_year: int | None = None
        for part in parts[2:]:
            if part.isdigit() and len(part) == 4:
                explicit_year = int(part)
                break

        if explicit_year is not None:
            year = explicit_year
        else:
            reference = now or datetime.now()
            current_year = reference.year
            current_month = reference.month
            parsed_month = int(month) if month.isdigit() else 1

            if current_month == 12 and parsed_month in (1, 2):
                year = current_year + 1
            elif current_month == 1 and parsed_month in (11, 12):
                year = current_year - 1
            else:
                year = current_year

        return f"{year:04d}-{month}-{day}"

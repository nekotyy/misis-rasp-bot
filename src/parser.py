from __future__ import annotations

import hashlib
import asyncio
from datetime import datetime
import logging

import httpx
from bs4 import BeautifulSoup

from src.models import DaySchedule, Lesson, ScheduleSnapshot

logger = logging.getLogger(__name__)

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


class ScheduleParser:
    def __init__(
        self,
        schedule_url: str,
        timeout: float = 20.0,
        request_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self.schedule_base_url = schedule_url.rstrip("/")
        if self.schedule_base_url.rsplit("/", 1)[-1].isdigit():
            self.schedule_base_url = self.schedule_base_url.rsplit("/", 1)[0]
        self.timeout = timeout
        self.request_retries = max(1, request_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    def build_schedule_url(self, schedule_id: int) -> str:
        return f"{self.schedule_base_url}/{schedule_id}"

    async def fetch_html(self, schedule_id: int) -> str:
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
                if attempt >= self.request_retries:
                    break
                logger.warning("Ошибка загрузки %s (попытка %s/%s): %s", url, attempt, self.request_retries, exc)
                await asyncio.sleep(self.retry_backoff_seconds * attempt)
        assert last_exc is not None
        raise last_exc

    async def parse(self, schedule_id: int) -> tuple[ScheduleSnapshot, str]:
        html = await self.fetch_html(schedule_id)
        snapshot = self.parse_html(html)
        return snapshot, self.compute_hash(snapshot)

    async def parse_from_url(self, url: str) -> tuple[ScheduleSnapshot, str]:
        html = await self.fetch_html_from_url(url)
        snapshot = self.parse_html(html)
        return snapshot, self.compute_hash(snapshot)

    def parse_html(self, html: str) -> ScheduleSnapshot:
        soup = BeautifulSoup(html, "html.parser")
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

    def compute_hash(self, snapshot: ScheduleSnapshot) -> str:
        normalized_parts: list[str] = [snapshot.group_name]
        for day in snapshot.days:
            normalized_parts.append(day.date_iso)
            for lesson in sorted(day.lessons, key=lambda item: item.number):
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

    def _date_label_to_iso(self, label: str) -> str:
        parts = label.lower().replace(",", " ").split()
        if len(parts) < 2:
            return label
        day = parts[0].zfill(2)
        month = MONTHS.get(parts[1], "01")
        year = str(datetime.now().year)
        return f"{year}-{month}-{day}"

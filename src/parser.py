from __future__ import annotations

import hashlib
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from src.models import DaySchedule, Lesson, ScheduleSnapshot

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
    def __init__(self, schedule_url: str, timeout: float = 20.0) -> None:
        self.schedule_url = schedule_url
        self.timeout = timeout

    async def fetch_html(self) -> str:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(self.schedule_url)
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text

    async def parse(self) -> tuple[ScheduleSnapshot, str]:
        html = await self.fetch_html()
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

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GroupInfo:
    department_id: int
    department_code: str
    department_name: str
    group_name: str
    schedule_id: int
    url: str


class GroupCatalog:
    def __init__(
        self,
        schedule_url: str,
        timeout: float = 30.0,
        request_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        parts = urlsplit(schedule_url)
        self.base_origin = f"{parts.scheme}://{parts.netloc}"
        self.timeout = timeout
        self.request_retries = max(1, request_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._lock = asyncio.Lock()
        self._loaded = False
        self._groups_by_name: dict[str, GroupInfo] = {}
        self._groups_by_schedule_id: dict[int, GroupInfo] = {}

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        await self.refresh()

    async def refresh(self) -> None:
        async with self._lock:
            if self._loaded:
                return

            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                try:
                    root_response = await self._get_with_retry(client, f"{self.base_origin}/")
                    root_soup = BeautifulSoup(root_response.content, "html.parser")
                except httpx.HTTPError:
                    logger.exception("Не удалось загрузить список отделений с %s", self.base_origin)
                    if not self._loaded:
                        self._groups_by_name = {}
                        self._groups_by_schedule_id = {}
                        self._loaded = True
                    return

                departments: list[tuple[int, str]] = []
                for link in root_soup.select("a[href^='/group/']"):
                    href = link.get("href", "")
                    department_id = href.rsplit("/", 1)[-1]
                    if not department_id.isdigit():
                        continue
                    departments.append((int(department_id), link.get_text(" ", strip=True)))

                groups_by_name: dict[str, GroupInfo] = {}
                groups_by_schedule_id: dict[int, GroupInfo] = {}
                for department_id, department_code in sorted(set(departments)):
                    try:
                        response = await self._get_with_retry(client, f"{self.base_origin}/group/{department_id}")
                    except httpx.HTTPError:
                        logger.warning("Пропускаю отделение id=%s из-за ошибки сети", department_id)
                        continue
                    soup = BeautifulSoup(response.content, "html.parser")
                    department_name_node = soup.find(id="titleS")
                    department_name = department_name_node.get_text(" ", strip=True) if department_name_node else ""
                    for link in soup.select("a[href^='/rasp/']"):
                        href = link.get("href", "")
                        schedule_id = href.rsplit("/", 1)[-1]
                        group_name = link.get_text(" ", strip=True)
                        if not schedule_id.isdigit() or not group_name:
                            continue
                        group = GroupInfo(
                            department_id=department_id,
                            department_code=department_code,
                            department_name=department_name,
                            group_name=group_name,
                            schedule_id=int(schedule_id),
                            url=f"{self.base_origin}/rasp/{schedule_id}",
                        )
                        groups_by_name[self.normalize(group_name)] = group
                        groups_by_schedule_id[group.schedule_id] = group

                self._groups_by_name = groups_by_name
                self._groups_by_schedule_id = groups_by_schedule_id
                self._loaded = True

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
                await asyncio.sleep(self.retry_backoff_seconds * attempt)
        assert last_exc is not None
        raise last_exc

    async def list_groups(self) -> list[GroupInfo]:
        await self.ensure_loaded()
        return sorted(
            self._groups_by_schedule_id.values(),
            key=lambda item: (item.department_code, item.group_name),
        )

    async def find_group(self, group_name: str) -> GroupInfo | None:
        await self.ensure_loaded()
        return self._groups_by_name.get(self.normalize(group_name))

    async def get_by_schedule_id(self, schedule_id: int | None) -> GroupInfo | None:
        if schedule_id is None:
            return None
        await self.ensure_loaded()
        return self._groups_by_schedule_id.get(schedule_id)

    @staticmethod
    def normalize(value: str) -> str:
        normalized = value.strip().casefold().replace("ё", "е")
        for dash in ("—", "–", "‑", "−"):
            normalized = normalized.replace(dash, "-")
        return " ".join(normalized.split())

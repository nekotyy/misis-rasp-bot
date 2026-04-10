from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from src.group_catalog import GroupCatalog


@dataclass(slots=True)
class SearchTarget:
    kind: str
    title: str
    url: str


class ScheduleSearchCatalog:
    def __init__(self, schedule_url: str, group_catalog: GroupCatalog, timeout: float = 30.0) -> None:
        parts = urlsplit(schedule_url)
        self.base_origin = f"{parts.scheme}://{parts.netloc}"
        self.group_catalog = group_catalog
        self.timeout = timeout
        self._prep_lock = asyncio.Lock()
        self._aud_lock = asyncio.Lock()
        self._preps_loaded = False
        self._auds_loaded = False
        self._preps: dict[str, SearchTarget] = {}
        self._auds: dict[str, SearchTarget] = {}

    async def find(self, query: str) -> SearchTarget | None:
        normalized = self.normalize(query)
        group = await self.group_catalog.find_group(query)
        if group is not None:
            return SearchTarget(kind="group", title=group.group_name, url=group.url)

        await self._ensure_preps_loaded()
        prep = self._preps.get(normalized)
        if prep is not None:
            return prep

        await self._ensure_auds_loaded()
        return self._auds.get(normalized)

    async def _ensure_preps_loaded(self) -> None:
        if self._preps_loaded:
            return
        async with self._prep_lock:
            if self._preps_loaded:
                return
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(f"{self.base_origin}/prep")
                response.raise_for_status()
                response.encoding = "utf-8"
                soup = BeautifulSoup(response.text, "html.parser")
                for link in soup.select("a[href^='/raspprep/']"):
                    title = link.get_text(" ", strip=True)
                    href = link.get("href", "")
                    if not title or not href:
                        continue
                    self._preps[self.normalize(title)] = SearchTarget(
                        kind="teacher",
                        title=title,
                        url=f"{self.base_origin}{href}",
                    )
                self._preps_loaded = True

    async def _ensure_auds_loaded(self) -> None:
        if self._auds_loaded:
            return
        async with self._aud_lock:
            if self._auds_loaded:
                return
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(f"{self.base_origin}/aud")
                response.raise_for_status()
                response.encoding = "utf-8"
                soup = BeautifulSoup(response.text, "html.parser")
                for link in soup.select("a[href^='/raspAud/']"):
                    title = link.get_text(" ", strip=True)
                    href = link.get("href", "")
                    if not title or not href:
                        continue
                    self._auds[self.normalize(title)] = SearchTarget(
                        kind="audience",
                        title=title,
                        url=f"{self.base_origin}{href}",
                    )
                self._auds_loaded = True

    @staticmethod
    def normalize(value: str) -> str:
        normalized = value.strip().casefold().replace("ё", "е")
        for dash in ("—", "–", "‑", "−"):
            normalized = normalized.replace(dash, "-")
        return " ".join(normalized.split())

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata
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
    def __init__(
        self,
        schedule_url: str,
        group_catalog: GroupCatalog,
        timeout: float = 30.0,
        request_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        parts = urlsplit(schedule_url)
        self.base_origin = f"{parts.scheme}://{parts.netloc}"
        self.group_catalog = group_catalog
        self.timeout = timeout
        self.request_retries = max(1, request_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._prep_lock = asyncio.Lock()
        self._aud_lock = asyncio.Lock()
        self._preps_loaded = False
        self._auds_loaded = False
        self._preps: dict[str, SearchTarget] = {}
        self._auds: dict[str, SearchTarget] = {}
        self._prep_items: list[tuple[str, SearchTarget]] = []
        self._aud_items: list[tuple[str, SearchTarget]] = []

    async def find(self, query: str) -> SearchTarget | None:
        normalized = self.normalize(query)
        group = await self.group_catalog.find_group(query)
        if group is not None:
            return SearchTarget(kind="group", title=group.group_name, url=group.url)

        await self._ensure_preps_loaded()
        prep = self._preps.get(normalized)
        if prep is not None:
            return prep
        prep = self._find_partial(normalized, self._prep_items)
        if prep is not None:
            return prep

        await self._ensure_auds_loaded()
        aud = self._auds.get(normalized)
        if aud is not None:
            return aud
        return self._find_partial(normalized, self._aud_items)

    async def _ensure_preps_loaded(self) -> None:
        if self._preps_loaded:
            return
        async with self._prep_lock:
            if self._preps_loaded:
                return
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await self._get_with_retry(client, f"{self.base_origin}/prep")
                soup = BeautifulSoup(response.content, "html.parser")
                for link in soup.select("a[href^='/raspprep/']"):
                    title = link.get_text(" ", strip=True)
                    href = link.get("href", "")
                    if not title or not href:
                        continue
                    target = SearchTarget(
                        kind="teacher",
                        title=title,
                        url=f"{self.base_origin}{href}",
                    )
                    for normalized_title in self._teacher_search_keys(title):
                        self._preps[normalized_title] = target
                        self._prep_items.append((normalized_title, target))
                self._preps_loaded = True

    async def _ensure_auds_loaded(self) -> None:
        if self._auds_loaded:
            return
        async with self._aud_lock:
            if self._auds_loaded:
                return
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await self._get_with_retry(client, f"{self.base_origin}/aud")
                soup = BeautifulSoup(response.content, "html.parser")
                for link in soup.select("a[href^='/raspAud/']"):
                    title = link.get_text(" ", strip=True)
                    href = link.get("href", "")
                    if not title or not href:
                        continue
                    normalized_title = self.normalize(title)
                    target = SearchTarget(
                        kind="audience",
                        title=title,
                        url=f"{self.base_origin}{href}",
                    )
                    self._auds[normalized_title] = target
                    self._aud_items.append((normalized_title, target))
                self._auds_loaded = True

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

    def _find_partial(self, normalized: str, items: list[tuple[str, SearchTarget]]) -> SearchTarget | None:
        if not normalized:
            return None
        normalized_compact = self._compact_name_key(normalized)
        exact_word_matches: list[SearchTarget] = []
        startswith_matches: list[SearchTarget] = []
        contains_matches: list[SearchTarget] = []
        for candidate_text, target in items:
            parts = candidate_text.split()
            candidate_compact = self._compact_name_key(candidate_text)
            if normalized in parts or normalized_compact == candidate_compact:
                exact_word_matches.append(target)
                continue
            if (
                any(part.startswith(normalized) for part in parts)
                or candidate_text.startswith(normalized)
                or candidate_compact.startswith(normalized_compact)
            ):
                startswith_matches.append(target)
                continue
            if normalized in candidate_text or normalized_compact in candidate_compact:
                contains_matches.append(target)
        for matches in (exact_word_matches, startswith_matches, contains_matches):
            unique_matches = {match.url: match for match in matches}
            if len(unique_matches) == 1:
                return next(iter(unique_matches.values()))
        fuzzy_match = self._find_fuzzy(normalized_compact, items)
        if fuzzy_match is not None:
            return fuzzy_match
        return None

    def _find_fuzzy(self, normalized_compact: str, items: list[tuple[str, SearchTarget]]) -> SearchTarget | None:
        if len(normalized_compact) < 5:
            return None

        ranked: dict[str, tuple[float, SearchTarget]] = {}
        for candidate_text, target in items:
            candidate_compact = self._compact_name_key(candidate_text)
            if not candidate_compact:
                continue
            ratio = SequenceMatcher(None, normalized_compact, candidate_compact).ratio()
            if ratio < 0.82:
                continue
            existing = ranked.get(target.url)
            if existing is None or ratio > existing[0]:
                ranked[target.url] = (ratio, target)

        if not ranked:
            return None

        ordered = sorted(ranked.values(), key=lambda item: item[0], reverse=True)
        best_ratio, best_target = ordered[0]
        if len(ordered) == 1:
            return best_target if best_ratio >= 0.86 else None

        second_ratio = ordered[1][0]
        if best_ratio >= 0.9 and best_ratio - second_ratio >= 0.03:
            return best_target
        return None

    def _teacher_search_keys(self, title: str) -> set[str]:
        normalized = self.normalize(title)
        keys = {normalized, self._compact_name_key(normalized)}
        parts = normalized.split()
        if parts:
            keys.add(parts[0])
        return {key for key in keys if key}

    @staticmethod
    def _compact_name_key(value: str) -> str:
        return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)

    @staticmethod
    def normalize(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).strip().translate(_LATIN_TO_CYRILLIC).casefold().replace("ё", "е")
        for dash in ("—", "–", "‑", "−"):
            normalized = normalized.replace(dash, "-")
        normalized = re.sub(r"\s*-\s*", "-", normalized, flags=re.UNICODE)
        normalized = re.sub(r"(?<=\w)\.(?=\w)", ". ", normalized, flags=re.UNICODE)
        normalized = re.sub(r"[^\w\s.-]+", " ", normalized, flags=re.UNICODE)
        return " ".join(normalized.split())


_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А",
        "a": "а",
        "B": "В",
        "E": "Е",
        "e": "е",
        "K": "К",
        "k": "к",
        "M": "М",
        "H": "Н",
        "O": "О",
        "o": "о",
        "P": "Р",
        "p": "р",
        "C": "С",
        "c": "с",
        "T": "Т",
        "y": "у",
        "X": "Х",
        "x": "х",
    }
)

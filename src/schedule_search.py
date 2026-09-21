from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from src.db import Database
from src.group_catalog import GroupCatalog
from src.http_retry import get_with_retry
from src.lesson_counters import normalize_lesson_text, teacher_matches
from src.text_normalize import LATIN_TO_CYRILLIC, normalize_dashes, strip_non_word_chars

logger = logging.getLogger(__name__)


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
        db: Database | None = None,
    ) -> None:
        parts = urlsplit(schedule_url)
        self.base_origin = f"{parts.scheme}://{parts.netloc}"
        self.group_catalog = group_catalog
        self.timeout = timeout
        self.request_retries = max(1, request_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.db = db
        self._prep_lock = asyncio.Lock()
        self._aud_lock = asyncio.Lock()
        self._preps_loaded = False
        self._auds_loaded = False
        self._preps: dict[str, SearchTarget] = {}
        self._auds: dict[str, SearchTarget] = {}
        self._prep_items: list[tuple[str, SearchTarget]] = []
        self._aud_items: list[tuple[str, SearchTarget]] = []
        self._ambiguous_preps: set[str] = set()

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
        prep = await self._find_teacher_from_groups(query)
        if prep is not None:
            return prep

        await self._ensure_auds_loaded()
        aud = self._auds.get(normalized)
        if aud is not None:
            return aud
        return self._find_partial(normalized, self._aud_items)

    async def _ensure_preps_loaded(self) -> None:
        if self._preps_loaded and self._prep_items:
            return
        async with self._prep_lock:
            if self._preps_loaded and self._prep_items:
                return
            try:
                pairs = await self._fetch_pairs("/prep", "a[href^='/raspprep/']")
            except Exception as exc:
                if not self._prep_items and self.db is not None:
                    cached = await self.db.get_search_targets("teacher")
                    if cached:
                        self._populate_preps([(item["title"], item["url"]) for item in cached])
                        logger.warning(
                            "Не удалось загрузить список преподавателей с сайта (%s),"
                            " использую сохранённый в БД снимок (%s записей).",
                            exc,
                            len(cached),
                        )
                        self._preps_loaded = True
                        return
                logger.warning("Не удалось загрузить список преподавателей с сайта: %s", exc)
                self._preps_loaded = True
                return
            self._populate_preps(pairs)
            self._preps_loaded = True
            if self.db is not None:
                await self._save_pairs_to_db("teacher", pairs)

    async def _find_teacher_from_groups(self, raw_query: str) -> SearchTarget | None:
        """Резервный поиск препода по ФИО, встречающимся в уже известных группах в БД.

        Справочник `/prep` — это отдельная страница сайта, и он может быть недоступен
        (сайт лежит, а свой кэш ещё не заполнен — свежий, только что после переезда на
        БД) даже когда сами группы прекрасно синхронизируются (с сайта или из OCR).
        Раз мы всё равно храним пары каждой группы и умеем находить среди них препода
        по ФИО (см. build_teacher_schedule_snapshot), тем же способом можно найти его
        и для регистрации — не только для уже существующей подписки.
        """
        if self.db is None:
            return None
        query_norm = normalize_lesson_text(raw_query)
        if not query_norm:
            return None

        variants_by_name: dict[str, Counter[str]] = {}
        for group_snapshot in await self.db.get_latest_group_snapshots("current"):
            for day in group_snapshot["content"].get("days", []):
                for lesson in day.get("lessons", []):
                    raw_name = str(lesson.get("teacher") or "").strip()
                    if not raw_name:
                        continue
                    normalized_name = normalize_lesson_text(raw_name)
                    if not normalized_name:
                        continue
                    variants_by_name.setdefault(normalized_name, Counter())[raw_name] += 1

        matches = {
            normalized_name: variants.most_common(1)[0][0]
            for normalized_name, variants in variants_by_name.items()
            if teacher_matches(query_norm, variants.most_common(1)[0][0])
        }
        if len(matches) != 1:
            # Пусто — не нашли; больше одного — неоднозначная фамилия, как и в /prep,
            # лучше попросить уточнить, чем угадать не того человека.
            return None
        best_raw_name = next(iter(matches.values()))
        return SearchTarget(kind="teacher", title=best_raw_name, url="")

    def _populate_preps(self, pairs: list[tuple[str, str]]) -> None:
        self._preps = {}
        self._prep_items = []
        self._ambiguous_preps = set()
        for title, url in pairs:
            target = SearchTarget(kind="teacher", title=title, url=url)
            for normalized_title in self._teacher_search_keys(title):
                if normalized_title in self._preps and self._preps[normalized_title].url != target.url:
                    self._ambiguous_preps.add(normalized_title)
                    self._preps.pop(normalized_title, None)
                elif normalized_title not in self._ambiguous_preps:
                    self._preps[normalized_title] = target
                self._prep_items.append((normalized_title, target))

    async def _ensure_auds_loaded(self) -> None:
        if self._auds_loaded and self._aud_items:
            return
        async with self._aud_lock:
            if self._auds_loaded and self._aud_items:
                return
            try:
                pairs = await self._fetch_pairs("/aud", "a[href^='/raspAud/']")
            except Exception as exc:
                if not self._aud_items and self.db is not None:
                    cached = await self.db.get_search_targets("audience")
                    if cached:
                        self._populate_auds([(item["title"], item["url"]) for item in cached])
                        logger.warning(
                            "Не удалось загрузить список аудиторий с сайта (%s),"
                            " использую сохранённый в БД снимок (%s записей).",
                            exc,
                            len(cached),
                        )
                        self._auds_loaded = True
                        return
                logger.warning("Не удалось загрузить список аудиторий с сайта: %s", exc)
                self._auds_loaded = True
                return
            self._populate_auds(pairs)
            self._auds_loaded = True
            if self.db is not None:
                await self._save_pairs_to_db("audience", pairs)

    def _populate_auds(self, pairs: list[tuple[str, str]]) -> None:
        self._auds = {}
        self._aud_items = []
        for title, url in pairs:
            normalized_title = self.normalize(title)
            target = SearchTarget(kind="audience", title=title, url=url)
            self._auds[normalized_title] = target
            self._aud_items.append((normalized_title, target))

    async def _fetch_pairs(self, path: str, link_selector: str) -> list[tuple[str, str]]:
        """Скачивает справочник (преподаватели/аудитории) и достаёт из него пары (название, ссылка)."""
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await self._get_with_retry(client, f"{self.base_origin}{path}")
            soup = BeautifulSoup(response.content, "html.parser")
            pairs: list[tuple[str, str]] = []
            for link in soup.select(link_selector):
                title = link.get_text(" ", strip=True)
                href = link.get("href", "")
                if not title or not href:
                    continue
                pairs.append((title, f"{self.base_origin}{href}"))
            return pairs

    async def _save_pairs_to_db(self, kind: str, pairs: list[tuple[str, str]]) -> None:
        if self.db is None:
            return
        try:
            await self.db.save_search_targets(kind, [{"title": title, "url": url} for title, url in pairs])
        except Exception:
            logger.warning("Не удалось сохранить справочник (%s) в БД.", kind, exc_info=True)

    async def _get_with_retry(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        return await get_with_retry(client, url, retries=self.request_retries, backoff_seconds=self.retry_backoff_seconds)

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
        return strip_non_word_chars(value)

    @staticmethod
    def normalize(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).strip().translate(LATIN_TO_CYRILLIC).casefold().replace("ё", "е")
        normalized = normalize_dashes(normalized)
        normalized = re.sub(r"\s*-\s*", "-", normalized, flags=re.UNICODE)
        normalized = re.sub(r"(?<=\w)\.(?=\w)", ". ", normalized, flags=re.UNICODE)
        normalized = re.sub(r"[^\w\s.-]+", " ", normalized, flags=re.UNICODE)
        return " ".join(normalized.split())

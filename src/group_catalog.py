from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from src.db import Database
from src.http_retry import get_with_retry
from src.text_normalize import LATIN_TO_CYRILLIC, normalize_dashes, strip_non_word_chars

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GroupInfo:
    department_id: int
    department_code: str
    department_name: str
    group_name: str
    schedule_id: int | None
    url: str


class GroupCatalog:
    def __init__(
        self,
        schedule_url: str,
        timeout: float = 30.0,
        request_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        db: Database | None = None,
    ) -> None:
        parts = urlsplit(schedule_url)
        self.base_origin = f"{parts.scheme}://{parts.netloc}"
        self.timeout = timeout
        self.request_retries = max(1, request_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.db = db
        self._lock = asyncio.Lock()
        self._loaded = False
        self.last_error: Exception | None = None
        self._groups_by_name: dict[str, GroupInfo] = {}
        self._groups_by_compact_name: dict[str, GroupInfo] = {}
        self._groups_by_schedule_id: dict[int, GroupInfo] = {}
        self._department_codes: dict[int, str] = {}
        self._failed_department_ids: set[int] = set()

    def __len__(self) -> int:
        return len(self._groups_by_name)

    async def ensure_loaded(self) -> None:
        if self._loaded and self._groups_by_name:
            return
        await self.refresh()

    async def refresh(self, *, force: bool = False) -> None:
        """Обновляет каталог с сайта.

        `force=True` заставляет заново сходить на сайт, даже если каталог уже
        загружен — нужно для периодического обновления по расписанию, а не
        только по факту первого обращения.
        """
        async with self._lock:
            if not force and self._loaded and self._groups_by_name:
                return

            try:
                groups_by_name, groups_by_schedule_id, failed_department_ids = await self._fetch_from_site()
            except Exception as exc:
                logger.exception("Не удалось загрузить список отделений с %s: %s", self.base_origin, exc)
                self.last_error = exc
                if await self._load_from_db():
                    logger.warning(
                        "Сайт расписания недоступен, использую сохранённый в БД каталог групп."
                        " Он может немного отставать от реального сайта.",
                    )
                    self._loaded = True
                    return
                if await self._bootstrap_from_subscriptions():
                    logger.warning(
                        "Каталог групп в БД пуст, а сайт недоступен — восстановил его из уже существующих"
                        " подписок пользователей. Неполно (нет групп без подписчиков), но лучше пустоты.",
                    )
                    self._loaded = True
                    return
                if not self._loaded:
                    self._groups_by_name = {}
                    self._groups_by_schedule_id = {}
                    self._groups_by_compact_name = {}
                    self._loaded = True
                return

            self._groups_by_name = groups_by_name
            self._groups_by_compact_name = {
                self._compact_name_key(group.group_name): group for group in groups_by_schedule_id.values()
            }
            self._groups_by_schedule_id = groups_by_schedule_id
            self._failed_department_ids = failed_department_ids
            self.last_error = None
            self._loaded = True
            await self._save_to_db()

    async def _fetch_from_site(self) -> tuple[dict[str, GroupInfo], dict[int, GroupInfo], set[int]]:
        """Загружает список групп с сайта. Бросает исключение, если недоступна даже стартовая страница.

        Отдельные отделения, которые не удалось загрузить (сеть шатается у этого сайта
        регулярно), не валят весь заход — они просто попадают в третий элемент кортежа,
        чтобы `retry_failed_departments` мог их скоро домести, не дожидаясь следующего
        планового обновления всего каталога через `group_catalog_refresh_days`.
        """
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            root_response = await self._get_with_retry(client, f"{self.base_origin}/")
            root_soup = BeautifulSoup(root_response.content, "html.parser")

            departments: list[tuple[int, str]] = []
            for link in root_soup.select("a[href^='/group/']"):
                href = link.get("href", "")
                department_id = href.rsplit("/", 1)[-1]
                if not department_id.isdigit():
                    continue
                departments.append((int(department_id), link.get_text(" ", strip=True)))
            self._department_codes = dict(departments)

            groups_by_name: dict[str, GroupInfo] = {}
            groups_by_schedule_id: dict[int, GroupInfo] = {}
            failed_department_ids: set[int] = set()
            for department_id, department_code in sorted(set(departments)):
                try:
                    department_groups = await self._fetch_department(client, department_id, department_code)
                except httpx.HTTPError:
                    logger.warning("Пропускаю отделение id=%s из-за ошибки сети", department_id)
                    failed_department_ids.add(department_id)
                    continue
                for group in department_groups:
                    normalized_name = self.normalize(group.group_name)
                    groups_by_name[normalized_name] = group
                    groups_by_schedule_id[group.schedule_id] = group
            return groups_by_name, groups_by_schedule_id, failed_department_ids

    async def _fetch_department(
        self, client: httpx.AsyncClient, department_id: int, department_code: str
    ) -> list[GroupInfo]:
        response = await self._get_with_retry(client, f"{self.base_origin}/group/{department_id}")
        soup = BeautifulSoup(response.content, "html.parser")
        department_name_node = soup.find(id="titleS")
        department_name = department_name_node.get_text(" ", strip=True) if department_name_node else ""
        groups: list[GroupInfo] = []
        for link in soup.select("a[href^='/rasp/']"):
            href = link.get("href", "")
            schedule_id = href.rsplit("/", 1)[-1]
            group_name = link.get_text(" ", strip=True)
            if not schedule_id.isdigit() or not group_name:
                continue
            groups.append(
                GroupInfo(
                    department_id=department_id,
                    department_code=department_code,
                    department_name=department_name,
                    group_name=group_name,
                    schedule_id=int(schedule_id),
                    url=f"{self.base_origin}/rasp/{schedule_id}",
                )
            )
        return groups

    async def retry_failed_departments(self) -> bool:
        """Точечно домётывает только те отделения, что не загрузились на последнем refresh.

        Сайт МИСИС падает не целиком, а вразнобой по отдельным страницам-отделениям —
        если отделение споткнулось именно в момент планового `refresh_group_catalog`
        (раз в `group_catalog_refresh_days`), без этой функции его новые группы (например,
        весь набор нового курса) были бы не видны поиску вплоть до следующего планового
        обновления. Здесь же тянутся только реально недостающие страницы, а не весь каталог.
        """
        async with self._lock:
            if not self._failed_department_ids:
                return False
            recovered_any = False
            still_failed: set[int] = set()
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                for department_id in sorted(self._failed_department_ids):
                    department_code = self._department_codes.get(department_id, "")
                    try:
                        groups = await self._fetch_department(client, department_id, department_code)
                    except httpx.HTTPError:
                        still_failed.add(department_id)
                        continue
                    for group in groups:
                        normalized_name = self.normalize(group.group_name)
                        self._groups_by_name[normalized_name] = group
                        self._groups_by_compact_name[self._compact_name_key(group.group_name)] = group
                        self._groups_by_schedule_id[group.schedule_id] = group
                    recovered_any = True
            self._failed_department_ids = still_failed
            if recovered_any:
                await self._save_to_db()
            return recovered_any

    async def _save_to_db(self) -> None:
        """Сохраняет каталог в БД, чтобы пережить перезапуск бота при недоступном сайте."""
        if self.db is None:
            return
        try:
            payload = [asdict(group) for group in self._groups_by_schedule_id.values()]
            await self.db.save_groups(payload)
        except Exception:
            logger.warning("Не удалось сохранить каталог групп в БД.", exc_info=True)

    async def _load_from_db(self) -> bool:
        """Восстанавливает каталог из последнего сохранённого в БД снимка."""
        if self.db is None:
            return False
        try:
            rows = await self.db.get_all_groups()
            groups = [GroupInfo(**row) for row in rows]
        except Exception:
            logger.warning("Не удалось прочитать каталог групп из БД.", exc_info=True)
            return False
        if not groups:
            return False

        self._groups_by_schedule_id = {group.schedule_id: group for group in groups if group.schedule_id is not None}
        self._groups_by_name = {self.normalize(group.group_name): group for group in groups}
        self._groups_by_compact_name = {self._compact_name_key(group.group_name): group for group in groups}
        return True

    async def _bootstrap_from_subscriptions(self) -> bool:
        """Крайний резерв: сайт недоступен, а в БД ещё ни разу не было настоящего снимка каталога.

        Бывает при самом первом запуске после обновления. Вместо пустого
        каталога достаём то, что уже знаем из подписок пользователей — их
        schedule_id получен с того же сайта раньше, просто не через общий
        каталог. Не покрывает группы без единого подписчика, зато не требует
        сайта и переживёт следующий такой же простой (сохраняется в БД).
        """
        if self.db is None:
            return False
        try:
            sources = await self.db.get_active_sources()
        except Exception:
            logger.warning("Не удалось прочитать подписки для восстановления каталога групп.", exc_info=True)
            return False

        groups = [
            GroupInfo(
                department_id=0,
                department_code="",
                department_name="",
                group_name=str(source["group_name"]),
                schedule_id=int(source["schedule_id"]),
                url=f"{self.base_origin}/rasp/{source['schedule_id']}",
            )
            for source in sources
            if source.get("source_type") == "group" and source.get("schedule_id") and source.get("group_name")
        ]
        if not groups:
            return False

        self._groups_by_schedule_id = {group.schedule_id: group for group in groups}
        self._groups_by_name = {self.normalize(group.group_name): group for group in groups}
        self._groups_by_compact_name = {self._compact_name_key(group.group_name): group for group in groups}
        await self._save_to_db()
        return True

    async def _get_with_retry(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        return await get_with_retry(client, url, retries=self.request_retries, backoff_seconds=self.retry_backoff_seconds)

    async def list_groups(self) -> list[GroupInfo]:
        await self.ensure_loaded()
        return sorted(
            self._groups_by_name.values(),
            key=lambda item: (item.department_code, item.group_name),
        )

    async def add_pending_groups(self, group_names: list[str]) -> None:
        """Добавляет подтверждённые админом OCR-группы без обращения к сайту."""
        clean_names = [" ".join(name.split()) for name in group_names if name and name.strip()]
        if not clean_names:
            return
        if self.db is not None:
            await self.db.add_pending_groups(clean_names)
        for group_name in clean_names:
            normalized = self.normalize(group_name)
            if normalized in self._groups_by_name:
                continue
            group = GroupInfo(
                department_id=0,
                department_code="",
                department_name="",
                group_name=group_name,
                schedule_id=None,
                url="",
            )
            self._groups_by_name[normalized] = group
            self._groups_by_compact_name[self._compact_name_key(group_name)] = group
        self._loaded = True

    async def find_group(self, group_name: str) -> GroupInfo | None:
        await self.ensure_loaded()
        normalized_name = self.normalize(group_name)
        group = self._groups_by_name.get(normalized_name)
        if group is not None:
            return group
        return self._groups_by_compact_name.get(self._compact_name_key(group_name))

    async def get_by_schedule_id(self, schedule_id: int | None) -> GroupInfo | None:
        if schedule_id is None:
            return None
        await self.ensure_loaded()
        return self._groups_by_schedule_id.get(schedule_id)

    @staticmethod
    def normalize(value: str) -> str:
        normalized = value.strip().translate(LATIN_TO_CYRILLIC).casefold().replace("ё", "е")
        normalized = normalize_dashes(normalized)
        normalized = re.sub(r"\s*-\s*", "-", normalized, flags=re.UNICODE)
        return " ".join(normalized.split())

    @staticmethod
    def _compact_name_key(value: str) -> str:
        return strip_non_word_chars(GroupCatalog.normalize(value))

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from src.db import Database

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
                groups_by_name, groups_by_schedule_id = await self._fetch_from_site()
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
            self.last_error = None
            self._loaded = True
            await self._save_to_db()

    async def _fetch_from_site(self) -> tuple[dict[str, GroupInfo], dict[int, GroupInfo]]:
        """Загружает список групп с сайта. Бросает исключение, если недоступна даже стартовая страница."""
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
                    normalized_name = self.normalize(group_name)
                    groups_by_name[normalized_name] = group
                    groups_by_schedule_id[group.schedule_id] = group
            return groups_by_name, groups_by_schedule_id

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
        normalized = value.strip().translate(_LATIN_TO_CYRILLIC).casefold().replace("ё", "е")
        for dash in ("—", "–", "‑", "−"):
            normalized = normalized.replace(dash, "-")
        normalized = re.sub(r"\s*-\s*", "-", normalized, flags=re.UNICODE)
        return " ".join(normalized.split())

    @staticmethod
    def _compact_name_key(value: str) -> str:
        return re.sub(r"[^\w]+", "", GroupCatalog.normalize(value), flags=re.UNICODE)


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
        "Y": "У",
        "y": "у",
        "X": "Х",
        "x": "х",
    }
)

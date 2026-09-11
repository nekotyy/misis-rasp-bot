"""Сервис импорта расписания из фото и его подача в общий конвейер.

Здесь собирается вся логика, общая для Telegram и VK: распознавание, подбор
целевого источника, слияние с последним снимком, текст предпросмотра и
применение. Оба бота работают только через этот сервис, чтобы поведение
платформ не расходилось.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path

from src.db import Database
from src.group_catalog import GroupCatalog
from src.models import DaySchedule, ScheduleSnapshot
from src.ocr_schedule import (
    DEFAULT_GEMINI_DOH_URL,
    GeminiOcrEngine,
    OcrEngineError,
    OcrGroupLessons,
    OcrParseResult,
    OcrScheduleParser,
    OcrSummaryParseResult,
    OcrVocabulary,
    SnapshotMergeResult,
    build_ocr_engine,
    merge_ocr_days,
)
from src.schedule_service import format_human_date
from src.scheduler import ScheduleJobs

logger = logging.getLogger(__name__)

MAX_PREVIEW_LESSONS = 40
MAX_PREVIEW_ISSUES = 12
MAX_PREVIEW_CORRECTIONS = 10
MAX_PREVIEW_SKIPPED = 5
MAX_PREVIEW_GROUPS = 30
# Как часто обновлять индикатор во время долгого распознавания.
HEARTBEAT_INTERVAL_SECONDS = 10
_OFFLINE_GROUP_RE = re.compile(r"^[а-яёa-z0-9]+(?:-[а-яёa-z0-9]+)+$", re.IGNORECASE)

# Этапы распознавания для индикатора прогресса. Проценты приблизительные:
# движок не сообщает реальный ход, но админу важно видеть, что процесс жив
# и на каком он шаге.
OCR_STAGE_UPLOAD = 'Загружаю фото'
OCR_STAGE_RECOGNIZE = 'Распознаю текст'
OCR_STAGE_PARSE = 'Разбираю таблицу'
OCR_STAGE_MATCH = 'Сверяю с прошлым расписанием'
OCR_STAGE_PREVIEW = 'Готовлю предпросмотр'

OCR_STAGES: tuple[tuple[str, int], ...] = (
    (OCR_STAGE_UPLOAD, 10),
    (OCR_STAGE_RECOGNIZE, 35),
    (OCR_STAGE_PARSE, 70),
    (OCR_STAGE_MATCH, 85),
    (OCR_STAGE_PREVIEW, 95),
)


def format_progress_bar(stage: str, percent: int, *, width: int = 12) -> str:
    """Текстовый индикатор хода распознавания."""
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    bar = chr(9608) * filled + chr(9617) * (width - filled)
    return f"{bar} {percent}%" + '\n' + f"{stage}..."
PREVIEW_TRUNCATION_MARK = "\n…"


@dataclass(slots=True)
class OcrImportDraft:
    """Готовый к подтверждению результат распознавания."""

    result: OcrParseResult
    merge: SnapshotMergeResult
    source: dict | None = None
    source_error: str = ""
    raw_text: str = ""

    @property
    def snapshot(self) -> ScheduleSnapshot:
        return self.merge.snapshot

    @property
    def can_apply(self) -> bool:
        return self.result.is_valid and self.source is not None


@dataclass(slots=True)
class OcrGroupResolution:
    """Одна группа сводного расписания: источник для неё и снимок на этот день, слитый с прошлым."""

    group_lessons: OcrGroupLessons
    source: dict | None
    source_error: str = ""
    merge: SnapshotMergeResult | None = None

    @property
    def can_apply(self) -> bool:
        return self.source is not None and self.merge is not None


@dataclass(slots=True)
class OcrSummaryImportDraft:
    """Готовый к подтверждению сводный лист — один день сразу на много групп."""

    result: OcrSummaryParseResult
    resolutions: list[OcrGroupResolution] = field(default_factory=list)
    raw_text: str = ""

    @property
    def resolved(self) -> list[OcrGroupResolution]:
        return [item for item in self.resolutions if item.can_apply]

    @property
    def unresolved(self) -> list[OcrGroupResolution]:
        return [item for item in self.resolutions if not item.can_apply]

    @property
    def can_apply(self) -> bool:
        return self.result.is_valid and bool(self.resolved)


class OcrScheduleImporter:
    def __init__(
        self,
        db: Database,
        schedule_jobs: ScheduleJobs | None,
        group_catalog: GroupCatalog | None,
        *,
        engine: GeminiOcrEngine | None = None,
        enabled: bool = True,
        min_confidence: float = 0.6,
        fuzzy_threshold: float = 0.78,
        recognize_timeout: float = 180.0,
        alert_manager=None,
    ) -> None:
        self.db = db
        self.schedule_jobs = schedule_jobs
        self.group_catalog = group_catalog
        self.enabled = enabled
        self.recognize_timeout = max(30.0, recognize_timeout)
        self.alert_manager = alert_manager
        self.is_warm = False
        self.last_error: str = ""
        self.last_success_at: str = ""
        self.parser = OcrScheduleParser(
            engine,
            fuzzy_threshold=fuzzy_threshold,
            min_confidence=min_confidence,
        )

    @property
    def engine(self) -> GeminiOcrEngine | None:
        return self.parser.engine

    def availability(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "Импорт расписания из фото отключён (OCR_ENABLED=false)."
        if self.engine is None:
            return False, "OCR-движок не настроен."
        if self.schedule_jobs is None:
            return False, "Планировщик расписания недоступен, импорт невозможен."
        return self.engine.availability()

    async def warm_up(self) -> None:
        """Устанавливает сессию Gemini заранее, чтобы первое фото не ждало авторизации."""
        if not self.enabled or self.engine is None:
            return
        available, message = self.engine.availability()
        if not available:
            logger.warning("Прогрев OCR пропущен: %s", message)
            return
        try:
            await self.engine.warm_up()
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("Не удалось прогреть OCR-движок: %s", exc)
            await self._report(False, str(exc), "Прогрев моделей распознавания не удался")
            return
        self.is_warm = True
        self.last_error = ""
        logger.info("OCR-движок %s готов к работе.", self.engine.name)
        await self._report(True)

    async def _report(self, ok: bool, error: str | None = None, details: str | None = None) -> None:
        if self.alert_manager is None:
            return
        try:
            await self.alert_manager.report_component_status("ocr", ok, error, details=details)
        except Exception:
            logger.exception("Не удалось отправить статус OCR в мониторинг.")

    def status_line(self, html: bool = True) -> str:
        """Строка о состоянии распознавания для экрана статуса."""
        available, message = self.availability()
        if not available:
            mark, state = "🔴", message
        elif not self.is_warm:
            mark, state = "🟡", "модели греются"
        elif self.last_error:
            mark, state = "🟡", f"последняя ошибка: {self.last_error}"
        else:
            engine = getattr(self.engine, "name", "?")
            state = f"готов ({engine})"
            if self.last_success_at:
                state += f", последнее фото {self.last_success_at}"
            mark = "🟢"
        title = "Распознавание с фото"
        if html:
            return f"{mark} <b>{title}</b>: {escape(state)}"
        return f"{mark} {title}: {state}"

    async def build_draft(self, images: list[bytes], progress=None) -> OcrImportDraft:
        """Распознаёт одно или несколько фото сразу и готовит черновик к подтверждению.

        Несколько фото — например, части одной таблицы или разные недели —
        уходят в Gemini одним запросом, чтобы модель сама свела их в общий снимок
        расписания, а не мы потом склеивали куски руками.

        `progress` — необязательная корутина `(stage, percent)`. Вызывается между
        этапами, чтобы админ видел ход работы: распознавание идёт десятки секунд,
        и без индикатора непонятно, живо оно или зависло.
        """
        if not images:
            raise OcrEngineError("Нет ни одного изображения для распознавания.")
        available, message = self.availability()
        if not available:
            raise OcrEngineError(message)

        async def notify_stage(stage: str, percent: int) -> None:
            if progress is None:
                return
            try:
                await progress(stage, percent)
            except Exception:
                # Индикатор не должен ронять импорт.
                logger.debug("Не удалось обновить индикатор прогресса.", exc_info=True)

        await notify_stage(OCR_STAGE_RECOGNIZE, 35)
        started = time.monotonic()

        async def heartbeat() -> None:
            """Показывает, что распознавание живо.

            Это самый долгий этап: на слабом процессоре он идёт минуты, и без
            пульса индикатор выглядит зависшим на 35%.
            """
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                elapsed = int(time.monotonic() - started)
                await notify_stage(f"{OCR_STAGE_RECOGNIZE} ({elapsed} с)", 35)

        ticker = asyncio.create_task(heartbeat())
        try:
            raw_text = await asyncio.wait_for(
                self.parser.recognize_image(images),
                timeout=self.recognize_timeout,
            )
        except TimeoutError as exc:
            error = OcrEngineError(f"Gemini не завершил распознавание за {int(self.recognize_timeout)} с.")
            self.last_error = str(error)
            await self._report(False, str(error), "Таймаут распознавания фото")
            raise error from exc
        except Exception as exc:
            self.last_error = str(exc)
            await self._report(False, str(exc), "Ошибка распознавания фото")
            raise
        finally:
            ticker.cancel()
        logger.info("Распознавание заняло %.1f с.", time.monotonic() - started)

        await notify_stage(OCR_STAGE_PARSE, 70)
        # Первый проход нужен только чтобы узнать группу и подобрать источник,
        # второй — уже со словарём известных значений этого источника.
        probe = self.parser.parse_text(raw_text)
        source, source_error = await self._resolve_source(probe.snapshot.group_name)

        await notify_stage(OCR_STAGE_MATCH, 85)
        vocabulary = await self._build_vocabulary(source)
        result = self.parser.parse_text(raw_text, vocabulary=vocabulary)

        base_content = None
        if source is not None:
            latest = await self.db.get_latest_snapshot(
                "current",
                schedule_id=source.get("schedule_id"),
                source_key=source.get("source_key"),
            )
            base_content = latest.get("content") if latest else None

        await notify_stage(OCR_STAGE_PREVIEW, 95)
        merge = merge_ocr_days(base_content, result.snapshot)
        if source is not None and not merge.snapshot.group_name.strip():
            merge.snapshot.group_name = str(source.get("group_name") or source.get("source_title") or "")

        self.last_error = ""
        self.last_success_at = datetime.now().strftime("%d.%m %H:%M")
        await self._report(True)
        return OcrImportDraft(
            result=result,
            merge=merge,
            source=source,
            source_error=source_error,
            raw_text=raw_text,
        )

    async def apply(self, draft: OcrImportDraft, *, notify: bool = True) -> tuple[bool, str]:
        """Проводит распознанный снимок через тот же конвейер, что и сайт."""
        if self.schedule_jobs is None:
            return False, "Планировщик расписания недоступен, импорт невозможен."
        if draft.source is None:
            return False, draft.source_error or "Не удалось определить группу для импорта."
        if not draft.result.is_valid:
            return False, "В распознанных данных есть ошибки, импорт остановлен."

        try:
            change_summary = await self.schedule_jobs.apply_manual_snapshot(
                draft.source,
                draft.snapshot,
                notify=notify,
            )
        except Exception as exc:
            logger.exception("Не удалось применить расписание из фото.")
            return False, f"Ошибка при сохранении расписания: {exc}"

        await self.register_pending_source(draft.source)

        title = str(draft.source.get("source_title") or draft.source.get("group_name") or "источник")
        lessons = draft.result.lessons_count
        dates = len(draft.result.snapshot.days)
        lines = [f"Расписание для {title} обновлено: {dates} дн., {lessons} пар."]
        if change_summary is None:
            lines.append("Отличий от эталона нет — рассылка не потребовалась.")
        elif notify:
            lines.append(f"Изменения разосланы подписчикам ({len(change_summary.changed_dates)} дн.).")
        else:
            lines.append("Снимок сохранён без рассылки.")
        return True, "\n".join(lines)

    async def build_summary_draft(self, images: list[bytes], progress=None) -> OcrSummaryImportDraft:
        """Распознаёт сводное расписание (один день сразу на много групп) и готовит черновик.

        В отличие от `build_draft`, здесь не одна группа, а список: каждая
        находит свой источник и сливается со своим прошлым снимком независимо
        от остальных — так что часть групп может не резолвиться (например,
        сайт лежит и такой группы ещё не было ни в одной подписке), а
        остальные всё равно применятся.
        """
        if not images:
            raise OcrEngineError("Нет ни одного изображения для распознавания.")
        available, message = self.availability()
        if not available:
            raise OcrEngineError(message)

        async def notify_stage(stage: str, percent: int) -> None:
            if progress is None:
                return
            try:
                await progress(stage, percent)
            except Exception:
                logger.debug("Не удалось обновить индикатор прогресса.", exc_info=True)

        await notify_stage(OCR_STAGE_RECOGNIZE, 35)
        started = time.monotonic()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                elapsed = int(time.monotonic() - started)
                await notify_stage(f"{OCR_STAGE_RECOGNIZE} ({elapsed} с)", 35)

        ticker = asyncio.create_task(heartbeat())
        try:
            raw_text = await asyncio.wait_for(
                self.parser.recognize_summary_image(images),
                timeout=self.recognize_timeout,
            )
        except TimeoutError as exc:
            error = OcrEngineError(f"Gemini не завершил распознавание за {int(self.recognize_timeout)} с.")
            self.last_error = str(error)
            await self._report(False, str(error), "Таймаут распознавания сводного фото")
            raise error from exc
        except Exception as exc:
            self.last_error = str(exc)
            await self._report(False, str(exc), "Ошибка распознавания сводного фото")
            raise
        finally:
            ticker.cancel()
        logger.info("Распознавание сводного листа заняло %.1f с.", time.monotonic() - started)

        await notify_stage(OCR_STAGE_PARSE, 70)
        result = self.parser.parse_summary_text(raw_text)

        await notify_stage(OCR_STAGE_MATCH, 85)
        # Один запрос активных источников на весь лист, а не на каждую из
        # десятков групп — иначе это десятки одинаковых SQL-запросов подряд.
        sources = await self.db.get_active_sources()
        resolutions: list[OcrGroupResolution] = []
        for group_lessons in result.groups:
            source, source_error = await self._resolve_source(group_lessons.group_name, sources)
            merge = None
            if source is not None:
                base_content = None
                latest = await self.db.get_latest_snapshot(
                    "current",
                    schedule_id=source.get("schedule_id"),
                    source_key=source.get("source_key"),
                )
                base_content = latest.get("content") if latest else None
                day_snapshot = ScheduleSnapshot(
                    group_name=group_lessons.group_name,
                    fetched_at=datetime.now(),
                    days=[DaySchedule(date_label=result.date_label, date_iso=result.date_iso, lessons=group_lessons.lessons)],
                )
                merge = merge_ocr_days(base_content, day_snapshot)
            resolutions.append(
                OcrGroupResolution(group_lessons=group_lessons, source=source, source_error=source_error, merge=merge)
            )

        await notify_stage(OCR_STAGE_PREVIEW, 95)
        self.last_error = ""
        self.last_success_at = datetime.now().strftime("%d.%m %H:%M")
        await self._report(True)
        return OcrSummaryImportDraft(result=result, resolutions=resolutions, raw_text=raw_text)

    async def apply_summary(self, draft: OcrSummaryImportDraft, *, notify: bool = True) -> tuple[bool, str]:
        """Проводит каждую резолвнутую группу через тот же конвейер, что и сайт."""
        if self.schedule_jobs is None:
            return False, "Планировщик расписания недоступен, импорт невозможен."
        if not draft.can_apply:
            return False, "Не нашлось ни одной группы, которую можно сохранить."

        applied = 0
        broadcasted = 0
        failed: list[str] = []
        for resolution in draft.resolved:
            try:
                change_summary = await self.schedule_jobs.apply_manual_snapshot(
                    resolution.source,
                    resolution.merge.snapshot,
                    notify=notify,
                )
            except Exception as exc:
                logger.exception(
                    "Не удалось применить расписание группы %s из сводного фото.", resolution.group_lessons.group_name
                )
                failed.append(f"{resolution.group_lessons.group_name}: {exc}")
                continue
            await self.register_pending_source(resolution.source)
            applied += 1
            if change_summary is not None:
                broadcasted += 1

        lines = [f"Обновлено групп: {applied} из {len(draft.resolved)}."]
        if applied:
            if notify:
                lines.append(f"Рассылка ушла по {broadcasted} из них (у остальных расписание не изменилось).")
            else:
                lines.append("Сохранено без рассылки.")
        if draft.unresolved:
            lines.append(f"Группы без источника (не сохранены): {len(draft.unresolved)}.")
        if failed:
            lines.append(f"Ошибка при сохранении: {len(failed)}.")
            lines.extend(f"  {item}" for item in failed[:10])
        return applied > 0, "\n".join(lines)

    async def _build_vocabulary(self, source: dict | None) -> OcrVocabulary:
        """Собирает эталонные значения из ранее сохранённых снимков."""
        contents: list[dict | None] = []
        if source is not None:
            for snapshot_type in ("current", "daily_baseline"):
                stored = await self.db.get_latest_snapshot(
                    snapshot_type,
                    schedule_id=source.get("schedule_id"),
                    source_key=source.get("source_key"),
                )
                if stored:
                    contents.append(stored.get("content"))
        if not contents:
            stored = await self.db.get_latest_snapshot("current")
            if stored:
                contents.append(stored.get("content"))
        return OcrVocabulary.from_snapshot_contents(contents)

    async def resolve_source_by_group(self, group_name: str) -> tuple[dict | None, str]:
        """Публичная обёртка: подбор источника без распознавания.

        Нужна для ручной заливки расписания, когда фото распознавать не надо,
        а найти группу и провести снимок по общему конвейеру — надо.
        """
        return await self._resolve_source(group_name)

    async def register_pending_source(self, source: dict) -> None:
        """После успешного импорта делает OCR-группу доступной обоим ботам."""
        if source.get("source_type") != "group" or source.get("schedule_id") is not None:
            return
        group_name = str(source.get("group_name") or source.get("source_title") or "").strip()
        if not group_name:
            return
        try:
            if self.group_catalog is not None:
                await self.group_catalog.add_pending_groups([group_name])
            else:
                await self.db.add_pending_groups([group_name])
        except Exception:
            logger.warning("Не удалось добавить OCR-группу %s в офлайн-каталог.", group_name, exc_info=True)

    async def _resolve_source(self, group_name: str, sources: list[dict] | None = None) -> tuple[dict | None, str]:
        """Ищет источник, к которому относится фото.

        Сначала по подписанным источникам в БД — это работает даже когда сайт
        расписания лежит. Затем, если сайт доступен, по каталогу групп.

        `sources` можно передать заранее — на сводном листе с десятками групп
        незачем запрашивать список активных источников заново на каждую.
        """
        normalized = GroupCatalog.normalize(group_name or "")
        if not normalized or normalized == GroupCatalog.normalize("Неизвестная группа"):
            return None, "На фото не удалось прочитать название группы. Проверь, что оно попало в кадр."

        if sources is None:
            sources = await self.db.get_active_sources()
        for source in sources:
            if source.get("source_type") != "group":
                continue
            candidates = {
                GroupCatalog.normalize(str(source.get("group_name") or "")),
                GroupCatalog.normalize(str(source.get("source_title") or "")),
            }
            if normalized in candidates:
                return dict(source), ""

        if self.group_catalog is not None:
            try:
                group = await self.group_catalog.find_group(group_name)
            except Exception as exc:
                logger.warning("Каталог групп недоступен при импорте из фото: %s", exc)
                group = None
            if group is not None:
                # Пока сайт не подтвердил номер группы, schedule_id может быть None:
                # ключ строим по названию, иначе все такие группы схлопнутся в один
                # источник и будут затирать снимки друг друга.
                source_key = (
                    f"group:{group.schedule_id}"
                    if group.schedule_id is not None
                    else f"group-pending:{GroupCatalog.normalize(group.group_name)}"
                )
                return (
                    {
                        "source_type": "group",
                        "source_key": source_key,
                        "source_title": group.group_name,
                        "source_url": group.url,
                        "schedule_id": group.schedule_id,
                        "group_name": group.group_name,
                    },
                    "",
                )

        if _is_plausible_group_name(normalized):
            canonical_name = normalized.upper()
            return (
                {
                    "source_type": "group",
                    "source_key": f"group-pending:{normalized}",
                    "source_title": canonical_name,
                    "source_url": "",
                    "schedule_id": None,
                    "group_name": canonical_name,
                },
                "",
            )

        known = sorted(
            {
                str(source.get("group_name") or source.get("source_title") or "")
                for source in sources
                if source.get("source_type") == "group"
            }
            - {""}
        )
        hint = f" Известные группы: {', '.join(known[:10])}." if known else ""
        return None, f"Группа «{group_name}» не найдена среди подписанных источников.{hint}"


def _is_plausible_group_name(normalized: str) -> bool:
    return len(normalized) <= 32 and any(char.isdigit() for char in normalized) and bool(
        _OFFLINE_GROUP_RE.fullmatch(normalized)
    )


def _float_setting(settings, name: str, default: float) -> float:
    """Числовая настройка с защитой от мусора в окружении."""
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def _string_setting(settings, name: str, default: str = "") -> str:
    value = getattr(settings, name, default)
    return value.strip() if isinstance(value, str) else default


def _path_setting(settings, name: str):
    value = getattr(settings, name, None)
    return value if isinstance(value, (str, Path)) else None


def build_ocr_importer(
    settings,
    db: Database,
    schedule_jobs: ScheduleJobs | None,
    group_catalog: GroupCatalog | None,
    alert_manager=None,
) -> OcrScheduleImporter:
    """Собирает сервис импорта по настройкам окружения."""
    engine = build_ocr_engine(
        secure_1psid=_string_setting(settings, "gemini_secure_1psid"),
        secure_1psidts=_string_setting(settings, "gemini_secure_1psidts"),
        model=_string_setting(settings, "ocr_gemini_model"),
        proxy=_string_setting(settings, "gemini_proxy"),
        doh_url=_string_setting(settings, "gemini_doh_url", DEFAULT_GEMINI_DOH_URL),
        timeout=_float_setting(settings, "ocr_timeout_seconds", 180.0),
        env_path=_path_setting(settings, "gemini_env_path"),
        refresh_interval=_float_setting(settings, "gemini_refresh_interval_seconds", 600.0),
        gem_id=_string_setting(settings, "gemini_ocr_gem_id"),
    )
    return OcrScheduleImporter(
        db,
        schedule_jobs,
        group_catalog,
        engine=engine,
        enabled=bool(getattr(settings, "ocr_enabled", True)),
        min_confidence=_float_setting(settings, "ocr_min_confidence", 0.6),
        fuzzy_threshold=_float_setting(settings, "ocr_fuzzy_threshold", 0.78),
        recognize_timeout=_float_setting(settings, "ocr_timeout_seconds", 180.0),
        alert_manager=alert_manager,
    )


def _truncate_preview(text: str, max_length: int) -> str:
    if max_length <= 0 or len(text) <= max_length:
        return text
    head = text[: max(0, max_length - len(PREVIEW_TRUNCATION_MARK))]
    if "\n" in head:
        head = head.rsplit("\n", 1)[0]
    return head + PREVIEW_TRUNCATION_MARK


def format_ocr_preview(draft: OcrImportDraft, *, html: bool = True, max_length: int = 0) -> str:
    """Текст предпросмотра с отчётом проверки — одинаковый для Telegram и VK."""

    def esc(value: str) -> str:
        return escape(str(value)) if html else str(value)

    def bold(value: str) -> str:
        return f"<b>{value}</b>" if html else value

    result = draft.result
    merge = draft.merge
    lines: list[str] = [bold("Распознавание расписания с фото")]

    group_title = draft.source.get("source_title") if draft.source else result.snapshot.group_name
    lines.append(f"Группа: {bold(esc(str(group_title or '—')))}")
    lines.append(f"Уверенность: {bold(f'{result.confidence:.0%}')}")
    lines.append(f"Распознано: {result.lessons_count} пар за {len(result.snapshot.days)} дн.")

    if draft.source is None:
        lines.append("")
        lines.append(bold("Источник не определён"))
        lines.append(esc(draft.source_error))

    lines.append("")
    lines.append(bold("Что распознано"))
    shown = 0
    for day in result.snapshot.days:
        lines.append(esc(format_human_date(day.date_label)))
        if not day.lessons:
            lines.append("  пар нет")
            continue
        for lesson in day.lessons:
            if shown >= MAX_PREVIEW_LESSONS:
                break
            shown += 1
            lines.append(
                f"  {lesson.number}. {esc(lesson.subject)} | {esc(lesson.teacher or '—')} | "
                f"ауд. {esc(lesson.classroom or '—')}"
            )
        if shown >= MAX_PREVIEW_LESSONS:
            lines.append("  …")
            break

    if result.corrections:
        lines.append("")
        lines.append(bold("Автоисправления по прошлым снимкам"))
        for correction in result.corrections[:MAX_PREVIEW_CORRECTIONS]:
            lines.append(
                f"  {esc(correction.field)}: «{esc(correction.raw)}» → «{esc(correction.corrected)}» "
                f"({correction.score:.0%})"
            )
        if len(result.corrections) > MAX_PREVIEW_CORRECTIONS:
            lines.append(f"  …ещё {len(result.corrections) - MAX_PREVIEW_CORRECTIONS}")

    errors = result.errors
    warnings = result.warnings
    if errors:
        lines.append("")
        lines.append(bold("Ошибки"))
        lines.extend(f"  {esc(issue.message)}" for issue in errors[:MAX_PREVIEW_ISSUES])
    if warnings:
        lines.append("")
        lines.append(bold("Предупреждения"))
        lines.extend(f"  {esc(issue.message)}" for issue in warnings[:MAX_PREVIEW_ISSUES])
        if len(warnings) > MAX_PREVIEW_ISSUES:
            lines.append(f"  …ещё {len(warnings) - MAX_PREVIEW_ISSUES}")

    if result.skipped_lines:
        lines.append("")
        lines.append(bold("Строки, которые не удалось разобрать"))
        lines.extend(f"  {esc(line)}" for line in result.skipped_lines[:MAX_PREVIEW_SKIPPED])
        if len(result.skipped_lines) > MAX_PREVIEW_SKIPPED:
            lines.append(f"  …ещё {len(result.skipped_lines) - MAX_PREVIEW_SKIPPED}")

    lines.append("")
    lines.append(bold("Что будет сохранено"))
    if merge.replaced_dates:
        lines.append(f"  Обновятся дни: {esc(', '.join(format_human_date(d) for d in merge.replaced_dates))}")
    if merge.added_dates:
        lines.append(f"  Добавятся дни: {esc(', '.join(format_human_date(d) for d in merge.added_dates))}")
    if merge.kept_dates:
        lines.append(f"  Останутся без изменений: {len(merge.kept_dates)} дн.")
    if merge.emptied_dates:
        lines.append(
            "  Внимание: станут пустыми дни "
            f"{esc(', '.join(format_human_date(d) for d in merge.emptied_dates))}"
        )

    lines.append("")
    if draft.can_apply:
        lines.append("Проверь данные и подтверди импорт.")
    else:
        lines.append("Импорт недоступен: сначала исправь ошибки выше и пришли фото заново.")
    return _truncate_preview("\n".join(lines), max_length)


def format_ocr_summary_preview(draft: OcrSummaryImportDraft, *, html: bool = True, max_length: int = 0) -> str:
    """Текст предпросмотра сводного листа — один день сразу на много групп."""

    def esc(value: str) -> str:
        return escape(str(value)) if html else str(value)

    def bold(value: str) -> str:
        return f"<b>{value}</b>" if html else value

    result = draft.result
    resolved = draft.resolved
    unresolved = draft.unresolved
    lines: list[str] = [bold("Распознавание сводного расписания")]

    lines.append(f"Дата: {bold(esc(format_human_date(result.date_label) or '—'))}")
    lines.append(f"Уверенность: {bold(f'{result.confidence:.0%}')}")
    lines.append(f"Распознано групп: {len(result.groups)}, пар: {result.lessons_count}.")
    lines.append(f"Найден источник: {bold(str(len(resolved)))} из {len(result.groups)}.")

    if resolved:
        lines.append("")
        lines.append(bold("Будут обновлены"))
        for resolution in resolved[:MAX_PREVIEW_GROUPS]:
            title = str(resolution.source.get("source_title") or resolution.group_lessons.group_name)
            lines.append(f"  {esc(title)}: {len(resolution.group_lessons.lessons)} пар.")
        if len(resolved) > MAX_PREVIEW_GROUPS:
            lines.append(f"  …ещё {len(resolved) - MAX_PREVIEW_GROUPS}")

    if unresolved:
        lines.append("")
        lines.append(bold("Источник не найден (не будут сохранены)"))
        for resolution in unresolved[:MAX_PREVIEW_GROUPS]:
            lines.append(f"  {esc(resolution.group_lessons.group_name)}: {esc(resolution.source_error)}")
        if len(unresolved) > MAX_PREVIEW_GROUPS:
            lines.append(f"  …ещё {len(unresolved) - MAX_PREVIEW_GROUPS}")

    errors = result.errors
    warnings = result.warnings
    if errors:
        lines.append("")
        lines.append(bold("Ошибки"))
        lines.extend(f"  {esc(issue.message)}" for issue in errors[:MAX_PREVIEW_ISSUES])
    if warnings:
        lines.append("")
        lines.append(bold("Предупреждения"))
        lines.extend(f"  {esc(issue.message)}" for issue in warnings[:MAX_PREVIEW_ISSUES])
        if len(warnings) > MAX_PREVIEW_ISSUES:
            lines.append(f"  …ещё {len(warnings) - MAX_PREVIEW_ISSUES}")

    if result.skipped_lines:
        lines.append("")
        lines.append(bold("Строки, которые не удалось разобрать"))
        lines.extend(f"  {esc(line)}" for line in result.skipped_lines[:MAX_PREVIEW_SKIPPED])
        if len(result.skipped_lines) > MAX_PREVIEW_SKIPPED:
            lines.append(f"  …ещё {len(result.skipped_lines) - MAX_PREVIEW_SKIPPED}")

    lines.append("")
    if draft.can_apply:
        lines.append("Проверь данные и подтверди импорт.")
    else:
        lines.append("Импорт недоступен: сначала исправь ошибки выше и пришли фото заново.")
    return _truncate_preview("\n".join(lines), max_length)

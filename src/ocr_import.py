"""Сервис импорта расписания из фото и его подача в общий конвейер.

Здесь собирается вся логика, общая для Telegram и VK: распознавание, подбор
целевого источника, слияние с последним снимком, текст предпросмотра и
применение. Оба бота работают только через этот сервис, чтобы поведение
платформ не расходилось.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape

from src.db import Database
from src.group_catalog import GroupCatalog
from src.models import ScheduleSnapshot
from src.ocr_schedule import (
    EasyOcrEngine,
    OcrEngineError,
    OcrParseResult,
    OcrScheduleParser,
    OcrVocabulary,
    SnapshotMergeResult,
    TesseractOcrEngine,
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


class OcrScheduleImporter:
    def __init__(
        self,
        db: Database,
        schedule_jobs: ScheduleJobs | None,
        group_catalog: GroupCatalog | None,
        *,
        engine: TesseractOcrEngine | EasyOcrEngine | None = None,
        enabled: bool = True,
        min_confidence: float = 0.6,
        fuzzy_threshold: float = 0.78,
    ) -> None:
        self.db = db
        self.schedule_jobs = schedule_jobs
        self.group_catalog = group_catalog
        self.enabled = enabled
        self.parser = OcrScheduleParser(
            engine,
            fuzzy_threshold=fuzzy_threshold,
            min_confidence=min_confidence,
        )

    @property
    def engine(self) -> TesseractOcrEngine | EasyOcrEngine | None:
        return self.parser.engine

    def availability(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "Импорт расписания из фото отключён (OCR_ENABLED=false)."
        if self.engine is None:
            return False, "OCR-движок не настроен."
        if self.schedule_jobs is None:
            return False, "Планировщик расписания недоступен, импорт невозможен."
        return self.engine.availability()

    async def build_draft(self, image_bytes: bytes) -> OcrImportDraft:
        """Распознаёт фото и готовит черновик к подтверждению."""
        available, message = self.availability()
        if not available:
            raise OcrEngineError(message)

        raw_text = await self.parser.recognize_image(image_bytes)

        # Первый проход нужен только чтобы узнать группу и подобрать источник,
        # второй — уже со словарём известных значений этого источника.
        probe = self.parser.parse_text(raw_text)
        source, source_error = await self._resolve_source(probe.snapshot.group_name)

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

        merge = merge_ocr_days(base_content, result.snapshot)
        if source is not None and not merge.snapshot.group_name.strip():
            merge.snapshot.group_name = str(source.get("group_name") or source.get("source_title") or "")

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

    async def _resolve_source(self, group_name: str) -> tuple[dict | None, str]:
        """Ищет источник, к которому относится фото.

        Сначала по подписанным источникам в БД — это работает даже когда сайт
        расписания лежит. Затем, если сайт доступен, по каталогу групп.
        """
        normalized = GroupCatalog.normalize(group_name or "")
        if not normalized or normalized == GroupCatalog.normalize("Неизвестная группа"):
            return None, "На фото не удалось прочитать название группы. Проверь, что оно попало в кадр."

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
                return (
                    {
                        "source_type": "group",
                        "source_key": f"group:{group.schedule_id}",
                        "source_title": group.group_name,
                        "source_url": group.url,
                        "schedule_id": group.schedule_id,
                        "group_name": group.group_name,
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


def build_ocr_importer(
    settings,
    db: Database,
    schedule_jobs: ScheduleJobs | None,
    group_catalog: GroupCatalog | None,
) -> OcrScheduleImporter:
    """Собирает сервис импорта по настройкам окружения."""
    engine = build_ocr_engine(
        engine=getattr(settings, "ocr_engine", "easyocr"),
        command=getattr(settings, "ocr_tesseract_cmd", ""),
        languages=getattr(settings, "ocr_languages", "rus+eng"),
        psm=getattr(settings, "ocr_psm", 6),
        timeout=getattr(settings, "ocr_timeout_seconds", 60.0),
    )
    return OcrScheduleImporter(
        db,
        schedule_jobs,
        group_catalog,
        engine=engine,
        enabled=getattr(settings, "ocr_enabled", True),
        min_confidence=getattr(settings, "ocr_min_confidence", 0.6),
        fuzzy_threshold=getattr(settings, "ocr_fuzzy_threshold", 0.78),
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

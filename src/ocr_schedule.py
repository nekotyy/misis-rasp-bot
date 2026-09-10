"""Импорт расписания из фотографии через Gemini (веб-аккаунт Google).

Резервный источник данных на случай, когда сайт расписания недоступен. На
выходе получается обычный `ScheduleSnapshot`, который дальше проходит по тому
же самому конвейеру (`compute_snapshot_hash` -> `Database.save_snapshot` ->
`ScheduleComparator` -> `Broadcaster`). Никакой отдельной ветки доставки
уведомлений тут нет.

Слои модуля:

1. `GeminiOcrEngine` — распознавание картинки через `gemini_webapi`
   (https://github.com/HanaokaYuzu/Gemini-API): неофициальный клиент
   gemini.google.com, авторизующийся печеньками `__Secure-1PSID` /
   `__Secure-1PSIDTS` обычного Google-аккаунта, без официального платного API.
   Модель сама читает таблицу на фото и возвращает готовый JSON — отдельного
   слоя детекции текста и подгонки регулярок под верстку таблицы не нужно.
2. `OcrScheduleParser.parse_text` — разбор JSON-ответа модели в снимок
   расписания с проверкой. Не требует сети, поэтому полностью покрыт тестами.
3. Проверка и фильтрация: отбраковка мусора, сверка со словарём известных
   значений и расчёт уверенности распознавания.
4. `merge_ocr_days` — аккуратное вливание распознанных дней в последний
   известный снимок, чтобы фото на 3 дня не затирало остальную неделю.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from gemini_webapi import GeminiClient
from gemini_webapi.exceptions import (
    AuthError,
    GeminiError,
    TemporarilyBlockedError,
    UsageLimitExceededError,
)
from gemini_webapi.exceptions import (
    TimeoutError as GeminiTimeoutError,
)

from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.parser import compute_snapshot_hash
from src.schedule_service import format_human_date

logger = logging.getLogger(__name__)

MIN_LESSON_NUMBER = 1
MAX_LESSON_NUMBER = 12
MAX_CLASSROOM_LENGTH = 20
MIN_SUBJECT_LENGTH = 3
MAX_DATE_DRIFT_DAYS = 400
# Сколько символов сырого ответа модели показывать в предпросмотре при сбое разбора.
RAW_PREVIEW_LENGTH = 500

# Латинские буквы, которые изредка подставляются вместо кириллических —
# модель иногда путает визуально похожие символы в именах и аудиториях.
_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А", "a": "а", "B": "В", "C": "С", "c": "с", "E": "Е", "e": "е",
        "H": "Н", "K": "К", "k": "к", "M": "М", "O": "О", "o": "о", "P": "Р",
        "p": "р", "T": "Т", "X": "Х", "x": "х", "Y": "У", "y": "у",
    }
)
_FOLD_DIGITS = str.maketrans({"6": "б", "3": "з", "0": "о", "4": "ч"})
_CLASSROOM_RE = re.compile(r"^[\w][\w\-/\\. ]*$", re.UNICODE)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

RECOGNITION_PROMPT = """\
На фото — расписание занятий учебной группы в виде таблицы (может быть \
несколько дней/дат на одном фото). Извлеки данные и верни ТОЛЬКО JSON без \
markdown-разметки и без пояснений, строго такой структуры:

{
  "group_name": "название группы, например ИСП-25-1",
  "days": [
    {
      "date_iso": "2026-09-10",
      "lessons": [
        {"number": 1, "subject": "название дисциплины", "teacher": "ФИО преподавателя", "classroom": "номер аудитории"}
      ]
    }
  ]
}

Правила:
- Дату переводи в формат ISO YYYY-MM-DD.
- Включай в "days" каждый день, у которого на фото есть хотя бы шапка таблицы \
с датой, даже если строк с парами под ней нет (пустой список lessons).
- Если номер пары в ячейке не указан явно, определяй его по порядку строки \
в таблице этого дня, начиная с 1.
- Не придумывай данные, которых нет на фото. Если поле не читается или его \
нет, оставляй пустую строку у этого поля, но не пропускай всю пару.
- Если на фото не видно ни одной даты или названия группы, верни то, что \
удалось прочитать, а пустые поля оставь пустыми строками.
- Верни только JSON, без ```json и без комментариев до или после него.
"""


class OcrEngineError(RuntimeError):
    """Движок распознавания недоступен или вернул ошибку."""


@dataclass(slots=True)
class OcrIssue:
    level: str  # "error" | "warning"
    message: str
    date_iso: str = ""
    lesson_number: int | None = None


@dataclass(slots=True)
class OcrCorrection:
    """Автоисправление значения по словарю известных значений."""

    field: str
    raw: str
    corrected: str
    score: float
    date_iso: str = ""
    lesson_number: int | None = None


@dataclass(slots=True)
class OcrParseResult:
    snapshot: ScheduleSnapshot
    group_name_raw: str = ""
    issues: list[OcrIssue] = field(default_factory=list)
    corrections: list[OcrCorrection] = field(default_factory=list)
    skipped_lines: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def errors(self) -> list[OcrIssue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[OcrIssue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    @property
    def lessons_count(self) -> int:
        return sum(len(day.lessons) for day in self.snapshot.days)

    @property
    def is_valid(self) -> bool:
        return not self.errors and self.lessons_count > 0

    def snapshot_hash(self) -> str:
        return compute_snapshot_hash(self.snapshot)


@dataclass(slots=True)
class OcrVocabulary:
    """Известные значения из прошлых снимков — эталон для автоисправления."""

    subjects: tuple[str, ...] = ()
    teachers: tuple[str, ...] = ()
    classrooms: tuple[str, ...] = ()

    @classmethod
    def from_snapshot_contents(cls, contents: Iterable[dict | None]) -> OcrVocabulary:
        subjects: dict[str, str] = {}
        teachers: dict[str, str] = {}
        classrooms: dict[str, str] = {}
        for content in contents:
            if not content:
                continue
            for day in content.get("days", []) or []:
                for lesson in day.get("lessons", []) or []:
                    for bucket, key in (
                        (subjects, "subject"),
                        (teachers, "teacher"),
                        (classrooms, "classroom"),
                    ):
                        value = str(lesson.get(key) or "").strip()
                        if value:
                            bucket.setdefault(_fold(value), value)
        return cls(
            subjects=tuple(subjects.values()),
            teachers=tuple(teachers.values()),
            classrooms=tuple(classrooms.values()),
        )

    @property
    def is_empty(self) -> bool:
        return not (self.subjects or self.teachers or self.classrooms)


@dataclass(slots=True)
class SnapshotMergeResult:
    snapshot: ScheduleSnapshot
    added_dates: list[str] = field(default_factory=list)
    replaced_dates: list[str] = field(default_factory=list)
    kept_dates: list[str] = field(default_factory=list)
    emptied_dates: list[str] = field(default_factory=list)


def _fold(value: str) -> str:
    """Ключ для нечёткого сравнения.

    Латиница приводится к кириллице даже там, где для вывода это было бы
    небезопасно: сравнение должно считать 'Ky6aHeBa' и 'Кубанева' похожими.
    """
    lowered = value.translate(_LATIN_TO_CYRILLIC).casefold().replace("ё", "е")
    lowered = lowered.translate(_FOLD_DIGITS)
    cleaned = re.sub(r"[^\w\s]", " ", lowered, flags=re.UNICODE)
    return " ".join(cleaned.split())


def _digit_signature(value: str) -> tuple[str, ...]:
    """Последовательность чисел в значении, посчитанная по свёрнутой форме.

    Свёртка сначала возвращает буквы на место цифр-двойников, поэтому 'с-3'
    и 'с-з' дают одинаковую подпись, а '305/1' и '305/2' — разные.
    """
    return tuple(re.findall(r"\d+", _fold(value)))


def _initials_signature(value: str) -> tuple[str, ...]:
    normalized = value.translate(_LATIN_TO_CYRILLIC).upper().replace("Ё", "Е")
    return tuple("".join(match.groups()) for match in re.finditer(r"([А-Я])\s*\.\s*([А-Я])\s*\.?", normalized))


def _looks_damaged(value: str) -> bool:
    """Есть ли в значении символы, которых в расписании быть не может."""
    return bool(re.search(r"[^\w\s/\\.\-]", value, flags=re.UNICODE))


def _is_compatible(value: str, option: str) -> bool:
    """Отсекает замены, меняющие смысл: другую аудиторию или другого человека.

    Похожесть по символам этого не видит: у '305/1' и '305/2' она 0.80, а у
    'Травкин А.В.' и 'Травкина Е.А.' — 0.78, хотя это разные аудитория и
    преподаватель. Цифры и инициалы должны совпадать точно — кроме случая,
    когда значение явно повреждено.
    """
    if not _looks_damaged(value) and _digit_signature(value) != _digit_signature(option):
        return False
    value_initials = _initials_signature(value)
    option_initials = _initials_signature(option)
    return not (value_initials and option_initials and value_initials != option_initials)


def _snap_to_vocabulary(value: str, options: tuple[str, ...], threshold: float) -> tuple[str, float]:
    """Подтягивает значение к ближайшему известному, если оно достаточно похоже."""
    if not value or not options:
        return value, 0.0

    folded_value = _fold(value)
    if not folded_value:
        return value, 0.0

    best_option = ""
    best_score = 0.0
    for option in options:
        folded_option = _fold(option)
        if folded_option == folded_value:
            return option, 1.0
        if not _is_compatible(value, option):
            continue
        score = difflib.SequenceMatcher(None, folded_value, folded_option).ratio()
        if score > best_score:
            best_score = score
            best_option = option

    if best_score >= threshold:
        return best_option, best_score
    return value, best_score


def _looks_like_classroom(value: str) -> bool:
    if not value or len(value) > MAX_CLASSROOM_LENGTH:
        return False
    return bool(_CLASSROOM_RE.match(value))


def _extract_json_payload(text: str) -> str:
    """Достаёт JSON из ответа модели, даже если она обернула его в ```json или дописала слово-другое."""
    cleaned = _JSON_FENCE_RE.sub("", text.strip()).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def _normalize_date_iso(raw: object, now: datetime) -> tuple[str, str] | None:
    """Проверяет и приводит дату к (date_iso, date_label), либо отбраковывает."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text[:10], "%Y-%m-%d")  # noqa: DTZ007 - дата без времени
    except ValueError:
        return None
    if abs((parsed.date() - now.date()).days) > MAX_DATE_DRIFT_DAYS:
        return None
    return parsed.date().isoformat(), parsed.strftime("%d.%m.%Y")


def _guess_image_extension(data: bytes) -> str:
    """По магическим байтам определяет расширение — нужно для правильного Content-Type.

    `gemini_webapi` угадывает MIME-тип по имени файла: без настоящего
    расширения (например, для случайного временного имени) картинка уходит
    как `text/plain`, и Gemini её просто не видит.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".jpg"


def _coerce_lesson_number(raw: object, fallback: int) -> int:
    try:
        number = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if MIN_LESSON_NUMBER <= number <= MAX_LESSON_NUMBER:
        return number
    return fallback


class GeminiOcrEngine:
    """Распознавание фото через веб-аккаунт Gemini (`gemini_webapi`).

    Не официальный API: авторизация идёт печеньками `__Secure-1PSID` и
    `__Secure-1PSIDTS` обычного Google-аккаунта, тем же способом, которым
    браузер держит сессию на gemini.google.com. Плюс — не нужен платный
    API-ключ и тяжёлые локальные модели (EasyOCR/torch занимали ~750 МБ и не
    помещались в контейнер на 2 ГБ). Минус — это неофициальный доступ поверх
    веб-интерфейса: печеньки нужно обновлять при выходе из аккаунта, а
    массовое/автоматизированное использование чужого аккаунта нарушает
    условия использования Google.
    """

    name = "gemini"

    def __init__(
        self,
        *,
        secure_1psid: str = "",
        secure_1psidts: str = "",
        model: str = "",
        proxy: str = "",
        timeout: float = 60.0,
    ) -> None:
        self.secure_1psid = secure_1psid.strip()
        self.secure_1psidts = secure_1psidts.strip()
        self.model = model.strip()
        self.proxy = proxy.strip() or None
        self.timeout = max(10.0, timeout)
        self._client: GeminiClient | None = None
        self._lock = asyncio.Lock()

    def availability(self) -> tuple[bool, str]:
        if not self.secure_1psid or not self.secure_1psidts:
            return False, (
                "Не заданы куки Google-аккаунта GEMINI_SECURE_1PSID / GEMINI_SECURE_1PSIDTS."
            )
        return True, f"gemini ({self.model or 'модель по умолчанию'})"

    async def _ensure_client(self) -> GeminiClient:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            available, message = self.availability()
            if not available:
                raise OcrEngineError(message)
            client = GeminiClient(self.secure_1psid, self.secure_1psidts, proxy=self.proxy)
            try:
                await client.init(timeout=self.timeout, auto_close=False)
            except AuthError as exc:
                raise OcrEngineError(
                    f"Google не принял куки аккаунта: {exc}. Возможно, они устарели — получи новые из браузера."
                ) from exc
            except GeminiTimeoutError as exc:
                raise OcrEngineError(f"Не удалось подключиться к Gemini: истёк таймаут ({exc}).") from exc
            except GeminiError as exc:
                raise OcrEngineError(f"Gemini недоступен: {exc}") from exc
            self._client = client
            return client

    async def warm_up(self) -> None:
        """Заранее устанавливает сессию, чтобы первое фото не ждало авторизации."""
        await self._ensure_client()

    async def recognize(self, image_bytes: bytes) -> str:
        if not image_bytes:
            raise OcrEngineError("Пустое изображение.")
        client = await self._ensure_client()

        # `BytesIO` без имени файла загружается как `.txt` и Gemini не видит в
        # нём картинку — нужен настоящий файл с расширением, определённым по
        # содержимому (типы вложений из Telegram/VK бывают разными).
        suffix = _guess_image_extension(image_bytes)
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(image_bytes)
            try:
                response = await client.generate_content(
                    RECOGNITION_PROMPT,
                    files=[tmp_path],
                    model=self.model or None,
                )
            except AuthError as exc:
                self._client = None  # сессия протухла — следующий вызов авторизуется заново
                raise OcrEngineError(f"Google разорвал сессию: {exc}. Попробуй ещё раз или обнови куки.") from exc
            except UsageLimitExceededError as exc:
                raise OcrEngineError(f"Исчерпан лимит запросов к Gemini на сегодня: {exc}") from exc
            except TemporarilyBlockedError as exc:
                raise OcrEngineError(f"Аккаунт Google временно заблокирован для запросов к Gemini: {exc}") from exc
            except GeminiTimeoutError as exc:
                raise OcrEngineError(f"Gemini не ответил вовремя: {exc}") from exc
            except GeminiError as exc:
                raise OcrEngineError(f"Ошибка распознавания через Gemini: {exc}") from exc
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.debug("Не удалось удалить временный файл %s.", tmp_path, exc_info=True)
        return response.text


class OcrScheduleParser:
    """Разбор ответа Gemini в `ScheduleSnapshot` с проверкой и фильтрацией."""

    def __init__(
        self,
        engine: GeminiOcrEngine | None = None,
        *,
        fuzzy_threshold: float = 0.78,
        min_confidence: float = 0.6,
    ) -> None:
        self.engine = engine
        self.fuzzy_threshold = fuzzy_threshold
        self.min_confidence = min_confidence

    async def recognize_image(self, image_bytes: bytes) -> str:
        if self.engine is None:
            raise OcrEngineError("Движок распознавания не настроен.")
        return await self.engine.recognize(image_bytes)

    async def parse_image(
        self,
        image_bytes: bytes,
        *,
        vocabulary: OcrVocabulary | None = None,
        now: datetime | None = None,
    ) -> OcrParseResult:
        text = await self.recognize_image(image_bytes)
        return self.parse_text(text, vocabulary=vocabulary, now=now)

    def parse_text(
        self,
        text: str,
        *,
        vocabulary: OcrVocabulary | None = None,
        now: datetime | None = None,
    ) -> OcrParseResult:
        reference_now = now or datetime.now()
        vocab = vocabulary or OcrVocabulary()

        issues: list[OcrIssue] = []
        corrections: list[OcrCorrection] = []
        skipped_lines: list[str] = []
        lesson_scores: list[float] = []

        try:
            data = json.loads(_extract_json_payload(text or ""))
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            issues.append(OcrIssue("error", "Не удалось разобрать ответ распознавания — это не похоже на JSON."))
            snapshot = ScheduleSnapshot(group_name="Неизвестная группа", fetched_at=reference_now, days=[])
            preview = (text or "").strip()
            if preview:
                skipped_lines.append(preview[:RAW_PREVIEW_LENGTH])
            return OcrParseResult(snapshot=snapshot, issues=issues, skipped_lines=skipped_lines, confidence=0.0)

        group_name = str(data.get("group_name") or "").strip()
        raw_days = data.get("days")
        days: list[DaySchedule] = []
        seen_dates: set[str] = set()

        for raw_day in raw_days if isinstance(raw_days, list) else []:
            if not isinstance(raw_day, dict):
                skipped_lines.append(str(raw_day))
                continue

            normalized = _normalize_date_iso(raw_day.get("date_iso"), reference_now)
            if normalized is None:
                issues.append(
                    OcrIssue("warning", f"Не удалось разобрать дату «{raw_day.get('date_iso')}» — день пропущен.")
                )
                continue
            date_iso, date_label = normalized
            if date_iso in seen_dates:
                issues.append(
                    OcrIssue(
                        "warning",
                        f"Дата {format_human_date(date_label)} встречается несколько раз — оставлен первый блок.",
                        date_iso=date_iso,
                    )
                )
                continue
            seen_dates.add(date_iso)
            day = DaySchedule(date_label=date_label, date_iso=date_iso, lessons=[])

            raw_lessons = raw_day.get("lessons")
            for position, raw_lesson in enumerate(raw_lessons if isinstance(raw_lessons, list) else [], start=1):
                if not isinstance(raw_lesson, dict):
                    skipped_lines.append(f"{date_label}: {raw_lesson!r}")
                    continue
                number = _coerce_lesson_number(raw_lesson.get("number"), position)
                subject = str(raw_lesson.get("subject") or "").strip()
                teacher = str(raw_lesson.get("teacher") or "").strip()
                classroom = str(raw_lesson.get("classroom") or "").strip()

                if not subject:
                    issues.append(
                        OcrIssue(
                            "warning",
                            f"{format_human_date(date_label)}, пара {number}: пустая дисциплина — строка пропущена.",
                            date_iso,
                            number,
                        )
                    )
                    skipped_lines.append(f"{date_label} пара {number}: без дисциплины")
                    continue

                score = self._score_and_correct(day, number, subject, teacher, classroom, vocab, issues, corrections)
                lesson_scores.append(score)

            days.append(day)

        self._validate_days(days, issues)
        confidence = self._overall_confidence(lesson_scores, skipped_lines, issues)

        if not days:
            issues.append(OcrIssue("error", "На фото не найдено ни одной даты. Проверь, что расписание видно целиком."))
        elif not any(day.lessons for day in days):
            issues.append(OcrIssue("error", "Даты распознаны, но ни одной пары прочитать не удалось."))

        if confidence < self.min_confidence and any(day.lessons for day in days):
            issues.append(
                OcrIssue(
                    "warning",
                    f"Низкая уверенность распознавания ({confidence:.0%}). Внимательно проверь текст перед подтверждением.",
                )
            )

        snapshot = ScheduleSnapshot(
            group_name=group_name or "Неизвестная группа",
            fetched_at=reference_now,
            days=sorted(days, key=lambda day: day.date_iso),
        )
        return OcrParseResult(
            snapshot=snapshot,
            group_name_raw=group_name,
            issues=issues,
            corrections=corrections,
            skipped_lines=skipped_lines,
            confidence=confidence,
        )

    def _score_and_correct(
        self,
        day: DaySchedule,
        number: int,
        subject: str,
        teacher: str,
        classroom: str,
        vocab: OcrVocabulary,
        issues: list[OcrIssue],
        corrections: list[OcrCorrection],
    ) -> float:
        score_parts: list[float] = [1.0]

        subject, subject_score = self._apply_correction(
            "Дисциплина", subject, vocab.subjects, day, number, corrections
        )
        teacher, teacher_score = self._apply_correction(
            "Преподаватель", teacher, vocab.teachers, day, number, corrections
        )
        classroom, classroom_score = self._apply_correction(
            "Аудитория", classroom, vocab.classrooms, day, number, corrections
        )
        score_parts.extend([subject_score, teacher_score, classroom_score])

        if len(subject) < MIN_SUBJECT_LENGTH:
            issues.append(
                OcrIssue("warning", f"Пара {number}: слишком короткое название дисциплины «{subject}».", day.date_iso, number)
            )
            score_parts.append(0.3)
        if not teacher:
            issues.append(
                OcrIssue("warning", f"Пара {number}: не распознан преподаватель.", day.date_iso, number)
            )
            score_parts.append(0.4)
        if not classroom:
            issues.append(
                OcrIssue("warning", f"Пара {number}: не распознана аудитория.", day.date_iso, number)
            )
            score_parts.append(0.5)
        elif not _looks_like_classroom(classroom):
            issues.append(
                OcrIssue("warning", f"Пара {number}: аудитория «{classroom}» выглядит подозрительно.", day.date_iso, number)
            )
            score_parts.append(0.4)

        existing = next((item for item in day.lessons if item.number == number), None)
        if existing is not None:
            issues.append(
                OcrIssue(
                    "warning",
                    f"Пара {number} на {format_human_date(day.date_label)} встретилась дважды — оставлен первый вариант.",
                    day.date_iso,
                    number,
                )
            )
            return sum(score_parts) / len(score_parts)

        day.lessons.append(Lesson(number=number, subject=subject, teacher=teacher, classroom=classroom))
        return sum(score_parts) / len(score_parts)

    def _apply_correction(
        self,
        field_name: str,
        value: str,
        options: tuple[str, ...],
        day: DaySchedule,
        number: int,
        corrections: list[OcrCorrection],
    ) -> tuple[str, float]:
        if not value:
            return value, 0.0
        if not options:
            return value, 0.9

        corrected, score = _snap_to_vocabulary(value, options, self.fuzzy_threshold)
        if corrected != value:
            corrections.append(
                OcrCorrection(
                    field=field_name,
                    raw=value,
                    corrected=corrected,
                    score=score,
                    date_iso=day.date_iso,
                    lesson_number=number,
                )
            )
        return corrected, max(score, 0.7) if score else 0.8

    def _validate_days(self, days: list[DaySchedule], issues: list[OcrIssue]) -> None:
        for day in days:
            day.lessons.sort(key=lambda lesson: lesson.number)
            if not day.lessons:
                continue
            numbers = [lesson.number for lesson in day.lessons]
            gaps = [
                number
                for number in range(min(numbers), max(numbers) + 1)
                if number not in numbers
            ]
            if gaps:
                issues.append(
                    OcrIssue(
                        "warning",
                        f"{format_human_date(day.date_label)}: пропущены номера пар {', '.join(map(str, gaps))}. "
                        "Возможно, строка не распозналась.",
                        day.date_iso,
                    )
                )

    def _overall_confidence(
        self,
        lesson_scores: list[float],
        skipped_lines: list[str],
        issues: list[OcrIssue],
    ) -> float:
        if not lesson_scores:
            return 0.0
        base = sum(lesson_scores) / len(lesson_scores)
        skip_penalty = min(0.3, 0.05 * len(skipped_lines))
        warning_penalty = min(0.2, 0.02 * len([i for i in issues if i.level == "warning"]))
        return max(0.0, round(base - skip_penalty - warning_penalty, 3))


def merge_ocr_days(base_content: dict | None, ocr_snapshot: ScheduleSnapshot) -> SnapshotMergeResult:
    """Вливает распознанные дни в последний известный снимок.

    Фото обычно покрывает 3-4 дня, а снимок с сайта — всю неделю. Полная замена
    удалила бы остальные дни, поэтому дни объединяются по `date_iso`.
    """
    result = SnapshotMergeResult(snapshot=ocr_snapshot)
    ocr_days = {day.date_iso: day for day in ocr_snapshot.days}

    if not base_content:
        result.added_dates = sorted(ocr_days)
        return result

    merged: dict[str, DaySchedule] = {}
    for raw_day in base_content.get("days", []) or []:
        date_iso = str(raw_day.get("date_iso") or "")
        if not date_iso:
            continue
        merged[date_iso] = DaySchedule(
            date_label=str(raw_day.get("date_label") or date_iso),
            date_iso=date_iso,
            lessons=[
                Lesson(
                    number=int(lesson["number"]),
                    subject=str(lesson.get("subject") or ""),
                    teacher=str(lesson.get("teacher") or ""),
                    classroom=str(lesson.get("classroom") or ""),
                )
                for lesson in raw_day.get("lessons", []) or []
            ],
        )

    for date_iso, day in ocr_days.items():
        previous = merged.get(date_iso)
        if previous is None:
            result.added_dates.append(date_iso)
        else:
            result.replaced_dates.append(date_iso)
            if previous.lessons and not day.lessons:
                result.emptied_dates.append(date_iso)
        merged[date_iso] = day

    result.kept_dates = sorted(set(merged) - set(ocr_days))
    result.snapshot = ScheduleSnapshot(
        group_name=ocr_snapshot.group_name or str(base_content.get("group_name") or ""),
        fetched_at=ocr_snapshot.fetched_at,
        days=[merged[key] for key in sorted(merged)],
    )
    for bucket in (result.added_dates, result.replaced_dates, result.emptied_dates):
        bucket.sort()
    return result


def build_ocr_engine(
    *,
    secure_1psid: str = "",
    secure_1psidts: str = "",
    model: str = "",
    proxy: str = "",
    timeout: float = 60.0,
) -> GeminiOcrEngine:
    return GeminiOcrEngine(
        secure_1psid=secure_1psid,
        secure_1psidts=secure_1psidts,
        model=model,
        proxy=proxy,
        timeout=timeout,
    )

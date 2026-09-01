"""Импорт расписания из фотографии (OCR).

Резервный источник данных на случай, когда сайт расписания недоступен.
Модуль устроен так же, как `src/parser.py`: на выходе получается обычный
`ScheduleSnapshot`, который дальше проходит по тому же самому конвейеру
(`compute_snapshot_hash` -> `Database.save_snapshot` -> `ScheduleComparator` ->
`Broadcaster`). Никакой отдельной ветки доставки уведомлений тут нет.

Слои модуля:

1. `TesseractOcrEngine` — распознавание картинки в текст (внешний движок).
2. `OcrScheduleParser.parse_text` — чистый разбор текста в снимок расписания.
   Не требует установленного OCR-движка, поэтому полностью покрыт тестами.
3. Проверка и фильтрация: нормализация, отбраковка мусорных строк, сверка со
   словарём известных значений и расчёт уверенности распознавания.
4. `merge_ocr_days` — аккуратное вливание распознанных дней в последний
   известный снимок, чтобы фото на 3 дня не затирало остальную неделю.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from PIL import ImageOps

from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.parser import compute_snapshot_hash
from src.schedule_service import format_human_date

logger = logging.getLogger(__name__)

MIN_LESSON_NUMBER = 1
MAX_LESSON_NUMBER = 12
MAX_CLASSROOM_LENGTH = 20
MIN_SUBJECT_LENGTH = 3
MAX_DATE_DRIFT_DAYS = 400
# Длинная сторона, до которой ужимается фото перед детекцией. Телефонные
# снимки бывают по 4000 px: время и память растут квадратично, а точность нет.
MAX_DETECTION_SIDE = 2000

# Латинские буквы, которые Tesseract часто подставляет вместо кириллических.
_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А", "a": "а", "B": "В", "C": "С", "c": "с", "E": "Е", "e": "е",
        "H": "Н", "K": "К", "k": "к", "M": "М", "O": "О", "o": "о", "P": "Р",
        "p": "р", "T": "Т", "X": "Х", "x": "х", "Y": "У", "y": "у",
    }
)

# Цифры, которыми Tesseract подменяет буквы внутри слов ('Ку6анева').
_WORD_DIGITS = str.maketrans({"6": "б", "3": "з", "0": "о", "4": "ч"})

# То же самое, но только для сравнения со словарём: аудитория 'с-з' (спортзал)
# распознаётся как 'с-3', и без этой нормализации похожесть выходит ниже порога.
_FOLD_DIGITS = _WORD_DIGITS

# Частые ошибки распознавания в номере пары.
_DIGIT_LOOKALIKES = str.maketrans(
    {
        "l": "1", "I": "1", "|": "1", "!": "1", "i": "1",
        "O": "0", "o": "0", "О": "0", "о": "0", "D": "0",
        "З": "3", "з": "3", "б": "6", "S": "5", "s": "5",
        "B": "8", "Ч": "4", "Т": "7",
    }
)

# Строки шапки таблицы и заголовка страницы — распознаются, но данными не являются.
_NOISE_TOKENS = frozenset(
    {
        "пара", "пары", "дисциплина", "предмет", "преподаватель", "преподователь",
        "ауд", "аудитория", "кабинет", "персональное", "расписание", "занятий",
        "занятия", "группа", "время", "номер",
    }
)

_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[.,/]\s*(\d{1,2})\s*[.,/]\s*(\d{2,4})(?!\d)")
_GROUP_RE = re.compile(r"\b([А-ЯЁ]{2,6})\s*[-–—‑]\s*(\d{2})\s*[-–—‑]\s*(\d{1,2})\b")
# Точки в инициалах необязательны: движки с детекцией их часто теряют
# ('Кубанева ЕА', 'Коренькова Т Н'). Хвостовой lookahead не даёт склеить
# сокращение вроде 'Основы БЖД' в мнимое ФИО.
_TEACHER_RE = re.compile(
    r"([А-ЯЁ][а-яё]+(?:[-–—‑][А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ])\s*\.?\s*([А-ЯЁ])(?![А-ЯЁа-яё])\s*\.?"
)
_LESSON_START_RE = re.compile(r"^[|\[(\s]*([\dlI!i]{1,2})\s*[.)\]|]?\s*(.+)$")
_CLASSROOM_RE = re.compile(r"^[\w][\w\-/\\. ]*$", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


class OcrEngineError(RuntimeError):
    """OCR-движок недоступен или вернул ошибку."""


@dataclass(slots=True)
class TextBox:
    """Найденный движком блок текста с координатами.

    Движки с детекцией (EasyOCR, PaddleOCR) отдают именно такие блоки, а не
    готовые строки. Координаты важны: на фото экрана колонки таблицы часто
    съезжают по вертикали, и собирать строку по порядку слов нельзя.
    """

    text: str
    left: float
    top: float
    right: float
    bottom: float
    confidence: float = 1.0

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)


def estimate_skew(boxes: Iterable[TextBox]) -> float:
    """Оценивает наклон таблицы: насколько y растёт с ростом x.

    Фото делают под углом, и правый край таблицы уезжает вверх. На реальном
    снимке аудитория '301' оказалась на 80 пикселей выше своего номера пары —
    без поправки блоки склеиваются в соседние строки.

    Наклон подбирается напрямую по тому, что нам нужно: при верном угле блоки
    собираются в минимальное число плотных строк. Слишком большой угол, наоборот,
    разносит их, поэтому минимум хорошо выражен. При равном числе строк
    выигрывает вариант с меньшим вертикальным разбросом внутри строк.

    Опорная точка — левый край блока, а не центр: ширина у распознанных блоков
    гуляет, и центр вносит лишний шум.
    """
    items = [box for box in boxes if box.text.strip()]
    if len(items) < 4:
        return 0.0

    heights = sorted(box.height for box in items)
    row_height = max(8.0, heights[len(heights) // 2])
    tolerance = row_height * 0.6

    best_slope = 0.0
    best_key = (len(items) + 1, float("inf"))
    for step in range(-50, 51):
        slope = step * 0.002  # +-0.1 => примерно +-6 градусов
        corrected = sorted(box.top - slope * box.left for box in items)

        rows = 1
        spread = 0.0
        row_start = corrected[0]
        previous = corrected[0]
        for value in corrected[1:]:
            if value - previous > tolerance:
                spread += previous - row_start
                rows += 1
                row_start = value
            previous = value
        spread += previous - row_start

        key = (rows, spread)
        if key < best_key:
            best_key = key
            best_slope = slope
    return best_slope


def _cluster_columns(boxes: list[TextBox]) -> list[list[TextBox]]:
    """Разбивает блоки на колонки таблицы по горизонтальному перекрытию."""
    columns: list[list[TextBox]] = []
    for box in sorted(boxes, key=lambda item: item.left):
        for column in columns:
            left = min(item.left for item in column)
            right = max(item.right for item in column)
            # Перекрытие хотя бы на четверть более узкого блока — та же колонка.
            overlap = min(right, box.right) - max(left, box.left)
            if overlap > 0.25 * min(right - left, box.right - box.left):
                column.append(box)
                break
        else:
            columns.append([box])
    return sorted(columns, key=lambda column: min(item.left for item in column))


def _estimate_row_pitch(columns: list[list[TextBox]]) -> float:
    """Оценивает шаг строк таблицы по расстояниям между верхами блоков.

    Рамки распознанных блоков высокие и почти касаются друг друга, поэтому
    зазор между ячейками ничего не говорит — а вот шаг между строками стабилен.
    Мелкие расстояния отбрасываются: это два куска одной ячейки, например
    разрезанное на 'Кузьминова' и 'ИН' ФИО.
    """
    heights = sorted(box.height for column in columns for box in column)
    if not heights:
        return 0.0
    median_height = heights[len(heights) // 2]

    diffs: list[float] = []
    for column in columns:
        tops = sorted(box.top for box in column)
        diffs.extend(
            second - first
            for first, second in zip(tops, tops[1:], strict=False)
            if second - first >= median_height * 0.5
        )
    if not diffs:
        return 0.0
    diffs.sort()
    return diffs[len(diffs) // 2]


def _merge_column_cells(column: list[TextBox], row_pitch: float) -> list[TextBox]:
    """Склеивает блоки одной колонки, попавшие в одну ячейку.

    EasyOCR часто режет ФИО на два блока ('Кузьминова' и 'ИН'). Внутри колонки
    x почти постоянен, поэтому перекос снимка здесь не мешает: новая ячейка
    начинается там, где расстояние между верхами близко к шагу строк.
    """
    threshold = row_pitch * 0.5 if row_pitch else 0.0
    cells: list[list[TextBox]] = []
    for box in sorted(column, key=lambda item: item.top):
        if cells and threshold and box.top - min(item.top for item in cells[-1]) < threshold:
            cells[-1].append(box)
        else:
            cells.append([box])

    merged: list[TextBox] = []
    for cell in cells:
        ordered = sorted(cell, key=lambda item: item.left)
        merged.append(
            TextBox(
                text=" ".join(item.text.strip() for item in ordered),
                left=min(item.left for item in cell),
                top=min(item.top for item in cell),
                right=max(item.right for item in cell),
                bottom=max(item.bottom for item in cell),
                confidence=min(item.confidence for item in cell),
            )
        )
    return merged


def rows_from_boxes(boxes: Iterable[TextBox]) -> list[str]:
    """Собирает строки таблицы по колонкам, а не по горизонтали.

    На фото, снятом под углом, аудитория может оказаться на 80 пикселей выше
    своего номера пары — сопоставлять ячейки по y там бесполезно. Зато порядок
    ячеек внутри колонки перспектива не меняет: n-я запись в колонке
    'Дисциплина' относится к той же паре, что n-я в колонке 'Ауд.'.

    Поэтому блоки делятся на колонки по x, внутри каждой сортируются сверху
    вниз, а строка собирается по одинаковому порядковому номеру.
    """
    items = sorted((box for box in boxes if box.text.strip()), key=lambda box: box.top)
    if not items:
        return []

    heights = sorted(box.height for box in items)
    row_height = max(8.0, heights[len(heights) // 2])

    # Таблица каждого дня разбирается отдельно, иначе колонки разных дней
    # склеятся и пары уедут в чужие даты.
    lines: list[str] = []
    block: list[TextBox] = []
    seen_date = False

    def flush(current: list[TextBox]) -> None:
        if not current:
            return
        if not seen_date:
            # Шапка страницы: заголовок и название группы идут отдельными строками.
            lines.extend(box.text.strip() for box in sorted(current, key=lambda item: item.top))
            return
        lines.extend(_table_rows(current, row_height))

    for box in items:
        if _DATE_RE.search(box.text):
            flush(block)
            block = []
            lines.append(box.text.strip())
            seen_date = True
            continue
        block.append(box)
    flush(block)
    return lines


def _table_rows(boxes: list[TextBox], row_height: float) -> list[str]:
    raw_columns = _cluster_columns(boxes)
    row_pitch = _estimate_row_pitch(raw_columns) or row_height
    columns = [_merge_column_cells(column, row_pitch) for column in raw_columns]
    columns = [column for column in columns if column]
    if len(columns) < 2:
        return lines_from_boxes(boxes)

    depth = max(len(column) for column in columns)
    rows: list[str] = []
    for index in range(depth):
        parts = [(column[index].left, column[index].text) for column in columns if index < len(column)]
        if parts:
            rows.append(" ".join(text for _left, text in sorted(parts)))
    return rows


def lines_from_boxes(
    boxes: Iterable[TextBox],
    *,
    row_tolerance: float = 0.6,
    deskew: bool = True,
) -> list[str]:
    """Склеивает блоки в строки таблицы с поправкой на наклон снимка."""
    items = [box for box in boxes if box.text.strip()]
    if not items:
        return []

    slope = estimate_skew(items) if deskew else 0.0

    def corrected_y(box: TextBox) -> float:
        return box.top - slope * box.left

    items.sort(key=lambda box: (corrected_y(box), box.left))
    rows: list[list[TextBox]] = [[items[0]]]
    for box in items[1:]:
        current = rows[-1]
        row_center = sum(corrected_y(item) for item in current) / len(current)
        row_height = sum(item.height for item in current) / len(current)
        if abs(corrected_y(box) - row_center) <= max(8.0, row_height * row_tolerance):
            current.append(box)
        else:
            rows.append([box])

    return [
        " ".join(box.text.strip() for box in sorted(row, key=lambda item: item.left))
        for row in rows
    ]


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


def fix_homoglyphs(value: str) -> str:  # noqa: D401
    """Чинит латиницу, подставленную вместо кириллицы.

    Если в строке уже преобладает кириллица, латинские двойники правятся целиком
    по строке. Иначе — только внутри смешанных слов, чтобы не портить настоящую
    латиницу вроде 'IT' или 'Web'.
    """
    letters = [char for char in value if char.isalpha()]
    if letters:
        cyrillic_share = sum(1 for char in letters if _CYRILLIC_RE.match(char)) / len(letters)
        if cyrillic_share >= 0.3:
            return value.translate(_LATIN_TO_CYRILLIC)

    def _fix_token(match: re.Match[str]) -> str:
        token = match.group(0)
        if not _CYRILLIC_RE.search(token):
            return token
        return token.translate(_LATIN_TO_CYRILLIC)

    return re.sub(r"\S+", _fix_token, value)


def fix_digits_in_words(value: str) -> str:
    """Возвращает буквы на место цифр внутри кириллических слов."""

    def _fix_token(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(_CYRILLIC_RE.findall(token)) < 3:
            return token
        return re.sub(
            r"(?<=[А-Яа-яЁё])(\d)(?=[А-Яа-яЁё])",
            lambda digit: digit.group(1).translate(_WORD_DIGITS),
            token,
        )

    return re.sub(r"\S+", _fix_token, value)


def fix_inner_caps(value: str) -> str:
    """Гасит заглавные буквы, застрявшие внутри слова ('КубаНеВа' -> 'Кубанева').

    Появляются после починки гомоглифов: Tesseract отдаёт заглавную латинскую
    'H' там, где на картинке строчная кириллическая 'н'.
    """

    def _fix_token(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(_CYRILLIC_RE.findall(token)) < 3:
            return token
        return re.sub(
            r"(?<=[а-яё])([А-ЯЁ])(?=[а-яё])",
            lambda inner: inner.group(1).lower(),
            token,
        )

    return re.sub(r"\S+", _fix_token, value)


def normalize_line(raw_line: str) -> str:
    line = raw_line.replace(" ", " ").replace("\t", " ")
    line = fix_homoglyphs(line)
    line = fix_digits_in_words(line)
    line = fix_inner_caps(line)
    return " ".join(line.split()).strip(" |")


def is_noise_line(line: str) -> bool:
    """Шапка таблицы, заголовок страницы или строка без полезных данных."""
    folded = _fold(line)
    if not folded:
        return True
    tokens = folded.split()
    header_hits = sum(1 for token in tokens if token in _NOISE_TOKENS)
    if header_hits >= 2:
        return True
    if header_hits and len(tokens) <= 2:
        return True
    # Строка без единой буквы и без цифр (артефакты рамок таблицы).
    return not re.search(r"[\w]", folded, flags=re.UNICODE)


def _fix_number_token(token: str) -> int | None:
    digits = token.translate(_DIGIT_LOOKALIKES)
    digits = re.sub(r"\D", "", digits)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _parse_date_header(line: str, now: datetime) -> tuple[str, str] | None:
    """Возвращает (date_iso, date_label), если строка — заголовок дня."""
    match = _DATE_RE.search(line)
    if match is None:
        return None

    # После удаления даты в строке должен остаться только мусор вида "г." —
    # иначе это строка с парой, в которой случайно оказалась дата.
    leftover = (line[: match.start()] + " " + line[match.end() :]).strip()
    leftover_letters = re.sub(r"[^А-Яа-яЁёA-Za-z]", "", leftover)
    if len(leftover) > 10 or len(leftover_letters) > 2:
        return None

    day_raw, month_raw, year_raw = match.groups()
    try:
        day = int(day_raw)
        month = int(month_raw)
        year = int(year_raw)
    except ValueError:
        return None
    if year < 100:
        year += 2000
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        parsed = datetime(year, month, day)  # noqa: DTZ001 - дата без времени
    except ValueError:
        return None

    if abs((parsed.date() - now.date()).days) > MAX_DATE_DRIFT_DAYS:
        return None
    return parsed.date().isoformat(), f"{day:02d}.{month:02d}.{year:04d}"


def _is_classroom_token(token: str, vocabulary: OcrVocabulary | None = None) -> bool:
    """Похож ли хвостовой токен на аудиторию ('301', '305/2', 'с-з')."""
    if not token or len(token) > MAX_CLASSROOM_LENGTH:
        return False
    if vocabulary is not None:
        folded = _fold(token)
        if any(_fold(known) == folded for known in vocabulary.classrooms):
            return True
    if not _CLASSROOM_RE.match(token):
        return False
    if any(char.isdigit() for char in token):
        return True
    # Буквенный код без цифр допустим только коротким: 'с-з', 'сз'.
    return len(token) <= 6 and "-" in token


def _split_trailing_classroom(rest: str, vocabulary: OcrVocabulary | None) -> tuple[str, str]:
    parts = rest.rsplit(" ", 1)
    if len(parts) == 2 and _is_classroom_token(parts[1], vocabulary):
        return parts[0].strip(" |,;"), parts[1].strip(" |,;")
    return rest, ""


def _split_subject_and_teacher(body: str, vocabulary: OcrVocabulary | None) -> tuple[str, str]:
    """Делит остаток на дисциплину и преподавателя, когда у ФИО нет инициалов.

    Инициалы — самый надёжный якорь, но преподаватель может быть записан одним
    словом ('Консультирующий'). Тогда опираемся на словарь прошлых снимков,
    а в крайнем случае — на сокращение с точкой ('Консульт. Консультирующий').
    """
    folded_body = _fold(body)

    if vocabulary is not None:
        for known_teacher in vocabulary.teachers:
            folded_teacher = _fold(known_teacher)
            if folded_teacher and folded_body.endswith(folded_teacher) and folded_body != folded_teacher:
                cut = len(body)
                while cut > 0 and _fold(body[:cut]) != folded_body[: -len(folded_teacher)].strip():
                    cut -= 1
                subject = body[:cut].strip(" |,;") if cut else ""
                if subject:
                    return subject, body[cut:].strip(" |,;")

        for known_subject in vocabulary.subjects:
            folded_subject = _fold(known_subject)
            if folded_subject and folded_body.startswith(folded_subject) and folded_body != folded_subject:
                cut = 0
                while cut < len(body) and _fold(body[: cut + 1]) != folded_subject:
                    cut += 1
                teacher = body[cut + 1 :].strip(" |,;") if cut < len(body) else ""
                if teacher:
                    return body[: cut + 1].strip(" |,;"), teacher

    # 'Консульт. Консультирующий' — точка сокращения плюс заглавная после неё.
    abbreviation = re.match(r"^(.+?\.)\s+([А-ЯЁ]\S.*)$", body)
    if abbreviation is not None:
        return abbreviation.group(1).strip(), abbreviation.group(2).strip()

    return body.strip(), ""


def _split_lesson_row(rest: str, vocabulary: OcrVocabulary | None = None) -> tuple[str, str, str, bool]:
    """Делит хвост строки на (дисциплина, преподаватель, аудитория, по_шаблону)."""
    teacher_match = _TEACHER_RE.search(rest)
    if teacher_match is not None:
        subject = rest[: teacher_match.start()].strip(" |,;")
        teacher = f"{teacher_match.group(1)} {teacher_match.group(2)}.{teacher_match.group(3)}."
        classroom = rest[teacher_match.end() :].strip(" |,;.")
        return subject.strip(), teacher, classroom.strip(), True

    parts = [part.strip(" |,;") for part in re.split(r"\s{2,}|\s*\|\s*", rest) if part.strip(" |,;")]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1], False
    if len(parts) == 2:
        return parts[0], parts[1], "", False

    body, classroom = _split_trailing_classroom(rest, vocabulary)
    subject, teacher = _split_subject_and_teacher(body, vocabulary)
    return subject, teacher, classroom, False


def _parse_lesson_line(line: str, vocabulary: OcrVocabulary | None = None) -> tuple[int, str, str, str, bool] | None:
    match = _LESSON_START_RE.match(line)
    if match is None:
        return None

    number = _fix_number_token(match.group(1))
    if number is None or not (MIN_LESSON_NUMBER <= number <= MAX_LESSON_NUMBER):
        return None

    rest = match.group(2).strip(" |.,")
    if len(rest) < MIN_SUBJECT_LENGTH or not _CYRILLIC_RE.search(rest):
        return None

    subject, teacher, classroom, structured = _split_lesson_row(rest, vocabulary)
    if not subject:
        return None
    return number, subject, teacher, classroom, structured


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
    """Есть ли в значении символы, которых в расписании быть не может.

    '51!/2М' — это испорченное распознаванием '511/2М', а '305/1' — честная
    другая аудитория. Первое чинить можно, второе нельзя.
    """
    return bool(re.search(r"[^\w\s/\\.\-]", value, flags=re.UNICODE))


def _is_compatible(value: str, option: str) -> bool:
    """Отсекает замены, меняющие смысл: другую аудиторию или другого человека.

    Похожесть по символам этого не видит: у '305/1' и '305/2' она 0.80, а у
    'Травкин А.В.' и 'Травкина Е.А.' — 0.78, хотя это разные аудитория и
    преподаватель. Цифры и инициалы должны совпадать точно — кроме случая,
    когда значение явно побито распознаванием.
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


class OcrScheduleParser:
    """Разбор распознанного текста в `ScheduleSnapshot` с проверкой и фильтрацией."""

    def __init__(
        self,
        engine: TesseractOcrEngine | None = None,
        *,
        fuzzy_threshold: float = 0.78,
        min_confidence: float = 0.6,
    ) -> None:
        self.engine = engine
        self.fuzzy_threshold = fuzzy_threshold
        self.min_confidence = min_confidence

    async def recognize_image(self, image_bytes: bytes) -> str:
        if self.engine is None:
            raise OcrEngineError("OCR-движок не настроен.")
        return await asyncio.to_thread(self.engine.recognize, image_bytes)

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

        group_name = ""
        days: list[DaySchedule] = []
        seen_dates: set[str] = set()
        current_day: DaySchedule | None = None

        for raw_line in (text or "").splitlines():
            line = normalize_line(raw_line)
            if not line or is_noise_line(line):
                continue

            date_header = _parse_date_header(line, reference_now)
            if date_header is not None:
                date_iso, date_label = date_header
                if date_iso in seen_dates:
                    issues.append(
                        OcrIssue(
                            "warning",
                            f"Дата {format_human_date(date_label)} встречается в фото несколько раз — оставлен первый блок.",
                            date_iso=date_iso,
                        )
                    )
                    current_day = next((day for day in days if day.date_iso == date_iso), None)
                    continue
                seen_dates.add(date_iso)
                current_day = DaySchedule(date_label=date_label, date_iso=date_iso, lessons=[])
                days.append(current_day)
                continue

            if not group_name:
                group_match = _GROUP_RE.search(line)
                if group_match is not None:
                    group_name = f"{group_match.group(1)}-{group_match.group(2)}-{group_match.group(3)}"
                    if _fold(line) == _fold(group_name):
                        continue

            parsed_lesson = _parse_lesson_line(line, vocab)
            if parsed_lesson is None:
                skipped_lines.append(line)
                continue

            if current_day is None:
                skipped_lines.append(line)
                issues.append(
                    OcrIssue("warning", f"Строка «{line}» идёт до первой даты и пропущена.")
                )
                continue

            number, subject, teacher, classroom, structured = parsed_lesson
            score = self._score_and_correct(
                current_day,
                number,
                subject,
                teacher,
                classroom,
                structured,
                vocab,
                issues,
                corrections,
            )
            lesson_scores.append(score)

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
        structured: bool,
        vocab: OcrVocabulary,
        issues: list[OcrIssue],
        corrections: list[OcrCorrection],
    ) -> float:
        score_parts: list[float] = [1.0 if structured else 0.55]

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
            return value, 0.8

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
        return corrected, max(score, 0.5) if score else 0.6

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


class EasyOcrEngine:
    """Распознавание через EasyOCR — детекция текста плюс распознавание.

    В отличие от Tesseract сначала нейросетью находит рамки с текстом, а уже
    потом читает каждую. На фотографиях экрана — с муаром, рамками таблицы и
    съёмкой под углом — это работает несопоставимо лучше: Tesseract на таком
    снимке не вытащил ни одной пары, EasyOCR прочитал все.

    Тяжёлый: тянет torch. Поэтому импортируется лениво, а Tesseract остаётся
    лёгкой запасной опцией.
    """

    name = "easyocr"

    def __init__(
        self,
        *,
        languages: str = "ru",
        min_confidence: float = 0.15,
        gpu: bool = False,
    ) -> None:
        self.languages = [part.strip() for part in languages.replace("+", ",").split(",") if part.strip()] or ["ru"]
        self.min_confidence = min_confidence
        self.gpu = gpu
        self._reader = None

    def availability(self) -> tuple[bool, str]:
        try:
            import easyocr  # noqa: F401
        except ImportError:
            return False, "EasyOCR не установлен. Поставь пакет easyocr или переключись на OCR_ENGINE=tesseract."
        return True, f"easyocr ({', '.join(self.languages)})"

    def _get_reader(self):
        if self._reader is None:
            import easyocr

            started = time.monotonic()
            logger.info("Загружаю модели EasyOCR (%s)...", ", ".join(self.languages))
            self._reader = easyocr.Reader(self.languages, gpu=self.gpu, verbose=False)
            logger.info("Модели EasyOCR загружены за %.1f с.", time.monotonic() - started)
        return self._reader

    def warm_up(self) -> None:
        """Заранее поднимает модели, чтобы первое фото не ждало их загрузки.

        Без прогрева первое распознавание тянет загрузку и инициализацию моделей
        прямо внутри запроса: админ видит «Распознаю...» и тишину на минуты.
        """
        self._get_reader()

    def detect(self, image_bytes: bytes) -> list[TextBox]:
        if not image_bytes:
            raise OcrEngineError("Пустое изображение.")
        try:
            import numpy as np
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as image:
                prepared = ImageOps.exif_transpose(image).convert("RGB")
                longest = max(prepared.width, prepared.height)
                if longest > MAX_DETECTION_SIDE:
                    scale = MAX_DETECTION_SIDE / longest
                    prepared = prepared.resize(
                        (max(1, int(prepared.width * scale)), max(1, int(prepared.height * scale))),
                        Image.LANCZOS,
                    )
                array = np.array(prepared)
            started = time.monotonic()
            detections = self._get_reader().readtext(array, detail=1, paragraph=False)
            logger.info(
                "EasyOCR распознал %s блоков за %.1f с (кадр %sx%s).",
                len(detections), time.monotonic() - started, array.shape[1], array.shape[0],
            )
        except OcrEngineError:
            raise
        except Exception as exc:
            raise OcrEngineError(f"Ошибка распознавания EasyOCR: {exc}") from exc

        boxes: list[TextBox] = []
        for box, text, confidence in detections:
            if not str(text).strip() or float(confidence) < self.min_confidence:
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            boxes.append(
                TextBox(
                    text=str(text),
                    left=min(xs),
                    top=min(ys),
                    right=max(xs),
                    bottom=max(ys),
                    confidence=float(confidence),
                )
            )
        return boxes

    def recognize(self, image_bytes: bytes) -> str:
        return "\n".join(rows_from_boxes(self.detect(image_bytes)))


class TesseractOcrEngine:
    """Распознавание через Tesseract — бесплатный офлайн-движок.

    Работает и через `pytesseract`, и напрямую через бинарник `tesseract`,
    поэтому Python-зависимость не обязательна.
    """

    name = "tesseract"

    def __init__(
        self,
        *,
        command: str = "",
        languages: str = "rus+eng",
        psm: int = 6,
        timeout: float = 60.0,
    ) -> None:
        self.command = command.strip()
        self.languages = languages or "rus"
        self.psm = psm
        self.timeout = timeout

    def resolve_command(self) -> str | None:
        if self.command:
            return self.command
        return shutil.which("tesseract")

    def availability(self) -> tuple[bool, str]:
        binary = self.resolve_command()
        if not binary:
            return False, (
                "Tesseract не найден. Установи пакет tesseract-ocr с языком rus "
                "или укажи путь в переменной OCR_TESSERACT_CMD."
            )
        try:
            completed = subprocess.run(  # noqa: S603
                [binary, "--version"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Tesseract не запускается: {exc}"
        if completed.returncode != 0:
            return False, "Tesseract вернул ошибку при проверке версии."
        version = (completed.stdout or b"").decode("utf-8", "replace").splitlines()
        return True, version[0].strip() if version else "tesseract"

    def warm_up(self) -> None:
        """Tesseract поднимается процессом на каждый вызов, греть нечего."""
        return None

    def recognize(self, image_bytes: bytes) -> str:
        if not image_bytes:
            raise OcrEngineError("Пустое изображение.")
        prepared = preprocess_image(image_bytes)
        text = self._recognize_with_pytesseract(prepared)
        if text is None:
            text = self._recognize_with_binary(prepared)
        return text

    def _recognize_with_pytesseract(self, image_bytes: bytes) -> str | None:
        try:
            import pytesseract
            from PIL import Image
        except ImportError:
            return None

        binary = self.resolve_command()
        if binary:
            pytesseract.pytesseract.tesseract_cmd = binary
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                return pytesseract.image_to_string(
                    image,
                    lang=self.languages,
                    config=f"--oem 3 --psm {self.psm}",
                    timeout=self.timeout,
                )
        except Exception as exc:  # pytesseract оборачивает ошибки в свои классы
            raise OcrEngineError(f"Ошибка распознавания: {exc}") from exc

    def _recognize_with_binary(self, image_bytes: bytes) -> str:
        binary = self.resolve_command()
        if not binary:
            raise OcrEngineError(self.availability()[1])
        try:
            completed = subprocess.run(  # noqa: S603
                [binary, "stdin", "stdout", "-l", self.languages, "--oem", "3", "--psm", str(self.psm)],
                input=image_bytes,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OcrEngineError(f"Не удалось запустить Tesseract: {exc}") from exc
        if completed.returncode != 0:
            details = (completed.stderr or b"").decode("utf-8", "replace").strip()
            raise OcrEngineError(f"Tesseract завершился с ошибкой: {details[:300]}")
        return (completed.stdout or b"").decode("utf-8", "replace")


def preprocess_image(image_bytes: bytes, *, target_width: int = 1800) -> bytes:
    """Подготовка фото к распознаванию: серый, контраст, апскейл, резкость.

    Если Pillow не установлен, изображение отдаётся движку как есть.
    """
    try:
        from PIL import Image, ImageFilter, ImageOps
    except ImportError:
        return image_bytes

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("L")
            if image.width < target_width:
                scale = min(3.0, target_width / max(1, image.width))
                new_size = (int(image.width * scale), int(image.height * scale))
                image = image.resize(new_size, Image.LANCZOS)
            image = ImageOps.autocontrast(image, cutoff=1)
            image = image.filter(ImageFilter.MedianFilter(size=3))
            image = image.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=3))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception as exc:
        logger.warning("Предобработка изображения не удалась, используем оригинал: %s", exc)
        return image_bytes


def build_ocr_engine(
    *,
    engine: str = "easyocr",
    command: str = "",
    languages: str = "rus+eng",
    psm: int = 6,
    timeout: float = 60.0,
) -> TesseractOcrEngine | EasyOcrEngine:
    """Собирает движок по имени, молча откатываясь на доступный.

    По умолчанию EasyOCR: на фотографиях он заметно надёжнее. Tesseract лёгкий
    и остаётся запасным — если easyocr не установлен, берём его.
    """
    requested = (engine or "").strip().lower()
    if requested in {"", "auto", "easyocr"}:
        easy = EasyOcrEngine(languages=_easyocr_languages(languages))
        if easy.availability()[0]:
            return easy
        if requested == "easyocr":
            return easy  # вернём как есть, чтобы админ увидел причину недоступности
        logger.warning("EasyOCR недоступен, используем Tesseract.")
    return TesseractOcrEngine(command=command, languages=languages, psm=psm, timeout=timeout)


def _easyocr_languages(tesseract_languages: str) -> str:
    """Переводит коды языков Tesseract ('rus+eng') в коды EasyOCR.

    Кириллическая модель EasyOCR уже читает латиницу и цифры, поэтому 'en'
    рядом с 'ru' ничего не добавляет, зато меняет результат детекции: на
    тестовом фото пара 'ru'+'en' теряла аудиторию 'с-з'. Оставляем только 'ru'.
    """
    mapping = {"rus": "ru", "eng": "en", "ru": "ru", "en": "en"}
    codes = [
        mapping.get(part.strip().lower(), part.strip().lower())
        for part in tesseract_languages.replace("+", ",").split(",")
        if part.strip()
    ]
    if "ru" in codes:
        return "ru"
    return ",".join(dict.fromkeys(codes)) or "ru"

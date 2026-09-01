"""Регрессия на настоящем фото расписания, снятом с монитора.

Координаты и тексты — фактический вывод EasyOCR по файлу
photo_2026-09-01_10-47-53.jpg. Сам движок здесь не запускается: тест проверяет
всё, что идёт после распознавания — сборку таблицы по колонкам, нормализацию,
сверку со словарём и итоговый текст для пользователя.

Снимок сделан под углом, и правая часть таблицы уехала вверх почти на строку:
аудитория '301' лежит на y=413, а её номер пары — на y=494. Именно на этом
разваливалась сборка строк по горизонтали.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from src.ocr_schedule import OcrScheduleParser, OcrVocabulary, TextBox, rows_from_boxes
from src.schedule_service import ScheduleFormatter

NOW = datetime(2026, 9, 1, 10, 47, 0)

# (left, top, right, bottom, confidence, text)
DETECTIONS = [
    (574, 42, 1360, 198, 1.00, "расписание занятий"),
    (47, 107, 585, 232, 1.00, "Персональное"),
    (548, 214, 853, 305, 0.66, "ИСП-25-1"),
    (502, 319, 907, 407, 0.93, "01.09.2026 г:"),
    (1291, 368, 1385, 418, 1.00, "Ауд"),
    (915, 380, 1222, 441, 1.00, "Преподаватель"),
    (1294, 413, 1377, 466, 1.00, "301"),
    (392, 415, 629, 468, 1.00, "Дисциплина"),
    (858, 424, 1141, 497, 0.98, "Кубанева ЕА"),
    (65, 445, 165, 487, 0.98, "Пара"),
    (175, 448, 816, 541, 0.80, "Операционные системы и среды"),
    (1104, 478, 1202, 533, 0.93, "ИН"),
    (1303, 483, 1369, 521, 0.22, "сз"),
    (862, 490, 1107, 550, 1.00, "Кузьминова"),
    (110, 494, 134, 538, 1.00, "1"),
    (183, 516, 598, 589, 0.99, "Физическая культура"),
    (116, 542, 142, 582, 1.00, "2"),
    (522, 578, 912, 658, 0.98, "02.09.2026 г:"),
    (1285, 628, 1367, 671, 0.92, "Ауд"),
    (905, 637, 1198, 689, 0.91, "Преподаватель"),
    (1249, 663, 1406, 724, 0.96, "51!/2М"),
    (399, 665, 626, 714, 0.97, "Дисциплина"),
    (849, 673, 1175, 747, 0.80, "Коренькова Т Н"),
    (84, 688, 177, 730, 1.00, "Пара"),
    (188, 697, 821, 778, 0.99, "Основы апгоритмизации и прогр:"),
    (1287, 723, 1361, 767, 0.83, "301"),
    (1040, 730, 1119, 781, 0.95, "ЕА"),
    (124, 736, 148, 776, 1.00, "1"),
    (854, 740, 1042, 794, 1.00, "Кубанева"),
    (195, 747, 810, 826, 0.82, "Операционные системы и среды"),
    (130, 782, 158, 824, 1.00, "2"),
    (544, 818, 918, 893, 0.93, "03.09.2026 г:"),
    (1279, 871, 1361, 907, 0.28, "АУД"),
    (925, 877, 1210, 927, 0.75, "Преподаватель"),
    (438, 900, 657, 943, 0.51, "Дисциплина"),
    (133, 919, 223, 957, 0.94, "Пзра"),
    (555, 950, 919, 1018, 0.75, "04.09.2026 г"),
    (1275, 1003, 1355, 1039, 0.63, "шиш"),
    (928, 1009, 1205, 1053, 0.96, "Преподаватель"),
    (450, 1028, 665, 1069, 0.99, "Дисциплина"),
    (152, 1042, 239, 1082, 1.00, "Пара"),
]

BOXES = [
    TextBox(text=text, left=left, top=top, right=right, bottom=bottom, confidence=confidence)
    for left, top, right, bottom, confidence, text in DETECTIONS
]

# Значения из прошлых снимков с сайта — так словарь выглядит в бою.
VOCABULARY = OcrVocabulary(
    subjects=("Операционные системы и среды", "Физическая культура", "Основы алгоритмизации и прогр."),
    teachers=("Кубанева Е.А.", "Кузьминова И.Н.", "Коренькова Т.Н."),
    classrooms=("301", "с-з", "511/2М"),
)

EXPECTED_LESSONS = {
    ("2026-09-01", 1, "Операционные системы и среды", "Кубанева Е.А.", "301"),
    ("2026-09-01", 2, "Физическая культура", "Кузьминова И.Н.", "с-з"),
    ("2026-09-02", 1, "Основы алгоритмизации и прогр.", "Коренькова Т.Н.", "511/2М"),
    ("2026-09-02", 2, "Операционные системы и среды", "Кубанева Е.А.", "301"),
}


def parse(vocabulary: OcrVocabulary | None):
    return OcrScheduleParser().parse_text("\n".join(rows_from_boxes(BOXES)), vocabulary=vocabulary, now=NOW)


def lessons_of(result) -> set[tuple]:
    return {
        (day.date_iso, lesson.number, lesson.subject, lesson.teacher, lesson.classroom)
        for day in result.snapshot.days
        for lesson in day.lessons
    }


class RowAssemblyTests(unittest.TestCase):
    def test_columns_are_paired_correctly_despite_skew(self) -> None:
        rows = rows_from_boxes(BOXES)

        self.assertIn("1 Операционные системы и среды Кубанева ЕА 301", rows)
        self.assertIn("2 Физическая культура Кузьминова ИН сз", rows)
        self.assertIn("1 Основы апгоритмизации и прогр: Коренькова Т Н 51!/2М", rows)
        self.assertIn("2 Операционные системы и среды Кубанева ЕА 301", rows)

    def test_days_are_not_mixed(self) -> None:
        rows = rows_from_boxes(BOXES)
        first_day = rows.index("01.09.2026 г:")
        second_day = rows.index("02.09.2026 г:")

        # Пары первого дня обязаны лежать между двумя заголовками дат.
        physical = next(index for index, row in enumerate(rows) if "Физическая культура" in row)
        self.assertLess(first_day, physical)
        self.assertLess(physical, second_day)

    def test_split_name_boxes_are_joined(self) -> None:
        """EasyOCR разрезал ФИО на 'Кузьминова' и 'ИН' — они одна ячейка."""
        self.assertTrue(any("Кузьминова ИН" in row for row in rows_from_boxes(BOXES)))


class RealPhotoParsingTests(unittest.TestCase):
    def test_all_lessons_recognised_with_vocabulary(self) -> None:
        result = parse(VOCABULARY)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.snapshot.group_name, "ИСП-25-1")
        self.assertEqual(lessons_of(result), EXPECTED_LESSONS)

    def test_all_four_dates_found(self) -> None:
        result = parse(VOCABULARY)
        self.assertEqual(
            [day.date_iso for day in result.snapshot.days],
            ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"],
        )

    def test_empty_days_stay_empty(self) -> None:
        result = parse(VOCABULARY)
        by_date = {day.date_iso: day for day in result.snapshot.days}
        self.assertEqual(by_date["2026-09-03"].lessons, [])
        self.assertEqual(by_date["2026-09-04"].lessons, [])

    def test_confidence_is_high(self) -> None:
        self.assertGreaterEqual(parse(VOCABULARY).confidence, 0.9)

    def test_initials_get_their_dots_back(self) -> None:
        """'Кубанева ЕА' и 'Коренькова Т Н' — точки теряет сам движок."""
        teachers = {lesson.teacher for day in parse(VOCABULARY).snapshot.days for lesson in day.lessons}
        self.assertEqual(teachers, {"Кубанева Е.А.", "Кузьминова И.Н.", "Коренькова Т.Н."})

    def test_damaged_values_are_repaired(self) -> None:
        corrections = {(c.raw, c.corrected) for c in parse(VOCABULARY).corrections}

        self.assertIn(("сз", "с-з"), corrections)
        self.assertIn(("51!/2М", "511/2М"), corrections)
        self.assertIn(("Основы апгоритмизации и прогр:", "Основы алгоритмизации и прогр."), corrections)

    def test_structure_survives_without_vocabulary(self) -> None:
        """Без словаря повреждённые значения остаются, но строки собраны верно."""
        result = parse(None)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.lessons_count, 4)
        self.assertEqual(len(result.snapshot.days), 4)

    def test_user_facing_output(self) -> None:
        result = parse(VOCABULARY)
        by_date = {day.date_iso: day for day in result.snapshot.days}

        self.assertEqual(
            ScheduleFormatter.format_day_plain(by_date["2026-09-01"]),
            "Расписание на 1 сентября 2026 года\n"
            "\n"
            "1. в 301 по Операционные системы и среды у Кубанева Е.А.\n"
            "2. в с-з по Физическая культура у Кузьминова И.Н.",
        )
        self.assertEqual(
            ScheduleFormatter.format_day_plain(by_date["2026-09-02"]),
            "Расписание на 2 сентября 2026 года\n"
            "\n"
            "1. в 511/2М по Основы алгоритмизации и прогр. у Коренькова Т.Н.\n"
            "2. в 301 по Операционные системы и среды у Кубанева Е.А.",
        )


if __name__ == "__main__":
    unittest.main()

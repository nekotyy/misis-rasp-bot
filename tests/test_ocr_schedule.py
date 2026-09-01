from __future__ import annotations

import unittest
from datetime import datetime

from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.ocr_schedule import (
    OcrScheduleParser,
    OcrVocabulary,
    TextBox,
    _easyocr_languages,
    _snap_to_vocabulary,
    build_ocr_engine,
    fix_digits_in_words,
    fix_homoglyphs,
    fix_inner_caps,
    is_noise_line,
    lines_from_boxes,
    merge_ocr_days,
    normalize_line,
)
from src.parser import ScheduleParser, compute_snapshot_hash

NOW = datetime(2026, 9, 1, 10, 0, 0)

CLEAN_TEXT = """Персональное расписание занятий

ИСП-25-1

01.09.2026 г.
Пара Дисциплина Преподаватель Ауд.
1 Операционные системы и среды Кубанева Е.А. 301
2 Физическая культура Кузьминова И.Н. с-3

02.09.2026 г.
Пара Дисциплина Преподаватель Ауд.
1 Основы алгоритмизации и прогр. Коренькова Т.Н. 511/2М
2 Операционные системы и среды Кубанева Е.А. 301

03.09.2026 г.
Пара Дисциплина Преподаватель Ауд.
"""

# Типичный "грязный" вывод Tesseract: латиница вместо кириллицы,
# цифры вместо букв, 'l' вместо номера пары.
DIRTY_TEXT = """ИCП-25-1
01.09.2026 г.
| Пара | Дисциплина | Преподаватель | Ауд. |
l Onepaционные cистемы и cреды Ky6aHeBa E.A. 301
2 Физическaя культурa Кузьминовa И.Н. c-3
"""

VOCABULARY = OcrVocabulary(
    subjects=("Операционные системы и среды", "Физическая культура", "Основы алгоритмизации и прогр."),
    teachers=("Кубанева Е.А.", "Кузьминова И.Н.", "Коренькова Т.Н."),
    classrooms=("301", "с-3", "511/2М"),
)


class NormalizationTests(unittest.TestCase):
    def test_fix_homoglyphs_repairs_cyrillic_line(self) -> None:
        self.assertEqual(fix_homoglyphs("Физическaя культурa"), "Физическая культура")

    def test_fix_homoglyphs_keeps_real_latin(self) -> None:
        self.assertEqual(fix_homoglyphs("Web IT"), "Web IT")

    def test_fix_digits_in_words(self) -> None:
        self.assertEqual(fix_digits_in_words("Ку6анева"), "Кубанева")

    def test_fix_digits_in_words_keeps_classroom(self) -> None:
        self.assertEqual(fix_digits_in_words("511/2М"), "511/2М")
        self.assertEqual(fix_digits_in_words("с-3"), "с-3")

    def test_fix_inner_caps(self) -> None:
        self.assertEqual(fix_inner_caps("КубаНеВа"), "Кубанева")

    def test_fix_inner_caps_keeps_abbreviation(self) -> None:
        self.assertEqual(fix_inner_caps("ИСП-25-1"), "ИСП-25-1")

    def test_normalize_line_collapses_spaces(self) -> None:
        self.assertEqual(normalize_line("|  1   Математика  |"), "1 Математика")

    def test_is_noise_line_detects_table_header(self) -> None:
        self.assertTrue(is_noise_line("Пара Дисциплина Преподаватель Ауд."))
        self.assertTrue(is_noise_line("Персональное расписание занятий"))

    def test_is_noise_line_keeps_lesson_row(self) -> None:
        self.assertFalse(is_noise_line("1 Операционные системы и среды Кубанева Е.А. 301"))


class ParseTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = OcrScheduleParser()

    def test_parses_clean_photo_text(self) -> None:
        result = self.parser.parse_text(CLEAN_TEXT, now=NOW)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.snapshot.group_name, "ИСП-25-1")
        self.assertEqual(result.lessons_count, 4)
        self.assertEqual([day.date_iso for day in result.snapshot.days], ["2026-09-01", "2026-09-02", "2026-09-03"])
        self.assertEqual(result.skipped_lines, [])
        self.assertEqual(result.errors, [])

    def test_parses_lesson_columns(self) -> None:
        result = self.parser.parse_text(CLEAN_TEXT, now=NOW)
        first_day = result.snapshot.days[0]

        self.assertEqual(first_day.date_label, "01.09.2026")
        self.assertEqual(
            [(lesson.number, lesson.subject, lesson.teacher, lesson.classroom) for lesson in first_day.lessons],
            [
                (1, "Операционные системы и среды", "Кубанева Е.А.", "301"),
                (2, "Физическая культура", "Кузьминова И.Н.", "с-3"),
            ],
        )

    def test_day_without_lessons_is_kept_empty(self) -> None:
        result = self.parser.parse_text(CLEAN_TEXT, now=NOW)
        self.assertEqual(result.snapshot.days[-1].lessons, [])

    def test_vocabulary_repairs_dirty_ocr(self) -> None:
        result = self.parser.parse_text(DIRTY_TEXT, vocabulary=VOCABULARY, now=NOW)

        self.assertTrue(result.is_valid)
        lessons = result.snapshot.days[0].lessons
        self.assertEqual(len(lessons), 2)
        self.assertEqual(lessons[0].subject, "Операционные системы и среды")
        self.assertEqual(lessons[0].teacher, "Кубанева Е.А.")
        self.assertEqual(lessons[0].classroom, "301")
        self.assertEqual(lessons[1].classroom, "с-3")

    def test_lookalike_lesson_number_is_recovered(self) -> None:
        """Строка, начинающаяся с 'l' вместо '1', не должна теряться."""
        result = self.parser.parse_text(DIRTY_TEXT, vocabulary=VOCABULARY, now=NOW)
        self.assertEqual([lesson.number for lesson in result.snapshot.days[0].lessons], [1, 2])

    def test_corrections_are_reported(self) -> None:
        result = self.parser.parse_text(DIRTY_TEXT, vocabulary=VOCABULARY, now=NOW)
        corrected_fields = {correction.field for correction in result.corrections}
        self.assertIn("Дисциплина", corrected_fields)

    def test_empty_text_is_invalid(self) -> None:
        result = self.parser.parse_text("", now=NOW)

        self.assertFalse(result.is_valid)
        self.assertEqual(result.lessons_count, 0)
        self.assertTrue(result.errors)

    def test_text_without_dates_is_invalid(self) -> None:
        result = self.parser.parse_text("1 Математика Иванов И.И. 301", now=NOW)

        self.assertFalse(result.is_valid)
        self.assertTrue(any("не найдено ни одной даты" in issue.message for issue in result.errors))

    def test_far_away_date_is_not_a_day_header(self) -> None:
        result = self.parser.parse_text("01.09.1999 г.\n1 Математика Иванов И.И. 301", now=NOW)
        self.assertFalse(result.is_valid)

    def test_duplicate_lesson_number_keeps_first(self) -> None:
        text = (
            "ИСП-25-1\n01.09.2026 г.\n"
            "1 Математика Иванов И.И. 301\n"
            "1 Физика Петров П.П. 202\n"
        )
        result = self.parser.parse_text(text, now=NOW)

        lessons = result.snapshot.days[0].lessons
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0].subject, "Математика")
        self.assertTrue(any("дважды" in issue.message for issue in result.warnings))

    def test_gap_in_lesson_numbers_warns(self) -> None:
        text = (
            "ИСП-25-1\n01.09.2026 г.\n"
            "1 Математика Иванов И.И. 301\n"
            "3 Физика Петров П.П. 202\n"
        )
        result = self.parser.parse_text(text, now=NOW)
        self.assertTrue(any("пропущены номера пар" in issue.message for issue in result.warnings))

    def test_teacher_without_initials_is_split_by_abbreviation(self) -> None:
        """'Консульт. Консультирующий 305/2' — у преподавателя нет инициалов."""
        text = "ИСП-25-1\n15.06.2026 г.\n4 Консульт. Консультирующий 305/2\n"
        result = self.parser.parse_text(text, now=datetime(2026, 6, 15))

        lesson = result.snapshot.days[0].lessons[0]
        self.assertEqual(lesson.subject, "Консульт.")
        self.assertEqual(lesson.teacher, "Консультирующий")
        self.assertEqual(lesson.classroom, "305/2")

    def test_teacher_without_initials_uses_vocabulary(self) -> None:
        vocabulary = OcrVocabulary(
            subjects=("Проектная деятельность",),
            teachers=("Консультирующий",),
            classrooms=("305/2",),
        )
        text = "ИСП-25-1\n15.06.2026 г.\n4 Проектная деятельность Консультирующий 305/2\n"
        result = self.parser.parse_text(text, vocabulary=vocabulary, now=datetime(2026, 6, 15))

        lesson = result.snapshot.days[0].lessons[0]
        self.assertEqual(lesson.subject, "Проектная деятельность")
        self.assertEqual(lesson.teacher, "Консультирующий")
        self.assertEqual(lesson.classroom, "305/2")

    def test_lowercase_after_abbreviation_stays_in_subject(self) -> None:
        """'Информ. технологии' — точка внутри названия, а не граница ФИО."""
        text = "ИСП-25-1\n15.06.2026 г.\n1 Информ. технологии 214\n"
        result = self.parser.parse_text(text, now=datetime(2026, 6, 15))

        lesson = result.snapshot.days[0].lessons[0]
        self.assertEqual(lesson.subject, "Информ. технологии")
        self.assertEqual(lesson.classroom, "214")

    def test_sports_hall_classroom_survives_digit_misread(self) -> None:
        """Спортзал 'с-з' Tesseract читает как 'с-3' — словарь возвращает букву."""
        vocabulary = OcrVocabulary(
            subjects=("Физическая культура",),
            teachers=("Кузьминова И.Н.",),
            classrooms=("с-з",),
        )
        text = "ИСП-25-1\n15.06.2026 г.\n1 Физическая культура Кузьминова И.Н. с-3\n"
        result = self.parser.parse_text(text, vocabulary=vocabulary, now=datetime(2026, 6, 15))

        self.assertEqual(result.snapshot.days[0].lessons[0].classroom, "с-з")

    def test_cyrillic_classroom_is_kept_without_vocabulary(self) -> None:
        text = "ИСП-25-1\n15.06.2026 г.\n1 Физическая культура Кузьминова И.Н. с-з\n"
        result = self.parser.parse_text(text, now=datetime(2026, 6, 15))

        self.assertEqual(result.snapshot.days[0].lessons[0].classroom, "с-з")

    def test_slash_classroom_is_kept(self) -> None:
        text = "ИСП-25-1\n15.06.2026 г.\n3 Иностранный язык Травкина Е.А. 305/2\n"
        result = self.parser.parse_text(text, now=datetime(2026, 6, 15))

        self.assertEqual(result.snapshot.days[0].lessons[0].classroom, "305/2")

    def test_two_days_on_one_photo(self) -> None:
        text = (
            "ИСП-25-1\n"
            "15.06.2026 г.\nПара Дисциплина Преподаватель Ауд.\n"
            "1 Физика Амельчакова Е.А. 312\n"
            "2 Математика Набережных И.А. 316\n"
            "16.06.2026 г.\nПара Дисциплина Преподаватель Ауд.\n"
            "1 Информатика Петров П.П. 214\n"
        )
        result = self.parser.parse_text(text, now=datetime(2026, 6, 15))

        self.assertEqual([day.date_iso for day in result.snapshot.days], ["2026-06-15", "2026-06-16"])
        self.assertEqual([len(day.lessons) for day in result.snapshot.days], [2, 1])
        self.assertEqual(result.snapshot.days[1].lessons[0].subject, "Информатика")

    def test_user_facing_output_matches_bot_format(self) -> None:
        """Разобранный день должен рендериться ровно так, как его увидит пользователь."""
        from src.schedule_service import ScheduleFormatter

        text = (
            "ИСП-25-1\n15.06.2026 г.\nПара Дисциплина Преподаватель Ауд.\n"
            "1 Физика Амельчакова Е.А. 312\n"
            "2 Математика Набережных И.А. 316\n"
            "3 Иностранный язык Травкина Е.А. 305/2\n"
            "4 Консульт. Консультирующий 305/2\n"
        )
        result = self.parser.parse_text(text, now=datetime(2026, 6, 15))

        self.assertEqual(
            ScheduleFormatter.format_day_plain(result.snapshot.days[0]),
            "Расписание на 15 июня 2026 года\n"
            "\n"
            "1. в 312 по Физика у Амельчакова Е.А.\n"
            "2. в 316 по Математика у Набережных И.А.\n"
            "3. в 305/2 по Иностранный язык у Травкина Е.А.\n"
            "4. в 305/2 по Консульт. у Консультирующий",
        )

    def test_missing_classroom_warns(self) -> None:
        text = "ИСП-25-1\n01.09.2026 г.\n1 Математика Иванов И.И.\n"
        result = self.parser.parse_text(text, now=NOW)

        self.assertEqual(result.snapshot.days[0].lessons[0].classroom, "")
        self.assertTrue(any("не распознана аудитория" in issue.message for issue in result.warnings))

    def test_low_confidence_warns(self) -> None:
        parser = OcrScheduleParser(min_confidence=0.99)
        result = parser.parse_text(CLEAN_TEXT, now=NOW)
        self.assertTrue(any("Низкая уверенность" in issue.message for issue in result.warnings))

    def test_confidence_is_high_for_clean_text(self) -> None:
        result = self.parser.parse_text(CLEAN_TEXT, now=NOW)
        self.assertGreaterEqual(result.confidence, 0.8)

    def test_hash_matches_site_parser_for_same_data(self) -> None:
        """OCR-снимок и снимок с сайта с одинаковыми данными дают одинаковый хеш."""
        result = self.parser.parse_text(CLEAN_TEXT, now=NOW)
        site_parser = ScheduleParser("http://example.com/rasp/600")

        self.assertEqual(result.snapshot_hash(), site_parser.compute_hash(result.snapshot))


class EngineSelectionTests(unittest.TestCase):
    def test_russian_stays_alone(self) -> None:
        """'ru'+'en' меняет детекцию и теряет ячейки — кириллическая модель самодостаточна."""
        self.assertEqual(_easyocr_languages("rus+eng"), "ru")
        self.assertEqual(_easyocr_languages("rus"), "ru")

    def test_maps_tesseract_codes(self) -> None:
        self.assertEqual(_easyocr_languages("eng"), "en")

    def test_empty_defaults_to_russian(self) -> None:
        self.assertEqual(_easyocr_languages(""), "ru")

    def test_tesseract_requested_explicitly(self) -> None:
        engine = build_ocr_engine(engine="tesseract", command="/usr/bin/tesseract")
        self.assertEqual(engine.name, "tesseract")

    def test_easyocr_requested_explicitly(self) -> None:
        engine = build_ocr_engine(engine="easyocr")
        self.assertEqual(engine.name, "easyocr")


class LinesFromBoxesTests(unittest.TestCase):
    """Сборка строк из координатных блоков — основа для движков с детекцией."""

    def test_groups_boxes_into_rows_by_vertical_position(self) -> None:
        boxes = [
            TextBox("Кубанева Е.А.", 700, 100, 900, 130),
            TextBox("1", 60, 102, 80, 132),
            TextBox("301", 980, 101, 1030, 131),
            TextBox("Операционные системы", 150, 100, 600, 130),
        ]
        self.assertEqual(
            lines_from_boxes(boxes),
            ["1 Операционные системы Кубанева Е.А. 301"],
        )

    def test_separates_distant_rows(self) -> None:
        boxes = [
            TextBox("1 Физика", 60, 100, 400, 130),
            TextBox("2 Математика", 60, 200, 400, 230),
        ]
        self.assertEqual(lines_from_boxes(boxes), ["1 Физика", "2 Математика"])

    def test_tolerates_column_drift(self) -> None:
        """Колонка справа съехала вверх на треть строки — это всё ещё одна строка."""
        boxes = [
            TextBox("1 Физическая культура", 60, 300, 600, 336),
            TextBox("Кузьминова И.Н.", 700, 288, 900, 324),
            TextBox("с-з", 980, 288, 1030, 324),
        ]
        self.assertEqual(lines_from_boxes(boxes), ["1 Физическая культура Кузьминова И.Н. с-з"])

    def test_ignores_empty_boxes(self) -> None:
        boxes = [TextBox("  ", 0, 0, 10, 10), TextBox("1 Физика", 60, 100, 400, 130)]
        self.assertEqual(lines_from_boxes(boxes), ["1 Физика"])

    def test_empty_input(self) -> None:
        self.assertEqual(lines_from_boxes([]), [])

    def test_result_feeds_parser(self) -> None:
        boxes = [
            TextBox("ИСП-25-1", 400, 40, 600, 80),
            TextBox("01.09.2026 г.", 400, 140, 650, 180),
            TextBox("1", 60, 240, 80, 276),
            TextBox("Операционные системы и среды", 150, 240, 600, 276),
            TextBox("Кубанева Е.А.", 700, 236, 900, 272),
            TextBox("301", 980, 236, 1030, 272),
        ]
        result = OcrScheduleParser().parse_text("\n".join(lines_from_boxes(boxes)), now=NOW)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.snapshot.group_name, "ИСП-25-1")
        lesson = result.snapshot.days[0].lessons[0]
        self.assertEqual(lesson.subject, "Операционные системы и среды")
        self.assertEqual(lesson.teacher, "Кубанева Е.А.")
        self.assertEqual(lesson.classroom, "301")


class VocabularySnapGuardTests(unittest.TestCase):
    """Автозамена не должна менять смысл — другую аудиторию или другого человека."""

    def test_repairs_ocr_damage(self) -> None:
        value, score = _snap_to_vocabulary("Консулы.", ("Консульт.", "Математика"), 0.78)
        self.assertEqual(value, "Консульт.")
        self.assertGreater(score, 0.78)

    def test_sports_hall_digit_misread_is_repaired(self) -> None:
        self.assertEqual(_snap_to_vocabulary("с-3", ("с-з", "301"), 0.78)[0], "с-з")
        self.assertEqual(_snap_to_vocabulary("C-3", ("с-з", "301"), 0.78)[0], "с-з")

    def test_different_classroom_is_not_substituted(self) -> None:
        """'305/1' и '305/2' похожи на 0.80, но это разные аудитории."""
        self.assertEqual(_snap_to_vocabulary("305/1", ("305/2", "301"), 0.75)[0], "305/1")

    def test_different_room_number_is_not_substituted(self) -> None:
        self.assertEqual(_snap_to_vocabulary("313", ("312", "316"), 0.75)[0], "313")

    def test_different_initials_are_not_substituted(self) -> None:
        """'Травкин А.В.' — это не 'Травкина Е.А.', даже при похожести 0.78."""
        self.assertEqual(
            _snap_to_vocabulary("Травкин А.В.", ("Травкина Е.А.",), 0.75)[0],
            "Травкин А.В.",
        )

    def test_extra_digit_in_subject_is_not_dropped(self) -> None:
        """'... прогр. 2' — отдельная дисциплина, хоть похожесть и 0.97."""
        self.assertEqual(
            _snap_to_vocabulary("Основы алгоритмизации и прогр. 2", ("Основы алгоритмизации и прогр.",), 0.78)[0],
            "Основы алгоритмизации и прогр. 2",
        )

    def test_new_subject_is_kept_as_is(self) -> None:
        self.assertEqual(_snap_to_vocabulary("Химия", ("Физика", "Математика"), 0.78)[0], "Химия")

    def test_exact_match_wins_regardless_of_guards(self) -> None:
        value, score = _snap_to_vocabulary("305/2", ("305/2", "305/1"), 0.78)
        self.assertEqual(value, "305/2")
        self.assertEqual(score, 1.0)


class VocabularyTests(unittest.TestCase):
    def test_from_snapshot_contents(self) -> None:
        content = {
            "days": [
                {
                    "date_iso": "2026-09-01",
                    "lessons": [
                        {"number": 1, "subject": "Математика", "teacher": "Иванов И.И.", "classroom": "301"},
                        {"number": 2, "subject": "Математика", "teacher": "Иванов И.И.", "classroom": "301"},
                    ],
                }
            ]
        }
        vocabulary = OcrVocabulary.from_snapshot_contents([content, None])

        self.assertEqual(vocabulary.subjects, ("Математика",))
        self.assertEqual(vocabulary.teachers, ("Иванов И.И.",))
        self.assertEqual(vocabulary.classrooms, ("301",))
        self.assertFalse(vocabulary.is_empty)

    def test_empty_vocabulary(self) -> None:
        self.assertTrue(OcrVocabulary.from_snapshot_contents([]).is_empty)


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ocr_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=NOW,
            days=[
                DaySchedule(
                    date_label="02.09.2026",
                    date_iso="2026-09-02",
                    lessons=[Lesson(number=1, subject="Физика", teacher="Петров П.П.", classroom="202")],
                ),
                DaySchedule(date_label="05.09.2026", date_iso="2026-09-05", lessons=[]),
            ],
        )
        self.base_content = {
            "group_name": "ИСП-25-1",
            "days": [
                {
                    "date_iso": "2026-09-01",
                    "date_label": "01.09.2026",
                    "lessons": [
                        {"number": 1, "subject": "Математика", "teacher": "Иванов И.И.", "classroom": "301"}
                    ],
                },
                {
                    "date_iso": "2026-09-02",
                    "date_label": "02.09.2026",
                    "lessons": [
                        {"number": 1, "subject": "История", "teacher": "Сидоров С.С.", "classroom": "105"}
                    ],
                },
            ],
        }

    def test_merge_without_base_keeps_ocr_days(self) -> None:
        merged = merge_ocr_days(None, self.ocr_snapshot)

        self.assertEqual(merged.snapshot, self.ocr_snapshot)
        self.assertEqual(merged.added_dates, ["2026-09-02", "2026-09-05"])

    def test_merge_replaces_and_keeps_days(self) -> None:
        merged = merge_ocr_days(self.base_content, self.ocr_snapshot)

        self.assertEqual(merged.replaced_dates, ["2026-09-02"])
        self.assertEqual(merged.added_dates, ["2026-09-05"])
        self.assertEqual(merged.kept_dates, ["2026-09-01"])

        by_date = {day.date_iso: day for day in merged.snapshot.days}
        self.assertEqual(by_date["2026-09-01"].lessons[0].subject, "Математика")
        self.assertEqual(by_date["2026-09-02"].lessons[0].subject, "Физика")

    def test_merge_reports_emptied_day(self) -> None:
        ocr_snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=NOW,
            days=[DaySchedule(date_label="02.09.2026", date_iso="2026-09-02", lessons=[])],
        )
        merged = merge_ocr_days(self.base_content, ocr_snapshot)

        self.assertEqual(merged.emptied_dates, ["2026-09-02"])

    def test_merged_days_are_sorted(self) -> None:
        merged = merge_ocr_days(self.base_content, self.ocr_snapshot)
        dates = [day.date_iso for day in merged.snapshot.days]
        self.assertEqual(dates, sorted(dates))

    def test_merged_snapshot_hash_is_stable(self) -> None:
        first = merge_ocr_days(self.base_content, self.ocr_snapshot)
        second = merge_ocr_days(self.base_content, self.ocr_snapshot)
        self.assertEqual(compute_snapshot_hash(first.snapshot), compute_snapshot_hash(second.snapshot))


if __name__ == "__main__":
    unittest.main()

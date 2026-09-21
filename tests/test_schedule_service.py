from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from src.models import DaySchedule, Lesson, ScheduleSnapshot
from src.schedule_service import (
    ScheduleComparator,
    ScheduleFormatter,
    format_human_date,
    get_day_by_offset,
    get_day_by_offset_from_content,
)


class ScheduleComparatorTests(unittest.TestCase):
    def test_compare_returns_summary_when_day_changes(self) -> None:
        target = datetime.now().date()
        previous = {
            "content": {
                "days": [
                    {
                        "date_iso": target.isoformat(),
                        "date_label": "Сегодня",
                        "lessons": [
                            {
                                "number": 1,
                                "subject": "Математика",
                                "teacher": "Иванов",
                                "classroom": "101",
                            }
                        ],
                    }
                ]
            }
        }
        current = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime.now(),
            days=[
                DaySchedule(
                    date_iso=target.isoformat(),
                    date_label="Сегодня",
                    lessons=[
                        Lesson(number=1, subject="Физика", teacher="Петров", classroom="202"),
                    ],
                )
            ],
        )

        summary = ScheduleComparator.compare(previous, current)

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.changed_dates, ["Сегодня"])
        self.assertIn("Обнаружены изменения", summary.message)
        self.assertIn("Физика", summary.message)
        self.assertIn("<b>Обнаружены изменения", summary.telegram_message)

    def test_compare_ignores_unchanged_schedule(self) -> None:
        target = datetime.now().date()
        previous = {
            "content": {
                "days": [
                    {
                        "date_iso": target.isoformat(),
                        "date_label": "Сегодня",
                        "lessons": [
                            {
                                "number": 1,
                                "subject": "Математика",
                                "teacher": "Иванов",
                                "classroom": "101",
                            }
                        ],
                    }
                ]
            }
        }
        current = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime.now(),
            days=[
                DaySchedule(
                    date_iso=target.isoformat(),
                    date_label="Сегодня",
                    lessons=[
                        Lesson(number=1, subject="Математика", teacher="Иванов", classroom="101"),
                    ],
                )
            ],
        )

        summary = ScheduleComparator.compare(previous, current)

        self.assertIsNone(summary)


class ScheduleComparatorDuplicateLessonNumberTests(unittest.TestCase):
    """Регресс: у личного расписания препода в один день пара с одинаковым номером

    может идти сразу в нескольких его группах — номер пары уникален в рамках одной
    группы, но не для препода, который ведёт в нескольких. Раньше сравнение строило
    dict {номер: (...)}, который схлопывал такие пары в одну, и результат зависел от
    порядка на входе (не гарантированного, т.к. приходит из группировки в БД) —
    отсюда ложные "изменения" и повторные рассылки на ровном месте."""

    def setUp(self) -> None:
        self.today = datetime.now().date().isoformat()

    def _day_dict(self, lessons: list[dict]) -> dict:
        return {"content": {"days": [{"date_iso": self.today, "date_label": "Сегодня", "lessons": lessons}]}}

    def _snapshot(self, lessons: list[Lesson]) -> ScheduleSnapshot:
        return ScheduleSnapshot(
            group_name="Иванов И.И.",
            fetched_at=datetime.now(),
            days=[DaySchedule(date_label="Сегодня", date_iso=self.today, lessons=lessons)],
        )

    def test_same_content_different_input_order_is_not_a_change(self) -> None:
        lesson_a = {"number": 1, "subject": "Математика", "teacher": "ИСП-25-1", "classroom": "301"}
        lesson_b = {"number": 1, "subject": "Физика", "teacher": "ИСП-25-4", "classroom": "202"}
        previous = self._day_dict([lesson_a, lesson_b])
        current = self._snapshot([
            Lesson(number=1, subject="Физика", teacher="ИСП-25-4", classroom="202"),
            Lesson(number=1, subject="Математика", teacher="ИСП-25-1", classroom="301"),
        ])

        summary = ScheduleComparator.compare(previous, current)

        self.assertIsNone(summary, "Тот же набор пар в другом порядке не должен считаться изменением")

    def test_adding_a_second_group_with_the_same_number_is_a_real_change(self) -> None:
        lesson_a = {"number": 1, "subject": "Математика", "teacher": "ИСП-25-1", "classroom": "301"}
        previous = self._day_dict([lesson_a])
        current = self._snapshot([
            Lesson(number=1, subject="Математика", teacher="ИСП-25-1", classroom="301"),
            Lesson(number=1, subject="Физика", teacher="ИСП-25-4", classroom="202"),
        ])

        summary = ScheduleComparator.compare(previous, current)

        self.assertIsNotNone(summary, "Появление второй группы с тем же номером пары — реальное изменение")


class GetDayByOffsetTests(unittest.TestCase):
    """Регресс на баг: кнопка «сегодня» показывала завтрашний день, если в снимке
    (например, из OCR-фото) вообще не было записи на сегодняшнюю дату — код брал
    просто первый день из будущих по счёту, а не день с точным сегодняшним date_iso."""

    def test_from_content_returns_none_when_today_is_missing_even_if_tomorrow_exists(self) -> None:
        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        content = {
            "days": [
                {
                    "date_iso": tomorrow,
                    "date_label": "Завтра",
                    "lessons": [
                        {"number": 2, "subject": "Физическая культура", "teacher": "Черкасских М.С.", "classroom": "ФОК"},
                    ],
                }
            ]
        }

        today_result = get_day_by_offset_from_content(content, 0)
        self.assertIsNone(today_result, "Если на сегодня нет данных, нельзя молча подставлять завтрашний день")

        tomorrow_result = get_day_by_offset_from_content(content, 1)
        self.assertIsNotNone(tomorrow_result)
        self.assertEqual(tomorrow_result.date_iso, tomorrow)

    def test_from_content_matches_exact_calendar_date_regardless_of_order(self) -> None:
        today = datetime.now().date().isoformat()
        day_after = (datetime.now().date() + timedelta(days=2)).isoformat()
        content = {
            "days": [
                {"date_iso": day_after, "date_label": "Послезавтра", "lessons": []},
                {"date_iso": today, "date_label": "Сегодня", "lessons": [
                    {"number": 1, "subject": "Математика", "teacher": "Иванов", "classroom": "101"},
                ]},
            ]
        }

        result = get_day_by_offset_from_content(content, 0)

        self.assertIsNotNone(result)
        self.assertEqual(result.date_iso, today)
        self.assertEqual(result.lessons[0].subject, "Математика")

    def test_snapshot_variant_matches_same_semantics(self) -> None:
        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        snapshot = ScheduleSnapshot(
            group_name="ИСП-25-1",
            fetched_at=datetime.now(),
            days=[DaySchedule(date_iso=tomorrow, date_label="Завтра", lessons=[])],
        )

        self.assertIsNone(get_day_by_offset(snapshot, 0))
        result = get_day_by_offset(snapshot, 1)
        self.assertIsNotNone(result)
        self.assertEqual(result.date_iso, tomorrow)


class FormatSearchSnapshotTests(unittest.TestCase):
    """Регресс: предпросмотр при поиске показывал вообще все дни, какие были в снимке

    (у препода снимок собирается сразу из всех его групп и может охватывать очень
    широкий диапазон; у группы из OCR список дней со временем только растёт) —
    должен показывать только ближайшие несколько дней."""

    @staticmethod
    def _day(offset: int, lessons_count: int = 1) -> dict:
        date_iso = (datetime.now().date() + timedelta(days=offset)).isoformat()
        return {
            "date_iso": date_iso,
            "date_label": date_iso,
            "lessons": [
                {"number": i + 1, "subject": "Математика", "teacher": "Иванов И.И.", "classroom": "301"}
                for i in range(lessons_count)
            ],
        }

    @staticmethod
    def _human(offset: int) -> str:
        date_iso = (datetime.now().date() + timedelta(days=offset)).isoformat()
        return format_human_date(date_iso)

    def test_limits_to_three_upcoming_days_by_default(self) -> None:
        content = {"days": [self._day(offset) for offset in range(10)]}

        text = ScheduleFormatter.format_search_snapshot("ИСП-25-1", content)

        for offset in range(3):
            self.assertIn(self._human(offset), text)
        for offset in range(3, 10):
            self.assertNotIn(self._human(offset), text)

    def test_past_days_are_excluded_even_within_the_count(self) -> None:
        content = {"days": [self._day(-5), self._day(-1), self._day(0), self._day(1)]}

        text = ScheduleFormatter.format_search_snapshot("ИСП-25-1", content)

        self.assertNotIn(self._human(-5), text)
        self.assertNotIn(self._human(-1), text)
        self.assertIn(self._human(0), text)
        self.assertIn(self._human(1), text)

    def test_days_are_shown_chronologically_regardless_of_input_order(self) -> None:
        content = {"days": [self._day(2), self._day(0), self._day(1)]}

        text = ScheduleFormatter.format_search_snapshot("ИСП-25-1", content)

        pos0 = text.index(self._human(0))
        pos1 = text.index(self._human(1))
        pos2 = text.index(self._human(2))
        self.assertTrue(pos0 < pos1 < pos2)

    def test_custom_days_count_is_respected(self) -> None:
        content = {"days": [self._day(offset) for offset in range(10)]}

        text = ScheduleFormatter.format_search_snapshot("ИСП-25-1", content, days_count=1)

        self.assertIn(self._human(0), text)
        self.assertNotIn(self._human(1), text)

    def test_no_lessons_in_window_shows_fallback(self) -> None:
        content = {"days": [self._day(offset, lessons_count=0) for offset in range(3)]}

        text = ScheduleFormatter.format_search_snapshot("ИСП-25-1", content)

        self.assertIn("Пар нет.", text)

    def test_plain_lesson_line_matches_the_rest_of_the_app(self) -> None:
        """Регресс: предпросмотр поиска писал "1 в 301 по ..." без точки после номера,

        хотя везде в остальном боте (обычное расписание, уведомления) формат "1. в ...".
        """
        content = {"days": [self._day(0)]}

        text = ScheduleFormatter.format_search_snapshot("ИСП-25-1", content)

        self.assertIn("1. в 301 по Математика у Иванов И.И.", text)

    def test_html_mode_matches_the_bold_style_of_the_regular_schedule_card(self) -> None:
        content = {"days": [self._day(0)]}

        text = ScheduleFormatter.format_search_snapshot("ИСП-25-1", content, html=True)

        self.assertIn("<b>1.</b> в <b>301</b> по <b>Математика</b> у <b>Иванов И.И.</b>", text)

    def test_html_mode_escapes_unsafe_characters(self) -> None:
        """Регресс: без экранирования сырой текст с сайта/OCR мог сломать разбор HTML

        в Telegram (parse_mode=HTML по умолчанию), и сообщение с результатом поиска
        просто не доходило бы до пользователя (safe_send_message глотает такую ошибку)."""
        today = datetime.now().date().isoformat()
        content = {
            "days": [
                {
                    "date_iso": today,
                    "date_label": today,
                    "lessons": [
                        {"number": 1, "subject": "1 < 2 & чат", "teacher": "Иванов<script>", "classroom": "3&4"},
                    ],
                }
            ]
        }

        text = ScheduleFormatter.format_search_snapshot("Гр<уппа>", content, html=True)

        self.assertNotIn("<script>", text)
        self.assertIn("Гр&lt;уппа&gt;", text)
        self.assertIn("3&amp;4", text)

    def test_plain_mode_does_not_escape(self) -> None:
        today = datetime.now().date().isoformat()
        content = {
            "days": [
                {
                    "date_iso": today,
                    "date_label": today,
                    "lessons": [
                        {"number": 1, "subject": "A & B", "teacher": "Иванов", "classroom": "1"},
                    ],
                }
            ]
        }

        text = ScheduleFormatter.format_search_snapshot("Группа", content)

        self.assertIn("A & B", text)
        self.assertNotIn("&amp;", text)


if __name__ == "__main__":
    unittest.main()

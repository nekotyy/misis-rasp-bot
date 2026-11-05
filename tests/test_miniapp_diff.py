import unittest

from src.miniapp_diff import compare_lessons
from src.models import Lesson


def lesson(number: int = 1, subject: str = "Математика", teacher: str = "Иванов", classroom: str = "301") -> Lesson:
    return Lesson(number, subject, teacher, classroom)


class MiniappLessonDiffTests(unittest.TestCase):
    def test_no_baseline_is_not_an_addition(self):
        item = lesson()
        result = compare_lessons(None, [item])
        self.assertEqual(result.lessons[0].status, "unchanged")
        self.assertEqual(result.missing, ())

    def test_empty_baseline_is_distinct_from_no_baseline(self):
        self.assertEqual(compare_lessons([], [lesson()]).lessons[0].status, "added")

    def test_fields_are_reported_for_unique_slot(self):
        for field, changed in (
            ("subject", lesson(subject="Физика")),
            ("teacher", lesson(teacher="Петров")),
            ("classroom", lesson(classroom="302")),
        ):
            with self.subTest(field=field):
                original = lesson()
                change = compare_lessons([original], [changed]).lessons[0]
                self.assertEqual((change.status, change.before, change.changed_fields), ("changed", original, (field,)))

    def test_exact_duplicates_are_matched_as_multiset(self):
        item = lesson()
        result = compare_lessons([item, item], [item, item, item])
        self.assertEqual([change.status for change in result.lessons], ["unchanged", "unchanged", "added"])
        self.assertEqual(result.missing, ())

    def test_duplicate_numbers_and_subgroups_are_not_collapsed(self):
        first = lesson(subject="Физика (1 подгруппа)")
        second = lesson(subject="Химия (2 подгруппа)", teacher="Петров")
        changed = lesson(subject=second.subject, teacher=second.teacher, classroom="302")
        result = compare_lessons([first, second], [changed, first])
        self.assertEqual([item.status for item in result.lessons], ["changed", "unchanged"])
        self.assertEqual(result.lessons[0].before, second)
        self.assertEqual(result.lessons[0].changed_fields, ("classroom",))
        self.assertEqual(result.missing, ())

    def test_ambiguous_candidates_are_not_falsely_paired(self):
        old = [lesson(subject="Физика"), lesson(subject="Химия")]
        new = [lesson(subject="История")]
        result = compare_lessons(old, new)
        self.assertEqual(result.lessons[0].status, "ambiguous")
        self.assertIsNone(result.lessons[0].before)
        self.assertEqual(result.missing, tuple(old))

    def test_missing_lesson_is_not_marked_as_cancelled(self):
        old = lesson()
        result = compare_lessons([old], [])
        self.assertEqual(result.lessons, ())
        self.assertEqual(result.missing, (old,))

    def test_changed_subgroup_is_not_paired_without_evidence(self):
        old = lesson(subject="Физика (1 подгруппа)")
        new = lesson(subject="Физика (2 подгруппа)")
        result = compare_lessons([old], [new])
        self.assertEqual(result.lessons[0].status, "added")
        self.assertEqual(result.missing, (old,))

    def test_unknown_subgroup_is_ambiguous_with_specific_subgroup(self):
        old = lesson(subject="Физика")
        new = lesson(subject="Физика (1 подгруппа)")
        result = compare_lessons([old], [new])
        self.assertEqual(result.lessons[0].status, "ambiguous")
        self.assertIsNone(result.lessons[0].before)
        self.assertEqual(result.missing, (old,))

    def test_unknown_subgroup_prevents_another_apparently_unique_pair(self):
        old = [lesson(subject="Физика (1 подгруппа)"), lesson(subject="Химия")]
        new = [lesson(subject="Физика (1 подгруппа)", classroom="302")]
        result = compare_lessons(old, new)
        self.assertEqual(result.lessons[0].status, "ambiguous")
        self.assertEqual(result.missing, tuple(old))

    def test_exact_match_is_removed_before_matching_remaining_pair(self):
        old = [lesson(subject="Физика"), lesson(subject="Химия")]
        new = [lesson(subject="История"), old[1]]
        result = compare_lessons(old, new)
        self.assertEqual([item.status for item in result.lessons], ["changed", "unchanged"])
        self.assertEqual(result.lessons[0].before, old[0])


if __name__ == "__main__":
    unittest.main()

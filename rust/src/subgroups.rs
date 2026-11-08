use crate::{DaySchedule, ScheduleSnapshot};
use regex::Regex;
use std::collections::BTreeSet;
use std::sync::OnceLock;

pub fn lesson_subgroups(subject: &str) -> BTreeSet<u32> {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    static NUMBERS: OnceLock<Regex> = OnceLock::new();
    let pattern = PATTERN.get_or_init(|| {
        Regex::new(r"(?i)(\d+(?:\s*(?:,|/|и|–|-)\s*\d+)*)\s*(?:подгрупп[а-я]*|п\s*/\s*г)\b")
            .unwrap()
    });
    let digits = NUMBERS.get_or_init(|| Regex::new(r"\d+").unwrap());
    let mut result = BTreeSet::new();
    for capture in pattern.captures_iter(subject) {
        let text = &capture[1];
        let numbers: Vec<u32> = digits
            .find_iter(text)
            .filter_map(|n| n.as_str().parse().ok())
            .collect();
        if numbers.len() == 2
            && (text.contains('–') || text.contains('-'))
            && numbers[0] > 0
            && numbers[0] <= numbers[1]
            && numbers[1] <= 99
        {
            result.extend(numbers[0]..=numbers[1]);
        } else {
            result.extend(numbers.into_iter().filter(|n| (1..=99).contains(n)));
        }
    }
    result
}

pub fn filter_day(day: &DaySchedule, subgroup: Option<u32>) -> DaySchedule {
    let mut result = day.clone();
    if let Some(group) = subgroup {
        result.lessons.retain(|lesson| {
            let groups = lesson_subgroups(&lesson.subject);
            groups.is_empty() || groups.contains(&group)
        });
    }
    result
}

pub fn filter_snapshot(snapshot: &ScheduleSnapshot, subgroup: Option<u32>) -> ScheduleSnapshot {
    let mut result = snapshot.clone();
    result.days = snapshot
        .days
        .iter()
        .map(|day| filter_day(day, subgroup))
        .collect();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parses_ranges_lists_and_bounds() {
        assert_eq!(
            lesson_subgroups("Математика 1–3 подгруппы и 5/7 п/г"),
            BTreeSet::from([1, 2, 3, 5, 7])
        );
        assert_eq!(lesson_subgroups("0, 100 подгруппа"), BTreeSet::new());
        assert_eq!(lesson_subgroups("История"), BTreeSet::new());
    }
}

//! Совместимые с Python модели расписания.
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Lesson {
    pub number: i64,
    pub subject: String,
    pub teacher: String,
    pub classroom: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DaySchedule {
    #[serde(default)]
    pub date_label: String,
    #[serde(default)]
    pub date_iso: String,
    #[serde(default)]
    pub lessons: Vec<Lesson>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub week_variant: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub current_week_variant: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScheduleSnapshot {
    pub group_name: String,
    pub fetched_at: String,
    pub days: Vec<DaySchedule>,
}

pub fn normalize_dashes(text: &str) -> String {
    text.chars()
        .map(|c| match c {
            '—' | '–' | '‑' | '−' => '-',
            other => other,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn python_day_defaults_and_optional_fields() {
        let day: DaySchedule = serde_json::from_value(json!({})).unwrap();
        assert_eq!(
            serde_json::to_value(day).unwrap(),
            json!({
                "date_label": "", "date_iso": "", "lessons": []
            })
        );
        let input = json!({"date_label": "Понедельник", "date_iso": "2026-11-09",
            "lessons": [{"number": 1, "subject": "Математика", "teacher": "Иванов", "classroom": "101"}],
            "week_variant": 1, "current_week_variant": 2});
        let parsed: DaySchedule = serde_json::from_value(input.clone()).unwrap();
        assert_eq!(serde_json::to_value(parsed).unwrap(), input);
    }

    #[test]
    fn dash_variants_match_python() {
        assert_eq!(normalize_dashes("А—Б–В‑Г−Д-Е"), "А-Б-В-Г-Д-Е");
    }
}

pub mod archive;
pub mod calendar;
pub mod diff;
pub mod sources;
pub mod subgroups;
pub mod subscription;

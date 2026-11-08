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

pub mod archive;
pub mod calendar;
pub mod diff;
pub mod format;
pub mod parser;
pub mod sources;
pub mod subgroups;
pub mod subscription;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct UserRecord {
    pub platform: String,
    pub user_id: i64,
    pub username: Option<String>,
    pub full_name: Option<String>,
    pub subscription_type: Option<String>,
    pub subscription_key: Option<String>,
    pub subscription_title: Option<String>,
    pub subscription_url: Option<String>,
    pub audience_subscription_key: Option<String>,
    pub audience_subscription_title: Option<String>,
    pub audience_subscription_url: Option<String>,
    pub group_name: Option<String>,
    pub schedule_id: Option<i64>,
    pub is_admin: bool,
    pub is_editor: bool,
    pub homework_notifications_enabled: bool,
    pub delivery_disabled_auto: bool,
    pub created_at: String,
    pub last_seen_at: String,
    #[serde(default)]
    pub custom_sticker_file_id: Option<String>,
    #[serde(default)]
    pub university_subgroup: Option<i64>,
}

pub mod config;
pub mod database;
pub mod outbox;
pub mod web_security;

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
pub mod account;
pub mod notification;
pub mod sync;
pub mod telegram_poll;
pub mod telegram_ui;
pub mod vk_poll;

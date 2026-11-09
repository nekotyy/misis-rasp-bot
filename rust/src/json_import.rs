use serde::{Deserialize, Serialize};

use crate::{DaySchedule, Lesson, ScheduleSnapshot};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImportLesson {
    #[serde(default)]
    pub number: i64,
    #[serde(default)]
    pub subject: String,
    #[serde(default)]
    pub teacher: String,
    #[serde(default)]
    pub classroom: String,
}

impl From<ImportLesson> for Lesson {
    fn from(l: ImportLesson) -> Self {
        Lesson {
            number: l.number,
            subject: l.subject,
            teacher: l.teacher,
            classroom: l.classroom,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImportDaySchedule {
    #[serde(default)]
    pub date_label: String,
    #[serde(default)]
    pub date_iso: String,
    #[serde(default)]
    pub lessons: Vec<ImportLesson>,
    #[serde(default)]
    pub week_variant: Option<i64>,
    #[serde(default)]
    pub current_week_variant: Option<i64>,
}

impl From<ImportDaySchedule> for DaySchedule {
    fn from(d: ImportDaySchedule) -> Self {
        DaySchedule {
            date_label: d.date_label,
            date_iso: d.date_iso,
            lessons: d.lessons.into_iter().map(Into::into).collect(),
            week_variant: d.week_variant,
            current_week_variant: d.current_week_variant,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
enum ImportVariant {
    Single(SingleGroupPayload),
    Multiple(Vec<SingleGroupPayload>),
    Summary(SummaryPayload),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SingleGroupPayload {
    pub group_name: String,
    #[serde(default)]
    pub schedule_id: Option<i64>,
    #[serde(default)]
    pub source_type: Option<String>,
    #[serde(default)]
    pub source_key: Option<String>,
    #[serde(default)]
    pub source_url: Option<String>,
    #[serde(default)]
    pub days: Vec<ImportDaySchedule>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SummaryPayload {
    #[serde(default)]
    pub date_iso: String,
    #[serde(default)]
    pub date_label: Option<String>,
    #[serde(default)]
    pub groups: Vec<SummaryGroup>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SummaryGroup {
    pub group_name: String,
    #[serde(default)]
    pub lessons: Vec<ImportLesson>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImportResult {
    pub snapshots: Vec<ScheduleSnapshot>,
    pub total_lessons: usize,
}

pub fn parse_schedule_json(raw: &str) -> Result<ImportResult, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("Пустой JSON.".into());
    }

    // Извлекаем JSON из markdown блоков ```json ... ``` если они присутствуют
    let json_text = if let Some(start) = trimmed.find("```") {
        let after_start = &trimmed[start + 3..];
        let content = if let Some(first_newline) = after_start.find('\n') {
            &after_start[first_newline + 1..]
        } else {
            after_start
        };
        if let Some(end) = content.rfind("```") {
            &content[..end]
        } else {
            content
        }
    } else {
        trimmed
    };

    let variant: ImportVariant = serde_json::from_str(json_text.trim())
        .map_err(|e| format!("Не удалось разобрать JSON: {e}"))?;

    let now_iso = chrono::Utc::now()
        .naive_utc()
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string();

    let mut snapshots = Vec::new();

    match variant {
        ImportVariant::Single(single) => {
            if single.group_name.trim().is_empty() {
                return Err("Поле group_name не может быть пустым.".into());
            }
            snapshots.push(ScheduleSnapshot {
                group_name: single.group_name.trim().to_string(),
                fetched_at: now_iso,
                days: single.days.into_iter().map(Into::into).collect(),
            });
        }
        ImportVariant::Multiple(list) => {
            for item in list {
                if item.group_name.trim().is_empty() {
                    continue;
                }
                snapshots.push(ScheduleSnapshot {
                    group_name: item.group_name.trim().to_string(),
                    fetched_at: now_iso.clone(),
                    days: item.days.into_iter().map(Into::into).collect(),
                });
            }
        }
        ImportVariant::Summary(summary) => {
            let date = if summary.date_iso.is_empty() {
                chrono::Utc::now().format("%Y-%m-%d").to_string()
            } else {
                summary.date_iso
            };
            let day_of_week = summary.date_label.unwrap_or_else(|| "Учебный день".into());

            for g in summary.groups {
                if g.group_name.trim().is_empty() {
                    continue;
                }
                snapshots.push(ScheduleSnapshot {
                    group_name: g.group_name.trim().to_string(),
                    fetched_at: now_iso.clone(),
                    days: vec![DaySchedule {
                        date_iso: date.clone(),
                        date_label: day_of_week.clone(),
                        lessons: g.lessons.into_iter().map(Into::into).collect(),
                        week_variant: None,
                        current_week_variant: None,
                    }],
                });
            }
        }
    }

    if snapshots.is_empty() {
        return Err("В JSON не найдено данных расписания для групп.".into());
    }

    let total_lessons: usize = snapshots
        .iter()
        .map(|s| s.days.iter().map(|d| d.lessons.len()).sum::<usize>())
        .sum();

    Ok(ImportResult {
        snapshots,
        total_lessons,
    })
}

pub fn apply_import_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    snapshots: &[ScheduleSnapshot],
    notify: bool,
    _now_str: &str,
    now_timestamp: f64,
) -> Result<usize, Box<dyn std::error::Error>> {
    let mut applied_count = 0;
    for snapshot in snapshots {
        let hash = crate::parser::compute_snapshot_hash(snapshot);
        let source_key = format!("group:{}", snapshot.group_name);
        let source = crate::sync::Source {
            source_type: "group",
            source_key: &source_key,
            source_title: &snapshot.group_name,
            source_url: "",
            group_name: Some(&snapshot.group_name),
            schedule_id: None,
        };
        let options = crate::sync::ApplyOptions {
            notify,
            trust_empty: false,
            observed_dates: None,
            manual_origin: "ocr_json_import",
            timezone: chrono_tz::Europe::Moscow,
            now: chrono::Local::now().naive_local(),
        };
        if notify {
            let platforms = crate::sync::DeliveryPlatforms {
                telegram: true,
                vk: true,
            };
            crate::sync::apply_and_enqueue_in_transaction(
                tx,
                &source,
                snapshot,
                &hash,
                &options,
                &platforms,
                now_timestamp,
            )?;
        } else {
            crate::sync::save_in_transaction(tx, &source, snapshot, &hash, "current", &options)?;
            crate::sync::save_in_transaction(
                tx,
                &source,
                snapshot,
                &hash,
                "daily_baseline",
                &options,
            )?;
        }
        applied_count += 1;
    }
    Ok(applied_count)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_single_group_json() {
        let json_text = r#"
        {
            "group_name": "ИСП-25-1",
            "days": [
                {
                    "date_iso": "2026-09-01",
                    "date_label": "Вторник",
                    "lessons": [
                        {
                            "number": 1,
                            "subject": "Информатика",
                            "teacher": "Иванов И.И.",
                            "classroom": "101"
                        }
                    ]
                }
            ]
        }
        "#;
        let res = parse_schedule_json(json_text).unwrap();
        assert_eq!(res.snapshots.len(), 1);
        assert_eq!(res.snapshots[0].group_name, "ИСП-25-1");
        assert_eq!(res.total_lessons, 1);
    }

    #[test]
    fn test_parse_summary_groups_json() {
        let json_text = r#"
        {
            "date_iso": "2026-09-02",
            "date_label": "Среда",
            "groups": [
                {
                    "group_name": "ИСП-25-1",
                    "lessons": [
                        { "number": 1, "subject": "Математика" }
                    ]
                },
                {
                    "group_name": "ИСП-25-2",
                    "lessons": [
                        { "number": 1, "subject": "Физика" },
                        { "number": 2, "subject": "История" }
                    ]
                }
            ]
        }
        "#;
        let res = parse_schedule_json(json_text).unwrap();
        assert_eq!(res.snapshots.len(), 2);
        assert_eq!(res.total_lessons, 3);
        assert_eq!(res.snapshots[0].days[0].date_iso, "2026-09-02");
    }

    #[test]
    fn test_parse_markdown_wrapped_json() {
        let md = "```json\n{\"group_name\": \"Э-24\", \"days\": []}\n```";
        let res = parse_schedule_json(md).unwrap();
        assert_eq!(res.snapshots.len(), 1);
        assert_eq!(res.snapshots[0].group_name, "Э-24");
    }
}

use crate::{subgroups::filter_day, DaySchedule};
use chrono::Datelike;

pub fn week_name(variant: Option<i64>) -> &'static str {
    match variant {
        Some(1) => "Числитель",
        Some(2) => "Знаменатель",
        _ => "неделя не определена",
    }
}
pub fn current_week(
    fetched: chrono::NaiveDate,
    today: chrono::NaiveDate,
    variant: Option<i64>,
) -> Option<i64> {
    let v = variant.filter(|x| *x == 1 || *x == 2)?;
    let weeks =
        (crate::calendar::week_start(today) - crate::calendar::week_start(fetched)).num_days() / 7;
    Some(if weeks.rem_euclid(2) == 1 { 3 - v } else { v })
}
pub fn human_date(value: &str, default_year: i32) -> String {
    let value = value.trim();
    let parsed = if value.len() >= 10 && value.as_bytes()[4] == b'-' && value.as_bytes()[7] == b'-'
    {
        let fields: Vec<_> = value.split('-').collect();
        fields
            .first()
            .and_then(|y| y.parse::<i32>().ok())
            .zip(fields.get(1).and_then(|m| m.parse::<u32>().ok()))
            .zip(
                fields
                    .get(2)
                    .and_then(|d| d.get(..2))
                    .and_then(|d| d.parse::<u32>().ok()),
            )
            .map(|((y, m), d)| (y, m, d))
    } else if value.contains('.') {
        let fields: Vec<_> = value.split('.').collect();
        fields
            .first()
            .and_then(|d| d.parse::<u32>().ok())
            .zip(fields.get(1).and_then(|m| m.parse::<u32>().ok()))
            .and_then(|(d, m)| {
                let year = if fields.len() >= 3 && fields[2].len() >= 4 {
                    fields[2].get(..4)?.parse().ok()?
                } else {
                    default_year
                };
                Some((year, m, d))
            })
    } else {
        None
    };
    if let Some((year, month, day)) = parsed {
        let names = [
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ];
        let name = month
            .checked_sub(1)
            .and_then(|m| names.get(m as usize))
            .map(|s| s.to_string())
            .unwrap_or_else(|| format!("{month:02}"));
        format!("{day} {name} {year} года")
    } else {
        value.into()
    }
}
pub fn escape_html(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}
pub fn format_day(
    day: &DaySchedule,
    subgroup: Option<u32>,
    html: bool,
    today: chrono::NaiveDate,
) -> String {
    let day = filter_day(day, subgroup);
    let label = human_date(
        if day.date_label.is_empty() {
            &day.date_iso
        } else {
            &day.date_label
        },
        today.year(),
    );
    let label = if html { escape_html(&label) } else { label };
    let mut result = String::new();
    if matches!(day.week_variant, Some(1 | 2)) {
        let selected = week_name(day.week_variant);
        let current = week_name(day.current_week_variant);
        if html {
            result.push_str(&format!(
                "<b>{selected}</b>\nУчимся по: <b>{current}</b>\nВыбрано: <b>{selected}</b>"
            ));
        } else {
            result.push_str(&format!(
                "{selected}\nУчимся по: {current}\nВыбрано: {selected}"
            ));
        }
        if let Some(group) = subgroup {
            result.push_str(&if html {
                format!("\nПодгруппа: <b>{group}</b>")
            } else {
                format!("\nПодгруппа: {group}")
            });
        }
        result.push_str("\n\n");
    }
    result.push_str(&format!("Расписание на {label}\n\n"));
    if day.lessons.is_empty() {
        result.push_str("Пар нет.");
        return result;
    }
    let mut lessons = day.lessons.iter().collect::<Vec<_>>();
    lessons.sort_by_key(|lesson| lesson.number);
    result.push_str(
        &lessons
            .iter()
            .map(|lesson| {
                if html {
                    format!(
                        "<b>{}.</b> в <b>{}</b> по <b>{}</b> у <b>{}</b>",
                        lesson.number,
                        escape_html(&lesson.classroom),
                        escape_html(&lesson.subject),
                        escape_html(&lesson.teacher)
                    )
                } else {
                    format!(
                        "{}. в {} по {} у {}",
                        lesson.number, lesson.classroom, lesson.subject, lesson.teacher
                    )
                }
            })
            .collect::<Vec<_>>()
            .join("\n"),
    );
    result
}

pub fn format_search_snapshot(
    title: &str,
    snapshot: &crate::ScheduleSnapshot,
    today: chrono::NaiveDate,
    html: bool,
) -> String {
    let safe = |s: &str| if html { escape_html(s) } else { s.to_owned() };
    let mut lines = vec![format!("Расписание для {}", safe(title)), String::new()];
    let date = today.to_string();
    let mut days = snapshot
        .days
        .iter()
        .filter(|day| day.date_iso >= date)
        .collect::<Vec<_>>();
    days.sort_by(|a, b| a.date_iso.cmp(&b.date_iso));
    let mut added = false;
    for day in days.into_iter().take(3) {
        if day.lessons.is_empty() {
            continue;
        }
        added = true;
        if matches!(day.week_variant, Some(1 | 2)) {
            let mut adjusted = day.clone();
            adjusted.current_week_variant = snapshot
                .fetched_at
                .get(..10)
                .and_then(|value| chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
                .and_then(|fetched| current_week(fetched, today, day.current_week_variant));
            lines.push(format_day(&adjusted, None, html, today));
        } else {
            let label = if day.date_label.is_empty() {
                &day.date_iso
            } else {
                &day.date_label
            };
            lines.push(safe(&human_date(label, today.year())));
            let mut lessons = day.lessons.iter().collect::<Vec<_>>();
            lessons.sort_by_key(|lesson| lesson.number);
            for lesson in lessons {
                lines.push(if html {
                    format!(
                        "<b>{}.</b> в <b>{}</b> по <b>{}</b> у <b>{}</b>",
                        lesson.number,
                        safe(&lesson.classroom),
                        safe(&lesson.subject),
                        safe(&lesson.teacher)
                    )
                } else {
                    format!(
                        "{}. в {} по {} у {}",
                        lesson.number, lesson.classroom, lesson.subject, lesson.teacher
                    )
                });
            }
        }
        lines.push(String::new());
    }
    if !added {
        lines.push("Пар нет.".into());
    } else {
        lines.pop();
    }
    lines.join("\n")
}

#[cfg(test)]
mod search_preview_tests {
    use super::*;
    #[test]
    fn limits_preview_to_first_three_dates_and_escapes_html() {
        let today = chrono::NaiveDate::from_ymd_opt(2026, 11, 8).unwrap();
        let lesson = crate::Lesson {
            number: 1,
            subject: "<физика>".into(),
            teacher: "Иванов & Петров".into(),
            classroom: "101".into(),
        };
        let day = |date: &str, lessons: Vec<crate::Lesson>| DaySchedule {
            date_label: date.into(),
            date_iso: date.into(),
            week_variant: None,
            current_week_variant: None,
            lessons,
        };
        let snapshot = crate::ScheduleSnapshot {
            group_name: "Г".into(),
            fetched_at: "2026-11-08".into(),
            days: vec![
                day("2026-11-11", vec![lesson.clone()]),
                day("2026-11-08", vec![lesson.clone()]),
                day("2026-11-07", vec![lesson.clone()]),
                day("2026-11-09", vec![]),
                day("2026-11-10", vec![lesson]),
            ],
        };
        let result = format_search_snapshot("<Г>", &snapshot, today, true);
        assert!(
            result.contains("&lt;Г&gt;")
                && result.contains("&lt;физика&gt;")
                && result.contains("Иванов &amp; Петров")
        );
        assert!(result.contains("8 ноября") && result.contains("10 ноября"));
        assert!(!result.contains("7 ноября") && !result.contains("11 ноября"));
    }
    #[test]
    fn preview_advances_current_week_from_snapshot_date() {
        let today = chrono::NaiveDate::from_ymd_opt(2026, 11, 9).unwrap();
        let snapshot = crate::ScheduleSnapshot {
            group_name: "Г".into(),
            fetched_at: "2026-11-02T12:00:00".into(),
            days: vec![DaySchedule {
                date_label: "2026-11-09".into(),
                date_iso: "2026-11-09".into(),
                week_variant: Some(2),
                current_week_variant: Some(1),
                lessons: vec![crate::Lesson {
                    number: 1,
                    subject: "Физика".into(),
                    teacher: "Иванов".into(),
                    classroom: "101".into(),
                }],
            }],
        };
        assert!(format_search_snapshot("Г", &snapshot, today, true)
            .contains("Учимся по: <b>Знаменатель</b>"));
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct ChangeSummary {
    pub changed_dates: Vec<String>,
    pub message: String,
    pub payload: std::collections::BTreeMap<String, String>,
    pub telegram_message: String,
    pub vk_message: String,
}
pub fn compare(
    previous: Option<&crate::ScheduleSnapshot>,
    current: &crate::ScheduleSnapshot,
    today: chrono::NaiveDate,
    subgroup: Option<u32>,
    notify_cancellations: bool,
) -> Option<ChangeSummary> {
    let previous = previous?;
    let days_to_check = if today.weekday() == chrono::Weekday::Sat {
        3
    } else {
        2
    };
    let mut changed_dates = Vec::new();
    let mut payload = std::collections::BTreeMap::new();
    let mut plain = vec!["Обнаружены изменения в расписании!".to_string()];
    let mut telegram = vec!["<b>Обнаружены изменения в расписании!</b>".to_string()];
    for original in &current.days {
        let Ok(date) = chrono::NaiveDate::parse_from_str(&original.date_iso, "%Y-%m-%d") else {
            continue;
        };
        if date < today || date >= today + chrono::Duration::days(days_to_check) {
            continue;
        }
        let day = filter_day(original, subgroup);
        let old = previous
            .days
            .iter()
            .find(|x| x.date_iso == day.date_iso)
            .map(|x| filter_day(x, subgroup));
        if day.lessons.is_empty()
            && (!notify_cancellations || old.as_ref().is_none_or(|x| x.lessons.is_empty()))
        {
            continue;
        }
        let identity = |d: &DaySchedule| {
            let mut items: Vec<_> = d
                .lessons
                .iter()
                .map(|l| {
                    (
                        l.number,
                        l.subject.clone(),
                        l.teacher.clone(),
                        l.classroom.clone(),
                    )
                })
                .collect();
            items.sort();
            items
        };
        if !day.lessons.is_empty() && old.as_ref().is_some_and(|x| identity(x) == identity(&day)) {
            continue;
        }
        changed_dates.push(day.date_label.clone());
        let text = format_day(&day, subgroup, false, today);
        payload.insert(day.date_label.clone(), text.clone());
        plain.extend([String::new(), text]);
        telegram.extend([String::new(), format_day(&day, subgroup, true, today)]);
    }
    if changed_dates.is_empty() {
        return None;
    }
    let message = plain.join("\n");
    Some(ChangeSummary {
        changed_dates,
        payload,
        telegram_message: telegram.join("\n"),
        vk_message: message.clone(),
        message,
    })
}

pub fn looks_like_stale_site_glitch(
    previous: Option<&crate::database::StoredSnapshot>,
    snapshot: &crate::ScheduleSnapshot,
    now: chrono::NaiveDateTime,
) -> bool {
    let Some(previous) = previous else {
        return false;
    };
    if snapshot.days.is_empty()
        || snapshot.days.iter().any(|day| !day.lessons.is_empty())
        || !previous
            .content
            .days
            .iter()
            .any(|day| !day.lessons.is_empty())
    {
        return false;
    }
    chrono::NaiveDateTime::parse_from_str(&previous.fetched_at, "%Y-%m-%dT%H:%M:%S%.f")
        .ok()
        .is_some_and(|time| now.signed_duration_since(time) < chrono::Duration::hours(12))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Lesson;
    #[test]
    fn formatted_schedule_escapes_html_and_filters_subgroups() {
        let day = DaySchedule {
            date_label: "2026-11-09".into(),
            date_iso: "2026-11-09".into(),
            week_variant: Some(1),
            current_week_variant: Some(2),
            lessons: vec![Lesson {
                number: 1,
                subject: "A & B (1 подгруппа)".into(),
                teacher: "<Иванов>".into(),
                classroom: "101".into(),
            }],
        };
        let today = chrono::NaiveDate::from_ymd_opt(2026, 11, 8).unwrap();
        let text = format_day(&day, Some(1), true, today);
        assert!(text.contains("9 ноября 2026 года"));
        assert!(text.contains("A &amp; B"));
        assert!(text.contains("&lt;Иванов&gt;"));
        assert!(format_day(&day, Some(2), false, today).ends_with("Пар нет."));
        assert_eq!(
            current_week(today, today + chrono::Duration::days(7), Some(1)),
            Some(2)
        );
    }
}

#[cfg(test)]
mod compare_tests {
    use super::*;
    use crate::{Lesson, ScheduleSnapshot};
    #[test]
    fn duplicate_slots_and_cancellations_are_handled() {
        let date = chrono::NaiveDate::from_ymd_opt(2026, 11, 7).unwrap();
        let lesson = |subject: &str| Lesson {
            number: 1,
            subject: subject.into(),
            teacher: "".into(),
            classroom: "".into(),
        };
        let day = DaySchedule {
            date_label: "2026-11-09".into(),
            date_iso: "2026-11-09".into(),
            lessons: vec![lesson("A"), lesson("B")],
            week_variant: None,
            current_week_variant: None,
        };
        let old = ScheduleSnapshot {
            group_name: "Г".into(),
            fetched_at: String::new(),
            days: vec![day.clone()],
        };
        let mut current = old.clone();
        current.days[0].lessons.reverse();
        assert!(compare(Some(&old), &current, date, None, false).is_none());
        current.days[0].lessons.pop();
        assert!(compare(Some(&old), &current, date, None, false).is_some());
        current.days[0].lessons.clear();
        assert!(compare(Some(&old), &current, date, None, false).is_none());
        assert!(compare(Some(&old), &current, date, None, true).is_some());
    }
}

#[cfg(test)]
mod glitch_tests {
    use super::*;
    #[test]
    fn fresh_nonempty_cache_rejects_untrusted_empty_site_response() {
        let previous_content = crate::ScheduleSnapshot {
            group_name: "x".into(),
            fetched_at: "2026-11-08T10:00:00".into(),
            days: vec![DaySchedule {
                date_label: String::new(),
                date_iso: "2026-11-09".into(),
                lessons: vec![crate::Lesson {
                    number: 1,
                    subject: "a".into(),
                    teacher: String::new(),
                    classroom: String::new(),
                }],
                week_variant: None,
                current_week_variant: None,
            }],
        };
        let previous = crate::database::StoredSnapshot {
            source_type: None,
            source_key: None,
            source_title: None,
            source_url: None,
            group_name: None,
            schedule_id: None,
            hash: String::new(),
            content: previous_content.clone(),
            fetched_at: previous_content.fetched_at.clone(),
            created_at: String::new(),
        };
        let mut empty = previous_content;
        empty.days[0].lessons.clear();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        assert!(looks_like_stale_site_glitch(Some(&previous), &empty, now));
        assert!(!looks_like_stale_site_glitch(
            Some(&previous),
            &empty,
            now + chrono::Duration::hours(13)
        ));
        empty.days.clear();
        assert!(!looks_like_stale_site_glitch(Some(&previous), &empty, now));
    }
}

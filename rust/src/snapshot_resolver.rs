use crate::{database::latest_snapshot, parser::ScheduleParser, ScheduleSnapshot, UserRecord};
use rusqlite::Connection;

pub async fn resolve(
    db: &Connection,
    parser: &ScheduleParser,
    user: &UserRecord,
    now: chrono::NaiveDateTime,
) -> Result<Option<ScheduleSnapshot>, Box<dyn std::error::Error>> {
    let Some(key) = user.subscription_key.as_deref().filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    let Some(title) = user.subscription_title.as_deref().filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    let cached =
        latest_snapshot(db, "current", user.schedule_id, Some(key))?.map(|row| row.content);
    let cached_day = cached
        .as_ref()
        .and_then(|s| s.days.iter().find(|d| d.date_iso == now.date().to_string()));
    let has_cached_day = cached_day.is_some();
    let valid_cached_variant =
        cached_day.is_some_and(|day| matches!(day.week_variant, Some(1 | 2)));
    let university = user.schedule_id.is_some_and(|id| id < 0)
        || user
            .subscription_url
            .as_deref()
            .and_then(crate::sources::university_kind)
            .is_some();
    if let Some(snapshot) = cached.as_ref() {
        if !university || valid_cached_variant {
            return Ok(Some(snapshot.clone()));
        }
    }
    let groups = if user.subscription_type.as_deref() == Some("teacher")
        && user
            .subscription_url
            .as_deref()
            .and_then(crate::sources::university_kind)
            != Some("teacher")
    {
        Some(crate::database::latest_group_snapshots(db, "current")?)
    } else {
        None
    };
    let fetch = async {
        let url = user.subscription_url.as_deref().unwrap_or("");
        if user.subscription_type.as_deref() == Some("teacher") {
            if crate::sources::university_kind(url) == Some("teacher") {
                parser
                    .parse_from_url(url, now)
                    .await
                    .map(|(snapshot, _)| Some(snapshot))
            } else {
                Ok(Some(crate::search::teacher_snapshot(
                    title,
                    groups.as_deref().unwrap_or_default(),
                    !url.is_empty(),
                    &now.format("%Y-%m-%dT%H:%M:%S").to_string(),
                )))
            }
        } else if user.subscription_type.as_deref() == Some("audience") && !url.is_empty() {
            parser
                .parse_from_url(url, now)
                .await
                .map(|(snapshot, _)| Some(snapshot))
        } else if let Some(id) = user.schedule_id {
            parser
                .parse(id, now)
                .await
                .map(|(snapshot, _)| Some(snapshot))
        } else {
            Ok(None)
        }
    };
    let fetched = if has_cached_day {
        tokio::time::timeout(std::time::Duration::from_secs(3), fetch)
            .await
            .ok()
            .and_then(Result::ok)
            .flatten()
    } else {
        fetch.await.ok().flatten()
    };
    Ok(fetched.or(if has_cached_day { cached } else { None }))
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[tokio::test]
    async fn returns_non_university_cache_without_network() {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let snapshot = ScheduleSnapshot {
            group_name: "Г".into(),
            fetched_at: "2026-11-08T12:00:00".into(),
            days: vec![],
        };
        crate::database::save_snapshot(
            &mut db,
            &crate::database::SnapshotInput {
                snapshot_type: "current",
                hash: "h",
                snapshot: &snapshot,
                schedule_id: Some(1),
                group_name: Some("Г"),
                source_type: Some("group"),
                source_key: Some("group:1"),
                source_title: Some("Г"),
                source_url: None,
                archive_origin: None,
                archive_dates: None,
                archive_projected: false,
                timezone: chrono_tz::UTC,
                created_at: "now",
            },
        )
        .unwrap();
        let user:UserRecord=serde_json::from_value(json!({"platform":"telegram","user_id":1,"subscription_type":"group","subscription_key":"group:1","subscription_title":"Г","schedule_id":1,"is_admin":false,"is_editor":false,"homework_notifications_enabled":true,"delivery_disabled_auto":false,"created_at":"now","last_seen_at":"now"})).unwrap();
        let parser = ScheduleParser::new(
            "http://127.0.0.1:1/rasp",
            "",
            std::time::Duration::from_millis(10),
            1,
            std::time::Duration::ZERO,
        )
        .unwrap();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        assert_eq!(
            resolve(&db, &parser, &user, now).await.unwrap(),
            Some(snapshot)
        );
    }
    #[tokio::test]
    async fn university_falls_back_to_existing_day_if_source_is_unavailable() {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let snapshot = ScheduleSnapshot {
            group_name: "Г".into(),
            fetched_at: "2026-11-08T12:00:00".into(),
            days: vec![crate::DaySchedule {
                date_label: "8 ноября".into(),
                date_iso: "2026-11-08".into(),
                week_variant: None,
                current_week_variant: None,
                lessons: vec![],
            }],
        };
        crate::database::save_snapshot(
            &mut db,
            &crate::database::SnapshotInput {
                snapshot_type: "current",
                hash: "h",
                snapshot: &snapshot,
                schedule_id: Some(-1),
                group_name: Some("Г"),
                source_type: Some("group"),
                source_key: Some("group:-1"),
                source_title: Some("Г"),
                source_url: None,
                archive_origin: None,
                archive_dates: None,
                archive_projected: false,
                timezone: chrono_tz::UTC,
                created_at: "now",
            },
        )
        .unwrap();
        let user: UserRecord = serde_json::from_value(json!({"platform":"telegram","user_id":1,"subscription_type":"group","subscription_key":"group:-1","subscription_title":"Г","schedule_id":-1,"is_admin":false,"is_editor":false,"homework_notifications_enabled":true,"delivery_disabled_auto":false,"created_at":"now","last_seen_at":"now"})).unwrap();
        let parser = ScheduleParser::new(
            "",
            "http://127.0.0.1:1",
            std::time::Duration::from_millis(100),
            1,
            std::time::Duration::ZERO,
        )
        .unwrap();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        assert_eq!(
            resolve(&db, &parser, &user, now).await.unwrap(),
            Some(snapshot)
        );
    }
}

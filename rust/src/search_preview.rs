use crate::{parser::ScheduleParser, search::SearchTarget, ScheduleSnapshot};
use rusqlite::Connection;

pub async fn resolve(
    db: &Connection,
    parser: &ScheduleParser,
    target: &SearchTarget,
    now: chrono::NaiveDateTime,
) -> Result<ScheduleSnapshot, Box<dyn std::error::Error>> {
    if target.kind == "teacher" {
        if crate::sources::university_kind(&target.url) == Some("teacher") {
            return Ok(parser.parse_from_url(&target.url, now).await?.0);
        }
        let groups = crate::database::latest_group_snapshots(db, "current")?;
        return Ok(crate::search::teacher_snapshot(
            &target.title,
            &groups,
            !target.url.is_empty(),
            &now.format("%Y-%m-%dT%H:%M:%S").to_string(),
        ));
    }
    let stored = match target.kind.as_str() {
        "group" => match crate::sources::schedule_numeric_id(&target.url) {
            Some(id) => crate::database::latest_snapshot(db, "current", Some(id), None)?,
            None => None,
        },
        "audience" => {
            let key = crate::sources::source_key_from_url("audience", &target.url);
            crate::database::latest_snapshot(db, "current", None, Some(&key))?
        }
        _ => return Err("Неизвестный тип расписания".into()),
    }
    .map(|row| row.content);
    if let Some(snapshot) = stored.as_ref() {
        if crate::sources::university_kind(&target.url).is_none()
            || snapshot
                .days
                .iter()
                .any(|day| matches!(day.week_variant, Some(1 | 2)))
        {
            return Ok(snapshot.clone());
        }
    }
    if let Some(snapshot) = stored {
        return match tokio::time::timeout(
            std::time::Duration::from_secs(3),
            parser.parse_from_url(&target.url, now),
        )
        .await
        {
            Ok(Ok((fetched, _))) => Ok(fetched),
            _ => Ok(snapshot),
        };
    }
    Ok(parser.parse_from_url(&target.url, now).await?.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn group_preview_reuses_current_snapshot_when_site_is_unavailable() {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let snapshot = ScheduleSnapshot {
            group_name: "Группа".into(),
            fetched_at: "2026-11-08T12:00:00".into(),
            days: vec![],
        };
        crate::database::save_snapshot(
            &mut db,
            &crate::database::SnapshotInput {
                snapshot_type: "current",
                hash: "h",
                snapshot: &snapshot,
                schedule_id: Some(123),
                group_name: Some("Группа"),
                source_type: Some("group"),
                source_key: Some("group:123"),
                source_title: Some("Группа"),
                source_url: None,
                archive_origin: None,
                archive_dates: None,
                archive_projected: false,
                timezone: chrono_tz::UTC,
                created_at: "now",
            },
        )
        .unwrap();
        let target = SearchTarget {
            kind: "group".into(),
            title: "Группа".into(),
            url: "http://127.0.0.1:1/rasp/123".into(),
        };
        let parser = ScheduleParser::new(
            "",
            "",
            std::time::Duration::from_millis(100),
            1,
            std::time::Duration::ZERO,
        )
        .unwrap();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        assert_eq!(resolve(&db, &parser, &target, now).await.unwrap(), snapshot);
    }
}

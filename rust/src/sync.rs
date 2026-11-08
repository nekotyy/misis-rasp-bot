use crate::{
    database::StoredSnapshot,
    format::{self, ChangeSummary},
    ScheduleSnapshot,
};

pub enum SnapshotDecision {
    RejectStaleEmpty,
    FirstBaseline,
    Unchanged,
    Changed(ChangeSummary),
}
pub fn evaluate(
    previous: Option<&StoredSnapshot>,
    baseline: Option<&StoredSnapshot>,
    current: &ScheduleSnapshot,
    now: chrono::NaiveDateTime,
    trust_empty: bool,
    university_group: bool,
) -> SnapshotDecision {
    if !trust_empty && format::looks_like_stale_site_glitch(previous, current, now) {
        return SnapshotDecision::RejectStaleEmpty;
    }
    let Some(baseline) = baseline else {
        return SnapshotDecision::FirstBaseline;
    };
    match format::compare(
        Some(&baseline.content),
        current,
        now.date(),
        None,
        university_group,
    ) {
        Some(change) => SnapshotDecision::Changed(change),
        None => SnapshotDecision::Unchanged,
    }
}

pub struct Source<'a> {
    pub source_type: &'a str,
    pub source_key: &'a str,
    pub source_title: &'a str,
    pub source_url: &'a str,
    pub group_name: Option<&'a str>,
    pub schedule_id: Option<i64>,
}
pub struct ApplyOptions<'a> {
    pub notify: bool,
    pub trust_empty: bool,
    pub observed_dates: Option<&'a std::collections::HashSet<String>>,
    pub manual_origin: &'a str,
    pub timezone: chrono_tz::Tz,
    pub now: chrono::NaiveDateTime,
}
pub enum ApplyResult {
    RejectedEmpty,
    FirstBaseline,
    Unchanged,
    ChangedWithoutBroadcast(ChangeSummary),
    Broadcast(ChangeSummary),
}
fn save(
    db: &mut rusqlite::Connection,
    source: &Source<'_>,
    snapshot: &ScheduleSnapshot,
    hash: &str,
    kind: &str,
    options: &ApplyOptions<'_>,
) -> Result<(), Box<dyn std::error::Error>> {
    let created = options.now.format("%Y-%m-%dT%H:%M:%S").to_string();
    let origin = if kind != "current" {
        None
    } else if options.trust_empty {
        Some(options.manual_origin)
    } else if source.source_type == "group" {
        Some("site")
    } else {
        Some("derived")
    };
    crate::database::save_snapshot(
        db,
        &crate::database::SnapshotInput {
            snapshot_type: kind,
            hash,
            snapshot,
            schedule_id: source.schedule_id,
            group_name: source.group_name,
            source_type: Some(source.source_type),
            source_key: Some(source.source_key),
            source_title: Some(source.source_title),
            source_url: Some(source.source_url),
            archive_origin: origin,
            archive_dates: options.observed_dates,
            archive_projected: true,
            timezone: options.timezone,
            created_at: &created,
        },
    )?;
    Ok(())
}
pub fn apply(
    db: &mut rusqlite::Connection,
    source: &Source<'_>,
    snapshot: &ScheduleSnapshot,
    hash: &str,
    options: &ApplyOptions<'_>,
) -> Result<ApplyResult, Box<dyn std::error::Error>> {
    let previous = crate::database::latest_snapshot(
        db,
        "current",
        source.schedule_id,
        Some(source.source_key),
    )?;
    let baseline = crate::database::latest_snapshot(
        db,
        "daily_baseline",
        source.schedule_id,
        Some(source.source_key),
    )?;
    let university = source.source_type == "group" && source.schedule_id.is_some_and(|id| id < 0);
    let decision = evaluate(
        previous.as_ref(),
        baseline.as_ref(),
        snapshot,
        options.now,
        options.trust_empty,
        university,
    );
    if matches!(decision, SnapshotDecision::RejectStaleEmpty) {
        return Ok(ApplyResult::RejectedEmpty);
    }
    save(db, source, snapshot, hash, "current", options)?;
    match decision {
        SnapshotDecision::RejectStaleEmpty => unreachable!(),
        SnapshotDecision::FirstBaseline => {
            save(db, source, snapshot, hash, "daily_baseline", options)?;
            Ok(ApplyResult::FirstBaseline)
        }
        SnapshotDecision::Unchanged => Ok(ApplyResult::Unchanged),
        SnapshotDecision::Changed(summary) => {
            if !options.notify {
                save(db, source, snapshot, hash, "daily_baseline", options)?;
                return Ok(ApplyResult::ChangedWithoutBroadcast(summary));
            }
            let created = options.now.format("%Y-%m-%dT%H:%M:%S").to_string();
            let recorded = crate::database::record_change(
                db,
                &crate::database::ChangeInput {
                    hash,
                    summary: &summary,
                    schedule_id: source.schedule_id,
                    group_name: source.group_name,
                    source_type: Some(source.source_type),
                    source_key: Some(source.source_key),
                    source_title: Some(source.source_title),
                    source_url: Some(source.source_url),
                    created_at: &created,
                },
            )?;
            if !recorded {
                save(db, source, snapshot, hash, "daily_baseline", options)?;
                Ok(ApplyResult::ChangedWithoutBroadcast(summary))
            } else {
                Ok(ApplyResult::Broadcast(summary))
            }
        }
    }
}
/// Вызывать после завершения рассылки: до этого исходная база сравнения остаётся доступной.
pub fn finish_broadcast(
    db: &mut rusqlite::Connection,
    source: &Source<'_>,
    snapshot: &ScheduleSnapshot,
    hash: &str,
    options: &ApplyOptions<'_>,
) -> Result<(), Box<dyn std::error::Error>> {
    save(db, source, snapshot, hash, "daily_baseline", options)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{DaySchedule, Lesson};
    fn stored(snapshot: ScheduleSnapshot) -> StoredSnapshot {
        StoredSnapshot {
            source_type: None,
            source_key: None,
            source_title: None,
            source_url: None,
            group_name: None,
            schedule_id: None,
            hash: String::new(),
            fetched_at: snapshot.fetched_at.clone(),
            created_at: String::new(),
            content: snapshot,
        }
    }
    #[test]
    fn guards_empty_site_and_allows_manual_override() {
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let snapshot = ScheduleSnapshot {
            group_name: "x".into(),
            fetched_at: "2026-11-08T10:00:00".into(),
            days: vec![DaySchedule {
                date_label: "2026-11-08".into(),
                date_iso: "2026-11-08".into(),
                lessons: vec![Lesson {
                    number: 1,
                    subject: "a".into(),
                    teacher: String::new(),
                    classroom: String::new(),
                }],
                week_variant: None,
                current_week_variant: None,
            }],
        };
        let previous = stored(snapshot.clone());
        let mut empty = snapshot.clone();
        empty.days[0].lessons.clear();
        assert!(matches!(
            evaluate(Some(&previous), Some(&previous), &empty, now, false, true),
            SnapshotDecision::RejectStaleEmpty
        ));
        assert!(matches!(
            evaluate(Some(&previous), Some(&previous), &empty, now, true, true),
            SnapshotDecision::Changed(_)
        ));
        assert!(matches!(
            evaluate(None, None, &snapshot, now, false, false),
            SnapshotDecision::FirstBaseline
        ));
        assert!(matches!(
            evaluate(
                Some(&previous),
                Some(&previous),
                &snapshot,
                now,
                false,
                false
            ),
            SnapshotDecision::Unchanged
        ));
    }
}
#[cfg(test)]
mod apply_tests {
    use super::*;
    #[test]
    fn applies_baseline_and_defers_advancement_until_delivery() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE schedule_snapshots(id INTEGER PRIMARY KEY,snapshot_type TEXT,source_type TEXT,source_key TEXT,source_title TEXT,source_url TEXT,group_name TEXT,schedule_id INTEGER,snapshot_hash TEXT,content_json TEXT,fetched_at TEXT,created_at TEXT); CREATE TABLE schedule_archive_projected_snapshots(snapshot_id INTEGER PRIMARY KEY); CREATE TABLE schedule_day_archive(source_key TEXT,source_type TEXT,date_iso TEXT,week_variant INTEGER,content_json TEXT,completeness TEXT,origin TEXT,observed_at TEXT,recorded_at TEXT,snapshot_id INTEGER,PRIMARY KEY(source_key,date_iso,week_variant)); CREATE TABLE schedule_day_references(source_key TEXT,date_iso TEXT,week_variant INTEGER,reference_kind TEXT,content_json TEXT,completeness TEXT,origin TEXT,observed_at TEXT,recorded_at TEXT,snapshot_id INTEGER,PRIMARY KEY(source_key,date_iso,week_variant,reference_kind)); CREATE TABLE change_events(source_type TEXT,source_key TEXT,source_title TEXT,source_url TEXT,group_name TEXT,schedule_id INTEGER,snapshot_hash TEXT,message TEXT,changed_dates_json TEXT,payload_json TEXT,created_at TEXT); CREATE UNIQUE INDEX dedup ON change_events(source_key,snapshot_hash,substr(created_at,1,10));").unwrap();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let source = Source {
            source_type: "group",
            source_key: "group:1",
            source_title: "Группа",
            source_url: "",
            group_name: Some("Группа"),
            schedule_id: Some(1),
        };
        let options = ApplyOptions {
            notify: true,
            trust_empty: false,
            observed_dates: None,
            manual_origin: "manual",
            timezone: chrono_tz::UTC,
            now,
        };
        let mut snapshot = ScheduleSnapshot {
            group_name: "Группа".into(),
            fetched_at: "2026-11-08T12:00:00".into(),
            days: vec![crate::DaySchedule {
                date_label: "2026-11-08".into(),
                date_iso: "2026-11-08".into(),
                lessons: vec![crate::Lesson {
                    number: 1,
                    subject: "A".into(),
                    teacher: String::new(),
                    classroom: String::new(),
                }],
                week_variant: None,
                current_week_variant: None,
            }],
        };
        assert!(matches!(
            apply(&mut db, &source, &snapshot, "old", &options).unwrap(),
            ApplyResult::FirstBaseline
        ));
        snapshot.days[0].lessons[0].subject = "B".into();
        assert!(matches!(
            apply(&mut db, &source, &snapshot, "new", &options).unwrap(),
            ApplyResult::Broadcast(_)
        ));
        assert_eq!(
            crate::database::latest_snapshot(&db, "daily_baseline", None, Some("group:1"))
                .unwrap()
                .unwrap()
                .hash,
            "old"
        );
        finish_broadcast(&mut db, &source, &snapshot, "new", &options).unwrap();
        assert_eq!(
            crate::database::latest_snapshot(&db, "daily_baseline", None, Some("group:1"))
                .unwrap()
                .unwrap()
                .hash,
            "new"
        );
    }
}

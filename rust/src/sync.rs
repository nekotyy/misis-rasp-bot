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

pub struct DeliveryPlatforms {
    pub telegram: bool,
    pub vk: bool,
}
pub enum AtomicApplyResult {
    RejectedEmpty,
    FirstBaseline,
    Unchanged,
    Changed {
        summary: ChangeSummary,
        queued: usize,
        duplicate: bool,
    },
}
fn save_in_transaction(
    tx: &rusqlite::Transaction<'_>,
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
    crate::database::save_snapshot_in_transaction(
        tx,
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
pub fn apply_and_enqueue(
    db: &mut rusqlite::Connection,
    source: &Source<'_>,
    snapshot: &ScheduleSnapshot,
    hash: &str,
    options: &ApplyOptions<'_>,
    platforms: &DeliveryPlatforms,
    now_timestamp: f64,
) -> Result<AtomicApplyResult, Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let previous = crate::database::latest_snapshot(
        &tx,
        "current",
        source.schedule_id,
        Some(source.source_key),
    )?;
    let baseline = crate::database::latest_snapshot(
        &tx,
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
        return Ok(AtomicApplyResult::RejectedEmpty);
    }
    save_in_transaction(&tx, source, snapshot, hash, "current", options)?;
    let result = match decision {
        SnapshotDecision::RejectStaleEmpty => unreachable!(),
        SnapshotDecision::FirstBaseline => {
            save_in_transaction(&tx, source, snapshot, hash, "daily_baseline", options)?;
            AtomicApplyResult::FirstBaseline
        }
        SnapshotDecision::Unchanged => AtomicApplyResult::Unchanged,
        SnapshotDecision::Changed(summary) => {
            let mut queued = 0;
            let mut duplicate = false;
            if options.notify {
                let created = options.now.format("%Y-%m-%dT%H:%M:%S").to_string();
                let recorded = crate::database::record_change(
                    &tx,
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
                duplicate = !recorded;
                if recorded && (platforms.telegram || platforms.vk) {
                    let target = if platforms.telegram && platforms.vk {
                        "all"
                    } else if platforms.telegram {
                        "telegram"
                    } else {
                        "vk"
                    };
                    let event_id = format!("{}:{hash}:{}", source.source_key, options.now.date());
                    let queue = |summary: &ChangeSummary,
                                 group: Option<Option<i64>>,
                                 suffix: &str|
                     -> rusqlite::Result<usize> {
                        crate::notification::enqueue_broadcast_in_transaction(
                            &tx,
                            &crate::notification::Broadcast {
                                event_id: &format!("{event_id}:{suffix}"),
                                plain: &summary.message,
                                telegram: Some(&summary.telegram_message),
                                vk: Some(&summary.vk_message),
                                campaign: "notification",
                                target_platform: target,
                                audience: "all",
                                schedule_id: source.schedule_id,
                                subscription_key: Some(source.source_key),
                                subgroup: group,
                            },
                            now_timestamp,
                        )
                    };
                    if university {
                        let baseline = baseline.as_ref().expect("changed requires baseline");
                        for group in crate::database::university_notification_subgroups(
                            &tx,
                            source.source_key,
                        )? {
                            let subgroup = group.and_then(|v| u32::try_from(v).ok());
                            if let Some(personal) = format::compare(
                                Some(&baseline.content),
                                snapshot,
                                options.now.date(),
                                subgroup,
                                true,
                            ) {
                                queued += queue(
                                    &personal,
                                    Some(group),
                                    &group.map(|x| x.to_string()).unwrap_or_else(|| "all".into()),
                                )?;
                            }
                        }
                    } else {
                        queued += queue(&summary, None, "all")?;
                    }
                }
            }
            save_in_transaction(&tx, source, snapshot, hash, "daily_baseline", options)?;
            AtomicApplyResult::Changed {
                summary,
                queued,
                duplicate,
            }
        }
    };
    tx.commit()?;
    Ok(result)
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

#[cfg(test)]
mod atomic_tests {
    use super::*;
    #[test]
    fn failed_enqueue_rolls_back_change_and_baseline() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch(crate::outbox::SCHEMA).unwrap();
        db.execute_batch("CREATE TABLE schedule_snapshots(id INTEGER PRIMARY KEY,snapshot_type TEXT,source_type TEXT,source_key TEXT,source_title TEXT,source_url TEXT,group_name TEXT,schedule_id INTEGER,snapshot_hash TEXT,content_json TEXT,fetched_at TEXT,created_at TEXT); CREATE TABLE schedule_archive_projected_snapshots(snapshot_id INTEGER PRIMARY KEY); CREATE TABLE schedule_day_archive(source_key TEXT,source_type TEXT,date_iso TEXT,week_variant INTEGER,content_json TEXT,completeness TEXT,origin TEXT,observed_at TEXT,recorded_at TEXT,snapshot_id INTEGER,PRIMARY KEY(source_key,date_iso,week_variant)); CREATE TABLE schedule_day_references(source_key TEXT,date_iso TEXT,week_variant INTEGER,reference_kind TEXT,content_json TEXT,completeness TEXT,origin TEXT,observed_at TEXT,recorded_at TEXT,snapshot_id INTEGER,PRIMARY KEY(source_key,date_iso,week_variant,reference_kind)); CREATE TABLE change_events(source_type TEXT,source_key TEXT,source_title TEXT,source_url TEXT,group_name TEXT,schedule_id INTEGER,snapshot_hash TEXT,message TEXT,changed_dates_json TEXT,payload_json TEXT,created_at TEXT); CREATE UNIQUE INDEX dedup ON change_events(source_key,snapshot_hash,substr(created_at,1,10)); CREATE TABLE users(platform TEXT,user_id INTEGER,username TEXT,full_name TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,audience_subscription_key TEXT,audience_subscription_title TEXT,audience_subscription_url TEXT,group_name TEXT,schedule_id INTEGER,is_admin INTEGER,is_editor INTEGER,homework_notifications_enabled INTEGER,delivery_disabled_auto INTEGER,created_at TEXT,last_seen_at TEXT,custom_sticker_file_id TEXT,university_subgroup INTEGER); CREATE TABLE accounts(id TEXT,notifications_enabled INTEGER); CREATE TABLE account_identities(account_id TEXT,platform TEXT,user_id INTEGER); CREATE TABLE account_subscriptions(account_id TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,group_name TEXT,schedule_id INTEGER,university_subgroup INTEGER,is_primary INTEGER,notifications_enabled INTEGER); INSERT INTO users(platform,user_id,subscription_type,subscription_key,group_name,schedule_id,is_admin,is_editor,homework_notifications_enabled,delivery_disabled_auto,created_at,last_seen_at) VALUES ('telegram',1,'group','group:1','Группа',1,0,0,1,0,'now','now');").unwrap();
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
        let platforms = DeliveryPlatforms {
            telegram: true,
            vk: false,
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
            apply_and_enqueue(&mut db, &source, &snapshot, "old", &options, &platforms, 100.0)
                .unwrap(),
            AtomicApplyResult::FirstBaseline
        ));
        snapshot.days[0].lessons[0].subject = "B".into();
        db.execute_batch("CREATE TRIGGER fail_outbox BEFORE INSERT ON delivery_outbox BEGIN SELECT RAISE(ABORT,'failure'); END;").unwrap();
        assert!(
            apply_and_enqueue(&mut db, &source, &snapshot, "new", &options, &platforms, 100.0)
                .is_err()
        );
        assert_eq!(
            crate::database::latest_snapshot(&db, "current", None, Some("group:1"))
                .unwrap()
                .unwrap()
                .hash,
            "old"
        );
        let events: i64 = db
            .query_row("SELECT count(*) FROM change_events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(events, 0);
        db.execute_batch("DROP TRIGGER fail_outbox").unwrap();
        assert!(matches!(
            apply_and_enqueue(&mut db, &source, &snapshot, "new", &options, &platforms, 100.0)
                .unwrap(),
            AtomicApplyResult::Changed {
                queued: 1,
                duplicate: false,
                ..
            }
        ));
        assert_eq!(
            crate::database::latest_snapshot(&db, "daily_baseline", None, Some("group:1"))
                .unwrap()
                .unwrap()
                .hash,
            "new"
        );
        let jobs: i64 = db
            .query_row("SELECT count(*) FROM delivery_outbox", [], |r| r.get(0))
            .unwrap();
        assert_eq!(jobs, 1);
    }
}

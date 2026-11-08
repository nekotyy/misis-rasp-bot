use crate::DaySchedule;
use chrono::{DateTime, NaiveDate, SecondsFormat, Utc};
use rusqlite::{params, Connection, OptionalExtension};
use std::time::Duration;

type ArchiveRow = (
    String,
    String,
    String,
    String,
    String,
    String,
    String,
    Option<i64>,
);

pub struct ArchiveEntry {
    pub source_key: String,
    pub source_type: String,
    pub day: DaySchedule,
    pub completeness: String,
    pub origin: String,
    pub observed_at: String,
    pub recorded_at: String,
    pub snapshot_id: Option<i64>,
}
pub struct ArchiveInput<'a> {
    pub source_key: &'a str,
    pub source_type: &'a str,
    pub day: &'a DaySchedule,
    pub observed_at: DateTime<Utc>,
    pub origin: &'a str,
    pub completeness: &'a str,
    pub snapshot_id: Option<i64>,
}
pub fn save_day(
    db: &Connection,
    input: &ArchiveInput<'_>,
) -> Result<bool, Box<dyn std::error::Error>> {
    let day = input.day;
    if input.source_key.trim().is_empty()
        || input.source_type.trim().is_empty()
        || input.origin.trim().is_empty()
    {
        return Err("Источник и происхождение должны быть указаны".into());
    }
    let date = NaiveDate::parse_from_str(&day.date_iso, "%Y-%m-%d")?;
    if date.format("%Y-%m-%d").to_string() != day.date_iso
        || !matches!(day.week_variant, None | Some(1 | 2))
        || !matches!(input.completeness, "unknown" | "complete")
        || day.lessons.iter().any(|x| x.number <= 0)
    {
        return Err("Некорректный день расписания".into());
    }
    db.busy_timeout(Duration::from_secs(30))?;
    let changed = db.execute("INSERT INTO schedule_day_archive (source_key,source_type,date_iso,week_variant,content_json,completeness,origin,observed_at,recorded_at,snapshot_id) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key,date_iso,week_variant) DO UPDATE SET source_type=excluded.source_type,content_json=excluded.content_json,completeness=excluded.completeness,origin=excluded.origin,observed_at=excluded.observed_at,recorded_at=excluded.recorded_at,snapshot_id=excluded.snapshot_id WHERE excluded.observed_at > schedule_day_archive.observed_at", params![input.source_key,input.source_type,day.date_iso,day.week_variant.unwrap_or(0),serde_json::to_string(day)?,input.completeness,input.origin,input.observed_at.to_rfc3339_opts(SecondsFormat::Micros, false),Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false),input.snapshot_id])?;
    Ok(changed == 1)
}
pub fn get_day(
    db: &Connection,
    source: &str,
    day: NaiveDate,
    variant: Option<i64>,
) -> Result<Option<ArchiveEntry>, Box<dyn std::error::Error>> {
    if !matches!(variant, None | Some(1 | 2)) {
        return Err("Некорректный вариант недели".into());
    }
    let result: Option<ArchiveRow> = db.query_row(
        "SELECT source_key,source_type,content_json,completeness,origin,observed_at,recorded_at,snapshot_id FROM schedule_day_archive WHERE source_key=? AND date_iso=? AND week_variant=?",
        params![source,day.to_string(),variant.unwrap_or(0)], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?,row.get(7)?))).optional()?;
    result
        .map(
            |(
                source_key,
                source_type,
                content,
                completeness,
                origin,
                observed_at,
                recorded_at,
                snapshot_id,
            )| {
                Ok(ArchiveEntry {
                    source_key,
                    source_type,
                    day: serde_json::from_str(&content)?,
                    completeness,
                    origin,
                    observed_at,
                    recorded_at,
                    snapshot_id,
                })
            },
        )
        .transpose()
}

#[derive(Clone, Copy)]
pub struct ProjectionInput<'a> {
    pub snapshot: &'a crate::ScheduleSnapshot,
    pub source_key: &'a str,
    pub source_type: &'a str,
    pub origin: &'a str,
    pub timezone: chrono_tz::Tz,
    pub snapshot_id: i64,
    pub dates: Option<&'a std::collections::HashSet<String>>,
}
pub fn archive_accepted_snapshot(
    db: &Connection,
    input: &ProjectionInput<'_>,
) -> Result<(), Box<dyn std::error::Error>> {
    let ProjectionInput {
        snapshot,
        source_key,
        source_type,
        origin,
        timezone,
        snapshot_id,
        dates,
    } = *input;
    use chrono::TimeZone;
    if source_key.is_empty() || source_type.is_empty() {
        return Ok(());
    }
    let observed = if let Ok(value) = DateTime::parse_from_rfc3339(&snapshot.fetched_at) {
        value.to_utc()
    } else {
        let local =
            chrono::NaiveDateTime::parse_from_str(&snapshot.fetched_at, "%Y-%m-%dT%H:%M:%S%.f")?;
        timezone
            .from_local_datetime(&local)
            .earliest()
            .ok_or("Невозможно определить время наблюдения")?
            .to_utc()
    };
    let observed_date = observed.with_timezone(&timezone).date_naive();
    let observed_iso = observed.to_rfc3339_opts(SecondsFormat::Micros, false);
    let recorded = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    for day in &snapshot.days {
        if dates.is_some_and(|set| !set.contains(&day.date_iso)) {
            continue;
        }
        let Ok(date) = NaiveDate::parse_from_str(&day.date_iso, "%Y-%m-%d") else {
            continue;
        };
        if date.to_string() != day.date_iso
            || !matches!(day.week_variant, None | Some(1 | 2))
            || day.lessons.iter().any(|item| item.number <= 0)
            || snapshot
                .days
                .iter()
                .filter(|other| {
                    other.date_iso == day.date_iso
                        && other.week_variant.unwrap_or(0) == day.week_variant.unwrap_or(0)
                })
                .count()
                != 1
        {
            continue;
        }
        let content = serde_json::to_string(day)?;
        let variant = day.week_variant.unwrap_or(0);
        db.execute("INSERT INTO schedule_day_archive (source_key,source_type,date_iso,week_variant,content_json,completeness,origin,observed_at,recorded_at,snapshot_id) VALUES (?,?,?,?,?,'unknown',?,?,?,?) ON CONFLICT(source_key,date_iso,week_variant) DO UPDATE SET source_type=excluded.source_type,content_json=excluded.content_json,completeness=excluded.completeness,origin=excluded.origin,observed_at=excluded.observed_at,recorded_at=excluded.recorded_at,snapshot_id=excluded.snapshot_id WHERE excluded.observed_at > schedule_day_archive.observed_at", params![source_key,source_type,day.date_iso,variant,content,origin,observed_iso,recorded,snapshot_id])?;
        if day.lessons.is_empty() || date < observed_date {
            continue;
        }
        let kinds: &[&str] = if date == observed_date {
            &["first_seen", "day_start"]
        } else {
            &["first_seen"]
        };
        for kind in kinds {
            db.execute("INSERT INTO schedule_day_references (source_key,date_iso,week_variant,reference_kind,content_json,completeness,origin,observed_at,recorded_at,snapshot_id) VALUES (?,?,?,?,?,'unknown',?,?,?,?) ON CONFLICT(source_key,date_iso,week_variant,reference_kind) DO UPDATE SET content_json=excluded.content_json,completeness=excluded.completeness,origin=excluded.origin,observed_at=excluded.observed_at,recorded_at=excluded.recorded_at,snapshot_id=excluded.snapshot_id WHERE excluded.observed_at < schedule_day_references.observed_at", params![source_key,day.date_iso,variant,kind,content,origin,observed_iso,recorded,snapshot_id])?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn newer_snapshot_wins_and_round_trips() {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE schedule_day_archive (source_key TEXT, source_type TEXT, date_iso TEXT, week_variant INTEGER, content_json TEXT, completeness TEXT, origin TEXT, observed_at TEXT, recorded_at TEXT, snapshot_id INTEGER, PRIMARY KEY(source_key,date_iso,week_variant));").unwrap();
        let day = DaySchedule {
            date_label: "Понедельник".into(),
            date_iso: "2026-11-09".into(),
            lessons: vec![],
            week_variant: None,
            current_week_variant: None,
        };
        let time = DateTime::parse_from_rfc3339("2026-11-08T12:00:00Z")
            .unwrap()
            .to_utc();
        let input = ArchiveInput {
            source_key: "group:1",
            source_type: "group",
            day: &day,
            observed_at: time,
            origin: "test",
            completeness: "unknown",
            snapshot_id: Some(1),
        };
        assert!(save_day(&db, &input).unwrap());
        assert!(!save_day(&db, &input).unwrap());
        assert_eq!(
            get_day(
                &db,
                "group:1",
                NaiveDate::from_ymd_opt(2026, 11, 9).unwrap(),
                None
            )
            .unwrap()
            .unwrap()
            .day,
            day
        );
    }
}

#[cfg(test)]
mod projection_tests {
    use super::*;
    #[test]
    fn projection_keeps_earliest_reference_and_latest_day() {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE schedule_day_archive (source_key TEXT,source_type TEXT,date_iso TEXT,week_variant INTEGER,content_json TEXT,completeness TEXT,origin TEXT,observed_at TEXT,recorded_at TEXT,snapshot_id INTEGER,PRIMARY KEY(source_key,date_iso,week_variant)); CREATE TABLE schedule_day_references (source_key TEXT,date_iso TEXT,week_variant INTEGER,reference_kind TEXT,content_json TEXT,completeness TEXT,origin TEXT,observed_at TEXT,recorded_at TEXT,snapshot_id INTEGER,PRIMARY KEY(source_key,date_iso,week_variant,reference_kind));").unwrap();
        let mut snapshot = crate::ScheduleSnapshot {
            group_name: "Группа".into(),
            fetched_at: "2026-11-08T10:00:00+00:00".into(),
            days: vec![DaySchedule {
                date_label: "".into(),
                date_iso: "2026-11-09".into(),
                lessons: vec![crate::Lesson {
                    number: 1,
                    subject: "A".into(),
                    teacher: "".into(),
                    classroom: "".into(),
                }],
                week_variant: None,
                current_week_variant: None,
            }],
        };
        archive_accepted_snapshot(
            &db,
            &ProjectionInput {
                snapshot: &snapshot,
                source_key: "group:1",
                source_type: "group",
                origin: "test",
                timezone: chrono_tz::UTC,
                snapshot_id: 1,
                dates: None,
            },
        )
        .unwrap();
        snapshot.fetched_at = "2026-11-08T11:00:00+00:00".into();
        snapshot.days[0].lessons[0].subject = "B".into();
        archive_accepted_snapshot(
            &db,
            &ProjectionInput {
                snapshot: &snapshot,
                source_key: "group:1",
                source_type: "group",
                origin: "test",
                timezone: chrono_tz::UTC,
                snapshot_id: 2,
                dates: None,
            },
        )
        .unwrap();
        let latest: i64 = db
            .query_row("SELECT snapshot_id FROM schedule_day_archive", [], |r| {
                r.get(0)
            })
            .unwrap();
        let reference: i64 = db
            .query_row("SELECT snapshot_id FROM schedule_day_references", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!((latest, reference), (2, 1));
        let date = NaiveDate::from_ymd_opt(2026, 11, 9).unwrap();
        let today = NaiveDate::from_ymd_opt(2026, 11, 8).unwrap();
        assert_eq!(
            get_reference(&db, "group:1", date, today, None)
                .unwrap()
                .unwrap()
                .snapshot_id,
            Some(1)
        );
        assert_eq!(
            get_days(&db, "group:1", today, date).unwrap()[0].snapshot_id,
            Some(2)
        );
        assert!(get_reference(&db, "group:1", today, date, None)
            .unwrap()
            .is_none());
    }
}

pub struct ReferenceEntry {
    pub source_key: String,
    pub day: DaySchedule,
    pub reference_kind: String,
    pub completeness: String,
    pub origin: String,
    pub observed_at: String,
    pub recorded_at: String,
    pub snapshot_id: Option<i64>,
}
pub fn get_reference(
    db: &Connection,
    source: &str,
    day: NaiveDate,
    today: NaiveDate,
    variant: Option<i64>,
) -> Result<Option<ReferenceEntry>, Box<dyn std::error::Error>> {
    if source.trim().is_empty() || !matches!(variant, None | Some(1 | 2)) {
        return Err("Не указан источник или неверный вариант недели".into());
    }
    if day < today {
        return Ok(None);
    }
    let kind = if day == today {
        "day_start"
    } else {
        "first_seen"
    };
    let result: Option<ArchiveRow> = db.query_row("SELECT source_key,reference_kind,content_json,completeness,origin,observed_at,recorded_at,snapshot_id FROM schedule_day_references WHERE source_key=? AND date_iso=? AND week_variant=? AND reference_kind=?",params![source,day.to_string(),variant.unwrap_or(0),kind],|r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?))).optional()?;
    result
        .map(
            |(
                source_key,
                reference_kind,
                content,
                completeness,
                origin,
                observed_at,
                recorded_at,
                snapshot_id,
            )| {
                Ok(ReferenceEntry {
                    source_key,
                    reference_kind,
                    day: serde_json::from_str(&content)?,
                    completeness,
                    origin,
                    observed_at,
                    recorded_at,
                    snapshot_id,
                })
            },
        )
        .transpose()
}
pub fn get_days(
    db: &Connection,
    source: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<ArchiveEntry>, Box<dyn std::error::Error>> {
    if source.trim().is_empty() || start > end {
        return Err("Не указан источник или неверный интервал дат".into());
    }
    let mut statement = db.prepare("SELECT source_key,source_type,content_json,completeness,origin,observed_at,recorded_at,snapshot_id FROM schedule_day_archive WHERE source_key=? AND date_iso BETWEEN ? AND ? ORDER BY date_iso DESC,week_variant ASC")?;
    let rows = statement.query_map(params![source, start.to_string(), end.to_string()], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, String>(3)?,
            r.get::<_, String>(4)?,
            r.get::<_, String>(5)?,
            r.get::<_, String>(6)?,
            r.get::<_, Option<i64>>(7)?,
        ))
    })?;
    rows.map(|row| {
        let (
            source_key,
            source_type,
            content,
            completeness,
            origin,
            observed_at,
            recorded_at,
            snapshot_id,
        ) = row?;
        Ok(ArchiveEntry {
            source_key,
            source_type,
            day: serde_json::from_str(&content)?,
            completeness,
            origin,
            observed_at,
            recorded_at,
            snapshot_id,
        })
    })
    .collect()
}

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

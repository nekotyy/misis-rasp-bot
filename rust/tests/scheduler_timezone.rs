use std::{collections::HashMap, sync::Arc};

use chrono::{NaiveDate, TimeZone, Utc};
use misis_rasp_core::{config::Settings, notification::ApiTransport, scheduler::Scheduler};

fn scheduler(timezone: &str) -> Result<Scheduler, Box<dyn std::error::Error>> {
    let settings = Settings::from_map(&HashMap::from([("APP_TIMEZONE".into(), timezone.into())]))?;
    let api = ApiTransport::new(
        "http://127.0.0.1:1",
        "http://127.0.0.1:1",
        "".into(),
        "".into(),
        "5.131",
    )?;
    Scheduler::new(settings, Arc::new(api))
}

#[test]
fn scheduler_uses_configured_timezone_across_date_boundary() {
    let now = Utc.with_ymd_and_hms(2026, 1, 1, 0, 30, 0).unwrap();
    assert_eq!(
        scheduler("Europe/Moscow").unwrap().local_time(now).date(),
        NaiveDate::from_ymd_opt(2026, 1, 1).unwrap()
    );
    assert_eq!(
        scheduler("America/Los_Angeles")
            .unwrap()
            .local_time(now)
            .date(),
        NaiveDate::from_ymd_opt(2025, 12, 31).unwrap()
    );
}

#[test]
fn scheduler_rejects_invalid_timezone() {
    assert!(scheduler("not/a-timezone").is_err());
}

#[test]
fn scheduler_respects_daylight_saving_transition() {
    let scheduler = scheduler("America/Los_Angeles").unwrap();
    let before = Utc.with_ymd_and_hms(2026, 3, 8, 9, 59, 0).unwrap();
    let after = Utc.with_ymd_and_hms(2026, 3, 8, 10, 0, 0).unwrap();
    let date = NaiveDate::from_ymd_opt(2026, 3, 8).unwrap();
    assert_eq!(
        scheduler.local_time(before),
        date.and_hms_opt(1, 59, 0).unwrap()
    );
    assert_eq!(
        scheduler.local_time(after),
        date.and_hms_opt(3, 0, 0).unwrap()
    );
}

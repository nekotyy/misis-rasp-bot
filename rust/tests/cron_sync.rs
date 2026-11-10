use chrono::TimeZone;
use cron_parser::Schedule;
use misis_rasp_core::cron_sync::{run_sync_cron, SyncSchedule, SyncTrigger, SYNC_CRONS};
use tokio::sync::{mpsc, watch};

#[test]
fn cron_matches_python_hours_and_extra_checks() {
    let patterns: Vec<_> = SYNC_CRONS
        .iter()
        .map(|pattern| Schedule::parse(pattern).unwrap())
        .collect();
    let timezone = chrono_tz::Europe::Moscow;
    for (hour, minute, expected) in [
        (8, 10, true),
        (8, 40, false),
        (9, 40, true),
        (13, 40, true),
        (14, 40, false),
        (14, 15, false),
    ] {
        let now = timezone
            .with_ymd_and_hms(2026, 11, 10, hour, minute, 0)
            .unwrap();
        assert_eq!(
            patterns.iter().any(|cron| cron.includes(&now)),
            expected,
            "{hour}:{minute}"
        );
    }
}

#[test]
fn next_sync_follows_hourly_and_extra_classroom_checks() {
    let schedule = SyncSchedule::new().unwrap();
    let timezone = chrono_tz::Europe::Moscow;
    for (hour, minute, next_hour, next_minute) in [(8, 59, 9, 10), (9, 11, 9, 40), (13, 41, 14, 10)]
    {
        let local = timezone
            .with_ymd_and_hms(2026, 11, 10, hour, minute, 0)
            .unwrap();
        let expected = timezone
            .with_ymd_and_hms(2026, 11, 10, next_hour, next_minute, 0)
            .unwrap();
        assert_eq!(
            schedule.next_after(local.with_timezone(&chrono::Utc), timezone),
            Some(expected.with_timezone(&chrono::Utc))
        );
    }
}

#[test]
fn hourly_schedule_visits_both_repeated_hours_on_dst_fallback() {
    let schedule = SyncSchedule::new().unwrap();
    let timezone = chrono_tz::America::Los_Angeles;
    let before = chrono::Utc.with_ymd_and_hms(2026, 11, 1, 8, 9, 0).unwrap();
    let first = schedule.next_after(before, timezone).unwrap();
    let second = schedule.next_after(first, timezone).unwrap();
    assert_eq!(
        first,
        chrono::Utc.with_ymd_and_hms(2026, 11, 1, 8, 10, 0).unwrap()
    );
    assert_eq!(
        second,
        chrono::Utc.with_ymd_and_hms(2026, 11, 1, 9, 10, 0).unwrap()
    );
}

#[test]
fn hourly_schedule_skips_nonexistent_local_hour_on_dst_start() {
    let schedule = SyncSchedule::new().unwrap();
    let timezone = chrono_tz::America::Los_Angeles;
    let before = chrono::Utc.with_ymd_and_hms(2026, 3, 8, 9, 9, 0).unwrap();
    let first = schedule.next_after(before, timezone).unwrap();
    let second = schedule.next_after(first, timezone).unwrap();
    assert_eq!(
        first,
        chrono::Utc.with_ymd_and_hms(2026, 3, 8, 9, 10, 0).unwrap()
    );
    assert_eq!(
        second,
        chrono::Utc.with_ymd_and_hms(2026, 3, 8, 10, 10, 0).unwrap()
    );
}

#[test]
fn overlapping_triggers_are_skipped_until_the_run_finishes() {
    let (sender, mut receiver) = mpsc::channel(1);
    let trigger = SyncTrigger::new(sender);
    assert!(trigger.trigger());
    assert!(!trigger.trigger());
    assert_eq!(receiver.try_recv(), Ok(()));
    assert!(!trigger.trigger());
    trigger.finished();
    assert!(trigger.trigger());
    assert_eq!(receiver.try_recv(), Ok(()));
    trigger.finished();
    drop(receiver);
    assert!(!trigger.trigger());
}

#[tokio::test]
async fn cron_honors_shutdown_before_waiting() {
    let (sender, _receiver) = mpsc::channel(1);
    let (_shutdown_tx, shutdown) = watch::channel(true);
    let result = run_sync_cron(
        SyncSchedule::new().unwrap(),
        chrono_tz::Europe::Moscow,
        SyncTrigger::new(sender),
        shutdown,
    )
    .await;
    assert!(result.is_ok());
}

#[tokio::test]
async fn cron_can_be_cancelled_while_waiting_for_the_next_job() {
    let (sender, _receiver) = mpsc::channel(1);
    let (shutdown_tx, shutdown) = watch::channel(false);
    let task = tokio::spawn(run_sync_cron(
        SyncSchedule::new().unwrap(),
        chrono_tz::Europe::Moscow,
        SyncTrigger::new(sender),
        shutdown,
    ));
    tokio::task::yield_now().await;
    shutdown_tx.send(true).unwrap();
    let result = tokio::time::timeout(std::time::Duration::from_secs(2), task)
        .await
        .unwrap()
        .unwrap();
    assert!(result.is_ok());
}

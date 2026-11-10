use chrono::TimeZone;
use misis_rasp_core::cron_sync::{run_cleanup_cron, CleanupSchedule, SyncTrigger};
use tokio::sync::{mpsc, watch};

#[test]
fn cleanup_is_scheduled_on_monday_at_0430_in_app_timezone() {
    let schedule = CleanupSchedule::new().unwrap();
    let zone = chrono_tz::Europe::Moscow;
    let sunday = zone
        .with_ymd_and_hms(2026, 11, 8, 23, 59, 0)
        .unwrap()
        .with_timezone(&chrono::Utc);
    let monday = zone
        .with_ymd_and_hms(2026, 11, 9, 4, 30, 0)
        .unwrap()
        .with_timezone(&chrono::Utc);
    let following_monday = zone
        .with_ymd_and_hms(2026, 11, 16, 4, 30, 0)
        .unwrap()
        .with_timezone(&chrono::Utc);
    assert_eq!(schedule.next_after(sunday, zone), Some(monday));
    assert_eq!(schedule.next_after(monday, zone), Some(following_monday));
}

#[test]
fn cleanup_uses_new_offset_after_daylight_saving_ends() {
    let schedule = CleanupSchedule::new().unwrap();
    let before_transition = chrono::Utc
        .with_ymd_and_hms(2026, 10, 31, 12, 0, 0)
        .unwrap();
    let expected = chrono::Utc
        .with_ymd_and_hms(2026, 11, 2, 12, 30, 0)
        .unwrap();
    assert_eq!(
        schedule.next_after(before_transition, chrono_tz::America::Los_Angeles),
        Some(expected)
    );
}

#[tokio::test]
async fn cleanup_timer_can_be_cancelled() {
    let (sender, _receiver) = mpsc::channel(1);
    let (shutdown_tx, shutdown) = watch::channel(false);
    let task = tokio::spawn(run_cleanup_cron(
        CleanupSchedule::new().unwrap(),
        chrono_tz::Europe::Moscow,
        SyncTrigger::new(sender),
        shutdown,
    ));
    tokio::task::yield_now().await;
    shutdown_tx.send(true).unwrap();
    assert!(
        tokio::time::timeout(std::time::Duration::from_secs(2), task)
            .await
            .unwrap()
            .unwrap()
            .is_ok()
    );
}

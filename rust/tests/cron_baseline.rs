use chrono::TimeZone;
use misis_rasp_core::cron_sync::{
    run_baseline_cron, BaselineKind, BaselineSchedule, BaselineTrigger,
};
use tokio::sync::{mpsc, watch};

#[test]
fn baseline_runs_at_midnight_and_fallback_at_five() {
    let schedule = BaselineSchedule::new().unwrap();
    let timezone = chrono_tz::Europe::Moscow;
    for (day, hour, minute, next_day, next_hour, expected_kind) in [
        (10, 23, 59, 11, 0, BaselineKind::Daily),
        (11, 0, 1, 11, 5, BaselineKind::Fallback),
        (11, 5, 1, 12, 0, BaselineKind::Daily),
    ] {
        let now = timezone
            .with_ymd_and_hms(2026, 11, day, hour, minute, 0)
            .unwrap()
            .with_timezone(&chrono::Utc);
        let next = timezone
            .with_ymd_and_hms(2026, 11, next_day, next_hour, 0, 0)
            .unwrap()
            .with_timezone(&chrono::Utc);
        assert_eq!(
            schedule.next_after(now, timezone),
            Some((next, expected_kind))
        );
    }
}

#[test]
fn baseline_retains_both_midnights_when_clock_moves_back() {
    let schedule = BaselineSchedule::new().unwrap();
    let zone = chrono_tz::America::Havana;
    let before = chrono::Utc.with_ymd_and_hms(2026, 11, 1, 3, 59, 0).unwrap();
    let first = schedule.next_after(before, zone).unwrap();
    let second = schedule.next_after(first.0, zone).unwrap();
    assert_eq!(
        first,
        (
            chrono::Utc.with_ymd_and_hms(2026, 11, 1, 4, 0, 0).unwrap(),
            BaselineKind::Daily
        )
    );
    assert_eq!(
        second,
        (
            chrono::Utc.with_ymd_and_hms(2026, 11, 1, 5, 0, 0).unwrap(),
            BaselineKind::Daily
        )
    );
}

#[test]
fn daily_and_fallback_do_not_overlap_or_queue_twice() {
    let (sender, mut receiver) = mpsc::channel(1);
    let trigger = BaselineTrigger::new(sender);
    assert!(trigger.trigger(BaselineKind::Daily));
    assert!(!trigger.trigger(BaselineKind::Fallback));
    assert_eq!(receiver.try_recv(), Ok(BaselineKind::Daily));
    assert!(!trigger.trigger(BaselineKind::Fallback));
    trigger.finished();
    assert!(trigger.trigger(BaselineKind::Fallback));
    assert_eq!(receiver.try_recv(), Ok(BaselineKind::Fallback));
}

#[test]
fn delayed_clock_coalesces_each_baseline_kind_without_inventing_future_jobs() {
    let schedule = BaselineSchedule::new().unwrap();
    let zone = chrono_tz::Europe::Moscow;
    let local = |day, hour, minute| {
        zone.with_ymd_and_hms(2026, 11, day, hour, minute, 0)
            .unwrap()
            .with_timezone(&chrono::Utc)
    };
    assert_eq!(
        schedule.due_since(local(10, 0, 0), local(10, 6, 0), zone),
        vec![BaselineKind::Daily, BaselineKind::Fallback]
    );
    assert_eq!(
        schedule.due_since(local(10, 0, 0), local(11, 6, 0), zone),
        vec![BaselineKind::Daily, BaselineKind::Fallback]
    );
    assert_eq!(
        schedule.due_since(local(10, 5, 0), local(10, 5, 0), zone),
        vec![BaselineKind::Fallback]
    );
    assert!(schedule
        .due_since(local(10, 5, 0), local(10, 4, 59), zone)
        .is_empty());
}

#[tokio::test]
async fn baseline_clock_can_be_cancelled() {
    let (sender, _receiver) = mpsc::channel(1);
    let (shutdown_tx, shutdown) = watch::channel(false);
    let task = tokio::spawn(run_baseline_cron(
        BaselineSchedule::new().unwrap(),
        chrono_tz::Europe::Moscow,
        BaselineTrigger::new(sender),
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

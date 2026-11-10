//! Календарная проверка расписания с готовым cron-парсером.

use chrono::{DateTime, Utc};
use cron_parser::Schedule;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use tokio::sync::{mpsc, watch};

/// Формат: минута, час, день месяца, месяц, день недели.
pub const SYNC_CRONS: [&str; 2] = ["10 * * * *", "40 9-13 * * *"];

pub struct SyncSchedule {
    patterns: Vec<Schedule>,
}

impl SyncSchedule {
    pub fn new() -> Result<Self, cron_parser::ParseError> {
        Ok(Self {
            patterns: SYNC_CRONS
                .iter()
                .map(|pattern| Schedule::parse(pattern))
                .collect::<Result<_, _>>()?,
        })
    }

    /// Следующий момент вычисляется библиотекой в полном часовом поясе,
    /// включая оба повторяющихся часа при переводе времени назад.
    pub fn next_after(
        &self,
        after: DateTime<Utc>,
        timezone: chrono_tz::Tz,
    ) -> Option<DateTime<Utc>> {
        let local = after.with_timezone(&timezone);
        self.patterns
            .iter()
            .filter_map(|pattern| pattern.next_after(&local))
            .map(|time| time.with_timezone(&Utc))
            .min()
    }
}

/// Сигнал не накапливается, пока предыдущая синхронизация выполняется.
#[derive(Clone)]
pub struct SyncTrigger {
    sender: mpsc::Sender<()>,
    busy: Arc<AtomicBool>,
}

impl SyncTrigger {
    pub fn new(sender: mpsc::Sender<()>) -> Self {
        Self {
            sender,
            busy: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn trigger(&self) -> bool {
        if self.busy.swap(true, Ordering::AcqRel) {
            return false;
        }
        if self.sender.try_send(()).is_err() {
            self.busy.store(false, Ordering::Release);
            return false;
        }
        true
    }

    pub fn finished(&self) {
        self.busy.store(false, Ordering::Release);
    }
}

/// Небольшой адаптер Tokio: cron-парсер вычисляет календарь, таймер
/// периодически перепроверяет системные часы и поддерживает отмену.
pub async fn run_sync_cron(
    schedule: SyncSchedule,
    timezone: chrono_tz::Tz,
    trigger: SyncTrigger,
    mut shutdown: watch::Receiver<bool>,
) -> Result<(), &'static str> {
    loop {
        if *shutdown.borrow() {
            return Ok(());
        }
        let now = Utc::now();
        let next = schedule
            .next_after(now, timezone)
            .ok_or("no next sync time")?;
        let delay = (next - now)
            .to_std()
            .map_err(|_| "invalid sync time")?
            .min(std::time::Duration::from_secs(60));
        tokio::select! {
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { return Ok(()); },
            _ = tokio::time::sleep(delay) => {
                if Utc::now() >= next { trigger.trigger(); }
            },
        }
    }
}

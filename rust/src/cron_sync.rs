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
pub const BASELINE_CRONS: [(&str, BaselineKind); 2] = [
    ("0 0 * * *", BaselineKind::Daily),
    ("0 5 * * *", BaselineKind::Fallback),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BaselineKind {
    Daily,
    Fallback,
}

pub struct BaselineSchedule {
    patterns: Vec<(Schedule, BaselineKind)>,
}

impl BaselineSchedule {
    pub fn new() -> Result<Self, cron_parser::ParseError> {
        Ok(Self {
            patterns: BASELINE_CRONS
                .iter()
                .map(|(pattern, kind)| Ok((Schedule::parse(pattern)?, *kind)))
                .collect::<Result<_, cron_parser::ParseError>>()?,
        })
    }

    pub fn next_after(
        &self,
        after: DateTime<Utc>,
        timezone: chrono_tz::Tz,
    ) -> Option<(DateTime<Utc>, BaselineKind)> {
        let local = after.with_timezone(&timezone);
        self.patterns
            .iter()
            .filter_map(|(pattern, kind)| {
                pattern
                    .next_after(&local)
                    .map(|time| (time.with_timezone(&Utc), *kind))
            })
            .min_by_key(|(time, _)| *time)
    }

    /// После паузы процесса возвращаем не более одного события каждого типа,
    /// как coalesce для отдельных задач в Python.
    pub fn due_since(
        &self,
        since: DateTime<Utc>,
        now: DateTime<Utc>,
        timezone: chrono_tz::Tz,
    ) -> Vec<BaselineKind> {
        if now < since {
            return Vec::new();
        }
        let local = now.with_timezone(&timezone);
        let mut due: Vec<_> = self
            .patterns
            .iter()
            .filter_map(|(pattern, kind)| {
                let last = if pattern.includes(&local) {
                    Some(local)
                } else {
                    pattern.previous_before(&local)
                }?;
                let utc = last.with_timezone(&Utc);
                (utc >= since).then_some((utc, *kind))
            })
            .collect();
        due.sort_by_key(|(time, _)| *time);
        due.into_iter().map(|(_, kind)| kind).collect()
    }
}

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

fn try_enqueue<T>(busy: &AtomicBool, sender: &mpsc::Sender<T>, value: T) -> bool {
    if busy.swap(true, Ordering::AcqRel) {
        return false;
    }
    if sender.try_send(value).is_err() {
        busy.store(false, Ordering::Release);
        return false;
    }
    true
}

impl SyncTrigger {
    pub fn new(sender: mpsc::Sender<()>) -> Self {
        Self {
            sender,
            busy: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn trigger(&self) -> bool {
        try_enqueue(&self.busy, &self.sender, ())
    }

    pub fn finished(&self) {
        self.busy.store(false, Ordering::Release);
    }
}

/// Основное и резервное сохранение используют один ограничитель параллельности.
#[derive(Clone)]
pub struct BaselineTrigger {
    sender: mpsc::Sender<BaselineKind>,
    busy: Arc<AtomicBool>,
}

impl BaselineTrigger {
    pub fn new(sender: mpsc::Sender<BaselineKind>) -> Self {
        Self {
            sender,
            busy: Arc::new(AtomicBool::new(false)),
        }
    }
    pub fn trigger(&self, kind: BaselineKind) -> bool {
        try_enqueue(&self.busy, &self.sender, kind)
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
                if !*shutdown.borrow() && Utc::now() >= next { trigger.trigger(); }
            },
        }
    }
}

pub async fn run_baseline_cron(
    schedule: BaselineSchedule,
    timezone: chrono_tz::Tz,
    trigger: BaselineTrigger,
    mut shutdown: watch::Receiver<bool>,
) -> Result<(), &'static str> {
    loop {
        if *shutdown.borrow() {
            return Ok(());
        }
        let now = Utc::now();
        let (next, _kind) = schedule
            .next_after(now, timezone)
            .ok_or("no next baseline time")?;
        let delay = (next - now)
            .to_std()
            .map_err(|_| "invalid baseline time")?
            .min(std::time::Duration::from_secs(60));
        tokio::select! {
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { return Ok(()); },
            _ = tokio::time::sleep(delay) => {
                let now = Utc::now();
                if !*shutdown.borrow() && now >= next {
                    for due in schedule.due_since(next, now, timezone) { trigger.trigger(due); }
                }
            },
        }
    }
}

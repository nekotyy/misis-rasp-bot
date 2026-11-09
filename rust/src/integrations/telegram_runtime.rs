//! Подготовка Telegram long poll к запуску без удержания БД во время ожидания.

use chrono::Utc;
use rusqlite::Connection;
use tokio::sync::{watch, Mutex};

use crate::{
    config::Settings,
    database,
    group_catalog::{load_groups, GroupCatalog},
    notification::{ApiTransport, DeliveryFailure},
    parser::ScheduleParser,
    search::{load_targets, SearchCatalog},
    telegram_engine::{
        process_poll_updates, EngineContext, PollError, PollOutcome, TelegramEngine,
    },
    telegram_poll::TelegramLongPoll,
};

/// Сетевое ожидание выполняется до захвата общего подключения SQLite.
/// Сетевые действия в обработчиках событий ещё требуют отдельного переноса.
pub async fn poll_once(
    poll: &mut TelegramLongPoll,
    api: &ApiTransport,
    engine: &mut TelegramEngine,
    db: &Mutex<Connection>,
    settings: &Settings,
    parser: Option<&ScheduleParser>,
) -> Result<PollOutcome, PollError> {
    let updates = poll.fetch(api).await.map_err(PollError::Transport)?;
    if updates.is_empty() {
        return Ok(PollOutcome::Processed(0));
    }
    let timezone: chrono_tz::Tz = settings.app_timezone.parse().map_err(|_| {
        PollError::Handler(Box::new(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "invalid APP_TIMEZONE",
        )))
    })?;
    let mut db = db.lock().await;
    let now = Utc::now();
    let groups = GroupCatalog::new(load_groups(&db).map_err(|e| PollError::Handler(Box::new(e)))?);
    let teachers = load_targets(&db, "teacher").map_err(|e| PollError::Handler(Box::new(e)))?;
    let audiences = load_targets(&db, "audience").map_err(|e| PollError::Handler(Box::new(e)))?;
    let search = SearchCatalog::new(&teachers, &audiences);
    let snapshots: Vec<_> = database::latest_group_snapshots(&db, "current")
        .map_err(PollError::Handler)?
        .into_iter()
        .map(|row| row.content)
        .collect();
    let context = EngineContext {
        settings,
        groups: &groups,
        search: &search,
        group_snapshots: &snapshots,
        now: now.with_timezone(&timezone).naive_local(),
        timestamp: now.timestamp() as f64,
    };
    process_poll_updates(poll, api, engine, &mut db, &context, parser, updates).await
}

#[derive(Debug, PartialEq, Eq)]
pub enum RuntimeStop {
    Shutdown,
    Unsupported { update_id: Option<i64> },
    HandlerFailure,
    AuthenticationOrConflict,
    InvalidRetryDelay,
}

fn server_retry_delay(seconds: f64) -> Option<std::time::Duration> {
    let delay = std::time::Duration::try_from_secs_f64(seconds).ok()?;
    if delay > std::time::Duration::from_secs(365 * 24 * 60 * 60) {
        return None;
    }
    Some(delay.max(std::time::Duration::from_secs(1)))
}

/// Остановка не подтверждает незавершённое событие. Неизвестное событие и
/// ошибка обработчика останавливают цикл, чтобы не потерять данные и не крутить CPU.
pub async fn run_until_shutdown(
    poll: &mut TelegramLongPoll,
    api: &ApiTransport,
    engine: &mut TelegramEngine,
    db: &Mutex<Connection>,
    settings: &Settings,
    parser: Option<&ScheduleParser>,
    shutdown: &mut watch::Receiver<bool>,
) -> RuntimeStop {
    let mut retry_seconds = 1_u64;
    loop {
        if *shutdown.borrow() {
            return RuntimeStop::Shutdown;
        }
        let outcome = tokio::select! {
            changed = shutdown.changed() => {
                if changed.is_err() || *shutdown.borrow() { return RuntimeStop::Shutdown; }
                continue;
            }
            result = poll_once(poll, api, engine, db, settings, parser) => result,
        };
        let delay = match outcome {
            Ok(PollOutcome::Processed(0)) => {
                retry_seconds = 1;
                std::time::Duration::from_millis(250)
            }
            Ok(PollOutcome::Processed(_)) => {
                retry_seconds = 1;
                continue;
            }
            Ok(PollOutcome::Unsupported { update_id, .. }) => {
                return RuntimeStop::Unsupported { update_id }
            }
            Err(PollError::Handler(_)) => return RuntimeStop::HandlerFailure,
            Err(PollError::Transport(DeliveryFailure::Telegram {
                code: 401 | 403 | 409,
                ..
            })) => return RuntimeStop::AuthenticationOrConflict,
            Err(PollError::Transport(DeliveryFailure::Telegram {
                retry_after: Some(delay),
                ..
            })) if delay.is_finite() && delay >= 0.0 => {
                let Some(delay) = server_retry_delay(delay) else {
                    return RuntimeStop::InvalidRetryDelay;
                };
                delay
            }
            Err(PollError::Transport(_)) => {
                let delay = std::time::Duration::from_secs(retry_seconds);
                retry_seconds = retry_seconds.saturating_mul(2).min(60);
                delay
            }
        };
        tokio::select! {
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { return RuntimeStop::Shutdown; },
            _ = tokio::time::sleep(delay) => {},
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn server_retry_after_is_not_shortened() {
        assert_eq!(
            super::server_retry_delay(120.0),
            Some(std::time::Duration::from_secs(120))
        );
        assert_eq!(
            super::server_retry_delay(0.0),
            Some(std::time::Duration::from_secs(1))
        );
        assert_eq!(super::server_retry_delay(f64::MAX), None);
        assert_eq!(super::server_retry_delay(-1.0), None);
    }
}

//! Подготовка Telegram long poll к запуску без удержания БД во время ожидания.

use chrono::Utc;
use rusqlite::Connection;
use tokio::sync::Mutex;

use crate::{
    config::Settings,
    database,
    group_catalog::{load_groups, GroupCatalog},
    notification::ApiTransport,
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

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;

use misis_rasp_core::config::Settings;
use misis_rasp_core::cron_sync::{
    run_baseline_cron, run_sync_cron, BaselineKind, BaselineSchedule, BaselineTrigger,
    SyncSchedule, SyncTrigger,
};
use misis_rasp_core::database;
use misis_rasp_core::notification::ApiTransport;
use misis_rasp_core::scheduler::Scheduler;
use misis_rasp_core::web_server::{create_router, AppState};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("Запуск сервиса misis-rasp-bot на Rust...");

    let settings = Settings::from_env().unwrap_or_else(|e| {
        eprintln!("Ошибка загрузки конфигурации: {e}");
        std::process::exit(1);
    });

    // Пока бот и админка не прошли проверку совместимости, нельзя допускать
    // запуск воркера доставки с настоящими токенами и существующей базой.
    if !settings.telegram_bot_token.is_empty()
        || !settings.vk_bot_token.is_empty()
        || !settings.vk_miniapp_secret.is_empty()
    {
        return Err("Rust-версия ещё не готова к работе с токенами или секретом VK Mini App; используйте изолированную тестовую конфигурацию".into());
    }

    println!("Подключение к базе данных {:?}", settings.database_path);
    let mut conn = rusqlite::Connection::open(&settings.database_path)?;
    database::initialize(&mut conn)?;
    // Сеть каждого фонового задания не должна удерживать mutex веб-сервера.
    // Все подключения работают с той же проверенной базой в режиме WAL.
    let outbox_conn = database::connect_existing(&settings.database_path)?;
    let schedule_conn = database::connect_existing(&settings.database_path)?;

    let db_mutex = Arc::new(Mutex::new(conn));
    // Синхронизация и эталоны остаются последовательными, чтобы не менять
    // порядок обработки расписания и сохранения контрольных точек.
    let db_schedule = Arc::new(Mutex::new(schedule_conn));
    let settings_arc = Arc::new(settings);

    let transport = Arc::new(ApiTransport::new(
        "https://api.telegram.org",
        "https://api.vk.com/method",
        settings_arc.telegram_bot_token.clone(),
        settings_arc.vk_bot_token.clone(),
        "5.131",
    )?);

    let scheduler = Arc::new(Scheduler::new((*settings_arc).clone(), transport.clone())?);

    // 1. Фоновый воркер доставки сообщений из outbox
    let sched_outbox = scheduler.clone();
    let db_outbox = Arc::new(Mutex::new(outbox_conn));
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_millis(1500)).await;
            let now = chrono::Utc::now().timestamp() as f64;
            let mut db = db_outbox.lock().await;
            let _ = sched_outbox.process_outbox_batch(&mut db, 30, now).await;
        }
    });

    // 2. Фоновый планировщик синхронизации расписания
    let sched_sync = scheduler.clone();
    let db_sync = db_schedule.clone();
    let (sync_tx, mut sync_rx) = tokio::sync::mpsc::channel(1);
    let sync_trigger = SyncTrigger::new(sync_tx);
    let sync_timezone = settings_arc.app_timezone.parse::<chrono_tz::Tz>()?;
    let sync_schedule = SyncSchedule::new()?;
    let (_cron_shutdown_tx, cron_shutdown) = tokio::sync::watch::channel(false);
    let baseline_shutdown = cron_shutdown.clone();
    let cron_trigger = sync_trigger.clone();
    let _sync_cron = tokio::spawn(async move {
        if run_sync_cron(sync_schedule, sync_timezone, cron_trigger, cron_shutdown)
            .await
            .is_err()
        {
            eprintln!("Не удалось вычислить время следующей синхронизации");
        }
    });
    tokio::spawn(async move {
        while sync_rx.recv().await.is_some() {
            let now = chrono::Utc::now();
            let now_dt = sched_sync.local_time(now);
            let now_ts = now.timestamp() as f64;
            let mut db = db_sync.lock().await;
            if sched_sync
                .sync_active_sources(&mut db, now_dt, now_ts)
                .await
                .is_err()
            {
                eprintln!("Не удалось выполнить синхронизацию расписания");
            }
            sync_trigger.finished();
        }
    });

    // 3. Эталон в полночь и попытка восстановить пропущенные источники в 05:00.
    let baseline_schedule = BaselineSchedule::new()?;
    let (baseline_tx, mut baseline_rx) = tokio::sync::mpsc::channel(1);
    let baseline_trigger = BaselineTrigger::new(baseline_tx);
    let cron_baseline_trigger = baseline_trigger.clone();
    let _baseline_cron = tokio::spawn(async move {
        if run_baseline_cron(
            baseline_schedule,
            sync_timezone,
            cron_baseline_trigger,
            baseline_shutdown,
        )
        .await
        .is_err()
        {
            eprintln!("Не удалось вычислить время сохранения эталонов");
        }
    });
    let sched_daily = scheduler.clone();
    let db_daily = db_schedule.clone();
    tokio::spawn(async move {
        while let Some(kind) = baseline_rx.recv().await {
            let mut db = db_daily.lock().await;
            let now_dt = sched_daily.local_time(chrono::Utc::now());
            let result = match kind {
                BaselineKind::Daily => sched_daily.save_daily_baselines(&mut db, now_dt).await,
                BaselineKind::Fallback => {
                    sched_daily
                        .save_missing_daily_baselines(&mut db, now_dt)
                        .await
                }
            };
            if result.is_err() {
                eprintln!("Не удалось сохранить эталоны расписания");
            }
            baseline_trigger.finished();
        }
    });

    // Очистку пока оставляем по прежнему Rust-интервалу до отдельной сверки с Python.
    let sched_cleanup = scheduler.clone();
    let db_cleanup = db_schedule.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(3600 * 6)).await;
            let db = db_cleanup.lock().await;
            let now_str = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();
            let _ = sched_cleanup.cleanup_old_records(&db, 30, &now_str);
        }
    });

    // 4. Веб-сервер axum (порт 8000 или из PORT)
    let port = std::env::var("PORT")
        .unwrap_or_else(|_| "8000".into())
        .parse::<u16>()
        .unwrap_or(8000);
    let app_state = AppState {
        db: db_mutex.clone(),
        settings: settings_arc.clone(),
    };
    let router = create_router(app_state);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    println!("Веб-сервер запущен на http://{addr}");

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, router)
        .with_graceful_shutdown(async {
            tokio::signal::ctrl_c().await.ok();
            println!("Получен сигнал завершения. Остановка сервиса...");
        })
        .await?;

    Ok(())
}

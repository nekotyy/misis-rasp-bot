use std::{collections::HashMap, sync::Arc};

use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use misis_rasp_core::{
    config::Settings,
    database,
    web_server::{create_router, AppState},
};
use tokio::sync::Mutex;
use tower::ServiceExt;

#[test]
fn executable_refuses_platform_tokens_before_opening_database() {
    for token_name in [
        "TELEGRAM_BOT_TOKEN",
        "VK_BOT_TOKEN",
        "VK_MINIAPP_SECRET",
        "VK_APP_SECRET",
        "VK_CLIENT_SECRET",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let database_path = dir.path().join("must-not-open.db");
        let result = std::process::Command::new(env!("CARGO_BIN_EXE_misis-rasp-core"))
            .current_dir(dir.path())
            .env_clear()
            .env("DATABASE_PATH", &database_path)
            .env(token_name, "synthetic-token")
            .output()
            .unwrap();
        assert!(!result.status.success(), "accepted {token_name}");
        assert!(!database_path.exists(), "opened DB for {token_name}");
    }
}

#[tokio::test]
async fn vk_miniapp_auth_uses_app_secret_instead_of_bot_token() {
    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
    use hmac::{Hmac, Mac};
    use sha2::Sha256;

    fn launch(secret: &str) -> String {
        let payload = "vk_app_id=1&vk_user_id=42";
        let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).unwrap();
        mac.update(payload.as_bytes());
        format!(
            "{payload}&sign={}",
            URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
        )
    }

    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    let env = HashMap::from([
        ("VK_BOT_TOKEN".into(), "bot-token".into()),
        ("VK_MINIAPP_SECRET".into(), "app-secret".into()),
        ("VK_APP_SECRET".into(), "fallback-secret".into()),
    ]);
    let router = create_router(AppState {
        db: Arc::new(Mutex::new(db)),
        settings: Arc::new(Settings::from_map(&env).unwrap()),
    });
    for (secret, expected) in [
        ("app-secret", StatusCode::OK),
        ("bot-token", StatusCode::UNAUTHORIZED),
        ("fallback-secret", StatusCode::UNAUTHORIZED),
    ] {
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/miniapp/v1/auth/vk")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        serde_json::json!({"launch_params": launch(secret)}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            response.status(),
            expected,
            "unexpected status for {secret}"
        );
    }
}

#[test]
fn cleanup_preserves_recent_records_and_rejects_invalid_dates() {
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    let now = chrono::NaiveDateTime::parse_from_str("2026-11-20T12:00:00", "%Y-%m-%dT%H:%M:%S")
        .unwrap()
        .and_utc()
        .timestamp() as f64;
    for (id, age, status) in [
        ("old", 100.0, "sent"),
        ("recent", 1.0, "sent"),
        ("within_retention", 8.0, "sent"),
        ("leased", 100.0, "sent"),
        ("pending", 100.0, "pending"),
    ] {
        let stamp = now - age * 86400.0;
        db.execute("INSERT INTO delivery_outbox (delivery_id,platform,peer_key,method,payload_json,status,created_at,updated_at,expires_at,next_attempt_at) VALUES (?,'telegram','1','sendMessage','{}',?,?,?,?,?)", rusqlite::params![id,status,stamp,stamp,stamp,stamp]).unwrap();
    }
    db.execute(
        "UPDATE delivery_outbox SET lease_until = ? WHERE delivery_id = 'leased'",
        [now + 60.0],
    )
    .unwrap();
    db.execute(
        "INSERT INTO telegram_processed_updates (update_id, processed_at) VALUES (1, ?), (2, ?)",
        [now - 100.0 * 86400.0, now - 86400.0],
    )
    .unwrap();
    assert!(database::cleanup_database(&db, 7, "invalid").is_err());
    assert!(database::cleanup_database(&db, -1, "2026-11-20T12:00:00").is_err());
    assert_eq!(
        database::cleanup_database(&db, 7, "2026-11-20T12:00:00").unwrap(),
        2
    );
    let remaining: i64 = db
        .query_row(
            "SELECT count(*) FROM delivery_outbox WHERE delivery_id IN ('recent', 'within_retention', 'leased', 'pending')",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(remaining, 4);
    let remaining_updates: i64 = db
        .query_row(
            "SELECT count(*) FROM telegram_processed_updates WHERE update_id = 2",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(remaining_updates, 1);
}

#[tokio::test]
async fn unprotected_admin_routes_are_disabled_but_health_remains_available() {
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    let settings = Settings::from_map(&HashMap::new()).unwrap();
    let router = create_router(AppState {
        db: Arc::new(Mutex::new(db)),
        settings: Arc::new(settings),
    });
    for (method, path) in [
        ("GET", "/"),
        ("GET", "/login"),
        ("POST", "/login"),
        ("POST", "/logout"),
        ("GET", "/api/metrics"),
        ("GET", "/control"),
        ("POST", "/control/broadcast"),
        ("POST", "/api/admin/schedule/import-json"),
    ] {
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method(method)
                    .uri(path)
                    .header("content-type", "application/json")
                    .body(Body::from("{\"text\":\"test\"}"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            response.status(),
            StatusCode::SERVICE_UNAVAILABLE,
            "{method} {path}"
        );
    }
    let response = router
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn fetched_telegram_batch_is_acknowledged_only_after_durable_handling() {
    use misis_rasp_core::{
        group_catalog::GroupCatalog,
        notification::ApiTransport,
        search::SearchCatalog,
        telegram_engine::{process_poll_updates, EngineContext, PollOutcome, TelegramEngine},
        telegram_poll::TelegramLongPoll,
    };
    use serde_json::json;

    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    let settings = Settings::from_map(&HashMap::new()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::default();
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let api = ApiTransport::new(
        "http://127.0.0.1:1",
        "http://127.0.0.1:1",
        "test".into(),
        "".into(),
        "5.131",
    )
    .unwrap();
    let mut engine = TelegramEngine::default();
    let mut poll = TelegramLongPoll::new(25);
    let updates = vec![
        json!({"update_id":20,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/start"}}),
        json!({"update_id":21,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/unknown"}}),
    ];
    let result = process_poll_updates(
        &mut poll,
        &api,
        &mut engine,
        &mut db,
        &context,
        None,
        updates,
    )
    .await;
    assert!(matches!(
        result,
        Ok(PollOutcome::Unsupported {
            processed: 1,
            update_id: Some(21)
        })
    ));
    assert_eq!(poll.next_offset(), Some(21));
    assert!(misis_rasp_core::outbox::get(&db, "telegram-update:20:0")
        .unwrap()
        .is_some());
}

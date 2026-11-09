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

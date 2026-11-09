use axum::{
    extract::{Path, State},
    http::{HeaderMap, Request, StatusCode},
    middleware::{self, Next},
    response::{Html, IntoResponse, Response},
    routing::{get, patch, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::sync::Mutex;
use tower_http::cors::{Any, CorsLayer};

use crate::account::{
    add_subscription, get_or_create_account, remove_subscription, update_preferences,
    update_subscription, AccountSubscription, Preferences,
};
use crate::config::Settings;
use crate::miniapp_auth::{
    authenticate_session, create_session, revoke_session, verify_telegram_init_data,
    verify_vk_launch_params,
};
use crate::web_security::verify_password;

#[derive(Clone)]
pub struct AppState {
    pub db: Arc<Mutex<rusqlite::Connection>>,
    pub settings: Arc<Settings>,
}

// The Rust admin does not yet implement the Python admin's authentication,
// session, CSRF and audit protections. Keep every admin handler inaccessible
// until those protections are implemented and verified together.
async fn admin_unavailable(_request: Request<axum::body::Body>, _next: Next) -> StatusCode {
    StatusCode::SERVICE_UNAVAILABLE
}

pub fn create_router(state: AppState) -> Router {
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    let admin = Router::new()
        .route("/", get(admin_index))
        .route("/login", get(admin_login_page).post(admin_login_post))
        .route("/logout", post(admin_logout_post))
        .route("/api/metrics", get(admin_metrics))
        .route("/control", get(admin_control_page))
        .route("/control/broadcast", post(admin_control_broadcast))
        .route(
            "/api/admin/schedule/import-json",
            post(admin_import_schedule_json),
        )
        .route_layer(middleware::from_fn(admin_unavailable));

    Router::new()
        // Health check
        .route("/health", get(health_check))
        .route("/api/health", get(health_check))
        // Mini App Auth
        .route("/api/miniapp/v1/auth/telegram", post(auth_telegram))
        .route("/api/miniapp/v1/auth/vk", post(auth_vk))
        .route("/api/miniapp/v1/auth/logout", post(auth_logout))
        // Mini App Profile & Subscriptions
        .route("/api/miniapp/v1/me", get(get_profile))
        .route("/api/miniapp/v1/me/preferences", patch(patch_preferences))
        .route("/api/miniapp/v1/groups", get(list_groups))
        .route("/api/miniapp/v1/me/subscriptions", post(post_subscription))
        .route(
            "/api/miniapp/v1/me/subscriptions/:sub_id",
            patch(patch_subscription).delete(del_subscription),
        )
        // Mini App Schedule & History
        .route("/api/miniapp/v1/schedule", get(get_schedule))
        .route("/api/miniapp/v1/history", get(get_history))
        // Mini App Telemetry
        .route("/api/miniapp/v1/telemetry/session", post(telemetry_session))
        .route("/api/miniapp/v1/telemetry/ping", post(telemetry_ping))
        .route("/api/miniapp/v1/telemetry/event", post(telemetry_event))
        .merge(admin)
        .layer(cors)
        .with_state(state)
}

// ----------------- Handlers -----------------

async fn health_check() -> impl IntoResponse {
    Json(json!({"status": "ok", "version": "1.0.0"}))
}

#[derive(Deserialize)]
pub struct AuthTelegramPayload {
    pub init_data: String,
}

async fn auth_telegram(
    State(state): State<AppState>,
    Json(payload): Json<AuthTelegramPayload>,
) -> Response {
    let now = chrono::Utc::now();
    let verified = verify_telegram_init_data(
        &payload.init_data,
        &state.settings.telegram_bot_token,
        86400,
        now.timestamp(),
    );

    let Some(user) = verified else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "Invalid initData"})),
        )
            .into_response();
    };

    let mut db = state.db.lock().await;
    let now_str = now.format("%Y-%m-%dT%H:%M:%S").to_string();

    let account_id = match db.transaction() {
        Ok(tx) => {
            let id = get_or_create_account(
                &tx,
                "telegram",
                user.user_id,
                user.username.as_deref(),
                user.first_name.as_deref(),
                &now_str,
            );
            match id {
                Ok(account_id) if tx.commit().is_ok() => account_id,
                _ => return (StatusCode::INTERNAL_SERVER_ERROR, "Database error").into_response(),
            }
        }
        Err(_) => return (StatusCode::INTERNAL_SERVER_ERROR, "Database error").into_response(),
    };

    let token = match create_session(&db, &account_id, "telegram", user.user_id, 86400 * 30, now) {
        Ok(t) => t,
        Err(_) => {
            return (StatusCode::INTERNAL_SERVER_ERROR, "Session creation failed").into_response()
        }
    };

    Json(json!({
        "token": token,
        "account_id": account_id,
        "platform": "telegram",
        "user_id": user.user_id,
    }))
    .into_response()
}

#[derive(Deserialize)]
pub struct AuthVkPayload {
    pub launch_params: String,
}

async fn auth_vk(State(state): State<AppState>, Json(payload): Json<AuthVkPayload>) -> Response {
    let verified =
        verify_vk_launch_params(&payload.launch_params, &state.settings.vk_miniapp_secret);

    let Some(user) = verified else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "Invalid launch_params"})),
        )
            .into_response();
    };

    let now = chrono::Utc::now();
    let mut db = state.db.lock().await;
    let now_str = now.format("%Y-%m-%dT%H:%M:%S").to_string();

    let account_id = match db.transaction() {
        Ok(tx) => {
            let id = get_or_create_account(&tx, "vk", user.user_id, None, None, &now_str);
            match id {
                Ok(account_id) if tx.commit().is_ok() => account_id,
                _ => return (StatusCode::INTERNAL_SERVER_ERROR, "Database error").into_response(),
            }
        }
        Err(_) => return (StatusCode::INTERNAL_SERVER_ERROR, "Database error").into_response(),
    };

    let token = match create_session(&db, &account_id, "vk", user.user_id, 86400 * 30, now) {
        Ok(t) => t,
        Err(_) => {
            return (StatusCode::INTERNAL_SERVER_ERROR, "Session creation failed").into_response()
        }
    };

    Json(json!({
        "token": token,
        "account_id": account_id,
        "platform": "vk",
        "user_id": user.user_id,
    }))
    .into_response()
}

fn extract_token(headers: &HeaderMap) -> Option<&str> {
    headers
        .get("Authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|h| {
            h.strip_prefix("Bearer ")
                .or_else(|| h.strip_prefix("bearer "))
        })
        .map(str::trim)
}

async fn auth_logout(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(token) = extract_token(&headers) else {
        return (StatusCode::UNAUTHORIZED, "Missing token").into_response();
    };

    let db = state.db.lock().await;
    let now_str = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();
    let _ = revoke_session(&db, token, &now_str);

    Json(json!({"status": "ok"})).into_response()
}

async fn get_profile(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(token) = extract_token(&headers) else {
        return (StatusCode::UNAUTHORIZED, "Missing token").into_response();
    };

    let db = state.db.lock().await;
    let now_str = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();

    let auth = match authenticate_session(&db, token, &now_str) {
        Ok(Some(a)) => a,
        _ => return (StatusCode::UNAUTHORIZED, "Invalid session").into_response(),
    };

    let (account_id, platform, user_id) = auth;

    // Считываем настройки аккаунта
    let account_row = db.query_row(
        "SELECT theme, accent, density, reduced_motion, notifications_enabled FROM accounts WHERE id = ?",
        [&account_id],
        |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, bool>(3)?,
                r.get::<_, bool>(4)?,
            ))
        },
    );

    let (theme, accent, density, reduced_motion, notifications_enabled) = match account_row {
        Ok(t) => t,
        Err(_) => return (StatusCode::NOT_FOUND, "Account not found").into_response(),
    };

    // Считываем подписки
    let mut stmt = match db.prepare(
        "SELECT id, subscription_type, subscription_key, subscription_title, subscription_url, group_name, schedule_id, university_subgroup, is_primary, notifications_enabled FROM account_subscriptions WHERE account_id = ? ORDER BY id ASC",
    ) {
        Ok(s) => s,
        Err(_) => return (StatusCode::INTERNAL_SERVER_ERROR, "Database error").into_response(),
    };

    let sub_rows = stmt.query_map([&account_id], |r| {
        Ok(json!({
            "id": r.get::<_, i64>(0)?,
            "subscription_type": r.get::<_, String>(1)?,
            "subscription_key": r.get::<_, String>(2)?,
            "subscription_title": r.get::<_, Option<String>>(3)?,
            "subscription_url": r.get::<_, Option<String>>(4)?,
            "group_name": r.get::<_, Option<String>>(5)?,
            "schedule_id": r.get::<_, Option<i64>>(6)?,
            "university_subgroup": r.get::<_, i64>(7)?,
            "is_primary": r.get::<_, bool>(8)?,
            "notifications_enabled": r.get::<_, bool>(9)?,
        }))
    });

    let subscriptions: Vec<Value> = sub_rows
        .ok()
        .map(|rows| rows.filter_map(Result::ok).collect())
        .unwrap_or_default();

    Json(json!({
        "account": {
            "id": account_id,
            "theme": theme,
            "accent": accent,
            "density": density,
            "reduced_motion": reduced_motion,
            "notifications_enabled": notifications_enabled,
        },
        "identity": {
            "platform": platform,
            "user_id": user_id,
        },
        "subscriptions": subscriptions,
    }))
    .into_response()
}

#[derive(Deserialize)]
pub struct PatchPreferencesPayload {
    pub theme: Option<String>,
    pub accent: Option<String>,
    pub density: Option<String>,
    pub reduced_motion: Option<bool>,
    pub notifications_enabled: Option<bool>,
}

async fn patch_preferences(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<PatchPreferencesPayload>,
) -> Response {
    let Some(token) = extract_token(&headers) else {
        return (StatusCode::UNAUTHORIZED, "Missing token").into_response();
    };

    let mut db = state.db.lock().await;
    let now_str = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();

    let auth = match authenticate_session(&db, token, &now_str) {
        Ok(Some(a)) => a,
        _ => return (StatusCode::UNAUTHORIZED, "Invalid session").into_response(),
    };

    let (account_id, _, _) = auth;

    let prefs = Preferences {
        theme: payload.theme.as_deref(),
        accent: payload.accent.as_deref(),
        density: payload.density.as_deref(),
        reduced_motion: payload.reduced_motion,
        notifications_enabled: payload.notifications_enabled,
    };

    let updated = match db.transaction() {
        Ok(tx) => {
            let res = update_preferences(&tx, &account_id, &prefs, &now_str);
            res.is_ok() && tx.commit().is_ok()
        }
        Err(_) => false,
    };

    if !updated {
        return (StatusCode::BAD_REQUEST, "Update preferences failed").into_response();
    }

    Json(json!({"status": "ok"})).into_response()
}

async fn list_groups(State(state): State<AppState>) -> Response {
    let db = state.db.lock().await;
    let mut stmt = match db
        .prepare("SELECT group_name, schedule_id FROM group_catalog ORDER BY group_name ASC")
    {
        Ok(s) => s,
        Err(_) => return (StatusCode::INTERNAL_SERVER_ERROR, "Database error").into_response(),
    };

    let rows = stmt.query_map([], |r| {
        Ok(json!({
            "group_name": r.get::<_, String>(0)?,
            "schedule_id": r.get::<_, Option<i64>>(1)?,
        }))
    });

    let groups: Vec<Value> = rows
        .ok()
        .map(|rows| rows.filter_map(Result::ok).collect())
        .unwrap_or_default();
    Json(groups).into_response()
}

#[derive(Deserialize)]
pub struct AddSubscriptionPayload {
    pub subscription_type: String,
    pub subscription_key: String,
    pub subscription_title: Option<String>,
    pub subscription_url: Option<String>,
    pub group_name: Option<String>,
    pub schedule_id: Option<i64>,
    pub university_subgroup: Option<i64>,
    pub is_primary: Option<bool>,
    pub notifications_enabled: Option<bool>,
}

async fn post_subscription(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<AddSubscriptionPayload>,
) -> Response {
    let Some(token) = extract_token(&headers) else {
        return (StatusCode::UNAUTHORIZED, "Missing token").into_response();
    };

    let mut db = state.db.lock().await;
    let now_str = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();

    let auth = match authenticate_session(&db, token, &now_str) {
        Ok(Some(a)) => a,
        _ => return (StatusCode::UNAUTHORIZED, "Invalid session").into_response(),
    };

    let (account_id, _, _) = auth;

    let sub = AccountSubscription {
        subscription_type: &payload.subscription_type,
        key: &payload.subscription_key,
        title: payload.subscription_title.as_deref(),
        url: payload.subscription_url.as_deref(),
        group_name: payload.group_name.as_deref(),
        schedule_id: payload.schedule_id,
        subgroup: payload.university_subgroup.unwrap_or(0),
        primary: payload.is_primary.unwrap_or(false),
        notifications: payload.notifications_enabled.unwrap_or(true),
    };

    let sub_id = match db.transaction() {
        Ok(tx) => {
            let res = add_subscription(&tx, &account_id, &sub, &now_str);
            if let Ok(id) = res {
                if tx.commit().is_ok() {
                    Some(id)
                } else {
                    None
                }
            } else {
                None
            }
        }
        Err(_) => None,
    };

    match sub_id {
        Some(id) => Json(json!({"id": id, "status": "created"})).into_response(),
        None => (StatusCode::BAD_REQUEST, "Failed to add subscription").into_response(),
    }
}

#[derive(Deserialize)]
pub struct PatchSubscriptionPayload {
    pub is_primary: Option<bool>,
    pub university_subgroup: Option<i64>,
    pub notifications_enabled: Option<bool>,
}

async fn patch_subscription(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(sub_id): Path<i64>,
    Json(payload): Json<PatchSubscriptionPayload>,
) -> Response {
    let Some(token) = extract_token(&headers) else {
        return (StatusCode::UNAUTHORIZED, "Missing token").into_response();
    };

    let mut db = state.db.lock().await;
    let now_str = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();

    let auth = match authenticate_session(&db, token, &now_str) {
        Ok(Some(a)) => a,
        _ => return (StatusCode::UNAUTHORIZED, "Invalid session").into_response(),
    };

    let (account_id, _, _) = auth;

    let updated = match db.transaction() {
        Ok(tx) => {
            let res = update_subscription(
                &tx,
                &account_id,
                sub_id,
                payload.is_primary,
                payload.university_subgroup,
                payload.notifications_enabled,
                &now_str,
            );
            res.unwrap_or(false) && tx.commit().is_ok()
        }
        Err(_) => false,
    };

    if updated {
        Json(json!({"status": "updated"})).into_response()
    } else {
        (StatusCode::NOT_FOUND, "Subscription not found").into_response()
    }
}

async fn del_subscription(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(sub_id): Path<i64>,
) -> Response {
    let Some(token) = extract_token(&headers) else {
        return (StatusCode::UNAUTHORIZED, "Missing token").into_response();
    };

    let mut db = state.db.lock().await;
    let now_str = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();

    let auth = match authenticate_session(&db, token, &now_str) {
        Ok(Some(a)) => a,
        _ => return (StatusCode::UNAUTHORIZED, "Invalid session").into_response(),
    };

    let (account_id, _, _) = auth;

    let removed = match db.transaction() {
        Ok(tx) => {
            let res = remove_subscription(&tx, &account_id, sub_id);
            res.unwrap_or(false) && tx.commit().is_ok()
        }
        Err(_) => false,
    };

    if removed {
        Json(json!({"status": "deleted"})).into_response()
    } else {
        (StatusCode::NOT_FOUND, "Subscription not found").into_response()
    }
}

async fn get_schedule(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(token) = extract_token(&headers) else {
        return (StatusCode::UNAUTHORIZED, "Missing token").into_response();
    };

    let db = state.db.lock().await;
    let now_str = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();

    let auth = match authenticate_session(&db, token, &now_str) {
        Ok(Some(a)) => a,
        _ => return (StatusCode::UNAUTHORIZED, "Invalid session").into_response(),
    };

    let (account_id, _, _) = auth;

    // Находим основную подписку
    let primary: Option<(Option<i64>, Option<String>)> = db
        .query_row(
            "SELECT schedule_id, subscription_key FROM account_subscriptions WHERE account_id = ? AND is_primary = 1 LIMIT 1",
            [&account_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .ok();

    let (schedule_id, subscription_key) = match primary {
        Some((sid, skey)) => (sid, skey),
        None => (None, None),
    };

    let snapshot =
        crate::database::latest_snapshot(&db, "current", schedule_id, subscription_key.as_deref());

    match snapshot {
        Ok(Some(stored)) => Json(json!({
            "group_name": stored.group_name,
            "fetched_at": stored.fetched_at,
            "days": stored.content.days,
        }))
        .into_response(),
        _ => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "Schedule not found"})),
        )
            .into_response(),
    }
}

async fn get_history(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(token) = extract_token(&headers) else {
        return (StatusCode::UNAUTHORIZED, "Missing token").into_response();
    };

    let db = state.db.lock().await;
    let now_str = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();

    let auth = match authenticate_session(&db, token, &now_str) {
        Ok(Some(a)) => a,
        _ => return (StatusCode::UNAUTHORIZED, "Invalid session").into_response(),
    };

    let (account_id, _, _) = auth;

    let sub_key: Option<String> = db
        .query_row(
            "SELECT subscription_key FROM account_subscriptions WHERE account_id = ? AND is_primary = 1 LIMIT 1",
            [&account_id],
            |r| r.get(0),
        )
        .ok();

    let Some(key) = sub_key else {
        return Json(json!({"events": []})).into_response();
    };

    let mut stmt = match db.prepare(
        "SELECT id, message, changed_dates_json, created_at FROM change_events WHERE source_key = ? ORDER BY id DESC LIMIT 50",
    ) {
        Ok(s) => s,
        Err(_) => return (StatusCode::INTERNAL_SERVER_ERROR, "Database error").into_response(),
    };

    let rows = stmt.query_map([&key], |r| {
        Ok(json!({
            "id": r.get::<_, i64>(0)?,
            "message": r.get::<_, String>(1)?,
            "changed_dates": serde_json::from_str::<Value>(&r.get::<_, String>(2)?).unwrap_or(json!([])),
            "created_at": r.get::<_, String>(3)?,
        }))
    });

    let events: Vec<Value> = rows
        .ok()
        .map(|rows| rows.filter_map(Result::ok).collect())
        .unwrap_or_default();
    Json(json!({"events": events})).into_response()
}

// Telemetry handlers
async fn telemetry_session() -> impl IntoResponse {
    Json(json!({"status": "recorded"}))
}

async fn telemetry_ping() -> impl IntoResponse {
    Json(json!({"status": "pong"}))
}

async fn telemetry_event() -> impl IntoResponse {
    Json(json!({"status": "logged"}))
}

// ----------------- Web Admin Handlers -----------------

async fn admin_index() -> impl IntoResponse {
    Html("<!DOCTYPE html><html><head><title>MISIS Bot Admin</title></head><body><h1>Панель управления MISIS Rasp Bot</h1><p><a href='/login'>Вход</a> | <a href='/control'>Управление</a> | <a href='/api/metrics'>Метрики</a></p></body></html>")
}

async fn admin_login_page() -> impl IntoResponse {
    Html("<!DOCTYPE html><html><head><title>Вход в панель</title></head><body><h2>Авторизация</h2><form method='post' action='/login'><label>Логин: <input name='username'></label><br><label>Пароль: <input type='password' name='password'></label><br><button type='submit'>Войти</button></form></body></html>")
}

#[derive(Deserialize)]
pub struct AdminLoginPayload {
    pub username: String,
    pub password: String,
}

async fn admin_login_post(
    State(state): State<AppState>,
    Json(payload): Json<AdminLoginPayload>,
) -> Response {
    let db = state.db.lock().await;
    let password_hash: Option<String> = db
        .query_row(
            "SELECT password_hash FROM web_users WHERE login = ?",
            [&payload.username],
            |r| r.get(0),
        )
        .ok();

    let Some(hash) = password_hash else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "Invalid username or password"})),
        )
            .into_response();
    };

    if !verify_password(&payload.password, &hash) {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "Invalid username or password"})),
        )
            .into_response();
    }

    // Успешный вход
    Json(json!({"status": "ok", "user": payload.username})).into_response()
}

async fn admin_logout_post() -> impl IntoResponse {
    Json(json!({"status": "logged_out"}))
}

async fn admin_metrics(State(state): State<AppState>) -> Response {
    let db = state.db.lock().await;
    let stats = crate::database::collect_admin_status(&db);
    match stats {
        Ok(s) => Json(json!({
            "total_users": s.total_users,
            "telegram_users": s.telegram_users,
            "vk_users": s.vk_users,
            "outbox_pending": s.outbox_pending,
            "outbox_sent": s.outbox_sent,
            "outbox_failed": s.outbox_failed,
        }))
        .into_response(),
        Err(_) => (StatusCode::INTERNAL_SERVER_ERROR, "Database error").into_response(),
    }
}

async fn admin_control_page() -> impl IntoResponse {
    Html("<!DOCTYPE html><html><head><title>Управление ботом</title></head><body><h2>Управление ботом</h2><form method='post' action='/control/broadcast'><textarea name='text' placeholder='Текст рассылки'></textarea><button type='submit'>Разослать</button></form></body></html>")
}

#[derive(Deserialize)]
pub struct AdminBroadcastPayload {
    pub text: String,
}

async fn admin_control_broadcast(
    State(state): State<AppState>,
    Json(payload): Json<AdminBroadcastPayload>,
) -> Response {
    if payload.text.trim().is_empty() {
        return (StatusCode::BAD_REQUEST, "Message text is empty").into_response();
    }

    let mut db = state.db.lock().await;
    let now = chrono::Utc::now().timestamp() as f64;
    let event_id = format!("broadcast:{}", chrono::Utc::now().timestamp());

    let broadcast = crate::notification::Broadcast {
        event_id: &event_id,
        plain: &payload.text,
        telegram: None,
        vk: None,
        campaign: "admin_broadcast",
        target_platform: "all",
        audience: "all",
        schedule_id: None,
        subscription_key: None,
        subgroup: None,
    };

    let queued = crate::notification::enqueue_broadcast(&mut db, &broadcast, now);
    match queued {
        Ok(count) => Json(json!({"status": "queued", "count": count})).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, format!("Failed: {e}")).into_response(),
    }
}

#[derive(Deserialize)]
pub struct ImportScheduleJsonPayload {
    pub group_name: String,
    pub schedule_id: Option<i64>,
    pub source_type: Option<String>,
    pub source_key: Option<String>,
    pub source_url: Option<String>,
    pub days: Vec<crate::DaySchedule>,
    pub notify: Option<bool>,
}

async fn admin_import_schedule_json(
    State(state): State<AppState>,
    Json(payload): Json<ImportScheduleJsonPayload>,
) -> Response {
    if payload.group_name.trim().is_empty() {
        return (StatusCode::BAD_REQUEST, "group_name is required").into_response();
    }

    let now_dt = chrono::Local::now().naive_local();
    let now_ts = chrono::Utc::now().timestamp() as f64;

    let snapshot = crate::ScheduleSnapshot {
        group_name: payload.group_name.clone(),
        fetched_at: now_dt.format("%Y-%m-%dT%H:%M:%S").to_string(),
        days: payload.days,
    };

    let hash = crate::parser::compute_snapshot_hash(&snapshot);
    let source_type = payload.source_type.unwrap_or_else(|| "group".into());
    let source_key = payload.source_key.unwrap_or_else(|| {
        if let Some(id) = payload.schedule_id {
            format!("group:{id}")
        } else {
            format!("group:{}", payload.group_name)
        }
    });
    let source_title = payload.group_name.clone();
    let source_url = payload.source_url.unwrap_or_default();

    let sync_source = crate::sync::Source {
        source_type: &source_type,
        source_key: &source_key,
        source_title: &source_title,
        source_url: &source_url,
        group_name: Some(&payload.group_name),
        schedule_id: payload.schedule_id,
    };

    let options = crate::sync::ApplyOptions {
        notify: payload.notify.unwrap_or(true),
        trust_empty: true,
        manual_origin: "manual_json_import",
        observed_dates: None,
        timezone: chrono_tz::Europe::Moscow,
        now: now_dt,
    };

    let platforms = crate::sync::DeliveryPlatforms {
        telegram: !state.settings.telegram_bot_token.is_empty(),
        vk: !state.settings.vk_bot_token.is_empty(),
    };

    let mut db = state.db.lock().await;
    match crate::sync::apply_and_enqueue(
        &mut db,
        &sync_source,
        &snapshot,
        &hash,
        &options,
        &platforms,
        now_ts,
    ) {
        Ok(outcome) => {
            let result_str = match outcome {
                crate::sync::AtomicApplyResult::RejectedEmpty => "rejected_empty",
                crate::sync::AtomicApplyResult::FirstBaseline => "first_baseline",
                crate::sync::AtomicApplyResult::Unchanged => "unchanged",
                crate::sync::AtomicApplyResult::Changed { queued, .. } => {
                    return Json(json!({
                        "status": "changed",
                        "hash": hash,
                        "queued_notifications": queued,
                    }))
                    .into_response();
                }
            };
            Json(json!({"status": result_str, "hash": hash})).into_response()
        }
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("Sync error: {e}"),
        )
            .into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::Request;
    use tower::util::ServiceExt;

    fn test_app_state() -> AppState {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();

        let mut env = std::collections::HashMap::new();
        env.insert("TELEGRAM_BOT_TOKEN".into(), "123:token".into());
        let settings = Settings::from_map(&env).unwrap();

        AppState {
            db: Arc::new(Mutex::new(db)),
            settings: Arc::new(settings),
        }
    }

    #[tokio::test]
    async fn health_endpoint_returns_ok() {
        let state = test_app_state();
        let app = create_router(state);

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/health")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn unauthorized_access_to_profile_is_rejected() {
        let state = test_app_state();
        let app = create_router(state);

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/miniapp/v1/me")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn authenticated_profile_and_preferences_flow() {
        let state = test_app_state();
        let now = chrono::Utc::now();
        let now_str = now.format("%Y-%m-%dT%H:%M:%S").to_string();

        let token = {
            let mut db = state.db.lock().await;
            let tx = db.transaction().unwrap();
            let account_id = get_or_create_account(
                &tx,
                "telegram",
                123,
                Some("tg_user"),
                Some("Test User"),
                &now_str,
            )
            .unwrap();
            tx.commit().unwrap();
            create_session(&db, &account_id, "telegram", 123, 3600, now).unwrap()
        };

        let app = create_router(state.clone());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/miniapp/v1/me")
                    .header("Authorization", format!("Bearer {token}"))
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);

        // Обновим тему на dark
        let app2 = create_router(state.clone());
        let payload = json!({"theme": "dark", "density": "compact"});
        let response2 = app2
            .oneshot(
                Request::builder()
                    .method("PATCH")
                    .uri("/api/miniapp/v1/me/preferences")
                    .header("Authorization", format!("Bearer {token}"))
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response2.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn admin_login_is_disabled_even_with_valid_credentials() {
        let state = test_app_state();
        {
            let db = state.db.lock().await;
            let hash = crate::web_security::hash_password("admin_secret").unwrap();
            db.execute(
                "INSERT INTO web_users (login, password_hash, permissions_json, created_at, updated_at) VALUES ('admin', ?, '[\"manage_web_users\"]', 100, 100)",
                [&hash],
            )
            .unwrap();
        }

        let app = create_router(state.clone());
        let payload = json!({"username": "admin", "password": "admin_secret"});
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/login")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);

        // Неверный пароль
        let app2 = create_router(state.clone());
        let bad_payload = json!({"username": "admin", "password": "wrong_password"});
        let response2 = app2
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/login")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(bad_payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response2.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn admin_import_is_disabled_without_session_protection() {
        let state = test_app_state();
        let app = create_router(state.clone());

        let payload = json!({
            "group_name": "ИСП-25-1",
            "schedule_id": 100,
            "days": [
                {
                    "date_iso": "2026-11-10",
                    "date_label": "10 ноября",
                    "lessons": [
                        {
                            "number": 1,
                            "subject": "Математика",
                            "teacher": "Сидоров",
                            "classroom": "303"
                        }
                    ]
                }
            ],
            "notify": false
        });

        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/admin/schedule/import-json")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    }
}

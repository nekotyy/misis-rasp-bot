use base64::{engine::general_purpose::STANDARD, Engine};
use hmac::{Hmac, Mac};
use rusqlite::OptionalExtension;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use subtle::ConstantTimeEq;

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelegramAuthUser {
    pub user_id: i64,
    pub username: Option<String>,
    pub first_name: Option<String>,
    pub last_name: Option<String>,
    pub auth_date: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VkAuthUser {
    pub user_id: i64,
    pub app_id: i64,
}

/// Вычисляет SHA256-хеш токена сессии
pub fn hash_session_token(token: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// Проверяет подпись initData Telegram WebApp
pub fn verify_telegram_init_data(
    init_data_raw: &str,
    bot_token: &str,
    max_age_seconds: i64,
    now_ts: i64,
) -> Option<TelegramAuthUser> {
    if init_data_raw.is_empty() || bot_token.is_empty() {
        return None;
    }

    let pairs = url::form_urlencoded::parse(init_data_raw.as_bytes());
    let mut map: BTreeMap<String, String> = BTreeMap::new();
    let mut received_hash: Option<String> = None;

    for (k, v) in pairs {
        if k == "hash" {
            received_hash = Some(v.into_owned());
        } else {
            map.insert(k.into_owned(), v.into_owned());
        }
    }

    let received_hash = received_hash?;

    // data_check_string = key1=val1\nkey2=val2...
    let check_string = map
        .iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join("\n");

    // secret_key = HMAC_SHA256("WebAppData", bot_token)
    let mut secret_mac = HmacSha256::new_from_slice(b"WebAppData").ok()?;
    secret_mac.update(bot_token.as_bytes());
    let secret_key = secret_mac.finalize().into_bytes();

    // expected_hash = HMAC_SHA256(secret_key, check_string)
    let mut hash_mac = HmacSha256::new_from_slice(&secret_key).ok()?;
    hash_mac.update(check_string.as_bytes());
    let expected_hash = format!("{:x}", hash_mac.finalize().into_bytes());

    if !bool::from(received_hash.as_bytes().ct_eq(expected_hash.as_bytes())) {
        return None;
    }

    let auth_date: i64 = map.get("auth_date").and_then(|s| s.parse().ok())?;
    if max_age_seconds > 0 && (now_ts - auth_date) > max_age_seconds {
        return None;
    }

    let user_str = map.get("user")?;
    let user_val: serde_json::Value = serde_json::from_str(user_str).ok()?;
    let user_id = user_val.get("id").and_then(|v| v.as_i64())?;

    Some(TelegramAuthUser {
        user_id,
        username: user_val
            .get("username")
            .and_then(|v| v.as_str())
            .map(str::to_string),
        first_name: user_val
            .get("first_name")
            .and_then(|v| v.as_str())
            .map(str::to_string),
        last_name: user_val
            .get("last_name")
            .and_then(|v| v.as_str())
            .map(str::to_string),
        auth_date,
    })
}

/// Проверяет подпись параметров запуска VK Mini Apps
pub fn verify_vk_launch_params(launch_params_raw: &str, client_secret: &str) -> Option<VkAuthUser> {
    if launch_params_raw.is_empty() || client_secret.is_empty() {
        return None;
    }

    let clean = launch_params_raw.trim().trim_start_matches(['?', '#']);
    let pairs = url::form_urlencoded::parse(clean.as_bytes());

    let mut vk_params: BTreeMap<String, String> = BTreeMap::new();
    let mut received_sign: Option<String> = None;

    for (k, v) in pairs {
        if k == "sign" {
            received_sign = Some(v.into_owned());
        } else if k.starts_with("vk_") {
            vk_params.insert(k.into_owned(), v.into_owned());
        }
    }

    let received_sign = received_sign?;
    if vk_params.is_empty() {
        return None;
    }

    // Формируем query_string из отсортированных vk_ параметров
    let mut serializer = url::form_urlencoded::Serializer::new(String::new());
    for (k, v) in &vk_params {
        serializer.append_pair(k, v);
    }
    let query_string = serializer.finish();

    let mut mac = HmacSha256::new_from_slice(client_secret.as_bytes()).ok()?;
    mac.update(query_string.as_bytes());
    let hash_bytes = mac.finalize().into_bytes();

    let expected_sign = STANDARD
        .encode(hash_bytes)
        .trim_end_matches('=')
        .replace('+', "-")
        .replace('/', "_");

    let received_clean = received_sign
        .trim_end_matches('=')
        .replace('+', "-")
        .replace('/', "_");

    if !bool::from(received_clean.as_bytes().ct_eq(expected_sign.as_bytes())) {
        return None;
    }

    let user_id = vk_params.get("vk_user_id").and_then(|s| s.parse().ok())?;
    let app_id = vk_params
        .get("vk_app_id")
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);

    Some(VkAuthUser { user_id, app_id })
}

/// Создает сессию пользователя и возвращает секретный сырой токен
pub fn create_session(
    conn: &rusqlite::Connection,
    account_id: &str,
    platform: &str,
    user_id: i64,
    ttl_seconds: i64,
    now: chrono::DateTime<chrono::Utc>,
) -> rusqlite::Result<String> {
    let mut rand_bytes = [0u8; 32];
    let _ = getrandom::getrandom(&mut rand_bytes);
    let raw_token = STANDARD.encode(rand_bytes).replace(['+', '/', '='], "");

    let token_hash = hash_session_token(&raw_token);
    let expires = now + chrono::Duration::seconds(ttl_seconds);
    let now_str = now.format("%Y-%m-%dT%H:%M:%S").to_string();
    let expires_str = expires.format("%Y-%m-%dT%H:%M:%S").to_string();

    conn.execute(
        "INSERT INTO miniapp_sessions (token_hash, account_id, platform, user_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        rusqlite::params![token_hash, account_id, platform, user_id, expires_str, now_str],
    )?;

    Ok(raw_token)
}

/// Проверяет и аутентифицирует сессию по сырому токену
pub fn authenticate_session(
    conn: &rusqlite::Connection,
    raw_token: &str,
    now_str: &str,
) -> rusqlite::Result<Option<(String, String, i64)>> {
    if raw_token.is_empty() {
        return Ok(None);
    }
    let token_hash = hash_session_token(raw_token);
    let row: Option<(String, String, i64)> = conn
        .query_row(
            "SELECT account_id, platform, user_id FROM miniapp_sessions WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?",
            rusqlite::params![token_hash, now_str],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    Ok(row)
}

/// Отзывает активную сессию
pub fn revoke_session(
    conn: &rusqlite::Connection,
    raw_token: &str,
    now_str: &str,
) -> rusqlite::Result<bool> {
    if raw_token.is_empty() {
        return Ok(false);
    }
    let token_hash = hash_session_token(raw_token);
    let changed = conn.execute(
        "UPDATE miniapp_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
        rusqlite::params![now_str, token_hash],
    )?;
    Ok(changed > 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verify_telegram_init_data_validates_signature() {
        let bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11";
        // Генерируем тестовую валидную строку init_data
        let auth_date = 1700000000;
        let user_json = r#"{"id":42,"first_name":"Иван","username":"ivan"}"#;
        let mut map: BTreeMap<String, String> = BTreeMap::new();
        map.insert("auth_date".into(), auth_date.to_string());
        map.insert("user".into(), user_json.into());

        let check_string = format!("auth_date={auth_date}\nuser={user_json}");
        let mut secret_mac = HmacSha256::new_from_slice(b"WebAppData").unwrap();
        secret_mac.update(bot_token.as_bytes());
        let secret_key = secret_mac.finalize().into_bytes();

        let mut hash_mac = HmacSha256::new_from_slice(&secret_key).unwrap();
        hash_mac.update(check_string.as_bytes());
        let hash = format!("{:x}", hash_mac.finalize().into_bytes());

        let mut serializer = url::form_urlencoded::Serializer::new(String::new());
        serializer.append_pair("auth_date", &auth_date.to_string());
        serializer.append_pair("user", user_json);
        serializer.append_pair("hash", &hash);
        let raw = serializer.finish();

        let user = verify_telegram_init_data(&raw, bot_token, 86400, auth_date + 100).unwrap();
        assert_eq!(user.user_id, 42);
        assert_eq!(user.username.as_deref(), Some("ivan"));
        assert_eq!(user.first_name.as_deref(), Some("Иван"));

        // Неверный токен бота
        assert!(verify_telegram_init_data(&raw, "wrong_token", 86400, auth_date + 100).is_none());

        // Истекшее время (max_age)
        assert!(verify_telegram_init_data(&raw, bot_token, 30, auth_date + 100).is_none());
    }

    #[test]
    fn session_lifecycle_roundtrip() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();

        // Создаем аккаунт
        let now = chrono::Utc::now();
        let now_str = now.format("%Y-%m-%dT%H:%M:%S").to_string();
        let tx = db.transaction().unwrap();
        let account_id = crate::account::get_or_create_account(
            &tx,
            "telegram",
            42,
            Some("user42"),
            Some("User 42"),
            &now_str,
        )
        .unwrap();
        tx.commit().unwrap();

        let raw_token = create_session(&db, &account_id, "telegram", 42, 3600, now).unwrap();
        assert!(!raw_token.is_empty());

        let auth = authenticate_session(&db, &raw_token, &now_str)
            .unwrap()
            .unwrap();
        assert_eq!(auth.0, account_id);
        assert_eq!(auth.1, "telegram");
        assert_eq!(auth.2, 42);

        // Отзываем
        let revoked = revoke_session(&db, &raw_token, &now_str).unwrap();
        assert!(revoked);

        // Повторная аутентификация должна вернуть None
        let auth_after = authenticate_session(&db, &raw_token, &now_str).unwrap();
        assert!(auth_after.is_none());
    }
}

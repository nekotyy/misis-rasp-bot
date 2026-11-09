//! Разбор прав веб-пользователей без доверия к неизвестным разрешениям.

use rusqlite::{Connection, OptionalExtension};
use subtle::ConstantTimeEq;

pub const ALL_PERMISSIONS: &[&str] = &[
    "stats_overview",
    "stats_users",
    "stats_services",
    "stats_schedule",
    "stats_delivery",
    "stats_lesson_counters",
    "stats_courses",
    "config_lesson_counters",
    "manage_bot_admin",
    "manage_web_users",
    "manage_gemini",
];

#[derive(Debug, PartialEq, Eq)]
pub struct WebUser {
    pub login: String,
    pub permissions: Vec<String>,
    pub is_superuser: bool,
}

impl WebUser {
    pub fn can(&self, permission: &str) -> bool {
        self.is_superuser || self.permissions.iter().any(|value| value == permission)
    }
}

/// Имя суперпользователя должно поступать из проверенной конфигурации, не из запроса.
pub fn load_user(
    db: &Connection,
    login: &str,
    superuser_login: &str,
) -> rusqlite::Result<Option<WebUser>> {
    let login = login.trim();
    if login.is_empty() {
        return Ok(None);
    }
    if !superuser_login.is_empty() && login == superuser_login {
        return Ok(Some(WebUser {
            login: login.into(),
            permissions: ALL_PERMISSIONS.iter().map(|p| (*p).into()).collect(),
            is_superuser: true,
        }));
    }
    let raw: Option<String> = db
        .query_row(
            "SELECT permissions_json FROM web_users WHERE login = ?",
            [login],
            |row| row.get(0),
        )
        .optional()?;
    let Some(raw) = raw else {
        return Ok(None);
    };
    let permissions = serde_json::from_str::<Vec<serde_json::Value>>(&raw)
        .unwrap_or_default()
        .into_iter()
        .filter_map(|value| {
            value
                .as_str()
                .filter(|p| ALL_PERMISSIONS.contains(p))
                .map(str::to_owned)
        })
        .collect();
    Ok(Some(WebUser {
        login: login.into(),
        permissions,
        is_superuser: false,
    }))
}

/// Проверяет пароль; зарезервированное имя суперпользователя нельзя
/// аутентифицировать паролем одноимённой строки из базы.
pub fn authenticate(
    db: &Connection,
    login: &str,
    password: &str,
    superuser_login: &str,
    superuser_password: &str,
) -> rusqlite::Result<Option<WebUser>> {
    let login = login.trim();
    if login.is_empty() {
        return Ok(None);
    }
    if !superuser_login.is_empty() && login == superuser_login {
        if superuser_password.is_empty()
            || !bool::from(password.as_bytes().ct_eq(superuser_password.as_bytes()))
        {
            return Ok(None);
        }
        return load_user(db, login, superuser_login);
    }
    let hash: Option<String> = db
        .query_row(
            "SELECT password_hash FROM web_users WHERE login = ?",
            [login],
            |row| row.get(0),
        )
        .optional()?;
    if !hash.is_some_and(|hash| crate::web::web_security::verify_password(password, &hash)) {
        return Ok(None);
    }
    load_user(db, login, superuser_login)
}

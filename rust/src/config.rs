use std::{collections::HashMap, path::PathBuf};

// Секреты не реализуют Debug, чтобы случайно не попасть в журналы.
#[derive(Clone)]
pub struct Settings {
    pub schedule_url: String,
    pub university_schedule_url: String,
    pub database_path: PathBuf,
    pub app_timezone: String,
    pub schedule_request_delay_seconds: f64,
    pub schedule_request_jitter_seconds: f64,
    pub telegram_bot_token: String,
    pub telegram_proxy: String,
    pub vk_bot_token: String,
    pub vk_miniapp_secret: String,
    pub vk_disable_ssl_verify: bool,
    pub admin_telegram_ids: Vec<i64>,
    pub limited_admin_telegram_ids: Vec<i64>,
    pub admin_vk_id: Option<i64>,
    pub rabbitmq_url: String,
    pub rabbitmq_queue: String,
    pub db_cleanup_queue: String,
    pub auto_daily_lesson_counter_queue: String,
    pub rabbitmq_prefetch_count: i64,
    pub lesson_counters_enabled: bool,
    pub lesson_counters_path: PathBuf,
    pub web_cookie_secure: bool,
    pub web_session_ttl_seconds: i64,
    pub ocr_enabled: bool,
    pub gemini_secure_1psid: String,
    pub gemini_secure_1psidts: String,
    pub gemini_proxy: String,
    pub gemini_doh_url: String,
    pub gemini_env_path: Option<PathBuf>,
    pub gemini_refresh_interval_seconds: f64,
    pub gemini_ocr_gem_id: String,
    pub ocr_gemini_model: String,
    pub ocr_timeout_seconds: f64,
    pub ocr_min_confidence: f64,
    pub ocr_fuzzy_threshold: f64,
    pub ocr_warmup_on_start: bool,
    pub ocr_ready_notice_hours: f64,
    pub miniapp_url: String,
}
impl Settings {
    pub fn from_env() -> Result<Self, String> {
        let _ = dotenvy::dotenv();
        Self::from_map(&std::env::vars().collect())
    }
    pub fn from_map(env: &HashMap<String, String>) -> Result<Self, String> {
        let get = |key: &str, default: &str| {
            env.get(key)
                .map(String::as_str)
                .unwrap_or(default)
                .to_string()
        };
        let trimmed = |key: &str, default: &str| get(key, default).trim().to_string();
        let integer = |key: &str, default: &str| {
            trimmed(key, default)
                .parse::<i64>()
                .map_err(|_| format!("Некорректное число: {key}"))
        };
        let number = |key: &str, default: &str| {
            trimmed(key, default)
                .parse::<f64>()
                .map_err(|_| format!("Некорректное число: {key}"))
        };
        let boolean = |key: &str, default: bool| {
            let raw = trimmed(key, "").to_lowercase();
            if raw.is_empty() {
                default
            } else {
                matches!(raw.as_str(), "1" | "true" | "yes" | "on" | "да" | "вкл")
            }
        };
        let list = |key: &str| {
            get(key, "")
                .split(',')
                .filter_map(|v| v.trim().parse().ok())
                .collect::<Vec<i64>>()
        };
        let clamped = |key: &str, default: f64, min: f64, max: f64| {
            trimmed(key, "")
                .parse::<f64>()
                .ok()
                .filter(|v| v.is_finite())
                .unwrap_or(default)
                .clamp(min, max)
        };
        let path = |v: String| {
            let p = PathBuf::from(v);
            if p.is_absolute() {
                p
            } else {
                std::env::current_dir().unwrap_or_default().join(p)
            }
        };
        let admin_vk = trimmed("ADMIN_VK_ID", "");
        let env_path = trimmed("GEMINI_ENV_PATH", "");
        Ok(Self {
            schedule_url: get("SCHEDULE_URL", "http://asu.sf-misis.ru/rasp/600"),
            university_schedule_url: trimmed("UNIVERSITY_SCHEDULE_URL", "http://r.sf-misis.ru")
                .trim_end_matches('/')
                .into(),
            database_path: path(get("DATABASE_PATH", "bot.db")),
            app_timezone: get("APP_TIMEZONE", "Europe/Moscow"),
            schedule_request_delay_seconds: number("SCHEDULE_REQUEST_DELAY_SECONDS", "8")?,
            schedule_request_jitter_seconds: number("SCHEDULE_REQUEST_JITTER_SECONDS", "4")?,
            telegram_bot_token: trimmed("TELEGRAM_BOT_TOKEN", ""),
            telegram_proxy: trimmed("TELEGRAM_PROXY", ""),
            vk_bot_token: trimmed("VK_BOT_TOKEN", ""),
            vk_miniapp_secret: ["VK_MINIAPP_SECRET", "VK_APP_SECRET", "VK_CLIENT_SECRET"]
                .iter()
                .map(|key| trimmed(key, ""))
                .find(|value| !value.is_empty())
                .unwrap_or_default(),
            vk_disable_ssl_verify: matches!(
                trimmed("VK_DISABLE_SSL_VERIFY", "").to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            ),
            admin_telegram_ids: list("ADMIN_TELEGRAM_ID"),
            limited_admin_telegram_ids: list("LIMITED_ADMIN_TELEGRAM_IDS"),
            admin_vk_id: if admin_vk.is_empty() {
                None
            } else {
                Some(
                    admin_vk
                        .parse()
                        .map_err(|_| "Некорректное число: ADMIN_VK_ID")?,
                )
            },
            rabbitmq_url: trimmed("RABBITMQ_URL", ""),
            rabbitmq_queue: trimmed("RABBITMQ_QUEUE", "misis_notifications"),
            db_cleanup_queue: trimmed("DB_CLEANUP_QUEUE", "misis_db_cleanup"),
            auto_daily_lesson_counter_queue: trimmed(
                "AUTO_DAILY_LESSON_COUNTER_QUEUE",
                "misis_auto_daily_lesson_counter",
            ),
            rabbitmq_prefetch_count: integer("RABBITMQ_PREFETCH_COUNT", "20")?,
            lesson_counters_enabled: boolean("LESSON_COUNTERS_ENABLED", false),
            lesson_counters_path: path(get("LESSON_COUNTERS_PATH", "storage/lesson_counters.json")),
            web_cookie_secure: boolean("WEB_COOKIE_SECURE", true),
            web_session_ttl_seconds: integer("WEB_SESSION_TTL_SECONDS", "43200")?.max(300),
            ocr_enabled: boolean("OCR_ENABLED", true),
            gemini_secure_1psid: trimmed("GEMINI_SECURE_1PSID", ""),
            gemini_secure_1psidts: trimmed("GEMINI_SECURE_1PSIDTS", ""),
            gemini_proxy: trimmed("GEMINI_PROXY", ""),
            gemini_doh_url: trimmed("GEMINI_DOH_URL", ""),
            gemini_env_path: if env_path.is_empty() {
                None
            } else {
                Some(path(env_path))
            },
            gemini_refresh_interval_seconds: number("GEMINI_REFRESH_INTERVAL_SECONDS", "600")?
                .max(60.0),
            gemini_ocr_gem_id: trimmed("GEMINI_OCR_GEM_ID", ""),
            ocr_gemini_model: trimmed("OCR_GEMINI_MODEL", ""),
            ocr_timeout_seconds: number("OCR_TIMEOUT_SECONDS", "180")?,
            ocr_min_confidence: clamped("OCR_MIN_CONFIDENCE", 0.6, 0.0, 1.0),
            ocr_fuzzy_threshold: clamped("OCR_FUZZY_THRESHOLD", 0.78, 0.5, 1.0),
            ocr_warmup_on_start: boolean("OCR_WARMUP_ON_START", true),
            ocr_ready_notice_hours: clamped("OCR_READY_NOTICE_HOURS", 12.0, 0.0, 720.0),
            miniapp_url: trimmed("MINIAPP_URL", "https://miniapp.opk.nekoty.ru"),
        })
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_and_security_options_match_environment() {
        let mut env = HashMap::new();
        env.insert("ADMIN_TELEGRAM_ID".into(), "1, x, -2".into());
        env.insert("WEB_SESSION_TTL_SECONDS".into(), "20".into());
        env.insert("OCR_MIN_CONFIDENCE".into(), "5".into());
        env.insert("OCR_ENABLED".into(), "да".into());
        let settings = Settings::from_map(&env).unwrap();
        assert_eq!(settings.admin_telegram_ids, vec![1, -2]);
        assert_eq!(settings.web_session_ttl_seconds, 300);
        assert_eq!(settings.ocr_min_confidence, 1.0);
        assert!(settings.ocr_enabled && settings.web_cookie_secure);
        assert_eq!(settings.rabbitmq_prefetch_count, 20);
    }
}

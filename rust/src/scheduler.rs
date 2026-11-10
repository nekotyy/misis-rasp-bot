use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;

use crate::config::Settings;
use crate::database::{cleanup_database, daily_baseline_source_keys, get_active_sources};
use crate::notification::{drain_once, ApiTransport, ProcessOutcome};
use crate::parser::ScheduleParser;
use crate::sync::{
    apply_and_enqueue, finish_broadcast, ApplyOptions, AtomicApplyResult, DeliveryPlatforms,
    Source as SyncSource,
};

pub struct Scheduler {
    pub settings: Settings,
    timezone: chrono_tz::Tz,
    pub parser: ScheduleParser,
    pub transport: Arc<ApiTransport>,
    pub sync_lock: Arc<Mutex<()>>,
    pub baseline_lock: Arc<Mutex<()>>,
}

impl Scheduler {
    pub fn new(
        settings: Settings,
        transport: Arc<ApiTransport>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let timezone = settings.app_timezone.parse::<chrono_tz::Tz>()?;
        let schedule_url = if settings.schedule_url.is_empty() {
            "http://asu.sf-misis.ru/rasp/600".to_string()
        } else {
            settings.schedule_url.clone()
        };
        let university_url = if settings.university_schedule_url.is_empty() {
            "https://misis.ru/students/schedule/".to_string()
        } else {
            settings.university_schedule_url.clone()
        };
        let parser = ScheduleParser::new(
            &schedule_url,
            &university_url,
            Duration::from_secs(20),
            3,
            Duration::from_secs(1),
        )?;
        Ok(Self {
            settings,
            timezone,
            parser,
            transport,
            sync_lock: Arc::new(Mutex::new(())),
            baseline_lock: Arc::new(Mutex::new(())),
        })
    }

    pub fn local_time(&self, now: chrono::DateTime<chrono::Utc>) -> chrono::NaiveDateTime {
        now.with_timezone(&self.timezone).naive_local()
    }

    /// Один шаг воркера доставки из `delivery_outbox` для Telegram и VK
    pub async fn process_outbox_batch(
        &self,
        db: &mut rusqlite::Connection,
        batch_size: usize,
        now: f64,
    ) -> Result<Vec<(String, ProcessOutcome)>, Box<dyn std::error::Error>> {
        let mut all_outcomes = Vec::new();
        // Обрабатываем очередь Telegram
        let tg_outcomes = drain_once(db, &self.transport, "telegram", now, batch_size).await?;
        all_outcomes.extend(tg_outcomes);

        // Обрабатываем очередь VK
        let vk_outcomes = drain_once(db, &self.transport, "vk", now, batch_size).await?;
        all_outcomes.extend(vk_outcomes);

        Ok(all_outcomes)
    }

    /// Проверка изменений расписания для всех активных групп и преподавателей
    pub async fn sync_active_sources(
        &self,
        db: &mut rusqlite::Connection,
        now_dt: chrono::NaiveDateTime,
        now_ts: f64,
    ) -> Result<Vec<AtomicApplyResult>, Box<dyn std::error::Error>> {
        let _guard = match self.sync_lock.try_lock() {
            Ok(g) => g,
            Err(_) => return Ok(vec![]), // Пропускаем шаг, если предыдущая синхронизация ещё выполняется
        };

        let sources = get_active_sources(db)?;
        let mut outcomes = Vec::new();

        let options = ApplyOptions {
            notify: true,
            trust_empty: false,
            observed_dates: None,
            manual_origin: "scheduler",
            timezone: self.timezone,
            now: now_dt,
        };

        let platforms = DeliveryPlatforms {
            telegram: !self.settings.telegram_bot_token.is_empty(),
            vk: !self.settings.vk_bot_token.is_empty(),
        };

        for source_row in sources {
            let sync_source = SyncSource {
                source_type: &source_row.source_type,
                source_key: &source_row.source_key,
                source_title: &source_row.source_title,
                source_url: &source_row.source_url,
                group_name: source_row.group_name.as_deref(),
                schedule_id: source_row.schedule_id,
            };

            // Скачиваем свежий снимок расписания
            let fetched = match source_row.source_type.as_str() {
                "group" => {
                    if let Some(id) = source_row.schedule_id {
                        self.parser.parse(id, now_dt).await.ok()
                    } else if !source_row.source_url.is_empty() {
                        self.parser
                            .parse_from_url(&source_row.source_url, now_dt)
                            .await
                            .ok()
                    } else {
                        None
                    }
                }
                "teacher" => {
                    if !source_row.source_url.is_empty() {
                        self.parser
                            .parse_from_url(&source_row.source_url, now_dt)
                            .await
                            .ok()
                    } else {
                        None
                    }
                }
                _ => None,
            };

            let Some((snapshot, hash)) = fetched else {
                continue;
            };

            let outcome = apply_and_enqueue(
                db,
                &sync_source,
                &snapshot,
                &hash,
                &options,
                &platforms,
                now_ts,
            )?;
            outcomes.push(outcome);

            // Небольшая пауза между запросами к сайту МИСИС, чтобы не перегружать сервер
            tokio::time::sleep(Duration::from_millis(100)).await;
        }

        Ok(outcomes)
    }

    /// Ежедневное сохранение базового снимка `daily_baseline`
    pub async fn save_daily_baselines(
        &self,
        db: &mut rusqlite::Connection,
        now_dt: chrono::NaiveDateTime,
    ) -> Result<usize, Box<dyn std::error::Error>> {
        self.save_baselines(db, now_dt, false).await
    }

    /// Повторная утренняя попытка только для источников без сегодняшнего эталона.
    pub async fn save_missing_daily_baselines(
        &self,
        db: &mut rusqlite::Connection,
        now_dt: chrono::NaiveDateTime,
    ) -> Result<usize, Box<dyn std::error::Error>> {
        self.save_baselines(db, now_dt, true).await
    }

    async fn save_baselines(
        &self,
        db: &mut rusqlite::Connection,
        now_dt: chrono::NaiveDateTime,
        only_missing: bool,
    ) -> Result<usize, Box<dyn std::error::Error>> {
        let _guard = match self.baseline_lock.try_lock() {
            Ok(g) => g,
            Err(_) => return Ok(0),
        };

        let sources = get_active_sources(db)?;
        let existing = if only_missing {
            Some(daily_baseline_source_keys(db, now_dt.date())?)
        } else {
            None
        };
        let mut saved = 0;

        let options = ApplyOptions {
            notify: false,
            trust_empty: false,
            observed_dates: None,
            manual_origin: "daily_baseline",
            timezone: self.timezone,
            now: now_dt,
        };

        for source_row in sources {
            if existing
                .as_ref()
                .is_some_and(|keys| keys.contains(&source_row.source_key))
            {
                continue;
            }
            let sync_source = SyncSource {
                source_type: &source_row.source_type,
                source_key: &source_row.source_key,
                source_title: &source_row.source_title,
                source_url: &source_row.source_url,
                group_name: source_row.group_name.as_deref(),
                schedule_id: source_row.schedule_id,
            };

            let fetched = match source_row.source_type.as_str() {
                "group" => {
                    if let Some(id) = source_row.schedule_id {
                        self.parser.parse(id, now_dt).await.ok()
                    } else if !source_row.source_url.is_empty() {
                        self.parser
                            .parse_from_url(&source_row.source_url, now_dt)
                            .await
                            .ok()
                    } else {
                        None
                    }
                }
                "teacher" => {
                    if !source_row.source_url.is_empty() {
                        self.parser
                            .parse_from_url(&source_row.source_url, now_dt)
                            .await
                            .ok()
                    } else {
                        None
                    }
                }
                _ => None,
            };

            let Some((snapshot, hash)) = fetched else {
                continue;
            };

            finish_broadcast(db, &sync_source, &snapshot, &hash, &options)?;
            saved += 1;
            tokio::time::sleep(Duration::from_millis(100)).await;
        }

        Ok(saved)
    }

    /// Очистка устаревших логов и записей outbox
    pub fn cleanup_old_records(
        &self,
        db: &rusqlite::Connection,
        days: i64,
        now: &str,
    ) -> rusqlite::Result<usize> {
        cleanup_database(db, days, now)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn test_settings() -> Settings {
        let mut env = std::collections::HashMap::new();
        env.insert("SCHEDULE_URL".into(), "http://127.0.0.1:9/rasp/600".into());
        env.insert("DATABASE_PATH".into(), ":memory:".into());
        Settings::from_map(&env).unwrap()
    }

    #[tokio::test]
    async fn scheduler_creation_and_cleanup() {
        let settings = test_settings();
        let transport = Arc::new(
            ApiTransport::new(
                "http://127.0.0.1:9",
                "http://127.0.0.1:9",
                "tg_token".into(),
                "vk_token".into(),
                "5.131",
            )
            .unwrap(),
        );
        let scheduler = Scheduler::new(settings, transport).unwrap();

        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();

        let cleaned = scheduler
            .cleanup_old_records(&db, 30, "2026-11-09T12:00:00")
            .unwrap();
        assert_eq!(cleaned, 0);
    }

    #[tokio::test]
    async fn outbox_batch_processes_and_handles_network_retry() {
        let settings = test_settings();
        // Используем фиктивный адрес 127.0.0.1:9, к которому не подключиться -> сетевая ошибка
        let transport = Arc::new(
            ApiTransport::new(
                "http://127.0.0.1:9",
                "http://127.0.0.1:9",
                "tg_token".into(),
                "vk_token".into(),
                "5.131",
            )
            .unwrap(),
        );
        let scheduler = Scheduler::new(settings, transport).unwrap();

        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();

        let payload = json!({"chat_id": 12345, "text": "Тест"});
        let metadata = json!({"campaign_type": "interactive"});
        let delivery = crate::outbox::Delivery {
            id: "msg_outbox_test",
            platform: "telegram",
            peer: "12345",
            method: "sendMessage",
            payload: &payload,
            metadata: &metadata,
            ttl: 3600.0,
        };
        crate::outbox::enqueue(&mut db, &[delivery], 100.0).unwrap();

        let outcomes = scheduler
            .process_outbox_batch(&mut db, 10, 100.0)
            .await
            .unwrap();
        assert_eq!(outcomes.len(), 1);
        assert_eq!(outcomes[0].0, "msg_outbox_test");
        assert_eq!(outcomes[0].1, ProcessOutcome::Retry);

        // Проверяем, что статус сообщения обновился на pending для повторной попытки
        let row = crate::outbox::get(&db, "msg_outbox_test").unwrap().unwrap();
        assert_eq!(row.status, "pending");
        assert_eq!(row.attempts, 1);
        assert!(row.next_attempt_at > 100.0);
    }
}

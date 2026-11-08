use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub fn split_text(text: &str, limit: usize) -> Vec<String> {
    if limit == 0 {
        return vec![String::new()];
    }
    let mut remaining: Vec<char> = text.chars().collect();
    if remaining.len() <= limit {
        return vec![text.to_owned()];
    }
    let mut chunks = Vec::new();
    while remaining.len() > limit {
        let part = &remaining[..limit];
        let double = part.windows(2).rposition(|p| p == ['\n', '\n']);
        let newline = part.iter().rposition(|c| *c == '\n');
        let space = part.iter().rposition(|c| *c == ' ');
        let mut cut = double.unwrap_or(0);
        if cut < limit / 2 {
            cut = newline.unwrap_or(0);
        }
        if cut < limit / 2 {
            cut = space.unwrap_or(0);
        }
        if cut == 0 {
            cut = limit;
        }
        let chunk: String = remaining[..cut].iter().collect();
        chunks.push(chunk.trim_end().to_string());
        let next = remaining[cut..]
            .iter()
            .position(|c| *c != '\n' && *c != ' ')
            .map(|n| cut + n)
            .unwrap_or(remaining.len());
        remaining.drain(..next);
    }
    if !remaining.is_empty() {
        chunks.push(remaining.iter().collect());
    }
    chunks.retain(|chunk| !chunk.is_empty());
    if chunks.is_empty() {
        vec![text.chars().take(limit).collect()]
    } else {
        chunks
    }
}
pub fn telegram_keyboard() -> Value {
    json!({"inline_keyboard":[[{"text":"Расписание на сегодня","callback_data":"schedule:today"}],[{"text":"Расписание на завтра","callback_data":"schedule:tomorrow"}],[{"text":"Расписание на 2 дня","callback_data":"schedule:day_after"}],[{"text":"Расписание звонков","callback_data":"schedule:bells"}],[{"text":"Найти расписание","callback_data":"schedule:find"}],[{"text":"Назад","callback_data":"menu:start"}]]})
}
pub fn vk_keyboard() -> Value {
    let labels = [
        "Расписание на сегодня",
        "Расписание на завтра",
        "Расписание на 2 дня",
        "Расписание звонков",
        "Найти расписание",
    ];
    json!({"one_time":false,"inline":true,"buttons":labels.iter().map(|label|vec![json!({"action":{"label":label,"type":"text"}})]).collect::<Vec<_>>()})
}
#[derive(Debug)]
pub struct PreparedMessage {
    pub id: String,
    pub platform: String,
    pub peer: String,
    pub method: String,
    pub payload: Value,
    pub metadata: Value,
}
fn vk_random_id(prefix: &str, part: &str) -> i64 {
    let digest = Sha256::digest(format!("{prefix}:{part}").as_bytes());
    let n = u32::from_be_bytes(digest[..4].try_into().unwrap()) & 0x7fff_ffff;
    i64::from(n.max(1))
}
pub fn prepare_notification(
    platform: &str,
    user_id: i64,
    text: &str,
    campaign: &str,
    message_id: &str,
    sticker: Option<&str>,
) -> Vec<PreparedMessage> {
    let prefix = format!("notification:{message_id}");
    let metadata = json!({"campaign_type":campaign});
    let mut result = Vec::new();
    let mut push = |part: &str, method: &str, payload: Value| {
        result.push(PreparedMessage {
            id: format!("{prefix}:{part}"),
            platform: platform.into(),
            peer: user_id.to_string(),
            method: method.into(),
            payload,
            metadata: metadata.clone(),
        })
    };
    let notification = campaign == "notification";
    if platform == "telegram" {
        if let Some(sticker) = sticker.filter(|_| notification) {
            push(
                "sticker",
                "sendSticker",
                json!({"chat_id":user_id,"sticker":sticker}),
            );
        }
        let chunks = split_text(text, 4096);
        let last = chunks.len() - 1;
        for (index, chunk) in chunks.into_iter().enumerate() {
            let mut payload = json!({"chat_id":user_id,"text":chunk,"parse_mode":"HTML"});
            if notification && index == last {
                payload["reply_markup"] = telegram_keyboard();
            }
            push(&format!("text:{index}"), "sendMessage", payload);
        }
    } else if platform == "vk" {
        if let Some(sticker) = sticker
            .filter(|_| notification)
            .and_then(|s| s.parse::<i64>().ok())
        {
            push(
                "sticker",
                "messages.send",
                json!({"peer_id":user_id,"sticker_id":sticker,"random_id":vk_random_id(&prefix,"sticker")}),
            );
        }
        let chunks = split_text(text, 4096);
        let last = chunks.len() - 1;
        for (index, chunk) in chunks.into_iter().enumerate() {
            let mut payload = json!({"peer_id":user_id,"message":chunk,"random_id":vk_random_id(&prefix,&index.to_string())});
            if notification && index == last {
                payload["keyboard"] = Value::String(vk_keyboard().to_string());
            }
            push(&format!("text:{index}"), "messages.send", payload);
        }
    }
    result
}

pub fn enqueue_prepared(
    db: &mut rusqlite::Connection,
    messages: &[PreparedMessage],
    now: f64,
) -> rusqlite::Result<()> {
    let items = messages
        .iter()
        .map(|m| crate::outbox::Delivery {
            id: &m.id,
            platform: &m.platform,
            peer: &m.peer,
            method: &m.method,
            payload: &m.payload,
            metadata: &m.metadata,
            ttl: 86400.0,
        })
        .collect::<Vec<_>>();
    crate::outbox::enqueue(db, &items, now)
}

#[derive(Debug, PartialEq)]
pub enum DeliveryFailure {
    Network,
    Telegram {
        code: i64,
        description: String,
        retry_after: Option<f64>,
    },
    Vk {
        code: i64,
        description: String,
    },
    InvalidResponse,
}
impl DeliveryFailure {
    pub fn retry_delay(&self, attempts: u32) -> Option<f64> {
        let delays = [10.0, 30.0, 60.0, 300.0];
        let delay = delays[attempts.saturating_sub(1).min(3) as usize];
        match self {
            Self::Network | Self::InvalidResponse => Some(delay),
            Self::Telegram {
                retry_after: Some(seconds),
                ..
            } => Some(seconds.max(0.0) + 0.5),
            Self::Telegram { code: 401, .. } => Some(300.0),
            Self::Telegram { code, .. } if *code >= 500 => Some(delay),
            Self::Vk {
                code: 1 | 5 | 6 | 10 | 27 | 28 | 603,
                ..
            } => Some(delay),
            _ => None,
        }
    }
    pub fn permanent_recipient(&self) -> bool {
        match self {
            Self::Telegram {
                code, description, ..
            } => {
                *code == 403
                    || [
                        "bot was blocked by the user",
                        "chat not found",
                        "user is deactivated",
                        "have no rights to send a message",
                        "chat_id is empty",
                        "group chat was upgraded to a supergroup chat",
                    ]
                    .iter()
                    .any(|text| description.to_lowercase().contains(text))
            }
            Self::Vk { code, .. } => {
                matches!(code, 7 | 18 | 900 | 901 | 902 | 917 | 936 | 945 | 946)
            }
            _ => false,
        }
    }
    pub fn invalid_keyboard(&self) -> bool {
        matches!(
            self,
            Self::Vk {
                code: 911 | 912,
                ..
            }
        )
    }
}
pub fn telegram_response(value: &Value) -> Result<Value, DeliveryFailure> {
    if value.get("ok").and_then(Value::as_bool) == Some(true) {
        return Ok(value.get("result").cloned().unwrap_or(Value::Null));
    }
    let code = value
        .get("error_code")
        .and_then(Value::as_i64)
        .ok_or(DeliveryFailure::InvalidResponse)?;
    Err(DeliveryFailure::Telegram {
        code,
        description: value
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        retry_after: value
            .pointer("/parameters/retry_after")
            .and_then(Value::as_f64),
    })
}
pub fn vk_response(value: &Value) -> Result<Value, DeliveryFailure> {
    if let Some(result) = value.get("response") {
        return Ok(result.clone());
    }
    let error = value.get("error").ok_or(DeliveryFailure::InvalidResponse)?;
    let code = error
        .get("error_code")
        .and_then(Value::as_i64)
        .ok_or(DeliveryFailure::InvalidResponse)?;
    Err(DeliveryFailure::Vk {
        code,
        description: error
            .get("error_msg")
            .and_then(Value::as_str)
            .unwrap_or("")
            .into(),
    })
}

pub struct ApiTransport {
    client: reqwest::Client,
    telegram_base: String,
    vk_base: String,
    telegram_token: String,
    vk_token: String,
    vk_version: String,
}
impl ApiTransport {
    pub fn new(
        telegram_base: &str,
        vk_base: &str,
        telegram_token: String,
        vk_token: String,
        vk_version: &str,
    ) -> Result<Self, reqwest::Error> {
        Ok(Self {
            client: reqwest::Client::builder()
                .connect_timeout(std::time::Duration::from_secs(15))
                .timeout(std::time::Duration::from_secs(90))
                .build()?,
            telegram_base: telegram_base.trim_end_matches('/').into(),
            vk_base: vk_base.trim_end_matches('/').into(),
            telegram_token,
            vk_token,
            vk_version: vk_version.into(),
        })
    }
    pub async fn send(
        &self,
        platform: &str,
        method: &str,
        payload: &Value,
    ) -> Result<Value, DeliveryFailure> {
        if method.is_empty()
            || !method
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'.' || c == b'_')
        {
            return Err(DeliveryFailure::InvalidResponse);
        }
        if platform == "telegram" {
            let url = format!(
                "{}/bot{}/{}",
                self.telegram_base, self.telegram_token, method
            );
            let response = self
                .client
                .post(url)
                .json(payload)
                .send()
                .await
                .map_err(|_| DeliveryFailure::Network)?;
            let status = response.status().as_u16();
            let value: Value = response.json().await.map_err(|_| {
                if status == 401 {
                    DeliveryFailure::Telegram {
                        code: 401,
                        description: String::new(),
                        retry_after: None,
                    }
                } else {
                    DeliveryFailure::InvalidResponse
                }
            })?;
            telegram_response(&value)
        } else if platform == "vk" {
            let Some(object) = payload.as_object() else {
                return Err(DeliveryFailure::InvalidResponse);
            };
            let mut fields: Vec<(String, String)> = object
                .iter()
                .map(|(key, value)| {
                    (
                        key.clone(),
                        match value {
                            Value::String(s) => s.clone(),
                            _ => value.to_string(),
                        },
                    )
                })
                .collect();
            fields.push(("access_token".into(), self.vk_token.clone()));
            fields.push(("v".into(), self.vk_version.clone()));
            let response = self
                .client
                .post(format!("{}/{}", self.vk_base, method))
                .form(&fields)
                .send()
                .await
                .map_err(|_| DeliveryFailure::Network)?;
            let value: Value = response
                .json()
                .await
                .map_err(|_| DeliveryFailure::InvalidResponse)?;
            vk_response(&value)
        } else {
            Err(DeliveryFailure::InvalidResponse)
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum ProcessOutcome {
    NotClaimed,
    Sent,
    Retry,
    Failed,
}
pub async fn process_one(
    db: &mut rusqlite::Connection,
    transport: &ApiTransport,
    id: &str,
    now: f64,
) -> Result<ProcessOutcome, Box<dyn std::error::Error>> {
    let mut bytes = [0u8; 16];
    getrandom::getrandom(&mut bytes).map_err(|_| "Не удалось создать аренду задания")?;
    let token = bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    if !crate::outbox::claim(db, id, &token, now, 120.0)? {
        return Ok(ProcessOutcome::NotClaimed);
    }
    let Some(row) = crate::outbox::get(db, id)? else {
        return Ok(ProcessOutcome::NotClaimed);
    };
    let payload: Value = match serde_json::from_str(&row.payload_json) {
        Ok(value) => value,
        Err(_) => {
            let error = json!({"type":"InvalidPayload"});
            crate::outbox::finish(db, id, &token, now, &Value::Null, Some(&error), None)?;
            return Ok(ProcessOutcome::Failed);
        }
    };
    let campaign = serde_json::from_str::<Value>(&row.metadata_json)
        .ok()
        .and_then(|m| {
            m.get("campaign_type")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .unwrap_or_else(|| "interactive".into());
    let started = std::time::Instant::now();
    let mut response = transport.send(&row.platform, &row.method, &payload).await;
    if row.platform == "vk"
        && response
            .as_ref()
            .err()
            .is_some_and(DeliveryFailure::invalid_keyboard)
        && payload.get("keyboard").is_some()
    {
        if !crate::outbox::renew(db, id, &token, now + started.elapsed().as_secs_f64())? {
            return Ok(ProcessOutcome::NotClaimed);
        }
        let mut fallback = payload.clone();
        fallback.as_object_mut().unwrap().remove("keyboard");
        fallback["random_id"] = json!(vk_random_id(&row.delivery_id, "keyboard-fallback"));
        response = transport.send(&row.platform, &row.method, &fallback).await;
    }
    let completed = now + started.elapsed().as_secs_f64();
    match response {
        Ok(value) => {
            if !crate::outbox::finish(db, id, &token, completed, &value, None, None)? {
                return Ok(ProcessOutcome::NotClaimed);
            }
            audit_delivery(db, &row, &payload, &campaign, "sent", None);
            Ok(ProcessOutcome::Sent)
        }
        Err(error) => {
            let retry = error.retry_delay(row.attempts as u32);
            let permanent = error.permanent_recipient();
            let error_json = match &error {
                DeliveryFailure::Telegram {
                    code, description, ..
                }
                | DeliveryFailure::Vk { code, description } => {
                    json!({"code":code,"message":description})
                }
                DeliveryFailure::Network => json!({"type":"Network"}),
                DeliveryFailure::InvalidResponse => json!({"type":"InvalidResponse"}),
            };
            if !crate::outbox::finish(
                db,
                id,
                &token,
                completed,
                &Value::Null,
                Some(&error_json),
                retry,
            )? {
                return Ok(ProcessOutcome::NotClaimed);
            }
            audit_delivery(
                db,
                &row,
                &payload,
                &campaign,
                "failed",
                Some(&error_json.to_string()),
            );
            if permanent && campaign != "admin_notify" {
                if let Ok(peer) = row.peer_key.parse::<i64>() {
                    let _=db.execute("UPDATE users SET delivery_disabled_auto=1,homework_notifications_enabled=0 WHERE platform=? AND user_id=?",rusqlite::params![row.platform,peer]);
                }
            }
            Ok(if retry.is_some() {
                ProcessOutcome::Retry
            } else {
                ProcessOutcome::Failed
            })
        }
    }
}

fn audit_delivery(
    db: &rusqlite::Connection,
    row: &crate::outbox::DeliveryRow,
    payload: &Value,
    campaign: &str,
    status: &str,
    error: Option<&str>,
) {
    let peer = payload
        .get("chat_id")
        .or_else(|| payload.get("peer_id"))
        .or_else(|| payload.get("user_id"))
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        });
    if let Some(peer) = peer {
        let now = chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string();
        let _=db.execute("INSERT INTO delivery_events(message_id,campaign_type,platform,user_id,via_broker,status,attempt,error_text,created_at) VALUES (?,?,?,?,0,?,?,?,?)",rusqlite::params![row.delivery_id,campaign,row.platform,peer,status,row.attempts,error,now]);
    }
}

/// Один проход устойчивой очереди без брокера; каждое задание хранится в SQLite.
pub async fn drain_once(
    db: &mut rusqlite::Connection,
    transport: &ApiTransport,
    platform: &str,
    now: f64,
    limit: usize,
) -> Result<Vec<(String, ProcessOutcome)>, Box<dyn std::error::Error>> {
    let started = std::time::Instant::now();
    let ids = crate::outbox::reserve_due(db, platform, now, limit)?;
    let mut outcomes = Vec::with_capacity(ids.len());
    for id in ids {
        let outcome =
            process_one(db, transport, &id, now + started.elapsed().as_secs_f64()).await?;
        outcomes.push((id, outcome));
    }
    Ok(outcomes)
}

pub struct Broadcast<'a> {
    pub event_id: &'a str,
    pub plain: &'a str,
    pub telegram: Option<&'a str>,
    pub vk: Option<&'a str>,
    pub campaign: &'a str,
    pub target_platform: &'a str,
    pub audience: &'a str,
    pub schedule_id: Option<i64>,
    pub subscription_key: Option<&'a str>,
    pub subgroup: Option<Option<i64>>,
}
pub fn enqueue_broadcast(
    db: &mut rusqlite::Connection,
    broadcast: &Broadcast<'_>,
    now: f64,
) -> rusqlite::Result<usize> {
    let mut recipients = Vec::new();
    for platform in ["telegram", "vk"] {
        if broadcast.target_platform != "all" && broadcast.target_platform != platform {
            continue;
        }
        for user in crate::database::users_for_notifications(
            db,
            platform,
            broadcast.schedule_id,
            broadcast.subscription_key,
            broadcast.audience,
        )? {
            if !broadcast
                .subgroup
                .is_some_and(|group| user.university_subgroup != group)
            {
                recipients.push(user);
            }
        }
    }
    let count = recipients.len();
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    for user in recipients {
        let platform = user.platform.as_str();
        let body = if platform == "telegram" {
            broadcast
                .telegram
                .filter(|s| !s.is_empty())
                .unwrap_or(broadcast.plain)
        } else {
            broadcast
                .vk
                .filter(|s| !s.is_empty())
                .unwrap_or(broadcast.plain)
        };
        let digest = Sha256::digest(
            format!("{}:{platform}:{}", broadcast.event_id, user.user_id).as_bytes(),
        );
        let message_id = format!("{digest:x}");
        let messages = prepare_notification(
            platform,
            user.user_id,
            body,
            broadcast.campaign,
            &message_id,
            user.custom_sticker_file_id.as_deref(),
        );
        let items = messages
            .iter()
            .map(|m| crate::outbox::Delivery {
                id: &m.id,
                platform: &m.platform,
                peer: &m.peer,
                method: &m.method,
                payload: &m.payload,
                metadata: &m.metadata,
                ttl: 86400.0,
            })
            .collect::<Vec<_>>();
        crate::outbox::enqueue_in_transaction(&tx, &items, now)?;
    }
    tx.commit()?;
    Ok(count)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn splits_unicode_and_keeps_keyboards_on_final_part() {
        let text = format!("{}\n\n{}", "а".repeat(3000), "б".repeat(3000));
        let tg = prepare_notification("telegram", 1, &text, "notification", "event", None);
        assert_eq!(tg.len(), 2);
        assert!(tg[0].payload.get("reply_markup").is_none());
        assert_eq!(
            tg[1].payload["reply_markup"]["inline_keyboard"]
                .as_array()
                .unwrap()
                .len(),
            6
        );
        let vk = prepare_notification("vk", 2, &text, "notification", "event", Some("42"));
        assert_eq!(vk.len(), 3);
        assert_eq!(vk[0].payload["sticker_id"], 42);
        assert!(vk[1].payload.get("keyboard").is_none());
        assert!(vk[2].payload.get("keyboard").is_some());
        let repeat = prepare_notification("vk", 2, &text, "notification", "event", Some("42"));
        assert_eq!(vk[2].payload["random_id"], repeat[2].payload["random_id"]);
    }
}

#[cfg(test)]
mod queue_tests {
    use super::*;
    #[test]
    fn persists_all_chunks_before_delivery_and_replay_deduplicates() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch(crate::outbox::SCHEMA).unwrap();
        let text = format!("{}\n\n{}", "а".repeat(3000), "б".repeat(3000));
        let messages = prepare_notification(
            "telegram",
            1,
            &text,
            "notification",
            "same",
            Some("sticker"),
        );
        enqueue_prepared(&mut db, &messages, 100.0).unwrap();
        enqueue_prepared(&mut db, &messages, 100.0).unwrap();
        let count: i64 = db
            .query_row("SELECT count(*) FROM delivery_outbox", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 3);
        assert_eq!(
            crate::outbox::reserve_due(&mut db, "telegram", 100.0, 50).unwrap(),
            vec!["notification:same:sticker"]
        );
    }
}

#[cfg(test)]
mod failure_tests {
    use super::*;
    #[test]
    fn rate_limits_and_permanent_errors_are_classified() {
        let err = telegram_response(
            &json!({"ok":false,"error_code":429,"parameters":{"retry_after":12}}),
        )
        .unwrap_err();
        assert_eq!(err.retry_delay(1), Some(12.5));
        let err =
            telegram_response(&json!({"ok":false,"error_code":403,"description":"Forbidden"}))
                .unwrap_err();
        assert!(err.permanent_recipient());
        assert_eq!(err.retry_delay(1), None);
        let err = vk_response(&json!({"error":{"error_code":911}})).unwrap_err();
        assert!(err.invalid_keyboard());
        assert_eq!(vk_response(&json!({"response":42})).unwrap(), json!(42));
        assert_eq!(DeliveryFailure::Network.retry_delay(5), Some(300.0));
    }
}
#[cfg(test)]
mod broadcast_tests {
    use super::*;
    #[test]
    fn fanout_is_durable_and_idempotent() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch(crate::outbox::SCHEMA).unwrap();
        db.execute_batch("CREATE TABLE users(platform TEXT,user_id INTEGER,username TEXT,full_name TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,audience_subscription_key TEXT,audience_subscription_title TEXT,audience_subscription_url TEXT,group_name TEXT,schedule_id INTEGER,is_admin INTEGER,is_editor INTEGER,homework_notifications_enabled INTEGER,delivery_disabled_auto INTEGER,created_at TEXT,last_seen_at TEXT,custom_sticker_file_id TEXT,university_subgroup INTEGER); CREATE TABLE accounts(id TEXT,notifications_enabled INTEGER); CREATE TABLE account_identities(account_id TEXT,platform TEXT,user_id INTEGER); CREATE TABLE account_subscriptions(account_id TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,group_name TEXT,schedule_id INTEGER,university_subgroup INTEGER,is_primary INTEGER,notifications_enabled INTEGER); INSERT INTO users(platform,user_id,subscription_type,subscription_key,group_name,schedule_id,is_admin,is_editor,homework_notifications_enabled,delivery_disabled_auto,created_at,last_seen_at) VALUES ('telegram',1,'group','group:1','G',1,0,0,1,0,'now','now'),('vk',2,'group','group:1','G',1,0,0,1,0,'now','now');").unwrap();
        let broadcast = Broadcast {
            event_id: "change-1",
            plain: "изменение",
            telegram: None,
            vk: None,
            campaign: "notification",
            target_platform: "all",
            audience: "all",
            schedule_id: Some(1),
            subscription_key: Some("group:1"),
            subgroup: None,
        };
        db.execute_batch("CREATE TRIGGER fail_vk BEFORE INSERT ON delivery_outbox WHEN NEW.platform='vk' BEGIN SELECT RAISE(ABORT,'failed'); END;").unwrap();
        assert!(enqueue_broadcast(&mut db, &broadcast, 100.0).is_err());
        let count: i64 = db
            .query_row("SELECT count(*) FROM delivery_outbox", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 0);
        db.execute_batch("DROP TRIGGER fail_vk;").unwrap();
        assert_eq!(enqueue_broadcast(&mut db, &broadcast, 100.0).unwrap(), 2);
        assert_eq!(enqueue_broadcast(&mut db, &broadcast, 100.0).unwrap(), 2);
        let count: i64 = db
            .query_row("SELECT count(*) FROM delivery_outbox", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 2);
    }
}

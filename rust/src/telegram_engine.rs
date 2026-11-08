use crate::{
    group_catalog::GroupCatalog,
    search::SearchCatalog,
    telegram_ui::{BotAction, SelectionFlow, SelectionRequest},
};
use rusqlite::{Connection, TransactionBehavior};
use serde_json::Value;

pub enum HandleOutcome {
    Handled,
    Unsupported,
}
#[derive(Default)]
pub struct TelegramEngine {
    selection: SelectionFlow,
}
pub struct EngineContext<'a> {
    pub settings: &'a crate::config::Settings,
    pub groups: &'a GroupCatalog,
    pub search: &'a SearchCatalog,
    pub group_snapshots: &'a [crate::ScheduleSnapshot],
    pub now: chrono::NaiveDateTime,
    pub timestamp: f64,
}
impl TelegramEngine {
    pub fn handle(
        &mut self,
        db: &mut Connection,
        update: &Value,
        context: &EngineContext<'_>,
    ) -> Result<HandleOutcome, Box<dyn std::error::Error>> {
        let Some(update_id) = update.get("update_id").and_then(Value::as_i64) else {
            return Ok(HandleOutcome::Unsupported);
        };
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let seen: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM telegram_processed_updates WHERE update_id=?)",
            [update_id],
            |r| r.get(0),
        )?;
        if seen {
            tx.commit()?;
            return Ok(HandleOutcome::Handled);
        }
        let mut selection = self.selection.clone();
        let now = context.now.format("%Y-%m-%dT%H:%M:%S").to_string();
        let actor = crate::telegram_ui::register_actor(
            &tx,
            update,
            &context.settings.admin_telegram_ids,
            &context.settings.limited_admin_telegram_ids,
            &now,
        )?;
        let Some(user) = actor else {
            return Ok(HandleOutcome::Unsupported);
        };
        let group_start = update
            .pointer("/message/chat/type")
            .and_then(Value::as_str)
            .is_some_and(|kind| matches!(kind, "group" | "supergroup"))
            && update
                .pointer("/message/text")
                .and_then(Value::as_str)
                .and_then(|text| text.split_whitespace().next())
                .and_then(|command| command.split('@').next())
                == Some("/start");
        if group_start {
            crate::telegram_ui::register_group_chat(&tx, update, &now)?;
        }

        let snapshot = if let (Some(key), Some(title)) = (
            user.subscription_key.as_deref(),
            user.subscription_title.as_deref(),
        ) {
            if key.is_empty() || title.is_empty() {
                None
            } else {
                crate::database::latest_snapshot(&tx, "current", user.schedule_id, Some(key))?
                    .map(|row| row.content)
            }
        } else {
            None
        };
        let university = crate::subgroups::is_university_group(Some(&user))
            || user
                .subscription_url
                .as_deref()
                .and_then(crate::sources::university_kind)
                .is_some();
        let callback = update.get("callback_query");
        let actions = if let Some(callback) = callback.filter(|v| {
            v.get("data")
                .and_then(Value::as_str)
                .is_some_and(|s| s.starts_with("teacher:"))
        }) {
            let Some(chat_id) = callback.pointer("/message/chat/id").and_then(Value::as_i64) else {
                return Ok(HandleOutcome::Unsupported);
            };
            let Some(callback_id) = callback.get("id").and_then(Value::as_str) else {
                return Ok(HandleOutcome::Unsupported);
            };
            selection.choose_in_transaction(
                &tx,
                chat_id,
                user.user_id,
                callback_id,
                callback["data"].as_str().unwrap(),
                &now,
            )?
        } else if let Some(message) = update.get("message").filter(|_| {
            user.subscription_key.as_ref().is_none_or(|s| s.is_empty())
                || user
                    .subscription_title
                    .as_ref()
                    .is_none_or(|s| s.is_empty())
        }) {
            let text = message.get("text").and_then(Value::as_str).unwrap_or("");
            let chat = message.pointer("/chat/id").and_then(Value::as_i64);
            let private = message.pointer("/chat/type").and_then(Value::as_str) == Some("private");
            if private && !text.is_empty() && !text.starts_with('/') {
                let Some(chat_id) = chat else {
                    return Ok(HandleOutcome::Unsupported);
                };
                selection.input_in_transaction(
                    &tx,
                    &SelectionRequest {
                        platform: "telegram",
                        chat_id,
                        user_id: user.user_id,
                        input: text,
                        groups: context.groups,
                        search: context.search,
                        snapshots: context.group_snapshots,
                        now: &now,
                    },
                )?
            } else {
                let Some(actions) = crate::telegram_ui::route_stateful_in_transaction(
                    &tx,
                    update,
                    snapshot.as_ref(),
                    context.now.date(),
                    university,
                    &context.settings.miniapp_url,
                    &now,
                )?
                else {
                    return Ok(HandleOutcome::Unsupported);
                };
                actions
            }
        } else {
            let Some(actions) = crate::telegram_ui::route_stateful_in_transaction(
                &tx,
                update,
                snapshot.as_ref(),
                context.now.date(),
                university,
                &context.settings.miniapp_url,
                &now,
            )?
            else {
                return Ok(HandleOutcome::Unsupported);
            };
            actions
        };
        enqueue_actions_in_transaction(&tx, update_id, &actions, context.timestamp)?;
        tx.execute(
            "INSERT INTO telegram_processed_updates(update_id,processed_at) VALUES (?,?)",
            rusqlite::params![update_id, context.timestamp],
        )?;
        tx.commit()?;
        self.selection = selection;
        Ok(HandleOutcome::Handled)
    }
}
pub fn ensure_schema(db: &Connection) -> rusqlite::Result<()> {
    db.execute_batch("CREATE TABLE IF NOT EXISTS telegram_processed_updates(update_id INTEGER PRIMARY KEY,processed_at REAL NOT NULL); CREATE INDEX IF NOT EXISTS idx_telegram_processed_at ON telegram_processed_updates(processed_at);")
}
pub fn enqueue_actions(
    db: &mut Connection,
    update_id: i64,
    actions: &[BotAction],
    now: f64,
) -> rusqlite::Result<()> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    enqueue_actions_in_transaction(&tx, update_id, actions, now)?;
    tx.commit()
}
pub fn enqueue_actions_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    update_id: i64,
    actions: &[BotAction],
    now: f64,
) -> rusqlite::Result<()> {
    for (index, action) in actions.iter().enumerate() {
        let id = format!("telegram-update:{update_id}:{index}");
        let peer = action
            .payload
            .get("chat_id")
            .and_then(|v| {
                v.as_i64()
                    .map(|n| n.to_string())
                    .or_else(|| v.as_str().map(str::to_owned))
            })
            .unwrap_or_else(|| format!("{}:{id}", action.method));
        let metadata = serde_json::json!({"campaign_type":"interactive"});
        let ttl = if action.method == "answerCallbackQuery" {
            8.0
        } else {
            86400.0
        };
        let item = crate::outbox::Delivery {
            id: &id,
            platform: "telegram",
            peer: &peer,
            method: &action.method,
            payload: &action.payload,
            metadata: &metadata,
            ttl,
        };
        crate::outbox::enqueue_in_transaction(tx, &[item], now)?;
    }
    Ok(())
}

pub fn cleanup_processed(db: &Connection, now: f64) -> rusqlite::Result<usize> {
    db.execute(
        "DELETE FROM telegram_processed_updates WHERE processed_at<?",
        [now - 90.0 * 86400.0],
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn start_registers_user_and_persists_response_once() {
        let mut db = Connection::open_in_memory().unwrap();
        db.execute_batch(crate::outbox::SCHEMA).unwrap();
        ensure_schema(&db).unwrap();
        db.execute_batch("CREATE TABLE users(platform TEXT,user_id INTEGER,username TEXT,full_name TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,audience_subscription_key TEXT,audience_subscription_title TEXT,audience_subscription_url TEXT,group_name TEXT,schedule_id INTEGER,is_admin INTEGER,is_editor INTEGER,homework_notifications_enabled INTEGER,delivery_disabled_auto INTEGER,created_at TEXT,last_seen_at TEXT,custom_sticker_file_id TEXT,university_subgroup INTEGER,PRIMARY KEY(platform,user_id));").unwrap();
        let settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        let groups = GroupCatalog::default();
        let search = SearchCatalog::default();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let context = EngineContext {
            settings: &settings,
            groups: &groups,
            search: &search,
            group_snapshots: &[],
            now,
            timestamp: 100.0,
        };
        let update = json!({"update_id":10,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42,"first_name":"Иван"},"text":"/start"}});
        let mut engine = TelegramEngine::default();
        assert!(matches!(
            engine.handle(&mut db, &update, &context).unwrap(),
            HandleOutcome::Handled
        ));
        assert!(matches!(
            engine.handle(&mut db, &update, &context).unwrap(),
            HandleOutcome::Handled
        ));
        assert_eq!(
            crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .full_name
                .as_deref(),
            Some("Иван")
        );
        let count: i64 = db
            .query_row("SELECT count(*) FROM delivery_outbox", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
        let row = crate::outbox::get(&db, "telegram-update:10:0")
            .unwrap()
            .unwrap();
        assert!(row.payload_json.contains("Укажи свою группу"));
        db.execute_batch("CREATE TABLE account_identities(account_id TEXT,platform TEXT,user_id INTEGER); CREATE TABLE accounts(id TEXT,theme TEXT,accent TEXT,density TEXT,reduced_motion INTEGER,notifications_enabled INTEGER,updated_at TEXT); CREATE TRIGGER fail_delivery BEFORE INSERT ON delivery_outbox BEGIN SELECT RAISE(ABORT,'failure'); END;").unwrap();
        let callback = json!({"update_id":11,"callback_query":{"id":"cb","from":{"id":42,"first_name":"Иван"},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:toggle_notifications"}});
        assert!(engine.handle(&mut db, &callback, &context).is_err());
        assert!(
            crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .homework_notifications_enabled
        );
        let marked: i64 = db
            .query_row(
                "SELECT count(*) FROM telegram_processed_updates WHERE update_id=11",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(marked, 0);
        db.execute_batch("DROP TRIGGER fail_delivery").unwrap();
        assert!(matches!(
            engine.handle(&mut db, &callback, &context).unwrap(),
            HandleOutcome::Handled
        ));
        assert!(matches!(
            engine.handle(&mut db, &callback, &context).unwrap(),
            HandleOutcome::Handled
        ));
        assert!(
            !crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .homework_notifications_enabled
        );
        let group = json!({"update_id":12,"message":{"chat":{"id":-100,"type":"supergroup","title":"Учебная"},"from":{"id":42,"first_name":"Иван"},"text":"/start"}});
        assert!(matches!(
            engine.handle(&mut db, &group, &context).unwrap(),
            HandleOutcome::Handled
        ));
        assert_eq!(
            crate::database::get_user(&db, "telegram", -100)
                .unwrap()
                .unwrap()
                .full_name
                .as_deref(),
            Some("TG group: Учебная")
        );
        assert_eq!(cleanup_processed(&db, 100.0 + 89.0 * 86400.0).unwrap(), 0);
        assert_eq!(cleanup_processed(&db, 100.0 + 91.0 * 86400.0).unwrap(), 3);
    }
}

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
    search: crate::telegram_search::SearchFlow,
    group: crate::telegram_group::GroupFlow,
    personalization: crate::telegram_personalization::PersonalizationFlow,
    audience: crate::telegram_audience::AudienceFlow,
}
pub struct EngineContext<'a> {
    pub settings: &'a crate::config::Settings,
    pub groups: &'a GroupCatalog,
    pub search: &'a SearchCatalog,
    pub group_snapshots: &'a [crate::ScheduleSnapshot],
    pub now: chrono::NaiveDateTime,
    pub timestamp: f64,
}
fn reset_user_id(update: &Value) -> Option<i64> {
    if update
        .pointer("/callback_query/data")
        .and_then(Value::as_str)
        == Some("menu:start")
    {
        return update
            .pointer("/callback_query/from/id")
            .and_then(Value::as_i64);
    }
    if update.pointer("/message/chat/type").and_then(Value::as_str) != Some("private") {
        return None;
    }
    let command = update
        .pointer("/message/text")
        .and_then(Value::as_str)
        .and_then(|text| text.split_whitespace().next())
        .and_then(|command| command.split('@').next());
    if matches!(command, Some("/start" | "/cancel")) {
        update.pointer("/message/from/id").and_then(Value::as_i64)
    } else {
        None
    }
}
impl TelegramEngine {
    pub fn handle(
        &mut self,
        db: &mut Connection,
        update: &Value,
        context: &EngineContext<'_>,
    ) -> Result<HandleOutcome, Box<dyn std::error::Error>> {
        self.handle_with_snapshot(db, update, context, None, None, None)
    }
    fn handle_with_snapshot(
        &mut self,
        db: &mut Connection,
        update: &Value,
        context: &EngineContext<'_>,
        resolved: Option<Option<&crate::ScheduleSnapshot>>,
        override_actions: Option<Vec<BotAction>>,
        group_access: Option<bool>,
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
        if let Some(actions) = crate::telegram_group::membership_actions(update) {
            enqueue_actions_in_transaction(&tx, update_id, &actions, context.timestamp)?;
            tx.execute(
                "INSERT INTO telegram_processed_updates(update_id,processed_at) VALUES (?,?)",
                rusqlite::params![update_id, context.timestamp],
            )?;
            tx.commit()?;
            return Ok(HandleOutcome::Handled);
        }
        let reset_user = reset_user_id(update);
        let mut selection = self.selection.clone();
        let mut search = reset_user
            .filter(|id| self.search.has_user(*id))
            .map(|_| self.search.clone());
        let mut group = self.group.interested(update).then(|| self.group.clone());
        let mut personalization = (self.personalization.interested(update)
            || reset_user.is_some_and(|id| self.personalization.has_user(id)))
        .then(|| self.personalization.clone());
        let mut audience = (self.audience.interested(update)
            || reset_user.is_some_and(|id| self.audience.has_user(id)))
        .then(|| self.audience.clone());
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
        if let Some(id) = reset_user {
            selection.reset_user(id);
            if let Some(search) = search.as_mut() {
                search.reset_user(id);
            }
            if let Some(personalization) = personalization.as_mut() {
                personalization.reset_user(id);
            }
            if let Some(audience) = audience.as_mut() {
                audience.reset_user(id);
            }
        }
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

        let snapshot = if let Some(resolved) = resolved {
            resolved.cloned()
        } else if let (Some(key), Some(title)) = (
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
        let personal_actions = match personalization.as_mut() {
            Some(flow) => flow.handle(&tx, update, &user)?,
            None => None,
        };
        let audience_actions = match audience.as_mut() {
            Some(flow) => flow.handle(&tx, update, &user, context, &now)?,
            None => None,
        };
        let actions = if let Some(actions) = override_actions {
            actions
        } else if let Some(actions) = audience_actions {
            actions
        } else if let Some(actions) = personal_actions {
            actions
        } else if let Some(actions) = match group.as_mut() {
            Some(group) => group.handle(&tx, update, group_access, context, &now)?,
            None => None,
        } {
            actions
        } else if let Some(actions) =
            crate::telegram_subgroup::handle(&tx, update, &user, snapshot.as_ref(), &now)?
        {
            actions
        } else if let Some(callback) = callback.filter(|v| {
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
        if let Some(search) = search {
            self.search = search;
        }
        if let Some(group) = group {
            self.group = group;
        }
        if let Some(personalization) = personalization {
            self.personalization = personalization;
        }
        if let Some(audience) = audience {
            self.audience = audience;
        }
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

pub enum PollError {
    Transport(crate::notification::DeliveryFailure),
    Handler(Box<dyn std::error::Error>),
}
pub enum PollOutcome {
    Processed(usize),
    Unsupported {
        processed: usize,
        update_id: Option<i64>,
    },
}
async fn week_actions(
    parser: &crate::parser::ScheduleParser,
    update: &Value,
    user: Option<&crate::UserRecord>,
    now: chrono::NaiveDateTime,
) -> Option<Vec<BotAction>> {
    let callback = update.get("callback_query")?;
    let id = callback.get("id")?.as_str()?;
    let chat_id = callback.pointer("/message/chat/id")?.as_i64()?;
    let data = callback
        .get("data")?
        .as_str()?
        .strip_prefix("schedule:week:")?;
    let result: Result<(String, String), Box<dyn std::error::Error>> = async {
        let (date, variant) = data.split_once(':').ok_or("Неверный вариант")?;
        let date = chrono::NaiveDate::parse_from_str(date, "%Y-%m-%d")?;
        let variant: u8 = variant.parse()?;
        let user = user.ok_or("Пользователь не найден")?;
        let url = if let Some(schedule_id) = user.schedule_id.filter(|id| *id < 0) {
            parser.build_schedule_url(schedule_id)?
        } else {
            user.subscription_url
                .as_deref()
                .filter(|url| crate::sources::university_kind(url).is_some())
                .ok_or("Источник недоступен")?
                .to_owned()
        };
        let snapshot = match variant {
            0 => parser.parse_from_url(&url, now).await?.0,
            1 | 2 => parser.parse_university_variant(&url, variant, date).await?,
            _ => return Err("Неверный вариант".into()),
        };
        let date_iso = date.to_string();
        let text = snapshot
            .days
            .iter()
            .find(|day| day.date_iso == date_iso)
            .map(|day| {
                crate::format::format_day(
                    day,
                    crate::subgroups::personal_subgroup(Some(user))
                        .and_then(|n| u32::try_from(n).ok()),
                    true,
                    now.date(),
                )
            })
            .unwrap_or_else(|| "На эту дату нет данных.".into());
        Ok((text, date_iso))
    }
    .await;
    let (text, keyboard) = match result {
        Ok((text, date)) => (text, crate::telegram_ui::university_keyboard(&date)),
        Err(_) => (
            "Не удалось загрузить вариант расписания. Попробуй позже.".into(),
            crate::notification::telegram_keyboard(),
        ),
    };
    Some(vec![
        BotAction {
            method: "sendMessage".into(),
            payload: serde_json::json!({"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": keyboard}),
        },
        BotAction {
            method: "answerCallbackQuery".into(),
            payload: serde_json::json!({"callback_query_id": id}),
        },
    ])
}
pub async fn poll_iteration(
    poll: &mut crate::telegram_poll::TelegramLongPoll,
    api: &crate::notification::ApiTransport,
    engine: &mut TelegramEngine,
    db: &mut Connection,
    context: &EngineContext<'_>,
) -> Result<PollOutcome, PollError> {
    poll_iteration_with_parser(poll, api, engine, db, context, None).await
}
pub async fn poll_iteration_with_parser(
    poll: &mut crate::telegram_poll::TelegramLongPoll,
    api: &crate::notification::ApiTransport,
    engine: &mut TelegramEngine,
    db: &mut Connection,
    context: &EngineContext<'_>,
    parser: Option<&crate::parser::ScheduleParser>,
) -> Result<PollOutcome, PollError> {
    let updates = poll.fetch(api).await.map_err(PollError::Transport)?;
    let mut processed = 0;
    for update in updates {
        let data = update
            .pointer("/callback_query/data")
            .and_then(Value::as_str);
        let command = update
            .pointer("/message/text")
            .and_then(Value::as_str)
            .and_then(|text| text.split_whitespace().next())
            .and_then(|text| text.split('@').next());
        let schedule_request = matches!(
            data,
            Some("schedule:today" | "schedule:tomorrow" | "schedule:day_after")
        );
        let needs_snapshot = matches!(
            data,
            Some(
                "schedule:today"
                    | "schedule:tomorrow"
                    | "schedule:day_after"
                    | "menu:start"
                    | "menu:subgroup"
            )
        ) || data.is_some_and(|data| data.starts_with("subgroup:"))
            || (matches!(command, Some("/start" | "/cancel"))
                && update.pointer("/message/chat/type").and_then(Value::as_str) == Some("private"));
        let actor_id = update
            .pointer("/callback_query/from/id")
            .or_else(|| update.pointer("/message/from/id"))
            .and_then(Value::as_i64);
        let is_week = data.is_some_and(|s| s.starts_with("schedule:week:"));
        let already_processed = match update.get("update_id").and_then(Value::as_i64) {
            Some(id) => db
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM telegram_processed_updates WHERE update_id=?)",
                    [id],
                    |row| row.get::<_, bool>(0),
                )
                .map_err(|err| PollError::Handler(Box::new(err)))?,
            _ => false,
        };
        let resolved = if let (Some(parser), Some(actor_id)) = (
            parser.filter(|_| needs_snapshot && !already_processed),
            actor_id,
        ) {
            let user = crate::database::get_user(db, "telegram", actor_id)
                .map_err(|err| PollError::Handler(Box::new(err)))?;
            match user {
                Some(user)
                    if schedule_request
                        || user.schedule_id.is_some_and(|id| id < 0)
                        || user
                            .subscription_url
                            .as_deref()
                            .and_then(crate::sources::university_kind)
                            .is_some() =>
                {
                    Some(
                        crate::snapshot_resolver::resolve(db, parser, &user, context.now)
                            .await
                            .map_err(PollError::Handler)?,
                    )
                }
                _ => None,
            }
        } else {
            None
        };
        let private_group_setup = crate::telegram_group::needs_bot_identity(&update);
        let private_setup_actions = if private_group_setup && !already_processed {
            let bot = api
                .send("telegram", "getMe", &serde_json::json!({}))
                .await
                .map_err(PollError::Transport)?;
            crate::telegram_group::private_setup_actions(
                &update,
                bot.get("username").and_then(Value::as_str).unwrap_or("bot"),
            )
        } else {
            None
        };
        let mut next_search = parser
            .filter(|_| {
                !already_processed
                    && !private_group_setup
                    && reset_user_id(&update).is_none()
                    && !engine.audience.takes_precedence(&update)
                    && engine.search.interested(&update)
            })
            .map(|_| engine.search.clone());
        let search_actions = if let (Some(parser), Some(search)) = (parser, next_search.as_mut()) {
            search
                .process(
                    db,
                    parser,
                    &update,
                    (context.groups, context.search, context.group_snapshots),
                    context.now,
                )
                .await
                .map_err(PollError::Handler)?
        } else {
            None
        };
        let week_actions = if let (Some(parser), Some(actor_id)) =
            (parser.filter(|_| is_week && !already_processed), actor_id)
        {
            let user = crate::database::get_user(db, "telegram", actor_id)
                .map_err(|err| PollError::Handler(Box::new(err)))?;
            week_actions(parser, &update, user.as_ref(), context.now).await
        } else {
            None
        };
        let group_access = if !already_processed {
            match engine.group.requires_authorization(&update) {
                Some((chat, actor)) => Some(
                    crate::telegram_group::check_admin(api, chat, actor)
                        .await
                        .map_err(PollError::Transport)?,
                ),
                None => None,
            }
        } else {
            None
        };
        match engine
            .handle_with_snapshot(
                db,
                &update,
                context,
                resolved.as_ref().map(Option::as_ref),
                private_setup_actions.or(search_actions).or(week_actions),
                group_access,
            )
            .map_err(PollError::Handler)?
        {
            HandleOutcome::Handled => {
                if let Some(search) = next_search {
                    engine.search = search;
                }
                if !poll.acknowledge(&update) {
                    return Ok(PollOutcome::Unsupported {
                        processed,
                        update_id: None,
                    });
                }
                processed += 1;
            }
            HandleOutcome::Unsupported => {
                return Ok(PollOutcome::Unsupported {
                    processed,
                    update_id: update.get("update_id").and_then(Value::as_i64),
                })
            }
        }
    }
    Ok(PollOutcome::Processed(processed))
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

#[cfg(test)]
mod selection_integration_tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn new_user_can_select_group_and_replayed_input_is_not_applied_twice() {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let groups = GroupCatalog::new(vec![crate::group_catalog::GroupInfo {
            department_id: 1,
            department_code: "ИСП".into(),
            department_name: "Отделение".into(),
            group_name: "ИСП-25".into(),
            schedule_id: Some(123),
            url: "http://college/rasp/123".into(),
        }]);
        let search = SearchCatalog::default();
        let settings = crate::config::Settings::from_map(&Default::default()).unwrap();
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
        let mut engine = TelegramEngine::default();
        let start = json!({"update_id":1,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42,"first_name":"Иван"},"text":"/start"}});
        assert!(matches!(
            engine.handle(&mut db, &start, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let input = json!({"update_id":2,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42,"first_name":"Иван"},"text":"ИCП 25"}});
        assert!(matches!(
            engine.handle(&mut db, &input, &context).unwrap(),
            HandleOutcome::Handled
        ));
        assert_eq!(
            crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .subscription_key
                .as_deref(),
            Some("group:123")
        );
        let count: i64 = db
            .query_row(
                "SELECT count(*) FROM account_subscriptions WHERE subscription_key='group:123'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
        assert!(matches!(
            engine.handle(&mut db, &input, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let count: i64 = db
            .query_row(
                "SELECT count(*) FROM delivery_outbox WHERE delivery_id LIKE 'telegram-update:2:%'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }
}

#[cfg(test)]
mod week_tests {
    use super::*;
    #[tokio::test]
    async fn invalid_week_request_returns_recoverable_response_and_acknowledges() {
        let parser = crate::parser::ScheduleParser::new(
            "",
            "",
            std::time::Duration::from_secs(1),
            1,
            std::time::Duration::ZERO,
        )
        .unwrap();
        let update = serde_json::json!({"callback_query":{"id":"cb","from":{"id":42},"message":{"chat":{"id":42}},"data":"schedule:week:2026-11-08:3"}});
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let actions = week_actions(&parser, &update, None, now).await.unwrap();
        assert!(actions[0].payload["text"]
            .as_str()
            .unwrap()
            .contains("Не удалось"));
        assert_eq!(actions[1].method, "answerCallbackQuery");
    }
}

#[cfg(test)]
mod help_integration_tests {
    use super::*;
    #[test]
    fn help_callback_queues_navigation_and_acknowledgement() {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        let groups = GroupCatalog::default();
        let search = SearchCatalog::default();
        let context = EngineContext {
            settings: &settings,
            groups: &groups,
            search: &search,
            group_snapshots: &[],
            now: chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
                .unwrap()
                .and_hms_opt(12, 0, 0)
                .unwrap(),
            timestamp: 100.0,
        };
        let update = serde_json::json!({"update_id":95,"callback_query":{"id":"cb","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:help"}});
        assert!(matches!(
            TelegramEngine::default()
                .handle(&mut db, &update, &context)
                .unwrap(),
            HandleOutcome::Handled
        ));
        assert!(crate::outbox::get(&db, "telegram-update:95:0")
            .unwrap()
            .unwrap()
            .payload_json
            .contains("help:group_setup"));
        assert!(crate::outbox::get(&db, "telegram-update:95:1")
            .unwrap()
            .is_some());
    }
}

#[cfg(test)]
mod membership_integration_tests {
    use super::*;
    #[test]
    fn bot_added_welcome_is_queued_once() {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        let groups = GroupCatalog::default();
        let search = SearchCatalog::default();
        let context = EngineContext {
            settings: &settings,
            groups: &groups,
            search: &search,
            group_snapshots: &[],
            now: chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
                .unwrap()
                .and_hms_opt(12, 0, 0)
                .unwrap(),
            timestamp: 100.0,
        };
        let update = serde_json::json!({"update_id":90,"my_chat_member":{"chat":{"id":-100},"old_chat_member":{"status":"left"},"new_chat_member":{"status":"member"}}});
        let mut engine = TelegramEngine::default();
        assert!(matches!(
            engine.handle(&mut db, &update, &context).unwrap(),
            HandleOutcome::Handled
        ));
        assert!(matches!(
            engine.handle(&mut db, &update, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let count:i64=db.query_row("SELECT count(*) FROM delivery_outbox WHERE delivery_id LIKE 'telegram-update:90:%'",[],|r|r.get(0)).unwrap();
        assert_eq!(count, 1);
    }
}

#[cfg(test)]
mod subgroup_integration_tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn subgroup_choice_and_reply_are_queued_atomically() {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,group_name,schedule_id,created_at,last_seen_at) VALUES ('telegram',42,'group','group:-123','Университет','Университет',-123,'now','now')",[]).unwrap();
        let snapshot = crate::ScheduleSnapshot {
            group_name: "Университет".into(),
            fetched_at: "2026-11-08".into(),
            days: vec![crate::DaySchedule {
                date_label: "8 ноября".into(),
                date_iso: "2026-11-08".into(),
                week_variant: Some(1),
                current_week_variant: Some(1),
                lessons: vec![crate::Lesson {
                    number: 1,
                    subject: "Физика (2 подгруппа)".into(),
                    teacher: "Иванов".into(),
                    classroom: "101".into(),
                }],
            }],
        };
        crate::database::save_snapshot(
            &mut db,
            &crate::database::SnapshotInput {
                snapshot_type: "current",
                hash: "h",
                snapshot: &snapshot,
                schedule_id: Some(-123),
                group_name: Some("Университет"),
                source_type: Some("group"),
                source_key: Some("group:-123"),
                source_title: Some("Университет"),
                source_url: None,
                archive_origin: None,
                archive_dates: None,
                archive_projected: false,
                timezone: chrono_tz::UTC,
                created_at: "now",
            },
        )
        .unwrap();
        let settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        let groups = GroupCatalog::default();
        let search = SearchCatalog::default();
        let context = EngineContext {
            settings: &settings,
            groups: &groups,
            search: &search,
            group_snapshots: &[],
            now: chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
                .unwrap()
                .and_hms_opt(12, 0, 0)
                .unwrap(),
            timestamp: 100.0,
        };
        let update = json!({"update_id":60,"callback_query":{"id":"cb","from":{"id":42},"message":{"chat":{"id":42}},"data":"subgroup:-123:2"}});
        let mut engine = TelegramEngine::default();
        db.execute_batch("CREATE TRIGGER fail_subgroup BEFORE INSERT ON delivery_outbox BEGIN SELECT RAISE(ABORT,'failure'); END;").unwrap();
        assert!(engine.handle(&mut db, &update, &context).is_err());
        assert_eq!(
            crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .university_subgroup,
            None
        );
        db.execute_batch("DROP TRIGGER fail_subgroup").unwrap();
        assert!(matches!(
            engine.handle(&mut db, &update, &context).unwrap(),
            HandleOutcome::Handled
        ));
        assert_eq!(
            crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .university_subgroup,
            Some(2)
        );
        assert!(crate::outbox::get(&db, "telegram-update:60:0")
            .unwrap()
            .is_some());
    }
}

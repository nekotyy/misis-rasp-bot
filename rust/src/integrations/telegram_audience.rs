use crate::{telegram_engine::EngineContext, telegram_ui::BotAction, UserRecord};
use rusqlite::Transaction;
use serde_json::{json, Value};
use std::collections::HashSet;

#[derive(Clone, Default)]
pub struct AudienceFlow {
    waiting: HashSet<i64>,
}

fn prompt(chat: i64, error: Option<&str>) -> BotAction {
    let mut text="<b>Укажи кабинет</b>\n\nНапиши кабинет точно в таком же формате, как на сайте расписания.\nНапример: <b>312</b>, <b>305/2</b>, <b>508/2М</b> или <b>с-з</b>.\n\nЭта подписка работает вместе с преподавателем и помогает быстрее замечать изменения по кабинету.".to_string();
    if let Some(error) = error {
        text.push_str("\n\n");
        text.push_str(error);
    }
    BotAction {
        method: "sendMessage".into(),
        payload: json!({"chat_id":chat,"text":text,"parse_mode":"HTML"}),
    }
}
fn private_start(update: &Value) -> bool {
    update.pointer("/message/chat/type").and_then(Value::as_str) == Some("private")
        && update
            .pointer("/message/text")
            .and_then(Value::as_str)
            .and_then(|text| text.split_whitespace().next())
            .and_then(|command| command.split('@').next())
            == Some("/start")
}
impl AudienceFlow {
    pub fn has_user(&self, user_id: i64) -> bool {
        self.waiting.contains(&user_id)
    }
    pub fn reset_user(&mut self, user_id: i64) {
        self.waiting.remove(&user_id);
    }
    pub fn takes_precedence(&self, update: &Value) -> bool {
        let Some(user) = update.pointer("/message/from/id").and_then(Value::as_i64) else {
            return false;
        };
        update.pointer("/message/chat/type").and_then(Value::as_str) == Some("private")
            && self.waiting.contains(&user)
            && update
                .pointer("/message/text")
                .and_then(Value::as_str)
                .is_some_and(|text| !text.starts_with('/'))
    }
    pub fn interested(&self, update: &Value) -> bool {
        if update
            .pointer("/callback_query/data")
            .and_then(Value::as_str)
            == Some("settings:audience_setup")
        {
            return true;
        }
        if update
            .pointer("/callback_query/data")
            .and_then(Value::as_str)
            == Some("menu:start")
        {
            return update
                .pointer("/callback_query/from/id")
                .and_then(Value::as_i64)
                .is_some_and(|id| self.waiting.contains(&id));
        }
        self.takes_precedence(update)
            || (private_start(update)
                && update
                    .pointer("/message/from/id")
                    .and_then(Value::as_i64)
                    .is_some_and(|id| self.waiting.contains(&id)))
    }
    pub fn handle(
        &mut self,
        tx: &Transaction<'_>,
        update: &Value,
        user: &UserRecord,
        context: &EngineContext<'_>,
        now: &str,
    ) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
        if private_start(update) {
            self.waiting.remove(&user.user_id);
            return Ok(None);
        }
        if let Some(callback) = update.get("callback_query") {
            let Some(data) = callback.get("data").and_then(Value::as_str) else {
                return Ok(None);
            };
            if data == "menu:start" {
                self.waiting.remove(&user.user_id);
                return Ok(None);
            }
            if data != "settings:audience_setup" {
                return Ok(None);
            }
            let Some(id) = callback.get("id").and_then(Value::as_str) else {
                return Ok(None);
            };
            let ack = || BotAction {
                method: "answerCallbackQuery".into(),
                payload: json!({"callback_query_id":id}),
            };
            let Some(chat) = callback.pointer("/message/chat/id").and_then(Value::as_i64) else {
                return Ok(Some(vec![ack()]));
            };
            if user.subscription_type.as_deref() != Some("teacher") {
                return Ok(Some(vec![BotAction {
                    method: "answerCallbackQuery".into(),
                    payload: json!({"callback_query_id":id,"text":"Эта настройка доступна после выбора преподавателя.","show_alert":true}),
                }]));
            }
            self.waiting.insert(user.user_id);
            return Ok(Some(vec![prompt(chat, None), ack()]));
        }
        if !self.takes_precedence(update) {
            return Ok(None);
        }
        let Some(chat) = update.pointer("/message/chat/id").and_then(Value::as_i64) else {
            return Ok(None);
        };
        let text = update
            .pointer("/message/text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let targets =
            context
                .search
                .find_all_with_snapshots(text, context.groups, context.group_snapshots);
        let [target] = targets.as_slice() else {
            return Ok(Some(vec![prompt(
                chat,
                Some("Кабинет не найден. Проверь написание и попробуй еще раз."),
            )]));
        };
        if target.kind != "audience" {
            return Ok(Some(vec![prompt(
                chat,
                Some("Кабинет не найден. Проверь написание и попробуй еще раз."),
            )]));
        }
        let key = crate::sources::source_key_from_url("audience", &target.url);
        crate::database::set_audience_subscription_in_transaction(
            tx,
            "telegram",
            user.user_id,
            &key,
            &target.title,
            &target.url,
            now,
        )?;
        self.waiting.remove(&user.user_id);
        let updated = crate::database::get_user(tx, "telegram", user.user_id)?
            .ok_or("Пользователь не найден")?;
        let current = if let Some(key) = updated
            .subscription_key
            .as_deref()
            .filter(|key| !key.is_empty())
        {
            crate::database::latest_snapshot(tx, "current", updated.schedule_id, Some(key))?
                .map(|row| row.content)
        } else {
            None
        };
        let week = crate::telegram_ui::snapshot_current_week(current.as_ref(), context.now.date());
        let university = updated
            .subscription_url
            .as_deref()
            .and_then(crate::sources::university_kind)
            .is_some();
        let text = crate::telegram_ui::welcome_text(Some(&updated), week, university);
        Ok(Some(vec![BotAction {
            method: "sendMessage".into(),
            payload: json!({"chat_id":chat,"text":text,"parse_mode":"HTML","reply_markup":crate::telegram_ui::start_keyboard(Some(&updated))}),
        }]))
    }
}

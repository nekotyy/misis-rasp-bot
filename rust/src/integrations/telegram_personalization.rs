use crate::{telegram_ui::BotAction, UserRecord};
use rusqlite::Transaction;
use serde_json::{json, Value};
use std::collections::HashSet;

#[derive(Clone, Default)]
pub struct PersonalizationFlow {
    waiting: HashSet<i64>,
}

fn screen(user: &UserRecord) -> (String, Value) {
    let has_sticker = user
        .custom_sticker_file_id
        .as_deref()
        .is_some_and(|s| !s.is_empty());
    let status = if has_sticker {
        "<b>Прикреплен</b>"
    } else {
        "<i>Не установлен</i>"
    };
    let text = format!("<b>Персонализация уведомлений</b>\n\n• Ваш стикер: {status}\n\nВы можете установить свой стикер. Он будет отправляться перед уведомлениями об изменениях и при вызове меню расписания.");
    let mut rows =
        vec![json!([{"text":"Установить стикер","callback_data":"settings:pers_set_sticker"}])];
    if has_sticker {
        rows.push(json!([{"text":"Предпросмотр стикера","callback_data":"settings:pers_preview_sticker"}]));
        rows.push(
            json!([{"text":"Сбросить стикер","callback_data":"settings:pers_clear_sticker"}]),
        );
    }
    rows.push(json!([{"text":"Назад","callback_data":"menu:settings"}]));
    (text, json!({"inline_keyboard":rows}))
}
fn ack(id: &str, text: Option<&str>) -> BotAction {
    let mut payload = json!({"callback_query_id":id});
    if let Some(text) = text {
        payload["text"] = json!(text);
    }
    BotAction {
        method: "answerCallbackQuery".into(),
        payload,
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
impl PersonalizationFlow {
    pub fn has_user(&self, user_id: i64) -> bool {
        self.waiting.contains(&user_id)
    }
    pub fn reset_user(&mut self, user_id: i64) {
        self.waiting.remove(&user_id);
    }
    pub fn interested(&self, update: &Value) -> bool {
        if let Some(callback) = update.get("callback_query") {
            let data = callback.get("data").and_then(Value::as_str).unwrap_or("");
            let user = callback.pointer("/from/id").and_then(Value::as_i64);
            return matches!(
                data,
                "settings:personalization"
                    | "settings:pers_set_sticker"
                    | "settings:pers_clear_sticker"
                    | "settings:pers_preview_sticker"
            ) || (data == "menu:start"
                && user.is_some_and(|id| self.waiting.contains(&id)));
        }
        let user = update.pointer("/message/from/id").and_then(Value::as_i64);
        if private_start(update) && user.is_some_and(|id| self.waiting.contains(&id)) {
            return true;
        }
        update
            .pointer("/message/sticker/file_id")
            .and_then(Value::as_str)
            .is_some()
    }
    pub fn handle(
        &mut self,
        tx: &Transaction<'_>,
        update: &Value,
        user: &UserRecord,
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
            if !matches!(
                data,
                "settings:personalization"
                    | "settings:pers_set_sticker"
                    | "settings:pers_clear_sticker"
                    | "settings:pers_preview_sticker"
            ) {
                return Ok(None);
            }
            let Some(id) = callback.get("id").and_then(Value::as_str) else {
                return Ok(None);
            };
            let Some(chat) = callback.pointer("/message/chat/id").and_then(Value::as_i64) else {
                return Ok(Some(vec![ack(id, None)]));
            };
            let Some(message_id) = callback
                .pointer("/message/message_id")
                .and_then(Value::as_i64)
            else {
                return Ok(Some(vec![ack(id, None)]));
            };
            if data == "settings:pers_set_sticker" {
                self.waiting.insert(user.user_id);
                return Ok(Some(vec![
                    BotAction {
                        method: "editMessageText".into(),
                        payload: json!({"chat_id":chat,"message_id":message_id,"text":"Пришли стикер, который будет высылаться перед уведомлениями и при вызове меню расписания:","parse_mode":"HTML","reply_markup":{"inline_keyboard":[[{"text":"Отмена","callback_data":"settings:personalization"}]]}}),
                    },
                    ack(id, None),
                ]));
            }
            if data == "settings:personalization" {
                self.waiting.remove(&user.user_id);
            }
            if data == "settings:pers_clear_sticker" {
                tx.execute("UPDATE users SET custom_sticker_file_id=NULL WHERE platform='telegram' AND user_id=?",[user.user_id])?;
            }
            let updated = crate::database::get_user(tx, "telegram", user.user_id)?
                .ok_or("Пользователь не найден")?;
            let (text, keyboard) = screen(&updated);
            if data == "settings:pers_preview_sticker" {
                if let Some(sticker) = updated.custom_sticker_file_id.filter(|s| !s.is_empty()) {
                    return Ok(Some(vec![
                        BotAction {
                            method: "sendSticker".into(),
                            payload: json!({"chat_id":chat,"sticker":sticker}),
                        },
                        BotAction {
                            method: "sendMessage".into(),
                            payload: json!({"chat_id":chat,"text":text,"parse_mode":"HTML","reply_markup":keyboard}),
                        },
                        ack(id, None),
                    ]));
                }
                return Ok(Some(vec![ack(id, None)]));
            }
            return Ok(Some(vec![
                BotAction {
                    method: "editMessageText".into(),
                    payload: json!({"chat_id":chat,"message_id":message_id,"text":text,"parse_mode":"HTML","reply_markup":keyboard}),
                },
                ack(
                    id,
                    if data == "settings:pers_clear_sticker" {
                        Some("Стикер сброшен")
                    } else {
                        None
                    },
                ),
            ]));
        }
        let Some(sticker) = update
            .pointer("/message/sticker/file_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        else {
            return Ok(None);
        };
        if !self.waiting.remove(&user.user_id) {
            return Ok(Some(vec![]));
        }
        let Some(chat) = update.pointer("/message/chat/id").and_then(Value::as_i64) else {
            return Ok(None);
        };
        tx.execute(
            "UPDATE users SET custom_sticker_file_id=? WHERE platform='telegram' AND user_id=?",
            rusqlite::params![sticker, user.user_id],
        )?;
        let updated = crate::database::get_user(tx, "telegram", user.user_id)?
            .ok_or("Пользователь не найден")?;
        let (text, keyboard) = screen(&updated);
        Ok(Some(vec![BotAction {
            method: "sendMessage".into(),
            payload: json!({"chat_id":chat,"text":text,"parse_mode":"HTML","reply_markup":keyboard}),
        }]))
    }
}

use crate::{
    format::escape_html, telegram_engine::EngineContext, telegram_ui::BotAction, UserRecord,
};
use regex::Regex;
use rusqlite::Transaction;
use serde_json::{json, Value};
use std::collections::HashMap;

#[derive(Clone, Default)]
pub struct ReportFlow {
    pub waiting: HashMap<i64, String>,
    pub drafts: HashMap<i64, (String, String)>,
}

fn report_title(kind: &str) -> &'static str {
    match kind {
        "bugreport" => "Сообщение об ошибке (баг-репорт)",
        "support" => "Запрос в службу поддержки",
        _ => "Обратная связь разработчику",
    }
}

fn report_prompt(kind: &str) -> &'static str {
    match kind {
        "bugreport" => "Опиши проблему, с которой ты столкнулся (укажи группу/преподавателя, что отображается неверно или не работает).\n\nОтправь текст сообщения следующим сообщением:",
        "support" => "Опиши твой вопрос или сложность в работе с ботом. Мы ответим прямо в этот диалог.\n\nОтправь текст сообщения следующим сообщением:",
        _ => "Напиши твоё сообщение, отзыв или предложение разработчику.\n\nОтправь текст сообщения следующим сообщением:",
    }
}

fn prompt_action(chat_id: i64, kind: &str) -> BotAction {
    let title = report_title(kind);
    let prompt = report_prompt(kind);
    BotAction {
        method: "sendMessage".into(),
        payload: json!({
            "chat_id": chat_id,
            "text": format!("<b>{title}</b>\n\n{prompt}"),
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "Отмена", "callback_data": "report:cancel"}]
                ]
            }
        }),
    }
}

fn draft_action(chat_id: i64, kind: &str, text: &str) -> BotAction {
    let title = report_title(kind);
    let escaped = escape_html(text);
    let preview = format!(
        "<b>Подтверждение отправки обращения</b>\n\n<b>Тип:</b> {title}\n\n<b>Текст обращения:</b>\n{escaped}\n\nПожалуйста, проверьте текст и подтвердите отправку нажатием кнопки:"
    );
    BotAction {
        method: "sendMessage".into(),
        payload: json!({
            "chat_id": chat_id,
            "text": preview,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "Отправить", "callback_data": "report:confirm"},
                        {"text": "Отмена", "callback_data": "report:cancel"}
                    ]
                ]
            }
        }),
    }
}

fn reply_help_action(chat_id: i64) -> BotAction {
    BotAction {
        method: "sendMessage".into(),
        payload: json!({
            "chat_id": chat_id,
            "text": "<b>Использование команды ответа:</b>\n\n• <code>/reply tg &lt;user_id&gt; &lt;текст ответа&gt;</code>\n• <code>/reply vk &lt;user_id&gt; &lt;текст ответа&gt;</code>\n• <code>/reply &lt;номер_обращения_id&gt; &lt;текст ответа&gt;</code>\n\n<i>Либо просто ответьте (Reply) на сообщение уведомления от бота.</i>",
            "parse_mode": "HTML"
        }),
    }
}

impl ReportFlow {
    pub fn has_user(&self, user_id: i64) -> bool {
        self.waiting.contains_key(&user_id) || self.drafts.contains_key(&user_id)
    }

    pub fn reset_user(&mut self, user_id: i64) {
        self.waiting.remove(&user_id);
        self.drafts.remove(&user_id);
    }

    pub fn takes_precedence(&self, update: &Value) -> bool {
        let Some(user) = update.pointer("/message/from/id").and_then(Value::as_i64) else {
            return false;
        };
        update.pointer("/message/chat/type").and_then(Value::as_str) == Some("private")
            && self.waiting.contains_key(&user)
            && update
                .pointer("/message/text")
                .and_then(Value::as_str)
                .is_some_and(|text| !text.starts_with('/') || text == "/cancel")
    }

    pub fn interested(&self, update: &Value) -> bool {
        if let Some(data) = update
            .pointer("/callback_query/data")
            .and_then(Value::as_str)
        {
            if matches!(
                data,
                "settings:bugreport"
                    | "settings:feedback"
                    | "settings:support"
                    | "report:confirm"
                    | "report:cancel"
            ) {
                return true;
            }
        }
        if let Some(text) = update.pointer("/message/text").and_then(Value::as_str) {
            let command = text
                .split_whitespace()
                .next()
                .and_then(|c| c.split('@').next());
            if matches!(
                command,
                Some("/bugreport" | "/feedback" | "/support" | "/reply")
            ) {
                return true;
            }
        }
        if self.takes_precedence(update) {
            return true;
        }
        if update.pointer("/message/reply_to_message").is_some()
            && update.pointer("/message/chat/type").and_then(Value::as_str) == Some("private")
            && update
                .pointer("/message/text")
                .and_then(Value::as_str)
                .is_some_and(|t| !t.starts_with('/'))
        {
            return true;
        }
        false
    }

    pub fn handle(
        &mut self,
        tx: &Transaction<'_>,
        update: &Value,
        user: &UserRecord,
        context: &EngineContext<'_>,
        now: &str,
    ) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
        if let Some(callback) = update.get("callback_query") {
            let Some(data) = callback.get("data").and_then(Value::as_str) else {
                return Ok(None);
            };
            let callback_id = callback.get("id").and_then(Value::as_str).unwrap_or("");
            let chat_id = callback
                .pointer("/message/chat/id")
                .and_then(Value::as_i64)
                .unwrap_or(user.user_id);
            let message_id = callback
                .pointer("/message/message_id")
                .and_then(Value::as_i64);

            if matches!(
                data,
                "settings:bugreport" | "settings:feedback" | "settings:support"
            ) {
                let kind = data.strip_prefix("settings:").unwrap();
                self.waiting.insert(user.user_id, kind.to_string());
                let ack = BotAction {
                    method: "answerCallbackQuery".into(),
                    payload: json!({"callback_query_id": callback_id}),
                };
                if message_id.is_some() {
                    return Ok(Some(vec![prompt_action(chat_id, kind), ack]));
                }
                return Ok(Some(vec![ack]));
            }

            if data == "report:cancel" {
                self.reset_user(user.user_id);
                let ack = BotAction {
                    method: "answerCallbackQuery".into(),
                    payload: json!({"callback_query_id": callback_id, "text": "Отменено"}),
                };
                if let Some(mid) = message_id {
                    let edit = BotAction {
                        method: "editMessageText".into(),
                        payload: json!({
                            "chat_id": chat_id,
                            "message_id": mid,
                            "text": "Отправка обращения отменена.",
                            "parse_mode": "HTML"
                        }),
                    };
                    return Ok(Some(vec![edit, ack]));
                }
                return Ok(Some(vec![ack]));
            }

            if data == "report:confirm" {
                let Some((kind, text)) = self.drafts.remove(&user.user_id) else {
                    return Ok(Some(vec![BotAction {
                        method: "answerCallbackQuery".into(),
                        payload: json!({
                            "callback_query_id": callback_id,
                            "text": "Черновик обращения не найден",
                            "show_alert": true
                        }),
                    }]));
                };
                self.waiting.remove(&user.user_id);

                let display_name = user
                    .full_name
                    .as_deref()
                    .or(user.username.as_deref())
                    .unwrap_or("");
                let fb_id = crate::database::create_feedback_message(
                    tx,
                    "telegram",
                    user.user_id,
                    Some(display_name),
                    &kind,
                    &text,
                    now,
                )?;

                let type_title = match kind.as_str() {
                    "bugreport" => "Баг-репорт",
                    "support" => "Запрос в службу поддержки",
                    _ => "Сообщение обратной связи",
                };

                let ack = BotAction {
                    method: "answerCallbackQuery".into(),
                    payload: json!({"callback_query_id": callback_id, "text": "Отправлено!"}),
                };

                let mut actions = Vec::new();
                if let Some(mid) = message_id {
                    actions.push(BotAction {
                        method: "editMessageText".into(),
                        payload: json!({
                            "chat_id": chat_id,
                            "message_id": mid,
                            "text": format!("<b>{type_title} успешно отправлен!</b>\n\nСпасибо за помощь в улучшении сервиса расписания."),
                            "parse_mode": "HTML"
                        }),
                    });
                }
                actions.push(ack);

                let admin_type_title = match kind.as_str() {
                    "bugreport" => "<b>Новый баг-репорт</b>",
                    "support" => "<b>Запрос в службу поддержки</b>",
                    _ => "<b>Новое сообщение обратной связи</b>",
                };
                let user_link = if let Some(username) =
                    user.username.as_deref().filter(|s| !s.is_empty())
                {
                    format!("<a href=\"https://t.me/{username}\">t.me/{username}</a> (ID: <code>{}</code>)", user.user_id)
                } else {
                    format!(
                        "<code>{}</code> (<a href=\"tg://user?id={}\">профиль</a>)",
                        user.user_id, user.user_id
                    )
                };
                let escaped_text = escape_html(&text);
                let admin_msg = format!(
                    "{admin_type_title} [#{fb_id}]\n\n<b>От:</b> {user_link} (ID: <code>{}</code>, Telegram)\n<b>Время:</b> {now}\n\n<b>Текст:</b>\n{escaped_text}\n\n──────────────────\n<b>Чтобы ответить:</b>\n• Ответьте (Reply) на это сообщение\n• Или командой: <code>/reply tg {} &lt;текст ответа&gt;</code>",
                    user.user_id,
                    user.user_id,
                );

                for admin_id in &context.settings.admin_telegram_ids {
                    actions.push(BotAction {
                        method: "sendMessage".into(),
                        payload: json!({
                            "chat_id": admin_id,
                            "text": admin_msg,
                            "parse_mode": "HTML"
                        }),
                    });
                }

                return Ok(Some(actions));
            }
        }

        let Some(message) = update.get("message") else {
            return Ok(None);
        };
        let chat_id = message
            .pointer("/chat/id")
            .and_then(Value::as_i64)
            .unwrap_or(user.user_id);
        let raw_text = message
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();

        // 1. Обработка ожидаемого ввода текста обращения
        if self.waiting.contains_key(&user.user_id) {
            if raw_text == "/cancel" || raw_text.eq_ignore_ascii_case("отмена") {
                self.reset_user(user.user_id);
                return Ok(Some(vec![BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": chat_id,
                        "text": "Отправка обращения отменена.",
                        "parse_mode": "HTML"
                    }),
                }]));
            }
            if raw_text.starts_with('/') {
                return Ok(None);
            }
            let kind = self.waiting.remove(&user.user_id).unwrap();
            self.drafts
                .insert(user.user_id, (kind.clone(), raw_text.to_string()));
            return Ok(Some(vec![draft_action(chat_id, &kind, raw_text)]));
        }

        // 2. Команды /bugreport, /feedback, /support
        let command = raw_text
            .split_whitespace()
            .next()
            .and_then(|c| c.split('@').next());
        if matches!(command, Some("/bugreport" | "/feedback" | "/support")) {
            let cmd = command.unwrap();
            let kind = match cmd {
                "/bugreport" => "bugreport",
                "/support" => "support",
                _ => "feedback",
            };
            let arg = raw_text
                .split_once(char::is_whitespace)
                .map(|x| x.1)
                .map(str::trim)
                .unwrap_or("");
            if !arg.is_empty() {
                self.waiting.remove(&user.user_id);
                self.drafts
                    .insert(user.user_id, (kind.to_string(), arg.to_string()));
                return Ok(Some(vec![draft_action(chat_id, kind, arg)]));
            }
            self.waiting.insert(user.user_id, kind.to_string());
            return Ok(Some(vec![prompt_action(chat_id, kind)]));
        }

        // 3. Команда /reply для администратора
        if command == Some("/reply") {
            if !context.settings.admin_telegram_ids.contains(&user.user_id) {
                return Ok(None);
            }
            let parts: Vec<&str> = raw_text.split_whitespace().collect();
            if parts.len() < 3 {
                return Ok(Some(vec![reply_help_action(chat_id)]));
            }
            let target_kind = parts[1].trim_start_matches('#');
            if target_kind.eq_ignore_ascii_case("tg")
                || target_kind.eq_ignore_ascii_case("telegram")
            {
                let Some(target_uid) = parts.get(2).and_then(|s| s.parse::<i64>().ok()) else {
                    return Ok(Some(vec![reply_help_action(chat_id)]));
                };
                let reply_body = raw_text
                    .splitn(4, char::is_whitespace)
                    .nth(3)
                    .unwrap_or("")
                    .trim();
                if reply_body.is_empty() {
                    return Ok(Some(vec![reply_help_action(chat_id)]));
                }

                if let Some(latest) =
                    crate::database::get_latest_open_feedback(tx, "telegram", target_uid)?
                {
                    crate::database::answer_feedback_message(tx, latest.id, reply_body, now)?;
                }

                let user_msg = BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": target_uid,
                        "text": format!("<b>Ответ администратора на обращение:</b>\n\n{}", escape_html(reply_body)),
                        "parse_mode": "HTML"
                    }),
                };
                let admin_confirm = BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": chat_id,
                        "text": format!("Ответ успешно отправлен пользователю <code>{target_uid}</code> (telegram)!"),
                        "parse_mode": "HTML"
                    }),
                };
                return Ok(Some(vec![user_msg, admin_confirm]));
            }

            if let Ok(feedback_id) = target_kind.parse::<i64>() {
                let reply_body = raw_text
                    .splitn(3, char::is_whitespace)
                    .nth(2)
                    .unwrap_or("")
                    .trim();
                if reply_body.is_empty() {
                    return Ok(Some(vec![reply_help_action(chat_id)]));
                }
                let Some(fb) = crate::database::get_feedback_message(tx, feedback_id)? else {
                    return Ok(Some(vec![BotAction {
                        method: "sendMessage".into(),
                        payload: json!({
                            "chat_id": chat_id,
                            "text": format!("Обращение #{feedback_id} не найдено."),
                            "parse_mode": "HTML"
                        }),
                    }]));
                };

                crate::database::answer_feedback_message(tx, feedback_id, reply_body, now)?;

                let mut actions = Vec::new();
                if fb.platform == "telegram" {
                    actions.push(BotAction {
                        method: "sendMessage".into(),
                        payload: json!({
                            "chat_id": fb.user_id,
                            "text": format!("<b>Ответ администратора на обращение:</b>\n\n{}", escape_html(reply_body)),
                            "parse_mode": "HTML"
                        }),
                    });
                }
                actions.push(BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": chat_id,
                        "text": format!("Ответ успешно отправлен пользователю <code>{}</code> ({})!", fb.user_id, fb.platform),
                        "parse_mode": "HTML"
                    }),
                });
                return Ok(Some(actions));
            }

            return Ok(Some(vec![reply_help_action(chat_id)]));
        }

        // 4. Ответ администратора через Reply на сообщение бота
        if let Some(reply_to) = message.get("reply_to_message") {
            if context.settings.admin_telegram_ids.contains(&user.user_id)
                && !raw_text.starts_with('/')
            {
                let orig_text = reply_to.get("text").and_then(Value::as_str).unwrap_or("");
                let re_id = Regex::new(r"\[#(\d+)\]").unwrap();
                if let Some(caps) = re_id.captures(orig_text) {
                    if let Ok(feedback_id) = caps[1].parse::<i64>() {
                        if let Some(fb) = crate::database::get_feedback_message(tx, feedback_id)? {
                            crate::database::answer_feedback_message(
                                tx,
                                feedback_id,
                                raw_text,
                                now,
                            )?;
                            let mut actions = Vec::new();
                            if fb.platform == "telegram" {
                                actions.push(BotAction {
                                    method: "sendMessage".into(),
                                    payload: json!({
                                        "chat_id": fb.user_id,
                                        "text": format!("<b>Ответ администратора на обращение:</b>\n\n{}", escape_html(raw_text)),
                                        "parse_mode": "HTML"
                                    }),
                                });
                            }
                            actions.push(BotAction {
                                method: "sendMessage".into(),
                                payload: json!({
                                    "chat_id": chat_id,
                                    "text": format!("Ответ успешно отправлен пользователю <code>{}</code> ({})!", fb.user_id, fb.platform),
                                    "parse_mode": "HTML"
                                }),
                            });
                            return Ok(Some(actions));
                        }
                    }
                }

                let re_user =
                    Regex::new(r"ID:\s*<code>(\d+)</code>.*?(Telegram|VK|ВКонтакте)").unwrap();
                if let Some(caps) = re_user.captures(orig_text) {
                    if let Ok(target_uid) = caps[1].parse::<i64>() {
                        let platform_raw = caps[2].to_lowercase();
                        let target_platform = if platform_raw.contains("vk")
                            || platform_raw.contains("вконтакте")
                        {
                            "vk"
                        } else {
                            "telegram"
                        };
                        if let Some(latest) = crate::database::get_latest_open_feedback(
                            tx,
                            target_platform,
                            target_uid,
                        )? {
                            crate::database::answer_feedback_message(tx, latest.id, raw_text, now)?;
                        }
                        let mut actions = Vec::new();
                        if target_platform == "telegram" {
                            actions.push(BotAction {
                                method: "sendMessage".into(),
                                payload: json!({
                                    "chat_id": target_uid,
                                    "text": format!("<b>Ответ администратора на обращение:</b>\n\n{}", escape_html(raw_text)),
                                    "parse_mode": "HTML"
                                }),
                            });
                        }
                        actions.push(BotAction {
                            method: "sendMessage".into(),
                            payload: json!({
                                "chat_id": chat_id,
                                "text": format!("Ответ успешно отправлен пользователю <code>{target_uid}</code> ({target_platform})!"),
                                "parse_mode": "HTML"
                            }),
                        });
                        return Ok(Some(actions));
                    }
                }
            }
        }

        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        config::Settings, group_catalog::GroupCatalog, search::SearchCatalog,
        telegram_engine::TelegramEngine,
    };
    use rusqlite::Connection;

    fn setup() -> (Connection, Settings, GroupCatalog, SearchCatalog) {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let mut env = HashMap::new();
        env.insert("ADMIN_TELEGRAM_ID".into(), "100, 200".into());
        let settings = Settings::from_map(&env).unwrap();
        let groups = GroupCatalog::default();
        let search = SearchCatalog::default();
        (db, settings, groups, search)
    }

    #[test]
    fn user_report_flow_persists_in_database_and_notifies_admins() {
        let (mut db, settings, groups, search) = setup();
        let mut engine = TelegramEngine::default();
        let now_dt = chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let context = EngineContext {
            settings: &settings,
            groups: &groups,
            search: &search,
            group_snapshots: &[],
            now: now_dt,
            timestamp: 100.0,
        };

        // 1. Пользователь нажимает кнопку "Сообщить о баге" в настройках
        let cb_prompt = json!({
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 42, "username": "student", "first_name": "Иван"},
                "message": {"message_id": 10, "chat": {"id": 42}},
                "data": "settings:bugreport"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &cb_prompt, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));
        let prompt_action = crate::outbox::get(&db, "telegram-update:1:0")
            .unwrap()
            .unwrap();
        assert!(prompt_action.payload_json.contains("баг-репорт"));
        assert!(prompt_action.payload_json.contains("report:cancel"));

        // 2. Пользователь отправляет текст баг-репорта
        let text_input = json!({
            "update_id": 2,
            "message": {
                "message_id": 11,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "username": "student", "first_name": "Иван"},
                "text": "Не отображается пара по физике"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &text_input, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));
        let draft_action = crate::outbox::get(&db, "telegram-update:2:0")
            .unwrap()
            .unwrap();
        assert!(draft_action.payload_json.contains("Подтверждение отправки"));
        assert!(draft_action.payload_json.contains("report:confirm"));
        assert!(draft_action
            .payload_json
            .contains("Не отображается пара по физике"));

        // 3. Пользователь подтверждает отправку
        let cb_confirm = json!({
            "update_id": 3,
            "callback_query": {
                "id": "cb2",
                "from": {"id": 42, "username": "student", "first_name": "Иван"},
                "message": {"message_id": 12, "chat": {"id": 42}},
                "data": "report:confirm"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &cb_confirm, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));

        // Проверяем, что в feedback_messages появилась запись
        let fb = crate::database::get_feedback_message(&db, 1)
            .unwrap()
            .unwrap();
        assert_eq!(fb.user_id, 42);
        assert_eq!(fb.message_type, "bugreport");
        assert_eq!(fb.message_text, "Не отображается пара по физике");
        assert_eq!(fb.status, "open");

        // Проверяем действия в outbox: редактирование сообщения пользователя, answerCallbackQuery и отправку обоим админам (100 и 200)
        let user_edit = crate::outbox::get(&db, "telegram-update:3:0")
            .unwrap()
            .unwrap();
        assert_eq!(user_edit.method, "editMessageText");
        assert!(user_edit
            .payload_json
            .contains("Баг-репорт успешно отправлен!"));

        let ack = crate::outbox::get(&db, "telegram-update:3:1")
            .unwrap()
            .unwrap();
        assert_eq!(ack.method, "answerCallbackQuery");

        let admin1 = crate::outbox::get(&db, "telegram-update:3:2")
            .unwrap()
            .unwrap();
        assert_eq!(admin1.method, "sendMessage");
        assert!(admin1.payload_json.contains("100"));
        assert!(admin1.payload_json.contains("[#1]"));
        assert!(admin1
            .payload_json
            .contains("Не отображается пара по физике"));

        let admin2 = crate::outbox::get(&db, "telegram-update:3:3")
            .unwrap()
            .unwrap();
        assert_eq!(admin2.method, "sendMessage");
        assert!(admin2.payload_json.contains("200"));

        // 4. Админ 100 отвечает на обращение через команду /reply
        let reply_cmd = json!({
            "update_id": 4,
            "message": {
                "message_id": 20,
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 100},
                "text": "/reply 1 Поправили в расписании!"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &reply_cmd, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));

        // Проверяем, что обращение помечено отвеченным
        let updated_fb = crate::database::get_feedback_message(&db, 1)
            .unwrap()
            .unwrap();
        assert_eq!(updated_fb.status, "answered");
        assert_eq!(
            updated_fb.reply_text.as_deref(),
            Some("Поправили в расписании!")
        );

        // Проверяем отправку ответа пользователю и подтверждение админу
        let user_ans = crate::outbox::get(&db, "telegram-update:4:0")
            .unwrap()
            .unwrap();
        assert!(user_ans.payload_json.contains("42"));
        assert!(user_ans.payload_json.contains("Поправили в расписании!"));

        let admin_confirm = crate::outbox::get(&db, "telegram-update:4:1")
            .unwrap()
            .unwrap();
        assert!(admin_confirm.payload_json.contains("100"));
        assert!(admin_confirm
            .payload_json
            .contains("Ответ успешно отправлен"));
    }

    #[test]
    fn admin_reply_via_message_quote_works_and_non_admin_is_ignored() {
        let (mut db, settings, groups, search) = setup();
        let mut engine = TelegramEngine::default();
        let now_dt = chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let context = EngineContext {
            settings: &settings,
            groups: &groups,
            search: &search,
            group_snapshots: &[],
            now: now_dt,
            timestamp: 100.0,
        };

        // Создаем открытое обращение в базе
        let tx = db.transaction().unwrap();
        let fb_id = crate::database::create_feedback_message(
            &tx,
            "telegram",
            55,
            Some("Пользователь"),
            "feedback",
            "Спасибо за бота!",
            "now",
        )
        .unwrap();
        tx.commit().unwrap();

        // Не-админ пытается выполнить /reply -> Unsupported
        let non_admin_reply = json!({
            "update_id": 10,
            "message": {
                "chat": {"id": 55, "type": "private"},
                "from": {"id": 55},
                "text": "/reply 1 Ответ"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &non_admin_reply, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Unsupported
        ));

        // Админ отвечает через Reply на уведомление бота с [#1]
        let admin_quote_reply = json!({
            "update_id": 11,
            "message": {
                "message_id": 50,
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 100},
                "text": "Рады стараться!",
                "reply_to_message": {
                    "message_id": 45,
                    "text": format!("Новое сообщение обратной связи [#{fb_id}]\nОт: Полезный (ID: 55)")
                }
            }
        });
        assert!(matches!(
            engine
                .handle(&mut db, &admin_quote_reply, &context)
                .unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));

        let updated_fb = crate::database::get_feedback_message(&db, fb_id)
            .unwrap()
            .unwrap();
        assert_eq!(updated_fb.status, "answered");
        assert_eq!(updated_fb.reply_text.as_deref(), Some("Рады стараться!"));

        let user_ans = crate::outbox::get(&db, "telegram-update:11:0")
            .unwrap()
            .unwrap();
        assert!(user_ans.payload_json.contains("55"));
        assert!(user_ans.payload_json.contains("Рады стараться!"));
    }
}

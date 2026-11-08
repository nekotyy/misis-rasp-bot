use crate::{
    group_catalog::GroupCatalog,
    parser::ScheduleParser,
    search::{SearchCatalog, SearchTarget},
    telegram_ui::BotAction,
};
use rusqlite::Connection;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

#[derive(Clone)]
struct Choices {
    token: String,
    targets: Vec<SearchTarget>,
}
#[derive(Clone, Default)]
pub struct SearchFlow {
    pending: HashSet<(i64, i64)>,
    choices: HashMap<(i64, i64), Choices>,
}
fn message(chat_id: i64, text: &str, keyboard: Option<Value>) -> BotAction {
    let mut payload = json!({"chat_id":chat_id,"text":text,"parse_mode":"HTML"});
    if let Some(keyboard) = keyboard {
        payload["reply_markup"] = keyboard;
    }
    BotAction {
        method: "sendMessage".into(),
        payload,
    }
}
fn ack(id: &str) -> BotAction {
    BotAction {
        method: "answerCallbackQuery".into(),
        payload: json!({"callback_query_id":id}),
    }
}
fn prompt(chat: i64) -> BotAction {
    message(chat, "<b>Поиск расписания</b>\n\nПоиск осуществляется по группам, преподавателям и аудиториям!\n\nНапиши группу, фамилию преподавателя или аудиторию.", Some(json!({"inline_keyboard":[[{"text":"Назад","callback_data":"menu:start"}]]})))
}
impl SearchFlow {
    pub fn interested(&self, update: &Value) -> bool {
        if let Some(callback) = update.get("callback_query") {
            let data = callback.get("data").and_then(Value::as_str).unwrap_or("");
            if data == "schedule:find" {
                return true;
            }
            let key = callback
                .pointer("/message/chat/id")
                .and_then(Value::as_i64)
                .zip(callback.pointer("/from/id").and_then(Value::as_i64));
            return key.is_some_and(|key| {
                (data == "menu:start"
                    && (self.pending.contains(&key) || self.choices.contains_key(&key)))
                    || (data.starts_with("teacher:") && self.choices.contains_key(&key))
            });
        }
        let Some(message) = update.get("message") else {
            return false;
        };
        message
            .pointer("/chat/id")
            .and_then(Value::as_i64)
            .zip(message.pointer("/from/id").and_then(Value::as_i64))
            .is_some_and(|key| self.pending.contains(&key))
    }
    fn choices_screen(&self, key: (i64, i64), page: usize) -> Option<BotAction> {
        let choices = self.choices.get(&key)?;
        let start = page.checked_mul(8)?;
        if start >= choices.targets.len() {
            return None;
        }
        let mut rows: Vec<Value> = choices.targets.iter().enumerate().skip(start).take(8).map(|(i, target)| json!([{"text":crate::search::choice_label(target, &choices.targets),"callback_data":format!("teacher:pick:{}:{i}", choices.token)}])).collect();
        let mut nav = vec![];
        if page > 0 {
            nav.push(json!({"text":"Предыдущая страница","callback_data":format!("teacher:page:{}:{}", choices.token,page-1)}));
        }
        if start + 8 < choices.targets.len() {
            nav.push(json!({"text":"Следующая страница","callback_data":format!("teacher:page:{}:{}", choices.token,page+1)}));
        }
        if !nav.is_empty() {
            rows.push(Value::Array(nav));
        }
        rows.push(json!([{"text":"Назад","callback_data":"menu:start"}]));
        let text = if choices.targets[0].kind == "audience" {
            "Нашлось несколько аудиторий. Выбери нужную кнопку или уточни запрос."
        } else {
            "Нашлось несколько преподавателей. Какой именно нужен?\nВыбери преподавателя кнопкой или уточни запрос."
        };
        Some(message(key.0, text, Some(json!({"inline_keyboard":rows}))))
    }
    async fn show_target(
        &mut self,
        db: &Connection,
        parser: &ScheduleParser,
        key: (i64, i64),
        target: &SearchTarget,
        now: chrono::NaiveDateTime,
    ) -> Result<Vec<BotAction>, Box<dyn std::error::Error>> {
        match crate::search_preview::resolve(db, parser, target, now).await {
            Ok(snapshot) => {
                self.pending.remove(&key);
                self.choices.remove(&key);
                let text = crate::format::format_search_snapshot(
                    &target.title,
                    &snapshot,
                    now.date(),
                    true,
                );
                Ok(vec![message(
                    key.0,
                    &text,
                    Some(
                        json!({"inline_keyboard":[[{"text":"Найти расписание","callback_data":"schedule:find"}],[{"text":"Назад","callback_data":"menu:start"}]]}),
                    ),
                )])
            }
            Err(error) if error.downcast_ref::<rusqlite::Error>().is_some() => Err(error),
            Err(_) => Ok(vec![message(
                key.0,
                "Сайт расписания временно недоступен. Попробуй еще раз через минуту.",
                None,
            )]),
        }
    }
    pub async fn process(
        &mut self,
        db: &Connection,
        parser: &ScheduleParser,
        update: &Value,
        catalogs: (&GroupCatalog, &SearchCatalog, &[crate::ScheduleSnapshot]),
        now: chrono::NaiveDateTime,
    ) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
        let (groups, catalog, snapshots) = catalogs;
        if let Some(callback) = update.get("callback_query") {
            let Some(chat) = callback.pointer("/message/chat/id").and_then(Value::as_i64) else {
                return Ok(None);
            };
            let Some(user) = callback.pointer("/from/id").and_then(Value::as_i64) else {
                return Ok(None);
            };
            let Some(id) = callback.get("id").and_then(Value::as_str) else {
                return Ok(None);
            };
            let Some(data) = callback.get("data").and_then(Value::as_str) else {
                return Ok(None);
            };
            let key = (chat, user);
            if data == "schedule:find" {
                self.pending.insert(key);
                self.choices.remove(&key);
                return Ok(Some(vec![prompt(chat), ack(id)]));
            }
            if data == "menu:start" {
                self.pending.remove(&key);
                self.choices.remove(&key);
                return Ok(None);
            }
            if data.starts_with("teacher:") && self.choices.contains_key(&key) {
                let expired = || BotAction {
                    method: "answerCallbackQuery".into(),
                    payload: json!({"callback_query_id":id,"text":"Этот выбор устарел. Введи запрос заново.","show_alert":true}),
                };
                let parts: Vec<_> = data.split(':').collect();
                let choice = &self.choices[&key];
                let index = parts.get(3).and_then(|v| v.parse::<usize>().ok());
                if parts.len() != 4 || parts[2] != choice.token || index.is_none() {
                    return Ok(Some(vec![expired()]));
                }
                let index = index.unwrap();
                if parts[1] == "page" {
                    return Ok(Some(match self.choices_screen(key, index) {
                        Some(screen) => vec![screen, ack(id)],
                        None => vec![expired()],
                    }));
                }
                if parts[1] == "pick" {
                    if let Some(target) = choice.targets.get(index).cloned() {
                        let mut actions = self.show_target(db, parser, key, &target, now).await?;
                        actions.push(ack(id));
                        return Ok(Some(actions));
                    }
                }
                return Ok(Some(vec![expired()]));
            }
            return Ok(None);
        }
        let Some(msg) = update.get("message") else {
            return Ok(None);
        };
        if msg.pointer("/chat/type").and_then(Value::as_str) != Some("private") {
            return Ok(None);
        }
        let (Some(chat), Some(user), Some(input)) = (
            msg.pointer("/chat/id").and_then(Value::as_i64),
            msg.pointer("/from/id").and_then(Value::as_i64),
            msg.get("text").and_then(Value::as_str),
        ) else {
            return Ok(None);
        };
        let key = (chat, user);
        if !self.pending.contains(&key) || input.starts_with('/') {
            return Ok(None);
        }
        self.choices.remove(&key);
        let targets = catalog.find_all_with_snapshots(input, groups, snapshots);
        if targets.is_empty() {
            return Ok(Some(vec![message(
                chat,
                crate::telegram_ui::SEARCH_NOT_FOUND,
                None,
            )]));
        }
        if targets.len() == 1 {
            return Ok(Some(
                self.show_target(db, parser, key, &targets[0], now).await?,
            ));
        }
        let mut bytes = [0; 4];
        getrandom::getrandom(&mut bytes).map_err(|_| "Не удалось создать выбор")?;
        let token = bytes.iter().map(|b| format!("{b:02x}")).collect();
        self.choices.insert(key, Choices { token, targets });
        Ok(Some(vec![self
            .choices_screen(key, 0)
            .expect("choice exists")]))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn search_uses_saved_group_without_changing_subscription() {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,created_at,last_seen_at) VALUES ('telegram',42,'group','group:1','Старая','now','now')", []).unwrap();
        let snapshot = crate::ScheduleSnapshot {
            group_name: "ИСП-25".into(),
            fetched_at: "2026-11-08T12:00:00".into(),
            days: vec![crate::DaySchedule {
                date_label: "8 ноября".into(),
                date_iso: "2026-11-08".into(),
                week_variant: None,
                current_week_variant: None,
                lessons: vec![crate::Lesson {
                    number: 1,
                    subject: "Физика".into(),
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
                schedule_id: Some(123),
                group_name: Some("ИСП-25"),
                source_type: Some("group"),
                source_key: Some("group:123"),
                source_title: Some("ИСП-25"),
                source_url: None,
                archive_origin: None,
                archive_dates: None,
                archive_projected: false,
                timezone: chrono_tz::UTC,
                created_at: "now",
            },
        )
        .unwrap();
        let groups = GroupCatalog::new(vec![crate::group_catalog::GroupInfo {
            department_id: 1,
            department_code: "ИСП".into(),
            department_name: "Отделение".into(),
            group_name: "ИСП-25".into(),
            schedule_id: Some(123),
            url: "http://127.0.0.1:1/rasp/123".into(),
        }]);
        let catalog = SearchCatalog::default();
        let parser = ScheduleParser::new(
            "",
            "",
            std::time::Duration::from_millis(100),
            1,
            std::time::Duration::ZERO,
        )
        .unwrap();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let mut flow = SearchFlow::default();
        let start = json!({"callback_query":{"id":"cb","from":{"id":42},"message":{"chat":{"id":42}},"data":"schedule:find"}});
        assert!(flow
            .process(&db, &parser, &start, (&groups, &catalog, &[]), now)
            .await
            .unwrap()
            .is_some());
        let input =
            json!({"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"ИСП-25"}});
        let actions = flow
            .process(&db, &parser, &input, (&groups, &catalog, &[]), now)
            .await
            .unwrap()
            .unwrap();
        assert!(actions[0].payload["text"]
            .as_str()
            .unwrap()
            .contains("Физика"));
        assert_eq!(
            crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .subscription_key
                .as_deref(),
            Some("group:1")
        );
        assert!(flow
            .process(&db, &parser, &input, (&groups, &catalog, &[]), now)
            .await
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn ambiguous_results_require_scoped_choice_without_creating_subscription() {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let groups = GroupCatalog::default();
        let catalog = SearchCatalog::new(
            &[
                ("Иванов Иван".into(), "/raspprep/1".into()),
                ("Иванов Игорь".into(), "/raspprep/2".into()),
            ],
            &[],
        );
        let parser = ScheduleParser::new(
            "",
            "",
            std::time::Duration::from_millis(100),
            1,
            std::time::Duration::ZERO,
        )
        .unwrap();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let mut flow = SearchFlow::default();
        let start = json!({"callback_query":{"id":"cb","from":{"id":42},"message":{"chat":{"id":42}},"data":"schedule:find"}});
        flow.process(&db, &parser, &start, (&groups, &catalog, &[]), now)
            .await
            .unwrap();
        let input =
            json!({"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"Иванов"}});
        let actions = flow
            .process(&db, &parser, &input, (&groups, &catalog, &[]), now)
            .await
            .unwrap()
            .unwrap();
        let buttons = actions[0].payload["reply_markup"]["inline_keyboard"]
            .as_array()
            .unwrap();
        assert!(buttons.len() >= 3);
        let callback = buttons[0][0]["callback_data"].as_str().unwrap();
        let stale = format!("{}:999", callback.rsplit_once(':').unwrap().0);
        let stale_update = json!({"callback_query":{"id":"old","from":{"id":42},"message":{"chat":{"id":42}},"data":stale}});
        let stale_actions = flow
            .process(&db, &parser, &stale_update, (&groups, &catalog, &[]), now)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(stale_actions[0].payload["show_alert"], true);
        let wrong_user = json!({"callback_query":{"id":"cb2","from":{"id":43},"message":{"chat":{"id":42}},"data":callback}});
        assert!(flow
            .process(&db, &parser, &wrong_user, (&groups, &catalog, &[]), now)
            .await
            .unwrap()
            .is_none());
        let choice = json!({"callback_query":{"id":"cb3","from":{"id":42},"message":{"chat":{"id":42}},"data":callback}});
        let result = flow
            .process(&db, &parser, &choice, (&groups, &catalog, &[]), now)
            .await
            .unwrap()
            .unwrap();
        assert!(result[0].payload["text"]
            .as_str()
            .unwrap()
            .contains("Расписание для Иванов"));
        assert_eq!(result[1].method, "answerCallbackQuery");
        assert!(crate::database::get_user(&db, "telegram", 42)
            .unwrap()
            .is_none());
    }
}

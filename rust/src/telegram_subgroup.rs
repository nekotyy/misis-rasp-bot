use crate::{telegram_ui::BotAction, ScheduleSnapshot, UserRecord};
use rusqlite::{params, OptionalExtension, Transaction};
use serde_json::{json, Value};

pub fn handle(
    tx: &Transaction<'_>,
    update: &Value,
    user: &UserRecord,
    snapshot: Option<&ScheduleSnapshot>,
    now: &str,
) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
    let Some(callback) = update.get("callback_query") else {
        return Ok(None);
    };
    let Some(data) = callback.get("data").and_then(Value::as_str) else {
        return Ok(None);
    };
    if data != "menu:subgroup" && !data.starts_with("subgroup:") {
        return Ok(None);
    }
    let Some(id) = callback.get("id").and_then(Value::as_str) else {
        return Ok(None);
    };
    let Some(chat) = callback.pointer("/message/chat/id").and_then(Value::as_i64) else {
        return Ok(None);
    };
    let alert = |text| {
        vec![BotAction {
            method: "answerCallbackQuery".into(),
            payload: json!({"callback_query_id":id,"text":text,"show_alert":true}),
        }]
    };
    if chat != user.user_id {
        return Ok(Some(alert("Выбери подгруппу в личном чате с ботом.")));
    }
    if !crate::subgroups::is_university_group(Some(user)) {
        return Ok(Some(alert("Сначала выбери свою группу университета.")));
    }
    let schedule_id = user.schedule_id.expect("university group has schedule id");
    let options: std::collections::BTreeSet<u32> = snapshot
        .into_iter()
        .flat_map(|s| &s.days)
        .flat_map(|d| &d.lessons)
        .flat_map(|lesson| crate::subgroups::lesson_subgroups(&lesson.subject))
        .collect();
    let ack = || BotAction {
        method: "answerCallbackQuery".into(),
        payload: json!({"callback_query_id":id}),
    };
    if data == "menu:subgroup" {
        let mut rows: Vec<Value> = options.iter().map(|number| json!([{"text":format!("{number} подгруппа"),"callback_data":format!("subgroup:{schedule_id}:{number}")}])).collect();
        rows.push(json!([{"text":"Все подгруппы","callback_data":format!("subgroup:{schedule_id}:all")} ]));
        rows.push(json!([{"text":"Назад","callback_data":"menu:start"}]));
        let mut text = "<b>Твоя подгруппа</b>\n\nОбщие пары показываются всегда. Выбери свою подгруппу; полное расписание всех подгрупп доступно через поиск.".to_string();
        if options.is_empty() {
            text.push_str("\n\nВ сохранённом расписании пока нет данных о подгруппах.");
        }
        return Ok(Some(vec![
            BotAction {
                method: "sendMessage".into(),
                payload: json!({"chat_id":chat,"text":text,"parse_mode":"HTML","reply_markup":{"inline_keyboard":rows}}),
            },
            ack(),
        ]));
    }
    let parts: Vec<_> = data.split(':').collect();
    let selected = if parts.len() == 3 && parts[1].parse::<i64>().ok() == Some(schedule_id) {
        if parts[2] == "all" {
            Some(None)
        } else {
            parts[2]
                .parse::<u32>()
                .ok()
                .filter(|n| options.contains(n))
                .map(|n| Some(i64::from(n)))
        }
    } else {
        None
    };
    let Some(selected) = selected else {
        return Ok(Some(alert("Выбор устарел. Открой подгруппы заново.")));
    };
    let changed = tx.execute("UPDATE users SET university_subgroup=? WHERE platform='telegram' AND user_id=? AND subscription_type='group' AND schedule_id=? AND schedule_id<0",params![selected,user.user_id,schedule_id])?;
    if changed == 0 {
        return Ok(Some(alert("Выбор устарел. Открой подгруппы заново.")));
    }
    let primary: Option<(String,i64)> = tx.query_row("SELECT i.account_id,s.id FROM account_identities AS i JOIN account_subscriptions AS s ON s.account_id=i.account_id AND s.is_primary=1 WHERE i.platform='telegram' AND i.user_id=? LIMIT 1",[user.user_id],|row|Ok((row.get(0)?,row.get(1)?))).optional()?;
    if let Some((account, id)) = primary {
        crate::account::update_subscription(
            tx,
            &account,
            id,
            None,
            Some(selected.unwrap_or(0)),
            None,
            now,
        )?;
    }
    let updated =
        crate::database::get_user(tx, "telegram", user.user_id)?.ok_or("Пользователь не найден")?;
    let current = now
        .get(..10)
        .and_then(|date| chrono::NaiveDate::parse_from_str(date, "%Y-%m-%d").ok())
        .and_then(|date| crate::telegram_ui::snapshot_current_week(snapshot, date));
    let text = crate::telegram_ui::welcome_text(Some(&updated), current, true);
    Ok(Some(vec![
        BotAction {
            method: "sendMessage".into(),
            payload: json!({"chat_id":chat,"text":text,"parse_mode":"HTML","reply_markup":crate::telegram_ui::start_keyboard(Some(&updated))}),
        },
        ack(),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_selection_and_rolls_back_with_transaction() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,group_name,schedule_id,created_at,last_seen_at) VALUES ('telegram',42,'group','group:-123','Университет','Университет',-123,'now','now')",[]).unwrap();
        let user = crate::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap();
        let snapshot = ScheduleSnapshot {
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
        let callback = |data: &str| json!({"callback_query":{"id":"cb","from":{"id":42},"message":{"chat":{"id":42}},"data":data}});
        let tx = db.transaction().unwrap();
        let menu = handle(
            &tx,
            &callback("menu:subgroup"),
            &user,
            Some(&snapshot),
            "now",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            menu[0].payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            "subgroup:-123:2"
        );
        let stale = handle(
            &tx,
            &callback("subgroup:-123:3"),
            &user,
            Some(&snapshot),
            "now",
        )
        .unwrap()
        .unwrap();
        assert_eq!(stale[0].payload["show_alert"], true);
        let chosen = handle(
            &tx,
            &callback("subgroup:-123:2"),
            &user,
            Some(&snapshot),
            "now",
        )
        .unwrap()
        .unwrap();
        assert!(chosen[0].payload["text"]
            .as_str()
            .unwrap()
            .contains("Подгруппа: <b>2</b>"));
        tx.rollback().unwrap();
        assert_eq!(
            crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .university_subgroup,
            None
        );
    }
    #[test]
    fn selected_subgroup_updates_linked_account_subscription() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,group_name,schedule_id,created_at,last_seen_at) VALUES ('telegram',42,'group','group:-123','Университет','Университет',-123,'now','now')",[]).unwrap();
        let tx = db.transaction().unwrap();
        let account =
            crate::account::get_or_create_account(&tx, "telegram", 42, None, None, "now").unwrap();
        tx.commit().unwrap();
        let user = crate::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap();
        let snapshot = ScheduleSnapshot {
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
        let callback = json!({"callback_query":{"id":"cb","from":{"id":42},"message":{"chat":{"id":42}},"data":"subgroup:-123:2"}});
        let tx = db.transaction().unwrap();
        handle(
            &tx,
            &callback,
            &user,
            Some(&snapshot),
            "2026-11-08T12:00:00",
        )
        .unwrap();
        let subgroup: i64 = tx.query_row("SELECT university_subgroup FROM account_subscriptions WHERE account_id=? AND is_primary=1",[account],|row|row.get(0)).unwrap();
        assert_eq!(subgroup, 2);
        tx.commit().unwrap();
        assert_eq!(
            crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .university_subgroup,
            Some(2)
        );
    }
}

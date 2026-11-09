use crate::telegram_ui::BotAction;
use rusqlite::Transaction;
use serde_json::{json, Value};
use std::collections::HashSet;

#[derive(Clone, Default)]
pub struct GroupFlow {
    waiting: HashSet<i64>,
}
fn parse_message(update: &Value) -> Option<(i64, i64, &str, bool)> {
    let msg = update.get("message")?;
    let chat = msg.pointer("/chat/id")?.as_i64()?;
    let actor = msg.pointer("/from/id")?.as_i64()?;
    let text = msg.get("text")?.as_str()?;
    let group = matches!(
        msg.pointer("/chat/type").and_then(Value::as_str),
        Some("group" | "supergroup")
    );
    Some((chat, actor, text, group))
}
fn command(text: &str) -> &str {
    text.split_whitespace()
        .next()
        .unwrap_or("")
        .split('@')
        .next()
        .unwrap_or("")
}
fn setup_alias(text: &str) -> bool {
    use unicode_casefold::UnicodeCaseFold;
    if matches!(
        command(text),
        "/group_setup" | "/group_set" | "/group_clear" | "/group_status"
    ) {
        return false;
    }
    let raw = text
        .case_fold()
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    [
        "/startgroup",
        "/group",
        "startgroup",
        "group",
        "настройка группы",
        "настройки группы",
        "настроить группу",
        "группа",
    ]
    .contains(&raw.as_str())
        || [
            "/startgroup",
            "/group",
            "startgroup ",
            "group ",
            "настройка группы",
            "настройки группы",
            "настроить группу",
            "группа ",
            "группа:",
            "группа -",
            "группа-",
        ]
        .iter()
        .any(|prefix| raw.starts_with(prefix))
}
pub fn needs_bot_identity(update: &Value) -> bool {
    parse_message(update).is_some_and(|(_, _, text, group)| !group && setup_alias(text))
}
pub fn private_setup_actions(update: &Value, username: &str) -> Option<Vec<BotAction>> {
    let (chat, _, text, group) = parse_message(update)?;
    if group || !setup_alias(text) {
        return None;
    }
    let username = if !username.is_empty()
        && username
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
    {
        username
    } else {
        "bot"
    };
    let text = "<b>Настройка бота в групповом чате Telegram</b>\n\nЧтобы получать расписание и уведомления об изменениях в вашем чате:\n1. Нажмите кнопку ниже и добавьте бота в ваш групповой чат.\n2. Назначьте бота администратором чата (с правом отправки сообщений).\n3. В чате отправьте команду <code>/startgroup</code> (или <code>/group</code>).\n4. Напишите название вашей учебной группы (например: <code>ИСП-25-1</code>).";
    let mut reply = action(chat, text);
    reply.payload["reply_markup"] = json!({"inline_keyboard":[[{"text":"Добавить бота в группу","url":format!("https://t.me/{username}?startgroup=true")}]]});
    Some(vec![reply])
}
fn action(chat: i64, text: &str) -> BotAction {
    BotAction {
        method: "sendMessage".into(),
        payload: json!({"chat_id":chat,"text":text,"parse_mode":"HTML"}),
    }
}
pub fn membership_actions(update: &Value) -> Option<Vec<BotAction>> {
    let event = update.get("my_chat_member")?;
    let chat = event.pointer("/chat/id")?.as_i64()?;
    let old = event.pointer("/old_chat_member/status")?.as_str()?;
    let new = event.pointer("/new_chat_member/status")?.as_str()?;
    if matches!(new, "member" | "administrator") && !matches!(old, "member" | "administrator") {
        Some(vec![action(chat,"<b>Инструкция по настройке бота в групповом чате</b>\n\nБот успешно добавлен в ваш чат.\n\nПошаговая настройка:\n1. Назначьте бота администратором чата (с правом отправки сообщений).\n2. Отправьте в этот чат команду <code>/startgroup</code> (или <code>/group</code>).\n3. Напишите название вашей учебной группы (например: <code>ИСП-25-1</code>).\n\nПосле выполнения настройки бот привяжет расписание и будет автоматически отправлять ежедневные обновления и изменения пар.")])
    } else {
        Some(vec![])
    }
}
fn status(user: Option<&crate::UserRecord>) -> String {
    let caption = user.and_then(|u| {
        crate::subscription::subscription_caption(
            u.subscription_type.as_deref(),
            u.subscription_title.as_deref(),
            None,
        )
    });
    let mut lines = vec![
        "<b>Настройка уведомлений для этой группы</b>".to_string(),
        String::new(),
    ];
    if let Some(caption) = caption {
        if let Some((label, value)) = caption.split_once(':') {
            let mut values = value.lines().map(str::trim).filter(|s| !s.is_empty());
            lines.push(format!(
                "{}: <b>{}</b>",
                crate::format::escape_html(label),
                crate::format::escape_html(values.next().unwrap_or("-"))
            ));
            lines.extend(values.map(crate::format::escape_html));
        }
    } else {
        lines.push("Подписка: <b>не выбрана</b>".into());
    }
    lines.extend([
        "".into(),
        "Команды:".into(),
        "/group_setup - интерактивная настройка".into(),
        "/group_set ИСП-25-1 - быстрая настройка".into(),
        "/group_clear - очистить подписку".into(),
    ]);
    lines.join("\n")
}
impl GroupFlow {
    pub fn interested(&self, update: &Value) -> bool {
        let Some((chat, _, text, is_group)) = parse_message(update) else {
            return false;
        };
        matches!(
            command(text),
            "/group_setup" | "/group_set" | "/group_clear" | "/group_status"
        ) || setup_alias(text)
            || (is_group && self.waiting.contains(&chat) && !text.starts_with('/'))
    }
    pub fn requires_authorization(&self, update: &Value) -> Option<(i64, i64)> {
        let (chat, actor, text, group) = parse_message(update)?;
        if !group {
            return None;
        }
        let cmd = command(text);
        if matches!(cmd, "/group_setup" | "/group_set" | "/group_clear")
            || setup_alias(text)
            || (self.waiting.contains(&chat) && !text.starts_with('/'))
        {
            Some((chat, actor))
        } else {
            None
        }
    }
    pub fn handle(
        &mut self,
        tx: &Transaction<'_>,
        update: &Value,
        allowed: Option<bool>,
        context: &crate::telegram_engine::EngineContext<'_>,
        now: &str,
    ) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
        let Some((chat, _, text, is_group)) = parse_message(update) else {
            return Ok(None);
        };
        let cmd = command(text);
        let alias = setup_alias(text);
        let known = matches!(
            cmd,
            "/group_setup" | "/group_set" | "/group_clear" | "/group_status"
        ) || alias;
        if !known && !(is_group && self.waiting.contains(&chat) && !text.starts_with('/')) {
            return Ok(None);
        }
        if !is_group && alias {
            return Ok(None);
        }
        if !is_group {
            return Ok(Some(vec![]));
        }
        if cmd != "/group_status" && allowed.is_none() {
            return Ok(None);
        }
        crate::telegram_ui::register_group_chat(tx, update, now)?;
        if cmd == "/group_status" {
            return Ok(Some(vec![action(
                chat,
                &status(crate::database::get_user(tx, "telegram", chat)?.as_ref()),
            )]));
        }
        if allowed != Some(true) {
            return Ok(Some(vec![action(
                chat,
                if alias {
                    "Настройка группы доступна только администраторам чата или пользователю, добавившему бота."
                } else if cmd == "/group_clear" {
                    "Отключить уведомления может только администратор группы."
                } else {
                    "Настройку может выполнять только администратор группы."
                },
            )]));
        }
        if cmd == "/group_clear" {
            self.waiting.remove(&chat);
            crate::database::clear_user_subscription_in_transaction(tx, "telegram", chat)?;
            return Ok(Some(vec![action(chat,"Подписка группы очищена. Уведомления об изменениях расписания в этот чат больше не отправляются.")]));
        }
        if cmd == "/group_setup" {
            self.waiting.insert(chat);
            return Ok(Some(vec![action(chat,"<b>Настройка уведомлений группы</b>\n\nОтправь название группы (например, <b>ИСП-25-1</b>) или фамилию преподавателя.\n\nЛибо используй команду: <b>/group_set ИСП-25-1</b>")]));
        }
        if alias {
            self.waiting.insert(chat);
            return Ok(Some(vec![action(chat,"<b>Быстрая настройка группового чата</b>\n\nПришлите название вашей учебной группы одним сообщением (например: <code>ИСП-25-1</code> или <code>МТО-25</code>):")]));
        }
        let input = if cmd == "/group_set" {
            text.split_once(char::is_whitespace)
                .map(|(_, input)| input.trim())
                .unwrap_or("")
        } else {
            text.trim()
        };
        if input.is_empty() {
            return Ok(Some(vec![action(
                chat,
                "Использование: /group_set ИСП-25-1",
            )]));
        }
        let Some(subscription) = crate::telegram_ui::resolve_subscription_with_snapshots(
            input,
            context.groups,
            context.search,
            context.group_snapshots,
        ) else {
            self.waiting.insert(chat);
            return Ok(Some(vec![action(
                chat,
                crate::telegram_ui::SEARCH_NOT_FOUND,
            )]));
        };
        crate::database::set_subscription_in_transaction(tx, "telegram", chat, &subscription, now)?;
        self.waiting.remove(&chat);
        let user = crate::database::get_user(tx, "telegram", chat)?;
        Ok(Some(vec![action(
            chat,
            &format!("Подписка группы обновлена.\n\n{}", status(user.as_ref())),
        )]))
    }
}

pub async fn check_admin(
    api: &crate::notification::ApiTransport,
    chat: i64,
    user: i64,
) -> Result<bool, crate::notification::DeliveryFailure> {
    match api
        .send(
            "telegram",
            "getChatMember",
            &json!({"chat_id":chat,"user_id":user}),
        )
        .await
    {
        Ok(member) => Ok(matches!(
            member.get("status").and_then(Value::as_str),
            Some("administrator" | "creator")
        )),
        Err(crate::notification::DeliveryFailure::Telegram { code: 400, .. }) => Ok(false),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identifies_quick_setup_without_shadowing_group_commands() {
        assert!(setup_alias("Настройка группы"));
        assert!(setup_alias("/group@my_bot"));
        assert!(setup_alias("группа: ИСП-25"));
        assert!(!setup_alias("/group_set@my_bot ИСП-25"));
        assert!(!setup_alias("/group_clear"));
        let private =
            json!({"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/group"}});
        assert!(needs_bot_identity(&private));
        assert_eq!(
            private_setup_actions(&private, "schedule_bot").unwrap()[0].payload["reply_markup"]
                ["inline_keyboard"][0][0]["url"],
            "https://t.me/schedule_bot?startgroup=true"
        );
    }
    #[test]
    fn welcomes_only_when_bot_is_added_to_chat() {
        let event = |old: &str, new: &str| json!({"my_chat_member":{"chat":{"id":-100},"old_chat_member":{"status":old},"new_chat_member":{"status":new}}});
        assert!(
            membership_actions(&event("left", "member")).unwrap()[0].payload["text"]
                .as_str()
                .unwrap()
                .contains("Инструкция")
        );
        assert!(membership_actions(&event("member", "administrator"))
            .unwrap()
            .is_empty());
    }
    #[test]
    fn interactive_setup_accepts_only_authorized_messages_and_can_be_rolled_back() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let groups =
            crate::group_catalog::GroupCatalog::new(vec![crate::group_catalog::GroupInfo {
                department_id: 1,
                department_code: "ИСП".into(),
                department_name: "Отделение".into(),
                group_name: "ИСП-25".into(),
                schedule_id: Some(123),
                url: "http://college/rasp/123".into(),
            }]);
        let search = crate::search::SearchCatalog::default();
        let settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        let context = crate::telegram_engine::EngineContext {
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
        let update = |text: &str| json!({"message":{"chat":{"id":-100,"type":"supergroup","title":"Учебная"},"from":{"id":42},"text":text}});
        let mut flow = GroupFlow::default();
        let tx = db.transaction().unwrap();
        let denied = flow
            .handle(&tx, &update("/group_setup"), Some(false), &context, "now")
            .unwrap()
            .unwrap();
        assert!(denied[0].payload["text"]
            .as_str()
            .unwrap()
            .contains("только администратор"));
        assert!(flow.requires_authorization(&update("ИСП-25")).is_none());
        flow.handle(&tx, &update("/group_setup"), Some(true), &context, "now")
            .unwrap();
        assert_eq!(
            flow.requires_authorization(&update("ИСП-25")),
            Some((-100, 42))
        );
        flow.handle(&tx, &update("ИСП-25"), Some(true), &context, "now")
            .unwrap();
        assert_eq!(
            crate::database::get_user(&tx, "telegram", -100)
                .unwrap()
                .unwrap()
                .subscription_key
                .as_deref(),
            Some("group:123")
        );
        tx.rollback().unwrap();
        assert!(crate::database::get_user(&db, "telegram", -100)
            .unwrap()
            .is_none());
    }
}

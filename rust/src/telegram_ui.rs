use crate::{format, subgroups, subscription, UserRecord};
use serde_json::{json, Value};

pub const GROUP_PROMPT:&str="<b>Укажи свою группу</b>\n\nНапиши группу в таком же формате, как и на сайте.\nНапример: <b>ИСП-25-1</b> или <b>МТО-25</b>\n\nРегистр не важен.";
pub const GROUP_START:&str="Бот добавлен в группу.\n\nДля настройки уведомлений используй:\n/group_setup\nили\n/group_set ИСП-25-1\n\nТекущий статус: /group_status";
pub fn start_keyboard(user: Option<&UserRecord>) -> Value {
    let mut rows = vec![
        json!([{"text":"Узнать расписание","callback_data":"start:rasp"}]),
        json!([{"text":"Дополнительно","callback_data":"menu:settings"}]),
    ];
    if subgroups::is_university_group(user) {
        let selected = subgroups::personal_subgroup(user);
        rows.insert(1,json!([{"text":selected.map(|n|format!("Подгруппа: {n}")).unwrap_or_else(||"Выбрать подгруппу".into()),"callback_data":"menu:subgroup"}]));
    }
    if let Some(user) = user.filter(|u| u.subscription_type.as_deref() == Some("teacher")) {
        rows.insert(1,json!([{"text":if user.audience_subscription_key.as_ref().is_some_and(|x|!x.is_empty()){ "Изменить кабинет" } else {"Подписаться на кабинет"},"callback_data":"settings:audience_setup"}]));
    }
    json!({"inline_keyboard":rows})
}
pub fn welcome_text(
    user: Option<&UserRecord>,
    current_week: Option<i64>,
    university_source: bool,
) -> String {
    let mut lines = vec!["<b>Привет! Я бот расписания колледжа и университета</b>".to_string()];
    let caption = user.and_then(|u| {
        subscription::subscription_caption(
            u.subscription_type.as_deref(),
            u.subscription_title.as_deref(),
            u.audience_subscription_title.as_deref(),
        )
    });
    if let Some(caption) = caption {
        if let Some((label, value)) = caption.split_once(':') {
            let values: Vec<_> = value
                .trim()
                .lines()
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .collect();
            lines.push(String::new());
            lines.push(format!(
                "{}: <b>{}</b>",
                format::escape_html(label),
                format::escape_html(values.first().copied().unwrap_or("-"))
            ));
            lines.extend(values.iter().skip(1).map(|s| format::escape_html(s)));
        }
    }
    if university_source {
        lines.push(format!(
            "Учимся по: <b>{}</b>",
            format::week_name(current_week)
        ));
        if subgroups::is_university_group(user) {
            lines.push(format!(
                "Подгруппа: <b>{}</b>",
                subgroups::personal_subgroup(user)
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "все".into())
            ));
        }
    }
    if user.is_some_and(|u| {
        u.subscription_type.as_deref() == Some("teacher")
            && u.audience_subscription_key
                .as_ref()
                .is_none_or(|s| s.is_empty())
    }) {
        lines.extend([String::new(),"Ниже можно быстро привязать кабинет, чтобы следить за обновлениями аудитории отдельно.".into()]);
    }
    lines.extend([
        String::new(),
        "/rasp — посмотреть расписание".into(),
        "/settings — дополнительно".into(),
    ]);
    lines.join("\n")
}
pub fn university_keyboard(date: &str) -> Value {
    let mut rows = vec![
        json!([{"text":"Числитель","callback_data":format!("schedule:week:{date}:1")},{"text":"Знаменатель","callback_data":format!("schedule:week:{date}:2")}]),
        json!([{"text":"Актуальное","callback_data":format!("schedule:week:{date}:0")}]),
    ];
    rows.extend(
        crate::notification::telegram_keyboard()["inline_keyboard"]
            .as_array()
            .unwrap()
            .iter()
            .cloned(),
    );
    json!({"inline_keyboard":rows})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn menu_preserves_navigation_and_escapes_user_titles() {
        let keyboard = start_keyboard(None);
        assert_eq!(
            keyboard["inline_keyboard"][0][0]["callback_data"],
            "start:rasp"
        );
        let keyboard = university_keyboard("2026-11-09");
        assert_eq!(
            keyboard["inline_keyboard"][0][1]["callback_data"],
            "schedule:week:2026-11-09:2"
        );
        assert_eq!(keyboard["inline_keyboard"].as_array().unwrap().len(), 8);
        assert!(welcome_text(None, None, false).contains("/settings"));
        let user:UserRecord=serde_json::from_value(json!({"platform":"telegram","user_id":1,"subscription_type":"group","subscription_title":"<Группа>","is_admin":false,"is_editor":false,"homework_notifications_enabled":true,"delivery_disabled_auto":false,"created_at":"now","last_seen_at":"now"})).unwrap();
        assert!(welcome_text(Some(&user), None, false).contains("&lt;Группа&gt;"));

        assert!(GROUP_PROMPT.contains("ИСП-25-1"));
    }
}

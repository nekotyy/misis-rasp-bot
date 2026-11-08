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

pub struct Screen {
    pub text: String,
    pub keyboard: Value,
}
pub fn schedule_screen(
    action: &str,
    snapshot: Option<&crate::ScheduleSnapshot>,
    user: Option<&UserRecord>,
    today: chrono::NaiveDate,
    university_source: bool,
) -> Screen {
    let Some(snapshot) = snapshot else {
        return Screen {
            text: "Не удалось получить расписание для твоей группы.".into(),
            keyboard: crate::notification::telegram_keyboard(),
        };
    };
    let (offset, label) = match action {
        "today" => (0, "сегодня"),
        "tomorrow" => (1, "завтра"),
        "day_after" => (2, "2 дня"),
        _ => {
            return Screen {
                text: "Выбери нужный вариант расписания.".into(),
                keyboard: crate::notification::telegram_keyboard(),
            }
        }
    };
    let date = today + chrono::Duration::days(offset);
    let date_iso = date.to_string();
    let subgroup = subgroups::personal_subgroup(user).and_then(|n| u32::try_from(n).ok());
    let text = if let Some(day) = snapshot.days.iter().find(|d| d.date_iso == date_iso) {
        let mut day = day.clone();
        let variant = snapshot
            .days
            .iter()
            .find_map(|d| d.current_week_variant.filter(|v| *v == 1 || *v == 2));
        let fetched = snapshot
            .fetched_at
            .get(..10)
            .and_then(|s| chrono::NaiveDate::parse_from_str(s, "%Y-%m-%d").ok());
        day.current_week_variant = fetched.and_then(|f| format::current_week(f, today, variant));
        format::format_day(&day, subgroup, true, today)
    } else {
        format!("Расписание на {label}\n\nПар нет.")
    };
    Screen {
        text,
        keyboard: if university_source {
            university_keyboard(&date_iso)
        } else {
            crate::notification::telegram_keyboard()
        },
    }
}

#[derive(Debug, PartialEq)]
pub struct BotAction {
    pub method: String,
    pub payload: Value,
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
fn snapshot_current_week(
    snapshot: Option<&crate::ScheduleSnapshot>,
    today: chrono::NaiveDate,
) -> Option<i64> {
    let snapshot = snapshot?;
    let fetched = snapshot
        .fetched_at
        .get(..10)
        .and_then(|s| chrono::NaiveDate::parse_from_str(s, "%Y-%m-%d").ok())?;
    let variant = snapshot
        .days
        .iter()
        .find_map(|d| d.current_week_variant.filter(|v| *v == 1 || *v == 2));
    format::current_week(fetched, today, variant)
}
pub fn route_update(
    update: &Value,
    user: Option<&UserRecord>,
    snapshot: Option<&crate::ScheduleSnapshot>,
    today: chrono::NaiveDate,
    university_source: bool,
) -> Option<Vec<BotAction>> {
    if let Some(msg) = update.get("message") {
        let chat_id = msg.pointer("/chat/id")?.as_i64()?;
        let text = msg.get("text")?.as_str()?;
        let command = text.split_whitespace().next()?.split('@').next()?;
        if command != "/start" {
            return None;
        }
        if matches!(
            msg.pointer("/chat/type").and_then(Value::as_str),
            Some("group" | "supergroup")
        ) {
            return Some(vec![message(chat_id, GROUP_START, None)]);
        }
        let Some(user) = user.filter(|u| {
            u.subscription_key.as_ref().is_some_and(|s| !s.is_empty())
                && u.subscription_title.as_ref().is_some_and(|s| !s.is_empty())
        }) else {
            return Some(vec![message(chat_id, GROUP_PROMPT, None)]);
        };
        let current = snapshot_current_week(snapshot, today);
        return Some(vec![message(
            chat_id,
            &welcome_text(Some(user), current, university_source),
            Some(start_keyboard(Some(user))),
        )]);
    }
    let callback = update.get("callback_query")?;
    let id = callback.get("id")?.as_str()?;
    let chat_id = callback.pointer("/message/chat/id")?.as_i64()?;
    let data = callback.get("data")?.as_str()?;
    let ack = || BotAction {
        method: "answerCallbackQuery".into(),
        payload: json!({"callback_query_id":id}),
    };
    let screen = match data {
        "start:rasp" => {
            if user.is_none_or(|u| {
                u.subscription_key.as_ref().is_none_or(|s| s.is_empty())
                    || u.subscription_title.as_ref().is_none_or(|s| s.is_empty())
            }) {
                Screen {
                    text: GROUP_PROMPT.into(),
                    keyboard: Value::Null,
                }
            } else {
                Screen {
                    text: "Выбери нужный вариант расписания.".into(),
                    keyboard: crate::notification::telegram_keyboard(),
                }
            }
        }
        "schedule:today" | "schedule:tomorrow" | "schedule:day_after" => schedule_screen(
            data.strip_prefix("schedule:").unwrap(),
            snapshot,
            user,
            today,
            university_source,
        ),
        "menu:start" => Screen {
            text: welcome_text(
                user,
                snapshot_current_week(snapshot, today),
                university_source,
            ),
            keyboard: start_keyboard(user),
        },
        _ => return None,
    };
    Some(vec![
        message(
            chat_id,
            &screen.text,
            if screen.keyboard.is_null() {
                None
            } else {
                Some(screen.keyboard)
            },
        ),
        ack(),
    ])
}

pub const SEARCH_NOT_FOUND:&str="Ничего не найдено.\n\nЧто я пробовал найти:\n- группу, например: ИСП-25-1;\n- преподавателя по фамилии;\n- кабинет, например: 101.\n\nПроверь раскладку, дефисы и пробелы.\nЕсли группа введена точно, но не находится, значит проблема, скорее всего, в каталоге групп на стороне сайта.";
pub fn resolve_subscription(
    input: &str,
    groups: &crate::group_catalog::GroupCatalog,
    search: &crate::search::SearchCatalog,
) -> Option<crate::subscription::Subscription> {
    resolve_subscription_with_snapshots(input, groups, search, &[])
}
pub fn resolve_subscription_with_snapshots(
    input: &str,
    groups: &crate::group_catalog::GroupCatalog,
    search: &crate::search::SearchCatalog,
    snapshots: &[crate::ScheduleSnapshot],
) -> Option<crate::subscription::Subscription> {
    if let Some(group) = groups.find(input) {
        return Some(crate::subscription::group_subscription(
            &group.group_name,
            group.schedule_id,
        ));
    }
    let matches = search.find_all_with_snapshots(input, groups, snapshots);
    if let [target] = matches.as_slice() {
        if target.kind == "teacher" {
            return Some(crate::subscription::teacher_subscription(
                &target.title,
                &target.url,
            ));
        }
    }
    None
}

pub fn register_actor(
    db: &rusqlite::Connection,
    update: &Value,
    admins: &[i64],
    limited_admins: &[i64],
    now: &str,
) -> rusqlite::Result<Option<UserRecord>> {
    let actor = update
        .pointer("/message/from")
        .or_else(|| update.pointer("/callback_query/from"));
    let Some(actor) = actor else {
        return Ok(None);
    };
    let Some(user_id) = actor.get("id").and_then(Value::as_i64) else {
        return Ok(None);
    };
    let existing = crate::database::get_user(db, "telegram", user_id)?;
    let name = [
        actor.get("first_name").and_then(Value::as_str),
        actor.get("last_name").and_then(Value::as_str),
    ]
    .into_iter()
    .flatten()
    .filter(|s| !s.is_empty())
    .collect::<Vec<_>>()
    .join(" ");
    let mut user = existing.unwrap_or_else(|| UserRecord {
        platform: "telegram".into(),
        user_id,
        username: None,
        full_name: None,
        subscription_type: None,
        subscription_key: None,
        subscription_title: None,
        subscription_url: None,
        audience_subscription_key: None,
        audience_subscription_title: None,
        audience_subscription_url: None,
        group_name: None,
        schedule_id: None,
        is_admin: false,
        is_editor: false,
        homework_notifications_enabled: true,
        delivery_disabled_auto: false,
        created_at: now.into(),
        last_seen_at: now.into(),
        custom_sticker_file_id: None,
        university_subgroup: None,
    });
    user.username = actor
        .get("username")
        .and_then(Value::as_str)
        .map(str::to_owned);
    user.full_name = if name.is_empty() { None } else { Some(name) };
    user.is_admin = admins.contains(&user_id) || limited_admins.contains(&user_id);
    crate::database::upsert_user(db, &user, now)?;
    crate::database::get_user(db, "telegram", user_id)
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

#[cfg(test)]
mod schedule_tests {
    use super::*;
    #[test]
    fn uses_exact_date_and_keeps_schedule_buttons() {
        let today = chrono::NaiveDate::from_ymd_opt(2026, 11, 8).unwrap();
        let snapshot = crate::ScheduleSnapshot {
            group_name: "Г".into(),
            fetched_at: "2026-11-08T12:00:00".into(),
            days: vec![crate::DaySchedule {
                date_label: "2026-11-09".into(),
                date_iso: "2026-11-09".into(),
                lessons: vec![crate::Lesson {
                    number: 1,
                    subject: "Физика".into(),
                    teacher: "Иванов".into(),
                    classroom: "101".into(),
                }],
                week_variant: None,
                current_week_variant: None,
            }],
        };
        let missing = schedule_screen("today", Some(&snapshot), None, today, false);
        assert_eq!(missing.text, "Расписание на сегодня\n\nПар нет.");
        let tomorrow = schedule_screen("tomorrow", Some(&snapshot), None, today, true);
        assert!(tomorrow.text.contains("Физика"));
        assert_eq!(
            tomorrow.keyboard["inline_keyboard"]
                .as_array()
                .unwrap()
                .len(),
            8
        );
        assert!(schedule_screen("today", None, None, today, false)
            .text
            .contains("Не удалось"));
    }
}

#[cfg(test)]
mod route_tests {
    use super::*;
    #[test]
    fn handles_start_and_schedule_without_consuming_unknown_callbacks() {
        let today = chrono::NaiveDate::from_ymd_opt(2026, 11, 8).unwrap();
        let start = json!({"message":{"chat":{"id":1,"type":"private"},"text":"/start"}});
        assert!(
            route_update(&start, None, None, today, false).unwrap()[0].payload["text"]
                .as_str()
                .unwrap()
                .contains("Укажи свою группу")
        );
        let callback = json!({"callback_query":{"id":"x","message":{"chat":{"id":1}},"data":"schedule:today"}});
        let actions = route_update(&callback, None, None, today, false).unwrap();
        assert_eq!(actions.len(), 2);
        assert_eq!(actions[1].method, "answerCallbackQuery");
        let unknown = json!({"callback_query":{"id":"x","message":{"chat":{"id":1}},"data":"settings:other"}});
        assert!(route_update(&unknown, None, None, today, false).is_none());
    }
}

#[cfg(test)]
mod resolution_tests {
    use super::*;
    #[test]
    fn accepts_groups_and_unique_teachers_but_not_ambiguous_people_or_rooms() {
        let groups =
            crate::group_catalog::GroupCatalog::new(vec![crate::group_catalog::GroupInfo {
                department_id: 1,
                department_code: "A".into(),
                department_name: String::new(),
                group_name: "ИСП-25".into(),
                schedule_id: Some(1),
                url: "/rasp/1".into(),
            }]);
        let search = crate::search::SearchCatalog::new(
            &[
                ("Иванов Иван".into(), "/raspprep/1".into()),
                ("Иванов Игорь".into(), "/raspprep/2".into()),
                ("Петров Петр".into(), "/raspprep/3".into()),
            ],
            &[("101".into(), "/raspAud/1".into())],
        );
        assert_eq!(
            resolve_subscription("ИCП 25", &groups, &search)
                .unwrap()
                .subscription_key,
            "group:1"
        );
        assert_eq!(
            resolve_subscription("Петров", &groups, &search)
                .unwrap()
                .subscription_key,
            "teacher:3"
        );
        assert!(resolve_subscription("Иванов", &groups, &search).is_none());
        assert!(resolve_subscription("101", &groups, &search).is_none());
    }
}
#[cfg(test)]
mod registration_tests {
    use super::*;
    #[test]
    fn registers_actor_and_updates_admin_without_erasing_preferences() {
        let db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE users(platform TEXT,user_id INTEGER,username TEXT,full_name TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,audience_subscription_key TEXT,audience_subscription_title TEXT,audience_subscription_url TEXT,group_name TEXT,schedule_id INTEGER,is_admin INTEGER,is_editor INTEGER,homework_notifications_enabled INTEGER,delivery_disabled_auto INTEGER,created_at TEXT,last_seen_at TEXT,custom_sticker_file_id TEXT,university_subgroup INTEGER,PRIMARY KEY(platform,user_id));").unwrap();
        let update = json!({"message":{"from":{"id":42,"first_name":"Иван","last_name":"Иванов","username":"test"}}});
        let user = register_actor(&db, &update, &[42], &[], "now")
            .unwrap()
            .unwrap();
        assert!(user.is_admin);
        assert_eq!(user.full_name.as_deref(), Some("Иван Иванов"));
        db.execute("UPDATE users SET subscription_key='group:1',subscription_type='group',subscription_title='G',is_editor=1 WHERE user_id=42",[]).unwrap();
        let user = register_actor(&db, &update, &[], &[], "later")
            .unwrap()
            .unwrap();
        assert!(!user.is_admin);
        assert!(user.is_editor);
        assert_eq!(user.subscription_key.as_deref(), Some("group:1"));
    }
}

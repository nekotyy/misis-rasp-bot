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
pub(crate) fn snapshot_current_week(
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
pub fn bells_screens(university: bool) -> Vec<Screen> {
    let keyboard = crate::notification::telegram_keyboard();
    if university {
        let times = [
            "09:00–10:30",
            "10:40–12:10",
            "12:40–14:10",
            "14:20–15:50",
            "16:00–17:30",
            "18:30–20:00",
            "20:10–21:40",
        ];
        let text = format!(
            "🔔 <b>Звонки СТИ НИТУ МИСИС</b>\nУниверситет\n\n{}",
            times
                .iter()
                .enumerate()
                .map(|(i, t)| format!("<b>{} пара</b>   <code>{t}</code>", i + 1))
                .collect::<Vec<_>>()
                .join("\n")
        );
        vec![Screen { text, keyboard }]
    } else {
        let weekdays="Звонки ОПК СТИ НИТУ МИСИС\n\nБудни:\n\nПонедельник(Классный час) 8:30 - 9:20\n\n1 пара 9:00 - 10:30\n\n2 пара 10:40 - 12:10\n\nперерыв 12:10 - 12:40\n\n3 пара 12:40 - 14:10\n\nперерыв 14:10 - 14:30\n\n4 пара 14:30 - 16:00\n\n5 пара 16:10 - 17:40\n\n6 пара 17:50 - 19:20";
        let saturday="Звонки ОПК СТИ НИТУ МИСИС\n\nСуббота:\n\n1 пара 9:00 - 10:30\n\n2 пара 10:40 - 12:10\n\n3 пара 12:20 - 13:50\n\n4 пара 14:00 - 15:30\n\n5 пара 15:40 - 17:10";
        vec![
            Screen {
                text: weekdays.into(),
                keyboard: Value::Null,
            },
            Screen {
                text: saturday.into(),
                keyboard: Value::Null,
            },
            Screen {
                text: "Выбери нужный вариант расписания.".into(),
                keyboard,
            },
        ]
    }
}
pub fn route_update(
    update: &Value,
    user: Option<&UserRecord>,
    snapshot: Option<&crate::ScheduleSnapshot>,
    today: chrono::NaiveDate,
    university_source: bool,
) -> Option<Vec<BotAction>> {
    route_update_with_config(
        update,
        user,
        snapshot,
        today,
        university_source,
        "https://miniapp.opk.nekoty.ru",
    )
}
pub fn route_update_with_config(
    update: &Value,
    user: Option<&UserRecord>,
    snapshot: Option<&crate::ScheduleSnapshot>,
    today: chrono::NaiveDate,
    university_source: bool,
    miniapp_url: &str,
) -> Option<Vec<BotAction>> {
    if let Some(msg) = update.get("message") {
        let chat_id = msg.pointer("/chat/id")?.as_i64()?;
        let text = msg.get("text")?.as_str()?;
        let command = text.split_whitespace().next()?.split('@').next()?;
        if command == "/settings" {
            if msg.pointer("/chat/type").and_then(Value::as_str) != Some("private") {
                return Some(vec![]);
            }
            let screen = settings_screen(user, miniapp_url);
            return Some(vec![message(chat_id, &screen.text, Some(screen.keyboard))]);
        }
        if command == "/rasp" {
            if msg.pointer("/chat/type").and_then(Value::as_str) != Some("private") {
                return Some(vec![]);
            }
            if user.is_none_or(|u| {
                u.subscription_key.as_ref().is_none_or(|s| s.is_empty())
                    || u.subscription_title.as_ref().is_none_or(|s| s.is_empty())
            }) {
                return Some(vec![message(chat_id, GROUP_PROMPT, None)]);
            }
            return Some(vec![message(
                chat_id,
                "Выбери нужный вариант расписания.",
                Some(crate::notification::telegram_keyboard()),
            )]);
        }
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
    if data == "start:rasp"
        && user.is_some_and(|u| {
            u.subscription_key.as_ref().is_some_and(|s| !s.is_empty())
                && u.subscription_title.as_ref().is_some_and(|s| !s.is_empty())
        })
    {
        let mut actions = Vec::new();
        if let Some(sticker) = user
            .and_then(|u| u.custom_sticker_file_id.as_ref())
            .filter(|s| !s.is_empty())
        {
            actions.push(BotAction {
                method: "sendSticker".into(),
                payload: json!({"chat_id":chat_id,"sticker":sticker}),
            });
        }
        actions.push(message(
            chat_id,
            "Выбери нужный вариант расписания.",
            Some(crate::notification::telegram_keyboard()),
        ));
        actions.push(ack());
        return Some(actions);
    }
    if data == "schedule:bells" {
        let mut actions = bells_screens(university_source)
            .into_iter()
            .map(|screen| {
                message(
                    chat_id,
                    &screen.text,
                    if screen.keyboard.is_null() {
                        None
                    } else {
                        Some(screen.keyboard)
                    },
                )
            })
            .collect::<Vec<_>>();
        actions.push(ack());
        return Some(actions);
    }
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
        "menu:settings" => settings_screen(user, miniapp_url),
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

pub fn settings_screen(user: Option<&UserRecord>, miniapp_url: &str) -> Screen {
    let enabled = user.is_none_or(|u| u.homework_notifications_enabled);
    let mut lines = vec!["<b>Дополнительно</b>".to_string(), String::new()];
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
            lines.push(format!(
                "{}: <b>{}</b>",
                format::escape_html(label),
                format::escape_html(values.first().copied().unwrap_or("-"))
            ));
            lines.extend(values.iter().skip(1).map(|s| format::escape_html(s)));
        }
    } else {
        lines.push("Подписка: <b>не выбрана</b>".into());
    }
    lines.push(format!(
        "Уведомления: <b>{}</b>",
        if enabled {
            "включены"
        } else {
            "выключены"
        }
    ));
    let mut rows = vec![
        json!([{"text":"Пройденные пары","callback_data":"settings:lesson_counters"}]),
        json!([{"text":"О проекте","callback_data":"settings:about"}]),
        json!([{"text":if enabled {"Отключить уведомления"}else{"Включить уведомления"},"callback_data":"settings:toggle_notifications"}]),
    ];
    if let Some(user) = user.filter(|u| u.subscription_type.as_deref() == Some("teacher")) {
        let has_room = user
            .audience_subscription_key
            .as_ref()
            .is_some_and(|s| !s.is_empty());
        rows.push(json!([{"text":if has_room {"Изменить кабинет"}else{"Подписаться на кабинет"},"callback_data":"settings:audience_setup"}]));
        if has_room {
            rows.push(json!([{"text":"Убрать кабинет","callback_data":"settings:audience_clear"}]));
        }
    }
    if user.is_some_and(|u| u.subscription_key.as_ref().is_some_and(|s| !s.is_empty())) {
        rows.push(json!([{"text":"Отписаться","callback_data":"settings:clear_group"}]));
    }
    rows.push(json!([{"text":"Открыть Mini App","web_app":{"url":miniapp_url.trim()}}]));
    rows.push(json!([{"text":"Обратная связь","callback_data":"settings:feedback"},{"text":"Поддержка","callback_data":"settings:support"},{"text":"Сообщить о баге","callback_data":"settings:bugreport"}]));
    rows.push(json!([{"text":"Персонализация","callback_data":"settings:personalization"}]));
    rows.push(json!([{"text":"Помощь","callback_data":"settings:help"}]));
    rows.push(json!([{"text":"Назад","callback_data":"menu:start"}]));
    Screen {
        text: lines.join("\n"),
        keyboard: json!({"inline_keyboard":rows}),
    }
}

pub fn route_stateful(
    db: &mut rusqlite::Connection,
    update: &Value,
    snapshot: Option<&crate::ScheduleSnapshot>,
    today: chrono::NaiveDate,
    university_source: bool,
    miniapp_url: &str,
    now: &str,
) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let result = route_stateful_in_transaction(
        &tx,
        update,
        snapshot,
        today,
        university_source,
        miniapp_url,
        now,
    )?;
    tx.commit()?;
    Ok(result)
}
pub fn route_stateful_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    update: &Value,
    snapshot: Option<&crate::ScheduleSnapshot>,
    today: chrono::NaiveDate,
    university_source: bool,
    miniapp_url: &str,
    now: &str,
) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
    if let Some(callback) = update.get("callback_query") {
        if let Some((text, keyboard)) = callback
            .get("data")
            .and_then(Value::as_str)
            .and_then(crate::telegram_help::screen)
        {
            if let (Some(chat), Some(message_id), Some(id)) = (
                callback.pointer("/message/chat/id").and_then(Value::as_i64),
                callback
                    .pointer("/message/message_id")
                    .and_then(Value::as_i64),
                callback.get("id").and_then(Value::as_str),
            ) {
                return Ok(Some(vec![
                    BotAction {
                        method: "editMessageText".into(),
                        payload: json!({"chat_id":chat,"message_id":message_id,"text":text,"parse_mode":"HTML","reply_markup":keyboard}),
                    },
                    BotAction {
                        method: "answerCallbackQuery".into(),
                        payload: json!({"callback_query_id":id}),
                    },
                ]));
            }
            return Ok(None);
        }
    }
    let actor_id = update
        .pointer("/callback_query/from/id")
        .or_else(|| update.pointer("/message/from/id"))
        .and_then(Value::as_i64);
    let user = match actor_id {
        Some(id) => crate::database::get_user(tx, "telegram", id)?,
        None => None,
    };
    if matches!(
        update
            .pointer("/callback_query/data")
            .and_then(Value::as_str),
        Some("settings:audience_clear" | "settings:clear_group")
    ) {
        let Some(id) = actor_id else {
            return Ok(None);
        };
        let Some(chat) = update
            .pointer("/callback_query/message/chat/id")
            .and_then(Value::as_i64)
        else {
            return Ok(None);
        };
        let Some(message_id) = update
            .pointer("/callback_query/message/message_id")
            .and_then(Value::as_i64)
        else {
            return Ok(None);
        };
        let Some(callback_id) = update.pointer("/callback_query/id").and_then(Value::as_str) else {
            return Ok(None);
        };
        let is_room = update
            .pointer("/callback_query/data")
            .and_then(Value::as_str)
            == Some("settings:audience_clear");
        let (text, keyboard, answer) = if is_room {
            crate::database::clear_audience_in_transaction(tx, "telegram", id)?;
            let updated = crate::database::get_user(tx, "telegram", id)?;
            let screen = settings_screen(updated.as_ref(), miniapp_url);
            (screen.text, Some(screen.keyboard), "Кабинет отвязан")
        } else {
            crate::database::clear_user_subscription_in_transaction(tx, "telegram", id)?;
            ("<b>Укажи свою группу</b>\n\nНапиши её как на сайте: например, ИСП-25-1 или ТЭ/ПТ-26д.\nНапример: <b>ИСП-25-1</b>\n\nРегистр не важен.\n\nТы отписался от своей группы. Выбери новую, когда захочешь.".into(),None,"Группа сброшена")
        };
        let mut payload =
            json!({"chat_id":chat,"message_id":message_id,"text":text,"parse_mode":"HTML"});
        if let Some(keyboard) = keyboard {
            payload["reply_markup"] = keyboard;
        }
        return Ok(Some(vec![
            BotAction {
                method: "editMessageText".into(),
                payload,
            },
            BotAction {
                method: "answerCallbackQuery".into(),
                payload: json!({"callback_query_id":callback_id,"text":answer}),
            },
        ]));
    }
    if update
        .pointer("/callback_query/data")
        .and_then(Value::as_str)
        == Some("settings:toggle_notifications")
    {
        let Some(id) = actor_id else {
            return Ok(None);
        };
        let Some(chat_id) = update
            .pointer("/callback_query/message/chat/id")
            .and_then(Value::as_i64)
        else {
            return Ok(None);
        };
        let Some(message_id) = update
            .pointer("/callback_query/message/message_id")
            .and_then(Value::as_i64)
        else {
            return Ok(None);
        };
        let Some(callback_id) = update.pointer("/callback_query/id").and_then(Value::as_str) else {
            return Ok(None);
        };
        let enabled = !user
            .as_ref()
            .is_none_or(|u| u.homework_notifications_enabled);
        crate::database::set_notifications_enabled_in_transaction(
            tx, "telegram", id, enabled, now,
        )?;
        let updated = crate::database::get_user(tx, "telegram", id)?;
        let screen = settings_screen(updated.as_ref(), miniapp_url);
        return Ok(Some(vec![
            BotAction {
                method: "editMessageText".into(),
                payload: json!({"chat_id":chat_id,"message_id":message_id,"text":screen.text,"parse_mode":"HTML","reply_markup":screen.keyboard}),
            },
            BotAction {
                method: "answerCallbackQuery".into(),
                payload: json!({"callback_query_id":callback_id,"text":"Уведомления обновлены"}),
            },
        ]));
    }
    Ok(route_update_with_config(
        update,
        user.as_ref(),
        snapshot,
        today,
        university_source,
        miniapp_url,
    ))
}

pub fn apply_selected_subscription(
    db: &mut rusqlite::Connection,
    platform: &str,
    user_id: i64,
    subscription: &crate::subscription::Subscription,
    now: &str,
) -> Result<Option<UserRecord>, Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let result =
        apply_selected_subscription_in_transaction(&tx, platform, user_id, subscription, now)?;
    tx.commit()?;
    Ok(result)
}
pub fn apply_selected_subscription_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    platform: &str,
    user_id: i64,
    subscription: &crate::subscription::Subscription,
    now: &str,
) -> Result<Option<UserRecord>, Box<dyn std::error::Error>> {
    let previous = crate::database::get_user(tx, platform, user_id)?;
    if previous.is_none() {
        return Err("Пользователь не зарегистрирован".into());
    }
    let clear = subscription.subscription_type != "teacher"
        || previous.as_ref().is_some_and(|old| {
            old.subscription_type.as_deref() != Some("teacher")
                || old.subscription_key.as_deref() != Some(subscription.subscription_key.as_str())
        });
    crate::database::set_subscription_in_transaction(tx, platform, user_id, subscription, now)?;
    if clear {
        crate::database::clear_audience_in_transaction(tx, platform, user_id)?;
    }
    let user = crate::database::get_user(tx, platform, user_id)?;
    Ok(user)
}
pub enum SelectionResult {
    Selected(Box<UserRecord>),
    Ambiguous(Vec<crate::search::SearchTarget>),
    NotFound,
}
#[derive(Clone, Copy)]
pub struct SelectionContext<'a> {
    pub platform: &'a str,
    pub user_id: i64,
    pub input: &'a str,
    pub groups: &'a crate::group_catalog::GroupCatalog,
    pub search: &'a crate::search::SearchCatalog,
    pub snapshots: &'a [crate::ScheduleSnapshot],
    pub now: &'a str,
}
pub fn select_subscription(
    db: &mut rusqlite::Connection,
    context: &SelectionContext<'_>,
) -> Result<SelectionResult, Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let result = select_subscription_in_transaction(&tx, context)?;
    tx.commit()?;
    Ok(result)
}
pub fn select_subscription_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    context: &SelectionContext<'_>,
) -> Result<SelectionResult, Box<dyn std::error::Error>> {
    let SelectionContext {
        platform,
        user_id,
        input,
        groups,
        search,
        snapshots,
        now,
    } = *context;
    let matches = search.find_all_with_snapshots(input, groups, snapshots);
    if matches.len() > 1 && matches.iter().all(|item| item.kind == "teacher") {
        return Ok(SelectionResult::Ambiguous(matches));
    }
    let Some(subscription) = resolve_subscription_with_snapshots(input, groups, search, snapshots)
    else {
        return Ok(SelectionResult::NotFound);
    };
    let Some(user) =
        apply_selected_subscription_in_transaction(tx, platform, user_id, &subscription, now)?
    else {
        return Err("Пользователь не зарегистрирован".into());
    };
    Ok(SelectionResult::Selected(Box::new(user)))
}

#[derive(Clone)]
struct ChoiceState {
    token: String,
    targets: Vec<crate::search::SearchTarget>,
}
#[derive(Clone, Default)]
pub struct SelectionFlow {
    choices: std::collections::HashMap<(i64, i64), ChoiceState>,
}
pub struct SelectionRequest<'a> {
    pub platform: &'a str,
    pub chat_id: i64,
    pub user_id: i64,
    pub input: &'a str,
    pub groups: &'a crate::group_catalog::GroupCatalog,
    pub search: &'a crate::search::SearchCatalog,
    pub snapshots: &'a [crate::ScheduleSnapshot],
    pub now: &'a str,
}
impl SelectionFlow {
    pub fn input(
        &mut self,
        db: &mut rusqlite::Connection,
        request: &SelectionRequest<'_>,
    ) -> Result<Vec<BotAction>, Box<dyn std::error::Error>> {
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let mut next = self.clone();
        let result = next.input_in_transaction(&tx, request)?;
        tx.commit()?;
        *self = next;
        Ok(result)
    }
    pub fn input_in_transaction(
        &mut self,
        tx: &rusqlite::Transaction<'_>,
        request: &SelectionRequest<'_>,
    ) -> Result<Vec<BotAction>, Box<dyn std::error::Error>> {
        self.choices.remove(&(request.chat_id, request.user_id));
        let context = SelectionContext {
            platform: request.platform,
            user_id: request.user_id,
            input: request.input,
            groups: request.groups,
            search: request.search,
            snapshots: request.snapshots,
            now: request.now,
        };
        match select_subscription_in_transaction(tx, &context)? {
            SelectionResult::Selected(user) => {
                let university = subgroups::is_university_group(Some(&user))
                    || user
                        .subscription_url
                        .as_deref()
                        .and_then(crate::sources::university_kind)
                        .is_some();
                Ok(vec![message(
                    request.chat_id,
                    &welcome_text(Some(&user), None, university),
                    Some(start_keyboard(Some(&user))),
                )])
            }
            SelectionResult::NotFound => Ok(vec![message(
                request.chat_id,
                &format!("{GROUP_PROMPT}\n\n{SEARCH_NOT_FOUND}"),
                None,
            )]),
            SelectionResult::Ambiguous(targets) => {
                let mut bytes = [0_u8; 4];
                getrandom::getrandom(&mut bytes).map_err(|_| "Не удалось создать выбор")?;
                let token = bytes.iter().map(|b| format!("{b:02x}")).collect();
                self.choices.insert(
                    (request.chat_id, request.user_id),
                    ChoiceState { token, targets },
                );
                Ok(vec![self
                    .choice_screen(request.chat_id, request.user_id, 0)
                    .expect("choice exists")])
            }
        }
    }
    fn choice_screen(&self, chat_id: i64, user_id: i64, page: usize) -> Option<BotAction> {
        let choice = self.choices.get(&(chat_id, user_id))?;
        let start = page.checked_mul(8)?;
        if start >= choice.targets.len() {
            return None;
        }
        let mut rows = Vec::new();
        for (index, target) in choice.targets.iter().enumerate().skip(start).take(8) {
            rows.push(json!([{"text":crate::search::choice_label(target,&choice.targets),"callback_data":format!("teacher:pick:{}:{index}",choice.token)}]));
        }
        let mut navigation = Vec::new();
        if page > 0 {
            navigation.push(json!({"text":"Предыдущая страница","callback_data":format!("teacher:page:{}:{}",choice.token,page-1)}));
        }
        if start + 8 < choice.targets.len() {
            navigation.push(json!({"text":"Следующая страница","callback_data":format!("teacher:page:{}:{}",choice.token,page+1)}));
        }
        if !navigation.is_empty() {
            rows.push(Value::Array(navigation));
        }
        rows.push(json!([{"text":"Назад","callback_data":"menu:start"}]));
        Some(message(chat_id,"Нашлось несколько преподавателей. Какой именно нужен?\nВыбери преподавателя кнопкой или уточни запрос.",Some(json!({"inline_keyboard":rows}))))
    }
    pub fn choose(
        &mut self,
        db: &mut rusqlite::Connection,
        chat_id: i64,
        user_id: i64,
        callback_id: &str,
        data: &str,
        now: &str,
    ) -> Result<Vec<BotAction>, Box<dyn std::error::Error>> {
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let mut next = self.clone();
        let result = next.choose_in_transaction(&tx, chat_id, user_id, callback_id, data, now)?;
        tx.commit()?;
        *self = next;
        Ok(result)
    }
    pub fn choose_in_transaction(
        &mut self,
        tx: &rusqlite::Transaction<'_>,
        chat_id: i64,
        user_id: i64,
        callback_id: &str,
        data: &str,
        now: &str,
    ) -> Result<Vec<BotAction>, Box<dyn std::error::Error>> {
        let invalid = || {
            vec![BotAction {
                method: "answerCallbackQuery".into(),
                payload: json!({"callback_query_id":callback_id,"text":"Этот выбор устарел. Введи запрос заново.","show_alert":true}),
            }]
        };
        let parts: Vec<_> = data.split(':').collect();
        if parts.len() != 4 || parts[0] != "teacher" {
            return Ok(invalid());
        }
        let Some(choice) = self.choices.get(&(chat_id, user_id)) else {
            return Ok(invalid());
        };
        if choice.token != parts[2] {
            return Ok(invalid());
        }
        let Ok(index) = parts[3].parse::<usize>() else {
            return Ok(invalid());
        };
        if parts[1] == "page" {
            let Some(screen) = self.choice_screen(chat_id, user_id, index) else {
                return Ok(invalid());
            };
            return Ok(vec![
                screen,
                BotAction {
                    method: "answerCallbackQuery".into(),
                    payload: json!({"callback_query_id":callback_id}),
                },
            ]);
        }
        if parts[1] != "pick" {
            return Ok(invalid());
        }
        let Some(target) = choice.targets.get(index).cloned() else {
            return Ok(invalid());
        };
        let subscription = crate::subscription::teacher_subscription(&target.title, &target.url);
        let Some(user) = apply_selected_subscription_in_transaction(
            tx,
            "telegram",
            user_id,
            &subscription,
            now,
        )?
        else {
            return Err("Пользователь не зарегистрирован".into());
        };
        self.choices.remove(&(chat_id, user_id));
        let university = crate::sources::university_kind(&target.url).is_some();
        Ok(vec![
            message(
                chat_id,
                &welcome_text(Some(&user), None, university),
                Some(start_keyboard(Some(&user))),
            ),
            BotAction {
                method: "answerCallbackQuery".into(),
                payload: json!({"callback_query_id":callback_id}),
            },
        ])
    }
}

pub fn register_group_chat(
    db: &rusqlite::Connection,
    update: &Value,
    now: &str,
) -> rusqlite::Result<()> {
    let Some(chat) = update.pointer("/message/chat") else {
        return Ok(());
    };
    if !matches!(
        chat.get("type").and_then(Value::as_str),
        Some("group" | "supergroup")
    ) {
        return Ok(());
    }
    let Some(id) = chat.get("id").and_then(Value::as_i64) else {
        return Ok(());
    };
    let title = chat
        .get("title")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| id.to_string());
    let mut group = crate::database::get_user(db, "telegram", id)?.unwrap_or_else(|| UserRecord {
        platform: "telegram".into(),
        user_id: id,
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
    group.username = None;
    group.full_name = Some(format!("TG group: {title}"));
    group.is_admin = false;
    crate::database::upsert_user(db, &group, now)
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
        let rasp = json!({"message":{"chat":{"id":1,"type":"private"},"text":"/rasp"}});
        assert_eq!(
            route_update(&rasp, None, None, today, false).unwrap()[0].payload["text"],
            GROUP_PROMPT
        );
        let group_rasp = json!({"message":{"chat":{"id":-1,"type":"group"},"text":"/rasp"}});
        assert!(route_update(&group_rasp, None, None, today, false)
            .unwrap()
            .is_empty());
        let settings = json!({"message":{"chat":{"id":1,"type":"private"},"text":"/settings"}});
        let actions = route_update_with_config(
            &settings,
            None,
            None,
            today,
            false,
            "https://example.org/app",
        )
        .unwrap();
        assert!(actions[0].payload["reply_markup"]
            .to_string()
            .contains("https://example.org/app"));
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

#[cfg(test)]
mod bells_tests {
    use super::*;
    #[test]
    fn bells_keep_both_college_schedules_and_university_week() {
        let college = bells_screens(false);
        assert_eq!(college.len(), 3);
        assert!(college[0].text.contains("Классный час"));
        assert!(college[1].text.contains("Суббота"));
        let university = bells_screens(true);
        assert_eq!(university.len(), 1);
        assert!(university[0]
            .text
            .contains("<b>7 пара</b>   <code>20:10–21:40</code>"));
    }
}

#[cfg(test)]
mod settings_tests {
    use super::*;
    #[test]
    fn settings_keep_miniapp_navigation_and_notification_state() {
        let user:UserRecord=serde_json::from_value(json!({"platform":"telegram","user_id":1,"subscription_type":"teacher","subscription_key":"teacher:1","subscription_title":"<Иванов>","audience_subscription_key":"audience:2","audience_subscription_title":"101","is_admin":false,"is_editor":false,"homework_notifications_enabled":false,"delivery_disabled_auto":false,"created_at":"now","last_seen_at":"now"})).unwrap();
        let screen = settings_screen(Some(&user), "https://example.org/app");
        assert!(screen.text.contains("&lt;Иванов&gt;"));
        assert!(screen.text.contains("выключены"));
        let rows = screen.keyboard["inline_keyboard"].as_array().unwrap();
        assert!(rows
            .iter()
            .flat_map(|row| row.as_array().unwrap().iter())
            .any(|button| button["callback_data"] == "settings:audience_clear"));
        assert!(rows
            .iter()
            .flat_map(|row| row.as_array().unwrap().iter())
            .any(|button| button["web_app"]["url"] == "https://example.org/app"));
    }
}

#[cfg(test)]
mod stateful_tests {
    use super::*;
    #[test]
    fn toggles_notifications_and_returns_updated_keyboard() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE users(platform TEXT,user_id INTEGER,username TEXT,full_name TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,audience_subscription_key TEXT,audience_subscription_title TEXT,audience_subscription_url TEXT,group_name TEXT,schedule_id INTEGER,is_admin INTEGER,is_editor INTEGER,homework_notifications_enabled INTEGER,delivery_disabled_auto INTEGER,created_at TEXT,last_seen_at TEXT,custom_sticker_file_id TEXT,university_subgroup INTEGER,PRIMARY KEY(platform,user_id)); CREATE TABLE account_identities(account_id TEXT,platform TEXT,user_id INTEGER); CREATE TABLE accounts(id TEXT,theme TEXT,accent TEXT,density TEXT,reduced_motion INTEGER,notifications_enabled INTEGER,updated_at TEXT); INSERT INTO users(platform,user_id,is_admin,is_editor,homework_notifications_enabled,delivery_disabled_auto,created_at,last_seen_at) VALUES('telegram',42,0,0,1,0,'now','now');").unwrap();
        let callback = json!({"callback_query":{"id":"cb","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:toggle_notifications"}});
        let today = chrono::NaiveDate::from_ymd_opt(2026, 11, 8).unwrap();
        let actions = route_stateful(
            &mut db,
            &callback,
            None,
            today,
            false,
            "https://example.org",
            "now",
        )
        .unwrap()
        .unwrap();
        assert_eq!(actions[0].method, "editMessageText");
        assert!(actions[0].payload["text"]
            .as_str()
            .unwrap()
            .contains("выключены"));
        assert_eq!(actions[1].payload["text"], "Уведомления обновлены");
        assert!(
            !crate::database::get_user(&db, "telegram", 42)
                .unwrap()
                .unwrap()
                .homework_notifications_enabled
        );
        db.execute("UPDATE users SET subscription_type='teacher',subscription_key='teacher:1',subscription_title='T',audience_subscription_key='audience:1',audience_subscription_title='101'",[]).unwrap();
        let mut room = callback.clone();
        room["callback_query"]["data"] = json!("settings:audience_clear");
        let result = route_stateful(
            &mut db,
            &room,
            None,
            today,
            false,
            "https://example.org",
            "now",
        )
        .unwrap()
        .unwrap();
        assert_eq!(result[1].payload["text"], "Кабинет отвязан");
        assert!(crate::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap()
            .audience_subscription_key
            .is_none());
        let mut clear = callback.clone();
        clear["callback_query"]["data"] = json!("settings:clear_group");
        let result = route_stateful(
            &mut db,
            &clear,
            None,
            today,
            false,
            "https://example.org",
            "now",
        )
        .unwrap()
        .unwrap();
        assert_eq!(result[1].payload["text"], "Группа сброшена");
        assert!(crate::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap()
            .subscription_key
            .is_none());
    }
}

#[cfg(test)]
mod selection_flow_tests {
    use super::*;
    #[test]
    fn ambiguous_choices_are_scoped_and_paginated() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        let groups = crate::group_catalog::GroupCatalog::default();
        let teachers = (0..10)
            .map(|i| (format!("Иванов Имя{i}"), format!("/raspprep/{i}")))
            .collect::<Vec<_>>();
        let search = crate::search::SearchCatalog::new(&teachers, &[]);
        let mut flow = SelectionFlow::default();
        let request = SelectionRequest {
            platform: "telegram",
            chat_id: 1,
            user_id: 2,
            input: "Иванов",
            groups: &groups,
            search: &search,
            snapshots: &[],
            now: "now",
        };
        let actions = flow.input(&mut db, &request).unwrap();
        let rows = actions[0].payload["reply_markup"]["inline_keyboard"]
            .as_array()
            .unwrap();
        assert_eq!(rows.len(), 10);
        let next = rows[8][0]["callback_data"].as_str().unwrap();
        let page = flow.choose(&mut db, 1, 2, "cb", next, "now").unwrap();
        assert!(
            page[0].payload["reply_markup"]["inline_keyboard"]
                .as_array()
                .unwrap()
                .len()
                >= 3
        );
        let invalid = flow.choose(&mut db, 1, 3, "cb", next, "now").unwrap();
        assert_eq!(invalid[0].payload["show_alert"], true);
    }
}

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};

use crate::database::{get_user, latest_snapshot, upsert_user};
use crate::telegram_engine::EngineContext;
use crate::telegram_ui::BotAction;
use crate::UserRecord;

pub const VK_CHAT_PEER_THRESHOLD: i64 = 2_000_000_000;

#[derive(Debug, Default, Clone)]
pub struct VkEngine {
    pub awaiting_group: HashSet<i64>,
    pub awaiting_feedback: HashMap<i64, String>,
    pub awaiting_admin_json_import: HashSet<i64>,
    pub admin_json_drafts: HashMap<i64, crate::json_import::ImportResult>,
}

impl VkEngine {
    pub fn handle(
        &mut self,
        tx: &rusqlite::Transaction<'_>,
        update: &Value,
        context: &EngineContext<'_>,
        now: &str,
    ) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
        let event_type = update.get("type").and_then(Value::as_str).unwrap_or("");
        match event_type {
            "message_new" => self.handle_message_new(tx, update, context, now),
            "message_event" => self.handle_message_event(tx, update, context, now),
            _ => Ok(None),
        }
    }

    fn handle_message_new(
        &mut self,
        tx: &rusqlite::Transaction<'_>,
        update: &Value,
        context: &EngineContext<'_>,
        now: &str,
    ) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
        let object = update.get("object").and_then(Value::as_object);
        let message = object
            .and_then(|o| o.get("message").filter(|v| v.is_object()))
            .or_else(|| update.get("object"));

        let Some(message) = message else {
            return Ok(None);
        };

        let peer_id = message.get("peer_id").and_then(Value::as_i64).unwrap_or(0);
        let from_id = message.get("from_id").and_then(Value::as_i64).unwrap_or(0);
        if peer_id == 0 || from_id < 0 {
            // Игнорируем отрицательные from_id (сообщения от других ботов и групп)
            return Ok(None);
        }

        let is_chat = peer_id >= VK_CHAT_PEER_THRESHOLD;
        let actor_id = if is_chat { peer_id } else { from_id };

        // Регистрация пользователя / беседы в БД
        let existing = get_user(tx, "vk", actor_id)?;
        let mut user = existing.unwrap_or_else(|| UserRecord {
            platform: "vk".into(),
            user_id: actor_id,
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
        user.last_seen_at = now.into();
        let is_admin = user.is_admin || context.settings.admin_vk_id == Some(actor_id);
        if is_admin {
            user.is_admin = true;
        }
        upsert_user(tx, &user, now)?;

        // Обработка системного события добавления бота в беседу
        if let Some(action) = message.get("action") {
            let action_type = action.get("type").and_then(Value::as_str).unwrap_or("");
            let member_id = action.get("member_id").and_then(Value::as_i64).unwrap_or(0);
            if matches!(action_type, "chat_invite_user" | "chat_invite_user_by_link")
                && member_id < 0
            {
                let welcome = concat!(
                    "<b>Инструкция по настройке бота в беседе</b>\n\n",
                    "Бот успешно добавлен в вашу беседу.\n\n",
                    "Пошаговая настройка:\n",
                    "1. Предоставьте боту доступ к переписке (или назначьте администратором беседы).\n",
                    "2. Отправьте в беседу команду <b>/startgroup</b> или укажите группу.\n",
                    "3. Например: <b>ИСП-25-1</b>\n\n",
                    "После этого беседе будут доступны расписание и автоматические уведомления об изменениях."
                );
                return Ok(Some(vec![vk_send_message(peer_id, welcome, None)]));
            }
        }

        let raw_text = message
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let payload_str = message.get("payload").and_then(Value::as_str);
        let payload: Option<Value> = payload_str.and_then(|s| serde_json::from_str(s).ok());

        // Очистка упоминания бота: [club12345|@bot] текст -> текст
        let text = strip_vk_mention(raw_text);
        let normalized = text.to_lowercase();

        // Админ-команды и управление
        if matches!(
            normalized.as_str(),
            "/admin" | "admin" | "админка" | "админ"
        ) {
            if !is_admin {
                return Ok(Some(vec![vk_send_message(
                    peer_id,
                    "Команда доступна только администраторам бота.",
                    None,
                )]));
            }
            self.awaiting_admin_json_import.remove(&actor_id);
            self.admin_json_drafts.remove(&actor_id);
            let text = "<b>Панель администратора VK</b>\n───────────────────────────\nВыбери раздел для управления:";
            let kb = vk_admin_main_keyboard();
            return Ok(Some(vec![vk_send_message(peer_id, text, Some(kb))]));
        }

        if normalized == "закрыть админку" && is_admin {
            self.awaiting_admin_json_import.remove(&actor_id);
            self.admin_json_drafts.remove(&actor_id);
            let menu_text = format_vk_main_menu(&user);
            let kb = vk_main_keyboard(user.subscription_key.is_some());
            return Ok(Some(vec![vk_send_message(peer_id, &menu_text, Some(kb))]));
        }

        if normalized == "назад в админку" && is_admin {
            self.awaiting_admin_json_import.remove(&actor_id);
            self.admin_json_drafts.remove(&actor_id);
            let text = "<b>Панель администратора VK</b>\n───────────────────────────\nВыбери раздел для управления:";
            let kb = vk_admin_main_keyboard();
            return Ok(Some(vec![vk_send_message(peer_id, text, Some(kb))]));
        }

        if (normalized == "расписание и ocr" || normalized == "расписание и оцр") && is_admin
        {
            let text = "<b>Расписание и OCR</b>\n───────────────────────────\nУправление источниками и загрузка готового JSON:";
            let kb = vk_admin_schedule_keyboard();
            return Ok(Some(vec![vk_send_message(peer_id, text, Some(kb))]));
        }

        if (normalized == "импорт ocr json" || normalized == "импорт json") && is_admin
        {
            self.awaiting_admin_json_import.insert(actor_id);
            self.admin_json_drafts.remove(&actor_id);
            let prompt = concat!(
                "<b>Импорт готового JSON расписания (OCR)</b>\n───────────────────────────\n\n",
                "Пришли готовый JSON-текст расписания сообщением.\n\n",
                "Поддерживаются форматы:\n",
                "• Одиночная группа: <code>{\"group_name\": \"...\", \"days\": [...]}</code>\n",
                "• Сводное расписание: <code>{\"date_iso\": \"...\", \"groups\": [...]}</code>\n",
                "• Массив групп: <code>[{\"group_name\": \"...\", ...}]</code>"
            );
            let kb = vk_simple_keyboard(&[&["Отменить"]]);
            return Ok(Some(vec![vk_send_message(peer_id, prompt, Some(kb))]));
        }

        if is_admin && self.awaiting_admin_json_import.contains(&actor_id) {
            if normalized == "отменить" {
                self.awaiting_admin_json_import.remove(&actor_id);
                self.admin_json_drafts.remove(&actor_id);
                let text = "Импорт отменён.\n\nВозврат в меню расписания:";
                let kb = vk_admin_schedule_keyboard();
                return Ok(Some(vec![vk_send_message(peer_id, text, Some(kb))]));
            }

            match crate::json_import::parse_schedule_json(&text) {
                Ok(import_result) => {
                    self.awaiting_admin_json_import.remove(&actor_id);
                    let groups_list = import_result
                        .snapshots
                        .iter()
                        .map(|s| {
                            format!(
                                "• <b>{}</b> (пар: {})",
                                crate::format::escape_html(&s.group_name),
                                s.days.iter().map(|d| d.lessons.len()).sum::<usize>()
                            )
                        })
                        .collect::<Vec<_>>()
                        .join("\n");
                    let preview = format!(
                        "<b>Предпросмотр импорта расписания (OCR JSON)</b>\n───────────────────────────\n\n\
                        Групп в импорте: <b>{}</b>\nВсего пар: <b>{}</b>\n\n\
                        {}\n\n\
                        Выбери действие для применения:",
                        import_result.snapshots.len(),
                        import_result.total_lessons,
                        groups_list,
                    );
                    self.admin_json_drafts.insert(actor_id, import_result);
                    let kb = vk_simple_keyboard(&[
                        &["Подтвердить и разослать"],
                        &["Сохранить без рассылки"],
                        &["Отменить"],
                    ]);
                    return Ok(Some(vec![vk_send_message(peer_id, &preview, Some(kb))]));
                }
                Err(err) => {
                    let err_text = format!(
                        "❌ <b>Ошибка при разборе JSON:</b>\n\n<code>{}</code>\n\nПришли исправленный JSON или нажми «Отменить»:",
                        crate::format::escape_html(&err)
                    );
                    let kb = vk_simple_keyboard(&[&["Отменить"]]);
                    return Ok(Some(vec![vk_send_message(peer_id, &err_text, Some(kb))]));
                }
            }
        }

        if is_admin
            && (normalized == "подтвердить и разослать" || normalized == "сохранить без рассылки")
        {
            let notify = normalized == "подтвердить и разослать";
            if let Some(draft) = self.admin_json_drafts.remove(&actor_id) {
                let now_ts = context.timestamp;
                match crate::json_import::apply_import_in_transaction(
                    tx,
                    &draft.snapshots,
                    notify,
                    now,
                    now_ts,
                ) {
                    Ok(count) => {
                        let text = format!(
                            "✅ <b>Импорт успешно завершён!</b>\n\n• Обновлено групп: <b>{count}</b>\n• Всего пар: <b>{}</b>\n• Оповещение подписчиков: <b>{}</b>",
                            draft.total_lessons,
                            if notify { "Да" } else { "Нет (тихий режим)" },
                        );
                        let kb = vk_admin_schedule_keyboard();
                        return Ok(Some(vec![vk_send_message(peer_id, &text, Some(kb))]));
                    }
                    Err(err) => {
                        let text = format!(
                            "❌ <b>Ошибка при сохранении расписания:</b>\n\n<code>{}</code>",
                            crate::format::escape_html(&err.to_string())
                        );
                        let kb = vk_admin_schedule_keyboard();
                        return Ok(Some(vec![vk_send_message(peer_id, &text, Some(kb))]));
                    }
                }
            } else {
                let text = "⚠️ Данные импорта устарели. Пришлите JSON заново.";
                let kb = vk_admin_schedule_keyboard();
                return Ok(Some(vec![vk_send_message(peer_id, text, Some(kb))]));
            }
        }

        if normalized == "мониторинг" && is_admin {
            let stats = crate::database::collect_admin_status(tx)?;
            let text = crate::telegram_admin::format_admin_status_report(&stats);
            let kb = vk_simple_keyboard(&[&["Назад в админку"]]);
            return Ok(Some(vec![vk_send_message(peer_id, &text, Some(kb))]));
        }

        // 1. Команды старта / меню
        if matches!(normalized.as_str(), "/start" | "start" | "начать" | "меню") {
            self.awaiting_group.remove(&actor_id);
            self.awaiting_feedback.remove(&actor_id);
            if user.subscription_key.is_none() {
                self.awaiting_group.insert(actor_id);
                let prompt = "Привет! Я бот расписания колледжа и университета МИСИС.\n\nУкажи свою учебную группу (например: ИСП-25-1):";
                return Ok(Some(vec![vk_send_message(peer_id, prompt, None)]));
            } else {
                let menu_text = format_vk_main_menu(&user);
                let kb = vk_main_keyboard(user.subscription_key.is_some());
                return Ok(Some(vec![vk_send_message(peer_id, &menu_text, Some(kb))]));
            }
        }

        // 2. Ожидание группы
        if self.awaiting_group.contains(&actor_id) && !text.starts_with('/') {
            if let Some(target) = crate::telegram_ui::resolve_subscription_with_snapshots(
                &text,
                context.groups,
                context.search,
                context.group_snapshots,
            ) {
                crate::database::set_subscription_in_transaction(tx, "vk", actor_id, &target, now)?;
                self.awaiting_group.remove(&actor_id);
                let title = target.subscription_title.as_str();
                let success = format!(
                    "Группа успешно сохранена: <b>{}</b>!\n\nТеперь ты можешь быстро смотреть расписание и получать уведомления.",
                    title
                );
                let kb = vk_main_keyboard(true);
                return Ok(Some(vec![vk_send_message(peer_id, &success, Some(kb))]));
            } else {
                let not_found = format!(
                    "Группа «{text}» не найдена.\n\nПроверь правильность написания или напиши фамилию преподавателя (например: ИСП-25-1 или Волошина):"
                );
                return Ok(Some(vec![vk_send_message(peer_id, &not_found, None)]));
            }
        }

        let snapshot = if let (Some(key), Some(title)) = (
            user.subscription_key.as_deref(),
            user.subscription_title.as_deref(),
        ) {
            if key.is_empty() || title.is_empty() {
                None
            } else {
                latest_snapshot(tx, "current", user.schedule_id, Some(key))?.map(|row| row.content)
            }
        } else {
            None
        };
        let university = crate::subgroups::is_university_group(Some(&user));

        // 3. Быстрые команды расписания
        let cmd = payload
            .as_ref()
            .and_then(|p| p.get("cmd").and_then(Value::as_str))
            .unwrap_or(normalized.as_str());

        if matches!(
            cmd,
            "schedule:today" | "расписание на сегодня" | "сегодня" | "расписание сегодня"
        ) {
            let today = context.now.date();
            let screen = crate::telegram_ui::schedule_screen(
                "today",
                snapshot.as_ref(),
                Some(&user),
                today,
                university,
            );
            let kb = vk_main_keyboard(true);
            return Ok(Some(vec![vk_send_message(peer_id, &screen.text, Some(kb))]));
        }

        if matches!(
            cmd,
            "schedule:tomorrow" | "расписание на завтра" | "завтра" | "расписание завтра"
        ) {
            let tomorrow = context.now.date() + chrono::Duration::days(1);
            let screen = crate::telegram_ui::schedule_screen(
                "tomorrow",
                snapshot.as_ref(),
                Some(&user),
                tomorrow,
                university,
            );
            let kb = vk_main_keyboard(true);
            return Ok(Some(vec![vk_send_message(peer_id, &screen.text, Some(kb))]));
        }

        if matches!(
            cmd,
            "schedule:after" | "расписание на 2 дня" | "послезавтра"
        ) {
            let day_after = context.now.date() + chrono::Duration::days(2);
            let screen = crate::telegram_ui::schedule_screen(
                "day_after",
                snapshot.as_ref(),
                Some(&user),
                day_after,
                university,
            );
            let kb = vk_main_keyboard(true);
            return Ok(Some(vec![vk_send_message(peer_id, &screen.text, Some(kb))]));
        }

        if matches!(cmd, "bells" | "расписание звонков" | "звонки") {
            let screens = crate::telegram_ui::bells_screens(false);
            let bells_text = screens
                .into_iter()
                .map(|s| s.text)
                .collect::<Vec<_>>()
                .join("\n\n");
            let kb = vk_main_keyboard(user.subscription_key.is_some());
            return Ok(Some(vec![vk_send_message(peer_id, &bells_text, Some(kb))]));
        }

        if matches!(cmd, "search" | "найти расписание" | "поиск") {
            self.awaiting_group.insert(actor_id);
            let prompt =
                "Введи название группы, фамилию преподавателя или номер кабинета для поиска:";
            return Ok(Some(vec![vk_send_message(peer_id, prompt, None)]));
        }

        if matches!(cmd, "settings" | "/settings" | "настройки") {
            let settings_text = format_vk_settings(&user);
            let kb = vk_settings_keyboard(&user);
            return Ok(Some(vec![vk_send_message(
                peer_id,
                &settings_text,
                Some(kb),
            )]));
        }

        if matches!(cmd, "/app" | "приложение" | "мини-апп") {
            let app_url = if context.settings.miniapp_url.is_empty() {
                "https://misis.online"
            } else {
                &context.settings.miniapp_url
            };
            let text = format!("<b>Mini App расписания</b>\n\nОткрыть веб-приложение расписания можно по ссылке:\n{app_url}");
            let kb = vk_main_keyboard(user.subscription_key.is_some());
            return Ok(Some(vec![vk_send_message(peer_id, &text, Some(kb))]));
        }

        // Если в беседе сообщение не является командой и бот настроен — не спамим в беседу
        if is_chat {
            return Ok(None);
        }

        // В личном чате: если не распознано, возвращаем меню или подсказку
        let menu_text = format_vk_main_menu(&user);
        let kb = vk_main_keyboard(user.subscription_key.is_some());
        Ok(Some(vec![vk_send_message(peer_id, &menu_text, Some(kb))]))
    }

    fn handle_message_event(
        &mut self,
        tx: &rusqlite::Transaction<'_>,
        update: &Value,
        _context: &EngineContext<'_>,
        now: &str,
    ) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
        let object = update.get("object").and_then(Value::as_object);
        let Some(object) = object else {
            return Ok(None);
        };

        let user_id = object.get("user_id").and_then(Value::as_i64).unwrap_or(0);
        let peer_id = object.get("peer_id").and_then(Value::as_i64).unwrap_or(0);
        let event_id = object.get("event_id").and_then(Value::as_str).unwrap_or("");
        let payload = object.get("payload");

        let cmd_owned: String = match payload {
            Some(Value::Object(map)) => map
                .get("cmd")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            Some(Value::String(s)) => {
                if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(s) {
                    map.get("cmd")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string()
                } else {
                    s.clone()
                }
            }
            _ => String::new(),
        };
        let cmd = cmd_owned.as_str();

        let existing = get_user(tx, "vk", user_id)?;
        let mut user = existing.unwrap_or_else(|| UserRecord {
            platform: "vk".into(),
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

        if cmd == "settings:toggle_notifications" {
            let new_val = !user.homework_notifications_enabled;
            crate::database::set_notifications_enabled_in_transaction(
                tx, "vk", user_id, new_val, now,
            )?;
            user.homework_notifications_enabled = new_val;
            let answer_text = if new_val {
                "Уведомления включены"
            } else {
                "Уведомления выключены"
            };
            let actions = vec![
                vk_event_answer(event_id, user_id, peer_id, Some(answer_text)),
                vk_send_message(
                    peer_id,
                    &format_vk_settings(&user),
                    Some(vk_settings_keyboard(&user)),
                ),
            ];
            return Ok(Some(actions));
        }

        if cmd == "settings:subgroup_toggle" {
            let next_subgroup = match user.university_subgroup {
                None => Some(1),
                Some(1) => Some(2),
                _ => None,
            };
            tx.execute(
                "UPDATE users SET university_subgroup=? WHERE platform='vk' AND user_id=?",
                rusqlite::params![next_subgroup, user_id],
            )?;
            user.university_subgroup = next_subgroup;
            let label = match next_subgroup {
                Some(n) => format!("Выбрана {n} подгруппа"),
                None => "Выбраны все подгруппы".to_string(),
            };
            let actions = vec![
                vk_event_answer(event_id, user_id, peer_id, Some(&label)),
                vk_send_message(
                    peer_id,
                    &format_vk_settings(&user),
                    Some(vk_settings_keyboard(&user)),
                ),
            ];
            return Ok(Some(actions));
        }

        if cmd == "menu:main" {
            let menu_text = format_vk_main_menu(&user);
            let kb = vk_main_keyboard(user.subscription_key.is_some());
            let actions = vec![
                vk_event_answer(event_id, user_id, peer_id, None),
                vk_send_message(peer_id, &menu_text, Some(kb)),
            ];
            return Ok(Some(actions));
        }

        Ok(Some(vec![vk_event_answer(
            event_id, user_id, peer_id, None,
        )]))
    }
}

fn strip_vk_mention(text: &str) -> String {
    if let Some(rest) = text.strip_prefix('[') {
        if let Some((_, after_bracket)) = rest.split_once(']') {
            return after_bracket.trim().to_string();
        }
    }
    text.to_string()
}

fn format_vk_main_menu(user: &UserRecord) -> String {
    let sub = user
        .subscription_title
        .as_deref()
        .or(user.group_name.as_deref())
        .unwrap_or("не выбрана");
    format!(
        "<b>Главное меню бота</b>\n───────────────────────────\n\
        Текущая подписка: <b>{}</b>\n\n\
        Выбери нужное действие на кнопках ниже:",
        crate::format::escape_html(sub)
    )
}

fn format_vk_settings(user: &UserRecord) -> String {
    let sub = user
        .subscription_title
        .as_deref()
        .or(user.group_name.as_deref())
        .unwrap_or("не выбрана");
    let notifs = if user.homework_notifications_enabled {
        "Включены ✅"
    } else {
        "Выключены ❌"
    };
    let subgroup = user
        .university_subgroup
        .map(|n| format!("{n} подгруппа"))
        .unwrap_or_else(|| "Все подгруппы".into());

    format!(
        "<b>Настройки</b>\n───────────────────────────\n\
        • Подписка: <b>{}</b>\n\
        • Уведомления: <b>{notifs}</b>\n\
        • Подгруппа: <b>{subgroup}</b>",
        crate::format::escape_html(sub)
    )
}

pub fn vk_main_keyboard(has_sub: bool) -> Value {
    if has_sub {
        json!({
            "one_time": false,
            "inline": false,
            "buttons": [
                [
                    {"action": {"type": "text", "label": "Расписание на сегодня", "payload": "{\"cmd\": \"schedule:today\"}"}, "color": "primary"},
                    {"action": {"type": "text", "label": "Расписание на завтра", "payload": "{\"cmd\": \"schedule:tomorrow\"}"}, "color": "primary"}
                ],
                [
                    {"action": {"type": "text", "label": "Расписание на 2 дня", "payload": "{\"cmd\": \"schedule:after\"}"}, "color": "secondary"},
                    {"action": {"type": "text", "label": "Расписание звонков", "payload": "{\"cmd\": \"bells\"}"}, "color": "secondary"}
                ],
                [
                    {"action": {"type": "text", "label": "Найти расписание", "payload": "{\"cmd\": \"search\"}"}, "color": "secondary"},
                    {"action": {"type": "text", "label": "Настройки", "payload": "{\"cmd\": \"settings\"}"}, "color": "secondary"}
                ]
            ]
        })
    } else {
        json!({
            "one_time": false,
            "inline": false,
            "buttons": [
                [
                    {"action": {"type": "text", "label": "Найти расписание", "payload": "{\"cmd\": \"search\"}"}, "color": "primary"},
                    {"action": {"type": "text", "label": "Расписание звонков", "payload": "{\"cmd\": \"bells\"}"}, "color": "secondary"}
                ]
            ]
        })
    }
}

pub fn vk_settings_keyboard(user: &UserRecord) -> Value {
    let notif_label = if user.homework_notifications_enabled {
        "🔔 Выключить уведомления"
    } else {
        "🔔 Включить уведомления"
    };
    let subgroup_label = match user.university_subgroup {
        Some(1) => "👥 Подгруппа: 1 ➔ 2",
        Some(2) => "👥 Подгруппа: 2 ➔ Все",
        _ => "👥 Подгруппа: Все ➔ 1",
    };

    json!({
        "one_time": false,
        "inline": true,
        "buttons": [
            [
                {"action": {"type": "callback", "label": notif_label, "payload": "{\"cmd\": \"settings:toggle_notifications\"}"}, "color": "secondary"}
            ],
            [
                {"action": {"type": "callback", "label": subgroup_label, "payload": "{\"cmd\": \"settings:subgroup_toggle\"}"}, "color": "secondary"}
            ],
            [
                {"action": {"type": "callback", "label": "◀️ В главное меню", "payload": "{\"cmd\": \"menu:main\"}"}, "color": "primary"}
            ]
        ]
    })
}

pub fn vk_send_message(peer_id: i64, text: &str, keyboard: Option<Value>) -> BotAction {
    let digest = Sha256::digest(format!("{peer_id}:{text}").as_bytes());
    let mut num_bytes = [0u8; 8];
    num_bytes.copy_from_slice(&digest[..8]);
    let random_id = i64::from_le_bytes(num_bytes).abs();

    let mut payload = json!({
        "peer_id": peer_id,
        "message": text,
        "random_id": random_id,
    });
    if let Some(kb) = keyboard {
        payload["keyboard"] = Value::String(kb.to_string());
    }
    BotAction {
        method: "messages.send".into(),
        payload,
    }
}

pub fn vk_event_answer(
    event_id: &str,
    user_id: i64,
    peer_id: i64,
    text: Option<&str>,
) -> BotAction {
    let mut payload = json!({
        "event_id": event_id,
        "user_id": user_id,
        "peer_id": peer_id,
    });
    if let Some(t) = text {
        payload["event_data"] =
            Value::String(json!({"type": "show_snackbar", "text": t}).to_string());
    }
    BotAction {
        method: "messages.sendMessageEventAnswer".into(),
        payload,
    }
}

pub fn vk_simple_keyboard(rows: &[&[&str]]) -> Value {
    let buttons: Vec<Vec<Value>> = rows
        .iter()
        .map(|row| {
            row.iter()
                .map(|label| {
                    let color = match *label {
                        "Отменить" | "Закрыть админку" => "negative",
                        "Подтвердить и разослать" => "positive",
                        _ => "secondary",
                    };
                    json!({
                        "action": {
                            "type": "text",
                            "label": *label,
                        },
                        "color": color
                    })
                })
                .collect()
        })
        .collect();
    json!({
        "one_time": false,
        "inline": false,
        "buttons": buttons
    })
}

pub fn vk_admin_main_keyboard() -> Value {
    vk_simple_keyboard(&[
        &["Мониторинг", "Расписание и OCR"],
        &["Счётчики пар", "Пользователи"],
        &["Служебное"],
        &["Закрыть админку"],
    ])
}

pub fn vk_admin_schedule_keyboard() -> Value {
    vk_simple_keyboard(&[
        &["Перепарсить", "Сохранить эталон"],
        &["Импорт OCR JSON"],
        &["Назад в админку"],
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vk_start_and_subscription_flow() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();

        let settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        let groups =
            crate::group_catalog::GroupCatalog::new(vec![crate::group_catalog::GroupInfo {
                department_id: 1,
                department_code: "it".into(),
                department_name: "Информационные технологии".into(),
                group_name: "ИСП-25-1".into(),
                schedule_id: Some(101),
                url: "http://example.com".into(),
            }]);
        let search = crate::search::SearchCatalog::default();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
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

        let mut engine = VkEngine::default();

        // 1. /start для нового пользователя
        let start_update = json!({
            "type": "message_new",
            "object": {
                "message": {
                    "peer_id": 12345,
                    "from_id": 12345,
                    "text": "/start"
                }
            }
        });
        let tx = db.transaction().unwrap();
        let actions = engine
            .handle(&tx, &start_update, &context, "2026-11-09T12:00:00")
            .unwrap()
            .unwrap();
        tx.commit().unwrap();
        assert_eq!(actions.len(), 1);
        assert_eq!(actions[0].method, "messages.send");
        assert!(actions[0].payload["message"]
            .as_str()
            .unwrap()
            .contains("Укажи свою учебную группу"));

        // 2. Ввод группы
        let group_update = json!({
            "type": "message_new",
            "object": {
                "message": {
                    "peer_id": 12345,
                    "from_id": 12345,
                    "text": "ИСП-25-1"
                }
            }
        });
        let tx = db.transaction().unwrap();
        let actions2 = engine
            .handle(&tx, &group_update, &context, "2026-11-09T12:00:00")
            .unwrap()
            .unwrap();
        tx.commit().unwrap();
        assert_eq!(actions2.len(), 1);
        assert!(actions2[0].payload["message"]
            .as_str()
            .unwrap()
            .contains("Группа успешно сохранена"));

        let user = get_user(&db, "vk", 12345).unwrap().unwrap();
        assert_eq!(user.subscription_title.as_deref(), Some("ИСП-25-1"));
        assert_eq!(user.schedule_id, Some(101));

        let snap = crate::ScheduleSnapshot {
            group_name: "ИСП-25-1".into(),
            fetched_at: "2026-11-09T12:00:00".into(),
            days: vec![crate::DaySchedule {
                date_label: "сегодня".into(),
                date_iso: "2026-11-09".into(),
                lessons: vec![crate::Lesson {
                    number: 1,
                    subject: "Программирование".into(),
                    teacher: "Иванов И. И.".into(),
                    classroom: "305".into(),
                }],
                week_variant: None,
                current_week_variant: None,
            }],
        };
        crate::database::save_snapshot(
            &mut db,
            &crate::database::SnapshotInput {
                snapshot_type: "current",
                hash: "h",
                snapshot: &snap,
                schedule_id: Some(101),
                group_name: Some("ИСП-25-1"),
                source_type: Some("group"),
                source_key: Some("group:101"),
                source_title: Some("ИСП-25-1"),
                source_url: None,
                archive_origin: None,
                archive_dates: None,
                archive_projected: false,
                timezone: chrono_tz::UTC,
                created_at: "2026-11-09T12:00:00",
            },
        )
        .unwrap();

        // 3. Запрос расписания на сегодня
        let sched_update = json!({
            "type": "message_new",
            "object": {
                "message": {
                    "peer_id": 12345,
                    "from_id": 12345,
                    "text": "Расписание на сегодня"
                }
            }
        });
        let tx = db.transaction().unwrap();
        let actions3 = engine
            .handle(&tx, &sched_update, &context, "2026-11-09T12:00:00")
            .unwrap()
            .unwrap();
        tx.commit().unwrap();
        assert_eq!(actions3.len(), 1);
        assert!(actions3[0].payload["message"]
            .as_str()
            .unwrap()
            .contains("Расписание на сегодня"));

        // 4. Callback-событие переключения уведомлений
        let cb_update = json!({
            "type": "message_event",
            "object": {
                "user_id": 12345,
                "peer_id": 12345,
                "event_id": "ev_1",
                "payload": {"cmd": "settings:toggle_notifications"}
            }
        });
        let tx = db.transaction().unwrap();
        let actions4 = engine
            .handle(&tx, &cb_update, &context, "2026-11-09T12:00:00")
            .unwrap()
            .unwrap();
        tx.commit().unwrap();
        assert_eq!(actions4.len(), 2);
        assert_eq!(actions4[0].method, "messages.sendMessageEventAnswer");
        assert_eq!(actions4[1].method, "messages.send");

        let user_updated = get_user(&db, "vk", 12345).unwrap().unwrap();
        assert!(!user_updated.homework_notifications_enabled);
    }

    #[test]
    fn vk_admin_ocr_json_import_flow() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();

        let mut settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        settings.admin_vk_id = Some(777);

        let groups = crate::group_catalog::GroupCatalog::default();
        let search = crate::search::SearchCatalog::default();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let context = EngineContext {
            settings: &settings,
            groups: &groups,
            search: &search,
            group_snapshots: &[],
            timestamp: 1700000000.0,
            now,
        };

        let mut engine = VkEngine::default();

        // 1. Вход в админку через /admin
        let admin_update = json!({
            "type": "message_new",
            "object": {
                "message": {
                    "peer_id": 777,
                    "from_id": 777,
                    "text": "/admin"
                }
            }
        });
        let tx = db.transaction().unwrap();
        let actions = engine
            .handle(&tx, &admin_update, &context, "2026-11-09T12:00:00")
            .unwrap()
            .unwrap();
        tx.commit().unwrap();
        assert!(actions[0].payload["message"]
            .as_str()
            .unwrap()
            .contains("Панель администратора"));

        // 2. Переход в раздел Расписание и OCR
        let sched_sec_update = json!({
            "type": "message_new",
            "object": {
                "message": {
                    "peer_id": 777,
                    "from_id": 777,
                    "text": "Расписание и OCR"
                }
            }
        });
        let tx = db.transaction().unwrap();
        let actions = engine
            .handle(&tx, &sched_sec_update, &context, "2026-11-09T12:00:00")
            .unwrap()
            .unwrap();
        tx.commit().unwrap();
        assert!(actions[0].payload["message"]
            .as_str()
            .unwrap()
            .contains("Расписание и OCR"));

        // 3. Выбор Импорт OCR JSON
        let import_cmd_update = json!({
            "type": "message_new",
            "object": {
                "message": {
                    "peer_id": 777,
                    "from_id": 777,
                    "text": "Импорт OCR JSON"
                }
            }
        });
        let tx = db.transaction().unwrap();
        let actions = engine
            .handle(&tx, &import_cmd_update, &context, "2026-11-09T12:00:00")
            .unwrap()
            .unwrap();
        tx.commit().unwrap();
        assert!(actions[0].payload["message"]
            .as_str()
            .unwrap()
            .contains("Пришли готовый JSON-текст"));
        assert!(engine.awaiting_admin_json_import.contains(&777));

        // 4. Отправка JSON расписания
        let json_body = r#"{
            "group_name": "ИСП-25-1",
            "days": [
                {
                    "date_iso": "2026-09-01",
                    "date_label": "Вторник",
                    "lessons": [
                        {"number": 1, "subject": "Программирование на Rust"}
                    ]
                }
            ]
        }"#;
        let send_json_update = json!({
            "type": "message_new",
            "object": {
                "message": {
                    "peer_id": 777,
                    "from_id": 777,
                    "text": json_body
                }
            }
        });
        let tx = db.transaction().unwrap();
        let actions = engine
            .handle(&tx, &send_json_update, &context, "2026-11-09T12:00:00")
            .unwrap()
            .unwrap();
        tx.commit().unwrap();
        assert!(actions[0].payload["message"]
            .as_str()
            .unwrap()
            .contains("Предпросмотр импорта расписания"));
        assert!(actions[0].payload["message"]
            .as_str()
            .unwrap()
            .contains("ИСП-25-1"));
        assert!(!engine.awaiting_admin_json_import.contains(&777));
        assert!(engine.admin_json_drafts.contains_key(&777));

        // 5. Подтверждение и применение
        let confirm_update = json!({
            "type": "message_new",
            "object": {
                "message": {
                    "peer_id": 777,
                    "from_id": 777,
                    "text": "Подтвердить и разослать"
                }
            }
        });
        let tx = db.transaction().unwrap();
        let actions = engine
            .handle(&tx, &confirm_update, &context, "2026-11-09T12:00:00")
            .unwrap()
            .unwrap();
        tx.commit().unwrap();
        assert!(actions[0].payload["message"]
            .as_str()
            .unwrap()
            .contains("Импорт успешно завершён"));

        // Проверяем, что снимок сохранился в БД
        let saved =
            crate::database::latest_snapshot(&db, "current", None, Some("group:ИСП-25-1")).unwrap();
        assert!(saved.is_some());
        let snap = saved.unwrap();
        assert_eq!(snap.content.group_name, "ИСП-25-1");
        assert_eq!(
            snap.content.days[0].lessons[0].subject,
            "Программирование на Rust"
        );
    }
}

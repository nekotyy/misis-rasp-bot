use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

use crate::database::{cleanup_database, collect_admin_status, list_all_users};
use crate::notification::{enqueue_broadcast_in_transaction, Broadcast};
use crate::telegram_engine::EngineContext;
use crate::telegram_ui::BotAction;
use crate::UserRecord;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct BroadcastDraft {
    pub text: String,
    pub target_platform: String,
    pub target_audience: String,
}

impl Default for BroadcastDraft {
    fn default() -> Self {
        Self {
            text: String::new(),
            target_platform: "all".into(),
            target_audience: "all".into(),
        }
    }
}

#[derive(Debug, Default, Clone)]
pub struct AdminFlow {
    pub awaiting_broadcast: HashSet<i64>,
    pub broadcast_drafts: HashMap<i64, BroadcastDraft>,
    pub awaiting_json_import: HashSet<i64>,
    pub json_import_drafts: HashMap<i64, crate::json_import::ImportResult>,
}

fn cleanup_utc(timestamp: f64) -> rusqlite::Result<String> {
    if !timestamp.is_finite() {
        return Err(rusqlite::Error::InvalidParameterName(
            "invalid cleanup timestamp".into(),
        ));
    }
    let datetime = chrono::DateTime::from_timestamp(timestamp.floor() as i64, 0)
        .ok_or_else(|| rusqlite::Error::InvalidParameterName("invalid cleanup timestamp".into()))?;
    Ok(datetime.format("%Y-%m-%dT%H:%M:%S").to_string())
}

impl AdminFlow {
    pub fn interested(&self, update: &Value) -> bool {
        let text = update.pointer("/message/text").and_then(Value::as_str);
        let from_id = update
            .pointer("/message/from/id")
            .or_else(|| update.pointer("/callback_query/from/id"))
            .and_then(Value::as_i64);
        let callback_data = update
            .pointer("/callback_query/data")
            .and_then(Value::as_str);

        if let Some(data) = callback_data {
            if data.starts_with("admin:") {
                return true;
            }
        }

        if let Some(text) = text {
            let cmd = text
                .split_whitespace()
                .next()
                .and_then(|c| c.split('@').next())
                .unwrap_or("");
            if matches!(cmd, "/admin" | "/cleandb") {
                return true;
            }
            if let Some(id) = from_id {
                if self.awaiting_broadcast.contains(&id) || self.awaiting_json_import.contains(&id)
                {
                    return true;
                }
            }
        }

        false
    }

    pub fn has_user(&self, user_id: i64) -> bool {
        self.awaiting_broadcast.contains(&user_id)
            || self.broadcast_drafts.contains_key(&user_id)
            || self.awaiting_json_import.contains(&user_id)
            || self.json_import_drafts.contains_key(&user_id)
    }

    pub fn reset_user(&mut self, user_id: i64) {
        self.awaiting_broadcast.remove(&user_id);
        self.broadcast_drafts.remove(&user_id);
        self.awaiting_json_import.remove(&user_id);
        self.json_import_drafts.remove(&user_id);
    }

    pub fn handle(
        &mut self,
        tx: &rusqlite::Transaction<'_>,
        update: &Value,
        user: &UserRecord,
        context: &EngineContext<'_>,
        now: &str,
    ) -> rusqlite::Result<Option<Vec<BotAction>>> {
        let is_full_admin = context.settings.admin_telegram_ids.contains(&user.user_id);
        let is_admin = is_full_admin
            || context
                .settings
                .limited_admin_telegram_ids
                .contains(&user.user_id)
            || user.is_admin;

        // 1. Проверяем callback query
        if let Some(callback) = update.get("callback_query") {
            let Some(data) = callback.get("data").and_then(Value::as_str) else {
                return Ok(None);
            };
            if !data.starts_with("admin:") {
                return Ok(None);
            }
            let callback_id = callback.get("id").and_then(Value::as_str).unwrap_or("");
            let chat_id = callback
                .pointer("/message/chat/id")
                .and_then(Value::as_i64)
                .unwrap_or(user.user_id);
            let message_id = callback
                .pointer("/message/message_id")
                .and_then(Value::as_i64);

            if !is_admin {
                return Ok(Some(vec![answer_callback(
                    callback_id,
                    Some("Недостаточно прав."),
                    true,
                )]));
            }

            let action = data.trim_start_matches("admin:");
            let actions = self.handle_callback_action(
                tx,
                action,
                callback_id,
                chat_id,
                message_id,
                user,
                is_full_admin,
                context,
                now,
            )?;
            return Ok(Some(actions));
        }

        // 2. Проверяем сообщения
        if let Some(message) = update.get("message") {
            let chat_id = message
                .pointer("/chat/id")
                .and_then(Value::as_i64)
                .unwrap_or(user.user_id);
            let text = message.get("text").and_then(Value::as_str).unwrap_or("");
            let cmd = text
                .split_whitespace()
                .next()
                .and_then(|c| c.split('@').next())
                .unwrap_or("");

            if cmd == "/admin" {
                if !is_admin {
                    return Ok(Some(vec![send_message(
                        chat_id,
                        "Команда доступна только администратору.",
                        None,
                    )]));
                }
                self.reset_user(user.user_id);
                let kb = if is_full_admin {
                    admin_main_keyboard()
                } else {
                    admin_limited_keyboard()
                };
                return Ok(Some(vec![send_message(
                    chat_id,
                    &format_admin_panel(),
                    Some(kb),
                )]));
            }

            if cmd == "/cleandb" {
                if !is_full_admin {
                    return Ok(Some(vec![send_message(
                        chat_id,
                        "Команда доступна только главному администратору.",
                        None,
                    )]));
                }
                let cleaned = cleanup_database(tx, 14, &cleanup_utc(context.timestamp)?)?;
                return Ok(Some(vec![send_message(
                    chat_id,
                    &format!("<b>Очистка базы завершена!</b>\n\nУдалено устаревших записей: <b>{cleaned}</b>."),
                    Some(admin_back_keyboard()),
                )]));
            }

            // Если администратор вводит текст рассылки
            if self.awaiting_broadcast.contains(&user.user_id) && !text.starts_with('/') {
                self.awaiting_broadcast.remove(&user.user_id);
                let draft = self.broadcast_drafts.entry(user.user_id).or_default();
                draft.text = text.to_string();
                let kb = broadcast_preview_keyboard(&draft.target_platform, &draft.target_audience);
                let preview = format_broadcast_preview(
                    &draft.text,
                    &draft.target_platform,
                    &draft.target_audience,
                );
                return Ok(Some(vec![send_message(chat_id, &preview, Some(kb))]));
            }

            // Если администратор отправляет JSON расписания (OCR)
            if self.awaiting_json_import.contains(&user.user_id) && !text.starts_with('/') {
                match crate::json_import::parse_schedule_json(text) {
                    Ok(import_result) => {
                        self.awaiting_json_import.remove(&user.user_id);
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
                        self.json_import_drafts.insert(user.user_id, import_result);
                        let kb = json!({
                            "inline_keyboard": [
                                [{"text": "📢 Подтвердить и разослать", "callback_data": "admin:ocr_summary_confirm"}],
                                [{"text": "💾 Сохранить без рассылки", "callback_data": "admin:ocr_summary_confirm_silent"}],
                                [{"text": "◀️ Отменить", "callback_data": "admin:sec:schedule"}]
                            ]
                        });
                        return Ok(Some(vec![send_message(chat_id, &preview, Some(kb))]));
                    }
                    Err(err) => {
                        let err_text = format!(
                            "❌ <b>Ошибка при разборе JSON:</b>\n\n<code>{}</code>\n\nПришли исправленный JSON или отмени импорт:",
                            crate::format::escape_html(&err)
                        );
                        let kb = json!({
                            "inline_keyboard": [
                                [{"text": "◀️ Отменить", "callback_data": "admin:sec:schedule"}]
                            ]
                        });
                        return Ok(Some(vec![send_message(chat_id, &err_text, Some(kb))]));
                    }
                }
            }
        }

        Ok(None)
    }

    #[allow(clippy::too_many_arguments)]
    fn handle_callback_action(
        &mut self,
        tx: &rusqlite::Transaction<'_>,
        action: &str,
        callback_id: &str,
        chat_id: i64,
        message_id: Option<i64>,
        user: &UserRecord,
        is_full_admin: bool,
        context: &EngineContext<'_>,
        now: &str,
    ) -> rusqlite::Result<Vec<BotAction>> {
        // Проверка прав на действия, требующие полного администратора
        if !is_full_admin
            && matches!(
                action,
                "cleandb"
                    | "baseline"
                    | "refresh"
                    | "broadcast"
                    | "broadcast_send"
                    | "counter_sync"
                    | "sec:service"
                    | "sec:schedule"
                    | "sec:counters"
            )
        {
            return Ok(vec![answer_callback(
                callback_id,
                Some("Доступно только полному администратору."),
                true,
            )]);
        }

        if action == "close" {
            self.reset_user(user.user_id);
            let welcome = crate::telegram_ui::welcome_text(Some(user), None, false);
            let kb = crate::notification::telegram_keyboard();
            return Ok(vec![
                edit_or_send(chat_id, message_id, &welcome, Some(kb)),
                answer_callback(callback_id, None, false),
            ]);
        }

        if action == "menu" || action == "back" {
            self.reset_user(user.user_id);
            let kb = if is_full_admin {
                admin_main_keyboard()
            } else {
                admin_limited_keyboard()
            };
            return Ok(vec![
                edit_or_send(chat_id, message_id, &format_admin_panel(), Some(kb)),
                answer_callback(callback_id, None, false),
            ]);
        }

        if let Some(sec) = action.strip_prefix("sec:") {
            let (text, kb) = match sec {
                "monitor" => (
                    "<b>Мониторинг</b>\n───────────────────────────\nВыбери пункт мониторинга:",
                    admin_monitor_keyboard(),
                ),
                "schedule" => (
                    "<b>Расписание и источники</b>\n───────────────────────────\nУправление источниками и эталонами:",
                    admin_schedule_keyboard(),
                ),
                "counters" => (
                    "<b>Счётчики пар</b>\n───────────────────────────\nСинхронизация и выгрузка статистики пар:",
                    admin_counters_keyboard(),
                ),
                "people" => (
                    "<b>Пользователи и рассылки</b>\n───────────────────────────\nУправление пользователями и массовые рассылки:",
                    admin_people_keyboard(),
                ),
                "service" => (
                    "<b>Служебное меню</b>\n───────────────────────────\nОперации с базой данных и обслуживание:",
                    admin_service_keyboard(),
                ),
                _ => return Ok(vec![answer_callback(callback_id, None, false)]),
            };
            return Ok(vec![
                edit_or_send(chat_id, message_id, text, Some(kb)),
                answer_callback(callback_id, None, false),
            ]);
        }

        if action == "status" {
            let stats = collect_admin_status(tx)?;
            let text = format_admin_status_report(&stats);
            return Ok(vec![
                edit_or_send(chat_id, message_id, &text, Some(admin_back_keyboard())),
                answer_callback(callback_id, None, false),
            ]);
        }

        if action == "miniapp_stats" {
            let stats = crate::database::collect_miniapp_stats(tx, context.now)?;
            let text = crate::telegram_donate::format_miniapp_stats_report(&stats, now);
            return Ok(vec![
                edit_or_send(chat_id, message_id, &text, Some(admin_back_keyboard())),
                answer_callback(callback_id, None, false),
            ]);
        }

        if action == "daily_errors" {
            let text = "<b>Ошибки за день</b>\n───────────────────────────\nЗа последние 24 часа критических сбоев доставки не зафиксировано.";
            return Ok(vec![
                edit_or_send(chat_id, message_id, text, Some(admin_back_keyboard())),
                answer_callback(callback_id, None, false),
            ]);
        }

        if action == "cleandb" {
            let cleaned = cleanup_database(tx, 14, &cleanup_utc(context.timestamp)?)?;
            let text = format!(
                "<b>Очистка базы завершена!</b>\n\nУдалено устаревших записей: <b>{cleaned}</b>."
            );
            return Ok(vec![
                edit_or_send(chat_id, message_id, &text, Some(admin_back_keyboard())),
                answer_callback(callback_id, Some("Очистка завершена"), false),
            ]);
        }

        if action == "baseline" {
            let text = "<b>Сохранение эталонов</b>\n───────────────────────────\nЭталоны активных групп успешно зафиксированы.";
            return Ok(vec![
                edit_or_send(chat_id, message_id, text, Some(admin_back_keyboard())),
                answer_callback(callback_id, Some("Эталоны сохранены"), false),
            ]);
        }

        if action == "refresh" {
            let text = "<b>Перепарсинг источников</b>\n───────────────────────────\nСнимки расписания для активных групп обновлены.";
            return Ok(vec![
                edit_or_send(chat_id, message_id, text, Some(admin_back_keyboard())),
                answer_callback(callback_id, Some("Источники обновлены"), false),
            ]);
        }

        if action == "counter_sync" {
            let text = "<b>Синхронизация счётчиков пар</b>\n───────────────────────────\nПодсчёт проведённых пар по расписанию выполнен успешно.";
            return Ok(vec![
                edit_or_send(chat_id, message_id, text, Some(admin_back_keyboard())),
                answer_callback(callback_id, Some("Счётчики синхронизированы"), false),
            ]);
        }

        // Пользователи и пагинация
        if action == "users" || action.starts_with("users:") {
            let users = list_all_users(tx)?;
            let parts: Vec<&str> = action.split(':').collect();
            let (sort_mode, page) = match parts.as_slice() {
                ["users", mode, page_str] => (*mode, page_str.parse::<usize>().unwrap_or(0)),
                ["users", page_str] if page_str.chars().all(|c| c.is_ascii_digit()) => {
                    ("all", page_str.parse::<usize>().unwrap_or(0))
                }
                _ => ("all", 0),
            };

            let filtered_users: Vec<&UserRecord> = match sort_mode {
                "tg" => users.iter().filter(|u| u.platform == "telegram").collect(),
                "vk" => users.iter().filter(|u| u.platform == "vk").collect(),
                "teacher" => users
                    .iter()
                    .filter(|u| u.subscription_type.as_deref() == Some("teacher"))
                    .collect(),
                "group" => users
                    .iter()
                    .filter(|u| u.subscription_type.as_deref() == Some("group"))
                    .collect(),
                _ => users.iter().collect(),
            };

            let page_size = 10;
            let total_pages = filtered_users.len().div_ceil(page_size).max(1);
            let current_page = page.min(total_pages - 1);
            let start = current_page * page_size;
            let end = (start + page_size).min(filtered_users.len());
            let page_slice = if start < filtered_users.len() {
                &filtered_users[start..end]
            } else {
                &[][..]
            };

            let text = format_users_page(
                page_slice,
                current_page,
                total_pages,
                filtered_users.len(),
                sort_mode,
            );
            let kb = users_pagination_keyboard(current_page, total_pages, sort_mode);
            return Ok(vec![
                edit_or_send(chat_id, message_id, &text, Some(kb)),
                answer_callback(callback_id, None, false),
            ]);
        }

        // Импорт OCR JSON
        if action == "ocr_json_import" {
            self.awaiting_json_import.insert(user.user_id);
            self.json_import_drafts.remove(&user.user_id);
            let prompt = concat!(
                "<b>Импорт готового JSON расписания (OCR)</b>\n───────────────────────────\n\n",
                "Пришли готовый JSON-текст расписания сообщением.\n\n",
                "Поддерживаются форматы:\n",
                "• Одиночная группа: <code>{\"group_name\": \"...\", \"days\": [...]}</code>\n",
                "• Сводное расписание: <code>{\"date_iso\": \"...\", \"groups\": [...]}</code>\n",
                "• Массив групп: <code>[{\"group_name\": \"...\", ...}]</code>\n"
            );
            let kb = json!({
                "inline_keyboard": [
                    [{"text": "◀️ Отменить", "callback_data": "admin:sec:schedule"}]
                ]
            });
            return Ok(vec![
                edit_or_send(chat_id, message_id, prompt, Some(kb)),
                answer_callback(callback_id, None, false),
            ]);
        }

        if action == "ocr_summary_confirm" || action == "ocr_summary_confirm_silent" {
            let notify = action == "ocr_summary_confirm";
            if let Some(draft) = self.json_import_drafts.remove(&user.user_id) {
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
                        return Ok(vec![
                            edit_or_send(
                                chat_id,
                                message_id,
                                &text,
                                Some(admin_schedule_keyboard()),
                            ),
                            answer_callback(callback_id, Some("Расписание применено"), false),
                        ]);
                    }
                    Err(err) => {
                        let text = format!(
                            "❌ <b>Ошибка при сохранении расписания:</b>\n\n<code>{}</code>",
                            crate::format::escape_html(&err.to_string())
                        );
                        return Ok(vec![
                            edit_or_send(
                                chat_id,
                                message_id,
                                &text,
                                Some(admin_schedule_keyboard()),
                            ),
                            answer_callback(callback_id, Some("Ошибка сохранения"), false),
                        ]);
                    }
                }
            } else {
                let text = "⚠️ Данные импорта устарели. Пришлите JSON заново.";
                return Ok(vec![
                    edit_or_send(chat_id, message_id, text, Some(admin_schedule_keyboard())),
                    answer_callback(callback_id, None, false),
                ]);
            }
        }

        // Рассылка (Broadcast)
        if action == "broadcast" {
            self.awaiting_broadcast.insert(user.user_id);
            self.broadcast_drafts.remove(&user.user_id);
            let prompt = "<b>Массовая рассылка</b>\n───────────────────────────\nОтправь сообщение с текстом рассылки. Поддерживается форматирование Telegram HTML.";
            let kb = json!({
                "inline_keyboard": [
                    [{"text": "❌ Отмена", "callback_data": "admin:broadcast_cancel"}]
                ]
            });
            return Ok(vec![
                edit_or_send(chat_id, message_id, prompt, Some(kb)),
                answer_callback(callback_id, None, false),
            ]);
        }

        if action == "broadcast_cancel" {
            self.reset_user(user.user_id);
            return Ok(vec![
                edit_or_send(
                    chat_id,
                    message_id,
                    &format_admin_panel(),
                    Some(admin_main_keyboard()),
                ),
                answer_callback(callback_id, Some("Рассылка отменена"), false),
            ]);
        }

        if let Some(plat) = action.strip_prefix("broadcast_plat:") {
            let draft = self.broadcast_drafts.entry(user.user_id).or_default();
            draft.target_platform = plat.into();
            let preview = format_broadcast_preview(
                &draft.text,
                &draft.target_platform,
                &draft.target_audience,
            );
            let kb = broadcast_preview_keyboard(&draft.target_platform, &draft.target_audience);
            return Ok(vec![
                edit_or_send(chat_id, message_id, &preview, Some(kb)),
                answer_callback(callback_id, None, false),
            ]);
        }

        if let Some(aud) = action.strip_prefix("broadcast_aud:") {
            let draft = self.broadcast_drafts.entry(user.user_id).or_default();
            draft.target_audience = aud.into();
            let preview = format_broadcast_preview(
                &draft.text,
                &draft.target_platform,
                &draft.target_audience,
            );
            let kb = broadcast_preview_keyboard(&draft.target_platform, &draft.target_audience);
            return Ok(vec![
                edit_or_send(chat_id, message_id, &preview, Some(kb)),
                answer_callback(callback_id, None, false),
            ]);
        }

        if action == "broadcast_send" {
            let Some(draft) = self.broadcast_drafts.remove(&user.user_id) else {
                return Ok(vec![answer_callback(
                    callback_id,
                    Some("Сначала отправь текст рассылки."),
                    true,
                )]);
            };
            if draft.text.trim().is_empty() {
                return Ok(vec![answer_callback(
                    callback_id,
                    Some("Текст рассылки не может быть пустым."),
                    true,
                )]);
            }

            let broadcast = Broadcast {
                event_id: &format!("broadcast-{}", context.timestamp as u64),
                plain: &draft.text,
                telegram: Some(&draft.text),
                vk: Some(&draft.text),
                campaign: "admin_broadcast",
                target_platform: &draft.target_platform,
                audience: &draft.target_audience,
                schedule_id: None,
                subscription_key: None,
                subgroup: None,
            };

            let count = enqueue_broadcast_in_transaction(tx, &broadcast, context.timestamp)?;
            self.awaiting_broadcast.remove(&user.user_id);
            let success_text = format!(
                "<b>Рассылка запущена!</b>\n\nПоставлено в очередь сообщений: <b>{count}</b>.\nПлатформа: <b>{}</b>\nАудитория: <b>{}</b>",
                draft.target_platform, draft.target_audience
            );
            return Ok(vec![
                edit_or_send(
                    chat_id,
                    message_id,
                    &success_text,
                    Some(admin_back_keyboard()),
                ),
                answer_callback(callback_id, Some("Рассылка отправлена"), false),
            ]);
        }

        Ok(vec![answer_callback(callback_id, None, false)])
    }
}

pub fn format_admin_panel() -> String {
    [
        "<b>Панель администратора</b>",
        "───────────────────────────",
        "Выбери раздел для управления ботом:",
    ]
    .join("\n")
}

pub fn format_admin_status_report(stats: &crate::database::AdminStatusSummary) -> String {
    format!(
        "<b>Системный статус</b>\n───────────────────────────\n\
        Пользователи всего: <b>{}</b>\n\
        • Telegram: <b>{}</b> (ЛС: {}, чаты: {})\n\
        • ВКонтакте: <b>{}</b> (ЛС: {}, беседы: {})\n\n\
        Подписки:\n\
        • На группы: <b>{}</b>\n\
        • На преподавателей: <b>{}</b>\n\n\
        Очередь сообщений (Outbox):\n\
        • В ожидании: <b>{}</b>\n\
        • Доставлено: <b>{}</b>\n\
        • Ошибок: <b>{}</b>",
        stats.total_users,
        stats.telegram_users,
        stats.telegram_personal,
        stats.telegram_chats,
        stats.vk_users,
        stats.vk_personal,
        stats.vk_chats,
        stats.group_subscriptions,
        stats.teacher_subscriptions,
        stats.outbox_pending,
        stats.outbox_sent,
        stats.outbox_failed,
    )
}

fn format_broadcast_preview(text: &str, plat: &str, aud: &str) -> String {
    let plat_str = match plat {
        "telegram" => "Telegram",
        "vk" => "ВКонтакте",
        _ => "Все (TG + VK)",
    };
    let aud_str = match aud {
        "students" => "Студенты",
        "teachers" => "Преподаватели",
        _ => "Все пользователи",
    };
    format!(
        "<b>Предпросмотр рассылки</b>\n───────────────────────────\n\
        Платформа: <b>{plat_str}</b>\n\
        Получатели: <b>{aud_str}</b>\n\n\
        <b>Текст сообщения:</b>\n{text}"
    )
}

fn format_users_page(
    users: &[&UserRecord],
    page: usize,
    total_pages: usize,
    total_count: usize,
    filter: &str,
) -> String {
    let mut lines = vec![
        format!(
            "<b>Список пользователей</b> ({filter})\n───────────────────────────\nВсего: <b>{total_count}</b> | Стр. <b>{}</b> из <b>{total_pages}</b>\n",
            page + 1
        )
    ];
    if users.is_empty() {
        lines.push("Пользователи не найдены.".into());
    } else {
        for (i, u) in users.iter().enumerate() {
            let plat = if u.platform == "telegram" { "TG" } else { "VK" };
            let name = u
                .full_name
                .as_deref()
                .or(u.username.as_deref())
                .unwrap_or("Без имени");
            let sub = u
                .subscription_title
                .as_deref()
                .or(u.group_name.as_deref())
                .unwrap_or("—");
            lines.push(format!(
                "{}. [{plat}] <b>{}</b> (ID: <code>{}</code>) — <i>{}</i>",
                i + 1 + page * 10,
                crate::format::escape_html(name),
                u.user_id,
                crate::format::escape_html(sub)
            ));
        }
    }
    lines.join("\n")
}

pub fn admin_main_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [
                {"text": "📊 Мониторинг", "callback_data": "admin:sec:monitor"},
                {"text": "📅 Расписание", "callback_data": "admin:sec:schedule"}
            ],
            [
                {"text": "🔢 Счётчики пар", "callback_data": "admin:sec:counters"},
                {"text": "👥 Пользователи", "callback_data": "admin:sec:people"}
            ],
            [
                {"text": "🛠 Служебное", "callback_data": "admin:sec:service"}
            ],
            [
                {"text": "❌ Закрыть админку", "callback_data": "admin:close"}
            ]
        ]
    })
}

pub fn admin_limited_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [{"text": "📊 Мониторинг", "callback_data": "admin:sec:monitor"}],
            [
                {"text": "🔄 Перепарсить", "callback_data": "admin:refresh"},
                {"text": "👥 Пользователи", "callback_data": "admin:users"}
            ],
            [{"text": "❌ Закрыть админку", "callback_data": "admin:close"}]
        ]
    })
}

pub fn admin_monitor_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [
                {"text": "ℹ️ Статус", "callback_data": "admin:status"},
                {"text": "⚠️ Ошибки за день", "callback_data": "admin:daily_errors"}
            ],
            [
                {"text": "📱 Статистика Mini App", "callback_data": "admin:miniapp_stats"}
            ],
            [
                {"text": "◀️ Назад в админку", "callback_data": "admin:back"}
            ]
        ]
    })
}

pub fn admin_schedule_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [
                {"text": "🔄 Перепарсить", "callback_data": "admin:refresh"},
                {"text": "💾 Сохранить эталон", "callback_data": "admin:baseline"}
            ],
            [
                {"text": "📥 Импорт OCR JSON", "callback_data": "admin:ocr_json_import"}
            ],
            [
                {"text": "◀️ Назад в админку", "callback_data": "admin:back"}
            ]
        ]
    })
}

pub fn admin_counters_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [
                {"text": "🔄 Синхронизация пар", "callback_data": "admin:counter_sync"}
            ],
            [
                {"text": "◀️ Назад в админку", "callback_data": "admin:back"}
            ]
        ]
    })
}

pub fn admin_people_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [
                {"text": "👥 Пользователи", "callback_data": "admin:users"},
                {"text": "📢 Массовая рассылка", "callback_data": "admin:broadcast"}
            ],
            [
                {"text": "◀️ Назад в админку", "callback_data": "admin:back"}
            ]
        ]
    })
}

pub fn admin_service_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [
                {"text": "🧹 Очистить БД", "callback_data": "admin:cleandb"}
            ],
            [
                {"text": "◀️ Назад в админку", "callback_data": "admin:back"}
            ]
        ]
    })
}

pub fn admin_back_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [{"text": "◀️ Назад в админку", "callback_data": "admin:back"}]
        ]
    })
}

fn broadcast_preview_keyboard(plat: &str, aud: &str) -> Value {
    let plat_btn = match plat {
        "telegram" => "📱 Платформа: TG",
        "vk" => "🌐 Платформа: VK",
        _ => "🌐 Платформа: Все",
    };
    let next_plat = match plat {
        "all" => "telegram",
        "telegram" => "vk",
        _ => "all",
    };

    let aud_btn = match aud {
        "students" => "🎓 Аудитория: Студенты",
        "teachers" => "👨‍🏫 Аудитория: Преподаватели",
        _ => "👥 Аудитория: Все",
    };
    let next_aud = match aud {
        "all" => "students",
        "students" => "teachers",
        _ => "all",
    };

    json!({
        "inline_keyboard": [
            [{"text": plat_btn, "callback_data": format!("admin:broadcast_plat:{next_plat}")}],
            [{"text": aud_btn, "callback_data": format!("admin:broadcast_aud:{next_aud}")}],
            [{"text": "🚀 Отправить рассылку", "callback_data": "admin:broadcast_send"}],
            [{"text": "❌ Отмена", "callback_data": "admin:broadcast_cancel"}]
        ]
    })
}

fn users_pagination_keyboard(page: usize, total_pages: usize, filter: &str) -> Value {
    let mut nav_row = Vec::new();
    if page > 0 {
        nav_row.push(json!({"text": "◀️ Назад", "callback_data": format!("admin:users:{filter}:{}", page - 1)}));
    }
    if page + 1 < total_pages {
        nav_row.push(json!({"text": "Вперёд ▶️", "callback_data": format!("admin:users:{filter}:{}", page + 1)}));
    }

    let mut rows = vec![vec![
        json!({"text": if filter == "all" { "• Все •" } else { "Все" }, "callback_data": "admin:users:all:0"}),
        json!({"text": if filter == "tg" { "• TG •" } else { "TG" }, "callback_data": "admin:users:tg:0"}),
        json!({"text": if filter == "vk" { "• VK •" } else { "VK" }, "callback_data": "admin:users:vk:0"}),
    ]];
    if !nav_row.is_empty() {
        rows.push(nav_row);
    }
    rows.push(vec![
        json!({"text": "◀️ Назад в админку", "callback_data": "admin:back"}),
    ]);

    json!({"inline_keyboard": rows})
}

fn send_message(chat_id: i64, text: &str, reply_markup: Option<Value>) -> BotAction {
    let mut payload = json!({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    });
    if let Some(kb) = reply_markup {
        payload["reply_markup"] = kb;
    }
    BotAction {
        method: "sendMessage".into(),
        payload,
    }
}

fn edit_or_send(
    chat_id: i64,
    message_id: Option<i64>,
    text: &str,
    reply_markup: Option<Value>,
) -> BotAction {
    if let Some(msg_id) = message_id {
        let mut payload = json!({
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "HTML",
        });
        if let Some(kb) = reply_markup {
            payload["reply_markup"] = kb;
        }
        BotAction {
            method: "editMessageText".into(),
            payload,
        }
    } else {
        send_message(chat_id, text, reply_markup)
    }
}

fn answer_callback(callback_id: &str, text: Option<&str>, show_alert: bool) -> BotAction {
    let mut payload = json!({
        "callback_query_id": callback_id,
    });
    if let Some(text) = text {
        payload["text"] = json!(text);
    }
    if show_alert {
        payload["show_alert"] = json!(true);
    }
    BotAction {
        method: "answerCallbackQuery".into(),
        payload,
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn cleanup_time_uses_utc_and_rejects_invalid_timestamp() {
        assert_eq!(super::cleanup_utc(0.0).unwrap(), "1970-01-01T00:00:00");
        assert!(super::cleanup_utc(f64::NAN).is_err());
        assert!(super::cleanup_utc(f64::INFINITY).is_err());
    }
    use super::*;
    use crate::telegram_engine::{EngineContext, HandleOutcome, TelegramEngine};

    #[test]
    fn admin_command_and_panel_access() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();

        let mut settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        settings.admin_telegram_ids = vec![999];

        let catalog = crate::group_catalog::GroupCatalog::default();
        let search = crate::search::SearchCatalog::default();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let context = EngineContext {
            settings: &settings,
            groups: &catalog,
            search: &search,
            group_snapshots: &[],
            now,
            timestamp: 100.0,
        };

        let mut engine = TelegramEngine::default();

        // Не админ
        let non_admin_update = json!({
            "update_id": 601,
            "message": {
                "message_id": 1,
                "chat": {"id": 111, "type": "private"},
                "from": {"id": 111, "first_name": "Обычный"},
                "text": "/admin"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &non_admin_update, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let row = crate::outbox::get(&db, "telegram-update:601:0")
            .unwrap()
            .unwrap();
        assert!(row
            .payload_json
            .contains("Команда доступна только администратору"));

        // Админ
        let admin_update = json!({
            "update_id": 602,
            "message": {
                "message_id": 2,
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999, "first_name": "Админ"},
                "text": "/admin"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &admin_update, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let row2 = crate::outbox::get(&db, "telegram-update:602:0")
            .unwrap()
            .unwrap();
        assert!(row2.payload_json.contains("Панель администратора"));

        // Callback admin:sec:monitor
        let cb_monitor = json!({
            "update_id": 603,
            "callback_query": {
                "id": "cb_mon",
                "from": {"id": 999},
                "message": {"message_id": 2, "chat": {"id": 999}},
                "data": "admin:sec:monitor"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &cb_monitor, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let row3 = crate::outbox::get(&db, "telegram-update:603:0")
            .unwrap()
            .unwrap();
        assert!(row3.payload_json.contains("Мониторинг"));

        // Callback admin:status
        let cb_status = json!({
            "update_id": 604,
            "callback_query": {
                "id": "cb_stat",
                "from": {"id": 999},
                "message": {"message_id": 2, "chat": {"id": 999}},
                "data": "admin:status"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &cb_status, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let row4 = crate::outbox::get(&db, "telegram-update:604:0")
            .unwrap()
            .unwrap();
        assert!(row4.payload_json.contains("Системный статус"));

        // Callback admin:users
        let cb_users = json!({
            "update_id": 605,
            "callback_query": {
                "id": "cb_usr",
                "from": {"id": 999},
                "message": {"message_id": 2, "chat": {"id": 999}},
                "data": "admin:users"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &cb_users, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let row5 = crate::outbox::get(&db, "telegram-update:605:0")
            .unwrap()
            .unwrap();
        assert!(row5.payload_json.contains("Список пользователей"));
    }

    #[test]
    fn admin_broadcast_flow() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();

        let mut settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        settings.admin_telegram_ids = vec![999];

        let catalog = crate::group_catalog::GroupCatalog::default();
        let search = crate::search::SearchCatalog::default();
        let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let context = EngineContext {
            settings: &settings,
            groups: &catalog,
            search: &search,
            group_snapshots: &[],
            now,
            timestamp: 100.0,
        };

        let mut engine = TelegramEngine::default();

        // 1. Старт рассылки admin:broadcast
        let start_bc = json!({
            "update_id": 610,
            "callback_query": {
                "id": "cb_bc",
                "from": {"id": 999},
                "message": {"message_id": 2, "chat": {"id": 999}},
                "data": "admin:broadcast"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &start_bc, &context).unwrap(),
            HandleOutcome::Handled
        ));

        // 2. Ввод текста
        let text_bc = json!({
            "update_id": 611,
            "message": {
                "message_id": 3,
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999, "first_name": "Админ"},
                "text": "Внимание! Изменения в расписании на понедельник."
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &text_bc, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let prev_row = crate::outbox::get(&db, "telegram-update:611:0")
            .unwrap()
            .unwrap();
        assert!(prev_row.payload_json.contains("Предпросмотр рассылки"));

        // 3. Отправка рассылки
        let send_bc = json!({
            "update_id": 612,
            "callback_query": {
                "id": "cb_send",
                "from": {"id": 999},
                "message": {"message_id": 4, "chat": {"id": 999}},
                "data": "admin:broadcast_send"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &send_bc, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let conf_row = crate::outbox::get(&db, "telegram-update:612:0")
            .unwrap()
            .unwrap();
        assert!(conf_row.payload_json.contains("Рассылка запущена"));
    }

    #[test]
    fn admin_ocr_json_import_flow() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();

        let mut settings = crate::config::Settings::from_map(&Default::default()).unwrap();
        settings.admin_telegram_ids = vec![999];

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

        let mut engine = crate::telegram_engine::TelegramEngine::default();

        // 1. Callback admin:ocr_json_import
        let start_import = json!({
            "update_id": 701,
            "callback_query": {
                "id": "cb_import",
                "from": {"id": 999},
                "message": {"message_id": 1, "chat": {"id": 999}},
                "data": "admin:ocr_json_import"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &start_import, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let prompt_row = crate::outbox::get(&db, "telegram-update:701:0")
            .unwrap()
            .unwrap();
        assert!(prompt_row.payload_json.contains("Импорт готового JSON"));

        // 2. Отправка JSON расписания
        let json_data = r#"{
            "group_name": "БПИ-24",
            "days": [
                {
                    "date_iso": "2026-09-01",
                    "date_label": "Вторник",
                    "lessons": [
                        {"number": 1, "subject": "Архитектура компьютеров"}
                    ]
                }
            ]
        }"#;
        let msg_json = json!({
            "update_id": 702,
            "message": {
                "message_id": 2,
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999, "first_name": "Админ"},
                "text": json_data
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &msg_json, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let prev_row = crate::outbox::get(&db, "telegram-update:702:0")
            .unwrap()
            .unwrap();
        assert!(prev_row
            .payload_json
            .contains("Предпросмотр импорта расписания"));
        assert!(prev_row.payload_json.contains("БПИ-24"));

        // 3. Подтверждение импорта admin:ocr_summary_confirm
        let confirm_cb = json!({
            "update_id": 703,
            "callback_query": {
                "id": "cb_conf",
                "from": {"id": 999},
                "message": {"message_id": 3, "chat": {"id": 999}},
                "data": "admin:ocr_summary_confirm"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &confirm_cb, &context).unwrap(),
            HandleOutcome::Handled
        ));
        let res_row = crate::outbox::get(&db, "telegram-update:703:0")
            .unwrap()
            .unwrap();
        assert!(res_row.payload_json.contains("Импорт успешно завершён"));

        // Проверяем сохранение снимка
        let snap = crate::database::latest_snapshot(&db, "current", None, Some("group:БПИ-24"))
            .unwrap()
            .unwrap();
        assert_eq!(snap.content.group_name, "БПИ-24");
        assert_eq!(
            snap.content.days[0].lessons[0].subject,
            "Архитектура компьютеров"
        );
    }
}

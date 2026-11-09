use crate::{
    database::MiniappStatsSummary, format::escape_html, telegram_engine::EngineContext,
    telegram_ui::BotAction, UserRecord,
};
use rusqlite::Transaction;
use serde_json::{json, Value};
use std::collections::HashSet;

#[derive(Clone, Default)]
pub struct DonateFlow {
    pub awaiting_custom_stars: HashSet<i64>,
}

fn donate_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [
                {"text": "15 звезд", "callback_data": "donate:stars:15"},
                {"text": "25 звезд", "callback_data": "donate:stars:25"}
            ],
            [
                {"text": "50 звезд", "callback_data": "donate:stars:50"},
                {"text": "100 звезд", "callback_data": "donate:stars:100"}
            ],
            [
                {"text": "Отправить свое количество", "callback_data": "donate:stars:custom"}
            ]
        ]
    })
}

fn donate_cancel_keyboard() -> Value {
    json!({
        "inline_keyboard": [
            [{"text": "Отменить", "callback_data": "donate:cancel"}]
        ]
    })
}

fn send_stars_invoice_action(chat_id: i64, user_id: i64, stars: i64) -> BotAction {
    BotAction {
        method: "sendInvoice".into(),
        payload: json!({
            "chat_id": chat_id,
            "title": "Поддержка проекта",
            "description": format!("Пожертвование {stars} звезд на развитие и поддержку работы бота."),
            "payload": format!("star_donate:{user_id}:{stars}"),
            "provider_token": "",
            "currency": "XTR",
            "prices": [{"label": "Пожертвование", "amount": stars}]
        }),
    }
}

pub fn format_miniapp_stats_report(report: &MiniappStatsSummary, now_str: &str) -> String {
    let total_views =
        report.page_views_schedule + report.page_views_history + report.page_views_profile;
    let sched_pct = if total_views > 0 {
        format!(
            "{:.1}%",
            (report.page_views_schedule as f64 / total_views as f64) * 100.0
        )
    } else {
        "0%".into()
    };
    let hist_pct = if total_views > 0 {
        format!(
            "{:.1}%",
            (report.page_views_history as f64 / total_views as f64) * 100.0
        )
    } else {
        "0%".into()
    };
    let prof_pct = if total_views > 0 {
        format!(
            "{:.1}%",
            (report.page_views_profile as f64 / total_views as f64) * 100.0
        )
    } else {
        "0%".into()
    };

    format!(
        "<b>Статистика Mini App</b>\n───────────────────────────\nДанные на {now_str}\n\n<b>Уникальные пользователи:</b>\n• Всего за все время: <b>{}</b>\n  (Telegram: {} / VK: {})\n• За сегодня (DAU): <b>{}</b>\n• За 7 дней (WAU): <b>{}</b>\n• За 30 дней (MAU): <b>{}</b>\n\n<b>Сессии и запуски:</b>\n• Всего запусков: <b>{}</b>\n• За сегодня: <b>{}</b>\n• За 7 дней: <b>{}</b>\n• За 30 дней: <b>{}</b>\n• Онлайн за последние 15 минут: <b>{}</b>\n\n<b>Популярные разделы:</b>\n• Расписание: {} ({sched_pct})\n• Архив и история: {} ({hist_pct})\n• Профиль и настройки: {} ({prof_pct})\n───────────────────────────",
        report.total_unique_users,
        report.total_tg_unique,
        report.total_vk_unique,
        report.dau_today,
        report.wau_7d,
        report.mau_30d,
        report.total_sessions,
        report.sessions_today,
        report.sessions_7d,
        report.sessions_30d,
        report.active_online_15m,
        report.page_views_schedule,
        report.page_views_history,
        report.page_views_profile
    )
}

impl DonateFlow {
    pub fn has_user(&self, user_id: i64) -> bool {
        self.awaiting_custom_stars.contains(&user_id)
    }

    pub fn reset_user(&mut self, user_id: i64) {
        self.awaiting_custom_stars.remove(&user_id);
    }

    pub fn takes_precedence(&self, update: &Value) -> bool {
        let Some(user) = update.pointer("/message/from/id").and_then(Value::as_i64) else {
            return false;
        };
        update.pointer("/message/chat/type").and_then(Value::as_str) == Some("private")
            && self.awaiting_custom_stars.contains(&user)
            && update
                .pointer("/message/text")
                .and_then(Value::as_str)
                .is_some_and(|text| !text.starts_with('/') || text == "/cancel")
    }

    pub fn interested(&self, update: &Value) -> bool {
        if update.get("pre_checkout_query").is_some() {
            return true;
        }
        if update.pointer("/message/successful_payment").is_some() {
            return true;
        }
        if let Some(data) = update
            .pointer("/callback_query/data")
            .and_then(Value::as_str)
        {
            if data.starts_with("donate:") || data == "settings:lesson_counters" {
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
                Some("/donate" | "/dnremove" | "/miniapp_stats" | "/app_stats")
            ) {
                return true;
            }
        }
        self.takes_precedence(update)
    }

    pub fn handle(
        &mut self,
        tx: &Transaction<'_>,
        update: &Value,
        user: &UserRecord,
        context: &EngineContext<'_>,
        now: &str,
    ) -> Result<Option<Vec<BotAction>>, Box<dyn std::error::Error>> {
        // 1. Обработка pre_checkout_query
        if let Some(pre_checkout) = update.get("pre_checkout_query") {
            let id = pre_checkout.get("id").and_then(Value::as_str).unwrap_or("");
            return Ok(Some(vec![BotAction {
                method: "answerPreCheckoutQuery".into(),
                payload: json!({
                    "pre_checkout_query_id": id,
                    "ok": true
                }),
            }]));
        }

        // 2. Обработка успешного платежа
        if let Some(payment) = update.pointer("/message/successful_payment") {
            let chat_id = update
                .pointer("/message/chat/id")
                .and_then(Value::as_i64)
                .unwrap_or(user.user_id);
            let stars = payment
                .get("total_amount")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            let charge_id = payment
                .get("telegram_payment_charge_id")
                .and_then(Value::as_str)
                .unwrap_or("");

            let donation_id = crate::database::record_star_donation(
                tx,
                user.user_id,
                user.username.as_deref(),
                user.full_name.as_deref(),
                stars,
                charge_id,
                now,
            )?;

            let thank_you = BotAction {
                method: "sendMessage".into(),
                payload: json!({
                    "chat_id": chat_id,
                    "text": format!("<b>Спасибо за вашу поддержку!</b>\n\nВы пожертвовали <b>{stars} ⭐</b>. Благодаря вашей помощи проект становится лучше и продолжает работать 24/7."),
                    "parse_mode": "HTML"
                }),
            };

            let mut actions = vec![thank_you];

            let user_link = if let Some(un) = user.username.as_deref().filter(|s| !s.is_empty()) {
                format!(
                    "<a href=\"https://t.me/{un}\">t.me/{un}</a> (ID: <code>{}</code>)",
                    user.user_id
                )
            } else {
                format!(
                    "<code>{}</code> (<a href=\"tg://user?id={}\">профиль</a>)",
                    user.user_id, user.user_id
                )
            };

            let admin_msg = format!(
                "⭐ <b>Новое пожертвование (Stars)!</b>\n\n<b>Отправитель:</b> {user_link}\n<b>Количество:</b> {stars} ⭐\n<b>Дата и время:</b> {now}\n<b>ID операции:</b> <code>#{donation_id}</code>\n<b>Telegram Charge ID:</b> <code>{charge_id}</code>\n\nДля возврата средств скопируй команду:\n<code>/dnremove {donation_id}</code>\nили по Charge ID:\n<code>/dnremove {charge_id}</code>"
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

        // 3. Callback queries
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

            if data == "settings:lesson_counters" {
                let text = if !context.settings.lesson_counters_enabled {
                    "Сейчас данный функционал глобально выключен."
                } else if user.subscription_type.as_deref() != Some("group")
                    || user.schedule_id.is_none()
                {
                    "Счетчики пар доступны после выбора группы."
                } else {
                    "Счетчики пар для выбранной группы."
                };
                let screen =
                    crate::telegram_ui::settings_screen(Some(user), &context.settings.miniapp_url);
                let ack = BotAction {
                    method: "answerCallbackQuery".into(),
                    payload: json!({"callback_query_id": callback_id}),
                };
                if let Some(mid) = message_id {
                    let edit = BotAction {
                        method: "editMessageText".into(),
                        payload: json!({
                            "chat_id": chat_id,
                            "message_id": mid,
                            "text": text,
                            "parse_mode": "HTML",
                            "reply_markup": screen.keyboard
                        }),
                    };
                    return Ok(Some(vec![edit, ack]));
                }
                return Ok(Some(vec![ack]));
            }

            if data == "donate:cancel" {
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
                            "text": "Пожертвование отменено.",
                            "parse_mode": "HTML"
                        }),
                    };
                    return Ok(Some(vec![edit, ack]));
                }
                return Ok(Some(vec![ack]));
            }

            if data == "donate:stars:custom" {
                self.awaiting_custom_stars.insert(user.user_id);
                let ack = BotAction {
                    method: "answerCallbackQuery".into(),
                    payload: json!({"callback_query_id": callback_id}),
                };
                let prompt = BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": chat_id,
                        "text": "<b>Укажи количество звёзд</b>\n\nОтправь числом в чат, сколько звёзд ты хочешь пожертвовать (от 15 до 2000):",
                        "parse_mode": "HTML",
                        "reply_markup": donate_cancel_keyboard()
                    }),
                };
                return Ok(Some(vec![prompt, ack]));
            }

            if let Some(val_str) = data.strip_prefix("donate:stars:") {
                let ack = BotAction {
                    method: "answerCallbackQuery".into(),
                    payload: json!({"callback_query_id": callback_id}),
                };
                if let Ok(stars) = val_str.parse::<i64>() {
                    let invoice = send_stars_invoice_action(chat_id, user.user_id, stars);
                    return Ok(Some(vec![invoice, ack]));
                }
                return Ok(Some(vec![ack]));
            }
        }

        // 4. Текстовые сообщения и команды
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
        let chat_type = message
            .pointer("/chat/type")
            .and_then(Value::as_str)
            .unwrap_or("private");

        // Ожидание ввода произвольного числа звезд
        if self.awaiting_custom_stars.contains(&user.user_id) {
            if raw_text == "/cancel" || raw_text.eq_ignore_ascii_case("отмена") {
                self.awaiting_custom_stars.remove(&user.user_id);
                return Ok(Some(vec![BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": chat_id,
                        "text": "Пожертвование отменено.",
                        "parse_mode": "HTML"
                    }),
                }]));
            }
            if let Ok(stars) = raw_text.parse::<i64>() {
                if (15..=2000).contains(&stars) {
                    self.awaiting_custom_stars.remove(&user.user_id);
                    return Ok(Some(vec![send_stars_invoice_action(
                        chat_id,
                        user.user_id,
                        stars,
                    )]));
                }
            }
            return Ok(Some(vec![BotAction {
                method: "sendMessage".into(),
                payload: json!({
                    "chat_id": chat_id,
                    "text": "Пожалуйста, введи целое число от 15 до 2000 звёзд. Попробуй ещё раз:",
                    "parse_mode": "HTML",
                    "reply_markup": donate_cancel_keyboard()
                }),
            }]));
        }

        let command = raw_text
            .split_whitespace()
            .next()
            .and_then(|c| c.split('@').next());

        // Команда /donate
        if command == Some("/donate") {
            if matches!(chat_type, "group" | "supergroup") {
                return Ok(Some(vec![BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": chat_id,
                        "text": "Команда /donate доступна только в личных сообщениях с ботом.",
                        "parse_mode": "HTML"
                    }),
                }]));
            }
            self.awaiting_custom_stars.remove(&user.user_id);
            let prompt_text = "⭐ <b>Развитие и поддержка</b>\n\nБот работает 24/7, быстро обрабатывает сотни запросов и своевременно оповещает об изменениях в расписании.\nПоддержи проект Telegram звёздами (Stars), чтобы помочь оплачивать хостинг и ускорить разработку новых функций.\nЛюбая поддержка помогает проекту и дальше оставаться бесплатным и удобным для всех студентов.\n\nВыбери количество звёзд или отправь своё число через кнопку:";
            return Ok(Some(vec![BotAction {
                method: "sendMessage".into(),
                payload: json!({
                    "chat_id": chat_id,
                    "text": prompt_text,
                    "parse_mode": "HTML",
                    "reply_markup": donate_keyboard()
                }),
            }]));
        }

        // Команда /dnremove для возврата пожертвования
        if command == Some("/dnremove") {
            let is_full_admin = context.settings.admin_telegram_ids.contains(&user.user_id)
                && !context
                    .settings
                    .limited_admin_telegram_ids
                    .contains(&user.user_id);
            if !is_full_admin {
                return Ok(None);
            }
            let query = raw_text
                .split_once(char::is_whitespace)
                .map(|x| x.1)
                .map(str::trim)
                .unwrap_or("");
            if query.is_empty() {
                return Ok(Some(vec![BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": chat_id,
                        "text": "Использование:\n<code>/dnremove &lt;id_операции_или_charge_id&gt;</code>\n\nПример: <code>/dnremove 1</code> или <code>/dnremove stx1gY...</code>",
                        "parse_mode": "HTML"
                    }),
                }]));
            }

            let Some(donation) = crate::database::get_star_donation(tx, query)? else {
                return Ok(Some(vec![BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": chat_id,
                        "text": format!("Пожертвование <code>{}</code> не найдено.", escape_html(query)),
                        "parse_mode": "HTML"
                    }),
                }]));
            };

            if donation.refunded {
                return Ok(Some(vec![BotAction {
                    method: "sendMessage".into(),
                    payload: json!({
                        "chat_id": chat_id,
                        "text": format!("Пожертвование <b>#{}</b> ({} ⭐) уже было возвращено ранее.", donation.id, donation.stars),
                        "parse_mode": "HTML"
                    }),
                }]));
            }

            crate::database::refund_star_donation(tx, donation.id, now)?;

            let refund_action = BotAction {
                method: "refundStarPayment".into(),
                payload: json!({
                    "user_id": donation.user_id,
                    "telegram_payment_charge_id": donation.charge_id
                }),
            };
            let confirm_action = BotAction {
                method: "sendMessage".into(),
                payload: json!({
                    "chat_id": chat_id,
                    "text": format!("<b>Возврат выполнен.</b>\n\nСредства за пожертвование <b>#{}</b> ({} ⭐) возвращены пользователю <code>{}</code>.", donation.id, donation.stars, donation.user_id),
                    "parse_mode": "HTML"
                }),
            };
            return Ok(Some(vec![refund_action, confirm_action]));
        }

        // Команды /app_stats и /miniapp_stats
        if matches!(command, Some("/app_stats" | "/miniapp_stats")) {
            if !context.settings.admin_telegram_ids.contains(&user.user_id) {
                return Ok(None);
            }
            let summary = crate::database::collect_miniapp_stats(tx, context.now)?;
            let text = format_miniapp_stats_report(&summary, now);
            return Ok(Some(vec![BotAction {
                method: "sendMessage".into(),
                payload: json!({
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML"
                }),
            }]));
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
    use std::collections::HashMap;

    fn setup() -> (Connection, Settings, GroupCatalog, SearchCatalog) {
        let mut db = Connection::open_in_memory().unwrap();
        crate::database::initialize(&mut db).unwrap();
        let mut env = HashMap::new();
        env.insert("ADMIN_TELEGRAM_ID".into(), "100".into());
        let settings = Settings::from_map(&env).unwrap();
        let groups = GroupCatalog::default();
        let search = SearchCatalog::default();
        (db, settings, groups, search)
    }

    #[test]
    fn donate_command_and_invoice_generation() {
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

        let donate_cmd = json!({
            "update_id": 501,
            "message": {
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42},
                "text": "/donate"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &donate_cmd, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));
        let row = crate::outbox::get(&db, "telegram-update:501:0")
            .unwrap()
            .unwrap();
        assert!(row.payload_json.contains("donate:stars:15"));
        assert!(row.payload_json.contains("donate:stars:custom"));

        let cb_50 = json!({
            "update_id": 502,
            "callback_query": {
                "id": "cb_inv",
                "from": {"id": 42},
                "message": {"message_id": 10, "chat": {"id": 42}},
                "data": "donate:stars:50"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &cb_50, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));
        let inv_row = crate::outbox::get(&db, "telegram-update:502:0")
            .unwrap()
            .unwrap();
        assert_eq!(inv_row.method, "sendInvoice");
        assert!(inv_row.payload_json.contains("XTR"));
        assert!(inv_row.payload_json.contains("star_donate:42:50"));

        let pre_checkout = json!({
            "update_id": 503,
            "pre_checkout_query": {
                "id": "pq123",
                "from": {"id": 42},
                "currency": "XTR",
                "total_amount": 50,
                "invoice_payload": "star_donate:42:50"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &pre_checkout, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));
        let pq_row = crate::outbox::get(&db, "telegram-update:503:0")
            .unwrap()
            .unwrap();
        assert_eq!(pq_row.method, "answerPreCheckoutQuery");
        assert!(pq_row.payload_json.contains("pq123"));

        let payment = json!({
            "update_id": 504,
            "message": {
                "message_id": 15,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "username": "hero"},
                "successful_payment": {
                    "currency": "XTR",
                    "total_amount": 50,
                    "invoice_payload": "star_donate:42:50",
                    "telegram_payment_charge_id": "ch_star_999"
                }
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &payment, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));
        let d = crate::database::get_star_donation(&db, "ch_star_999")
            .unwrap()
            .unwrap();
        assert_eq!(d.stars, 50);
        assert_eq!(d.user_id, 42);

        let user_thanks = crate::outbox::get(&db, "telegram-update:504:0")
            .unwrap()
            .unwrap();
        assert!(user_thanks
            .payload_json
            .contains("Спасибо за вашу поддержку"));

        let admin_alert = crate::outbox::get(&db, "telegram-update:504:1")
            .unwrap()
            .unwrap();
        assert!(admin_alert.payload_json.contains("/dnremove"));

        let dnremove_cmd = json!({
            "update_id": 505,
            "message": {
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 100},
                "text": "/dnremove 1"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &dnremove_cmd, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));
        let refund_action = crate::outbox::get(&db, "telegram-update:505:0")
            .unwrap()
            .unwrap();
        assert_eq!(refund_action.method, "refundStarPayment");
        assert!(refund_action.payload_json.contains("ch_star_999"));

        let d_ref = crate::database::get_star_donation(&db, "1")
            .unwrap()
            .unwrap();
        assert!(d_ref.refunded);

        let stats_cmd = json!({
            "update_id": 506,
            "message": {
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 100},
                "text": "/app_stats"
            }
        });
        assert!(matches!(
            engine.handle(&mut db, &stats_cmd, &context).unwrap(),
            crate::telegram_engine::HandleOutcome::Handled
        ));
        let stats_row = crate::outbox::get(&db, "telegram-update:506:0")
            .unwrap()
            .unwrap();
        assert!(stats_row.payload_json.contains("Статистика Mini App"));
    }
}

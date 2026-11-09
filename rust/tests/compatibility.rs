use misis_rasp_core::{parser, DaySchedule, Lesson, ScheduleSnapshot};

#[test]
fn snapshot_hash_matches_python_for_persisted_schedule() {
    let snapshot = ScheduleSnapshot {
        group_name: "ИСП-25".into(),
        fetched_at: "2026-11-08T12:00:00".into(),
        days: vec![DaySchedule {
            date_label: String::new(),
            date_iso: "2026-11-09".into(),
            week_variant: Some(1),
            current_week_variant: None,
            lessons: vec![
                Lesson {
                    number: 1,
                    subject: "Физика".into(),
                    teacher: "Иванов".into(),
                    classroom: "101".into(),
                },
                Lesson {
                    number: 1,
                    subject: "Алгебра".into(),
                    teacher: "Петров".into(),
                    classroom: "202".into(),
                },
            ],
        }],
    };
    assert_eq!(
        parser::compute_snapshot_hash(&snapshot),
        "30ba8d004400f6d8b31ca21b5167ba638dcd97d455bed0de92f33b9e29eb6050"
    );
}

#[test]
fn delivery_stops_after_limit_and_large_request_does_not_disable_recipient() {
    use misis_rasp_core::notification::{telegram_response, DeliveryFailure};
    let rate_limit = telegram_response(
        &serde_json::json!({"ok":false,"error_code":429,"parameters":{"retry_after":12}}),
    )
    .unwrap_err();
    assert_eq!(rate_limit.retry_delay(9), Some(12.5));
    assert_eq!(rate_limit.retry_delay(10), None);
    let large = telegram_response(
        &serde_json::json!({"ok":false,"error_code":413,"description":"Request Entity Too Large"}),
    )
    .unwrap_err();
    assert_eq!(large.retry_delay(1), None);
    assert!(!large.permanent_recipient());
    assert_eq!(DeliveryFailure::Network.retry_delay(10), None);
}

#[test]
fn sticker_personalization_rolls_back_and_replay_does_not_duplicate_delivery() {
    use misis_rasp_core::{
        config::Settings,
        group_catalog::GroupCatalog,
        search::SearchCatalog,
        telegram_engine::{EngineContext, HandleOutcome, TelegramEngine},
    };
    use serde_json::json;
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    let settings = Settings::from_map(&Default::default()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::default();
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let mut engine = TelegramEngine::default();
    let setup = json!({"update_id":200,"callback_query":{"id":"cb","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:pers_set_sticker"}});
    assert!(matches!(
        engine.handle(&mut db, &setup, &context).unwrap(),
        HandleOutcome::Handled
    ));
    let sticker = json!({"update_id":201,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"sticker":{"file_id":"sticker-file"}}});
    db.execute_batch("CREATE TRIGGER fail_sticker BEFORE INSERT ON delivery_outbox BEGIN SELECT RAISE(ABORT,'failure'); END;").unwrap();
    assert!(engine.handle(&mut db, &sticker, &context).is_err());
    assert!(misis_rasp_core::database::get_user(&db, "telegram", 42)
        .unwrap()
        .unwrap()
        .custom_sticker_file_id
        .is_none());
    db.execute_batch("DROP TRIGGER fail_sticker").unwrap();
    assert!(matches!(
        engine.handle(&mut db, &sticker, &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert!(matches!(
        engine.handle(&mut db, &sticker, &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert_eq!(
        misis_rasp_core::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap()
            .custom_sticker_file_id
            .as_deref(),
        Some("sticker-file")
    );
    let count: i64 = db
        .query_row(
            "SELECT count(*) FROM delivery_outbox WHERE delivery_id LIKE 'telegram-update:201:%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
    let clear = json!({"update_id":202,"callback_query":{"id":"cb2","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:pers_clear_sticker"}});
    assert!(matches!(
        engine.handle(&mut db, &clear, &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert!(misis_rasp_core::database::get_user(&db, "telegram", 42)
        .unwrap()
        .unwrap()
        .custom_sticker_file_id
        .is_none());
}

#[test]
fn cancelling_sticker_selection_leaves_existing_sticker_unchanged() {
    use misis_rasp_core::{
        config::Settings,
        group_catalog::GroupCatalog,
        search::SearchCatalog,
        telegram_engine::{EngineContext, HandleOutcome, TelegramEngine},
    };
    use serde_json::json;
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,custom_sticker_file_id,created_at,last_seen_at) VALUES ('telegram',42,'existing','now','now')",[]).unwrap();
    let settings = Settings::from_map(&Default::default()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::default();
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let mut engine = TelegramEngine::default();
    let callback = |id: i64, data: &str| json!({"update_id":id,"callback_query":{"id":format!("cb{id}"),"from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":data}});
    assert!(matches!(
        engine
            .handle(
                &mut db,
                &callback(210, "settings:pers_set_sticker"),
                &context
            )
            .unwrap(),
        HandleOutcome::Handled
    ));
    assert!(matches!(
        engine
            .handle(
                &mut db,
                &callback(211, "settings:personalization"),
                &context
            )
            .unwrap(),
        HandleOutcome::Handled
    ));
    let sticker = json!({"update_id":212,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"sticker":{"file_id":"replacement"}}});
    assert!(matches!(
        engine.handle(&mut db, &sticker, &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert_eq!(
        misis_rasp_core::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap()
            .custom_sticker_file_id
            .as_deref(),
        Some("existing")
    );
    assert!(matches!(
        engine
            .handle(
                &mut db,
                &callback(213, "settings:pers_set_sticker"),
                &context
            )
            .unwrap(),
        HandleOutcome::Handled
    ));
    let start = json!({"update_id":214,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/start"}});
    assert!(matches!(
        engine.handle(&mut db, &start, &context).unwrap(),
        HandleOutcome::Handled
    ));
    let after_start = json!({"update_id":215,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"sticker":{"file_id":"replacement"}}});
    assert!(matches!(
        engine.handle(&mut db, &after_start, &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert_eq!(
        misis_rasp_core::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap()
            .custom_sticker_file_id
            .as_deref(),
        Some("existing")
    );
    assert!(matches!(
        engine
            .handle(
                &mut db,
                &callback(216, "settings:pers_preview_sticker"),
                &context
            )
            .unwrap(),
        HandleOutcome::Handled
    ));
    assert_eq!(
        misis_rasp_core::outbox::get(&db, "telegram-update:216:0")
            .unwrap()
            .unwrap()
            .method,
        "sendSticker"
    );
    assert_eq!(
        misis_rasp_core::outbox::get(&db, "telegram-update:216:1")
            .unwrap()
            .unwrap()
            .method,
        "sendMessage"
    );
    assert_eq!(
        misis_rasp_core::outbox::get(&db, "telegram-update:216:2")
            .unwrap()
            .unwrap()
            .method,
        "answerCallbackQuery"
    );
}

#[test]
fn personalization_callback_without_message_is_acknowledged() {
    use misis_rasp_core::{
        config::Settings,
        group_catalog::GroupCatalog,
        search::SearchCatalog,
        telegram_engine::{EngineContext, HandleOutcome, TelegramEngine},
    };
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    let settings = Settings::from_map(&Default::default()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::default();
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let update = serde_json::json!({"update_id":220,"callback_query":{"id":"cb","from":{"id":42},"data":"settings:personalization"}});
    assert!(matches!(
        TelegramEngine::default()
            .handle(&mut db, &update, &context)
            .unwrap(),
        HandleOutcome::Handled
    ));
    assert_eq!(
        misis_rasp_core::outbox::get(&db, "telegram-update:220:0")
            .unwrap()
            .unwrap()
            .method,
        "answerCallbackQuery"
    );
}

#[test]
fn failed_cancel_keeps_pending_sticker_selection() {
    use misis_rasp_core::{
        config::Settings,
        group_catalog::GroupCatalog,
        search::SearchCatalog,
        telegram_engine::{EngineContext, HandleOutcome, TelegramEngine},
    };
    use serde_json::json;
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    let settings = Settings::from_map(&Default::default()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::default();
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let mut engine = TelegramEngine::default();
    let setup = json!({"update_id":270,"callback_query":{"id":"cb","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:pers_set_sticker"}});
    assert!(matches!(
        engine.handle(&mut db, &setup, &context).unwrap(),
        HandleOutcome::Handled
    ));
    let cancel = json!({"update_id":271,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/cancel"}});
    db.execute_batch("CREATE TRIGGER fail_cancel BEFORE INSERT ON delivery_outbox BEGIN SELECT RAISE(ABORT,'failure'); END;").unwrap();
    assert!(engine.handle(&mut db, &cancel, &context).is_err());
    db.execute_batch("DROP TRIGGER fail_cancel").unwrap();
    let sticker = json!({"update_id":272,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"sticker":{"file_id":"sticker-file"}}});
    assert!(matches!(
        engine.handle(&mut db, &sticker, &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert_eq!(
        misis_rasp_core::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap()
            .custom_sticker_file_id
            .as_deref(),
        Some("sticker-file")
    );
}

#[test]
fn cancel_does_not_clear_another_users_pending_sticker() {
    use misis_rasp_core::{
        config::Settings,
        group_catalog::GroupCatalog,
        search::SearchCatalog,
        telegram_engine::{EngineContext, HandleOutcome, TelegramEngine},
    };
    use serde_json::json;
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    let settings = Settings::from_map(&Default::default()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::default();
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let mut engine = TelegramEngine::default();
    let setup = |update: i64, user: i64| json!({"update_id":update,"callback_query":{"id":format!("cb{update}"),"from":{"id":user},"message":{"message_id":7,"chat":{"id":user}},"data":"settings:pers_set_sticker"}});
    assert!(matches!(
        engine.handle(&mut db, &setup(290, 42), &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert!(matches!(
        engine.handle(&mut db, &setup(291, 43), &context).unwrap(),
        HandleOutcome::Handled
    ));
    let cancel = json!({"update_id":292,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/cancel"}});
    assert!(matches!(
        engine.handle(&mut db, &cancel, &context).unwrap(),
        HandleOutcome::Handled
    ));
    let sticker = |update: i64, user: i64| json!({"update_id":update,"message":{"chat":{"id":user,"type":"private"},"from":{"id":user},"sticker":{"file_id":"sticker-file"}}});
    assert!(matches!(
        engine.handle(&mut db, &sticker(293, 42), &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert!(matches!(
        engine.handle(&mut db, &sticker(294, 43), &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert!(misis_rasp_core::database::get_user(&db, "telegram", 42)
        .unwrap()
        .unwrap()
        .custom_sticker_file_id
        .is_none());
    assert_eq!(
        misis_rasp_core::database::get_user(&db, "telegram", 43)
            .unwrap()
            .unwrap()
            .custom_sticker_file_id
            .as_deref(),
        Some("sticker-file")
    );
}

#[test]
fn cancel_invalidates_old_teacher_choice() {
    use misis_rasp_core::{
        config::Settings,
        group_catalog::GroupCatalog,
        search::SearchCatalog,
        telegram_engine::{EngineContext, HandleOutcome, TelegramEngine},
    };
    use serde_json::json;
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    let settings = Settings::from_map(&Default::default()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::new(
        &[
            ("Иванов Иван".into(), "/raspprep/1".into()),
            ("Иванов Игорь".into(), "/raspprep/2".into()),
        ],
        &[],
    );
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let mut engine = TelegramEngine::default();
    let input = json!({"update_id":300,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"Иванов"}});
    assert!(matches!(
        engine.handle(&mut db, &input, &context).unwrap(),
        HandleOutcome::Handled
    ));
    let row = misis_rasp_core::outbox::get(&db, "telegram-update:300:0")
        .unwrap()
        .unwrap();
    let payload: serde_json::Value = serde_json::from_str(&row.payload_json).unwrap();
    let data = payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        .as_str()
        .unwrap();
    let cancel = json!({"update_id":301,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/cancel"}});
    assert!(matches!(
        engine.handle(&mut db, &cancel, &context).unwrap(),
        HandleOutcome::Handled
    ));
    let choice = json!({"update_id":302,"callback_query":{"id":"old","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":data}});
    assert!(matches!(
        engine.handle(&mut db, &choice, &context).unwrap(),
        HandleOutcome::Handled
    ));
    let alert = misis_rasp_core::outbox::get(&db, "telegram-update:302:0")
        .unwrap()
        .unwrap();
    assert!(
        alert.payload_json.contains("Этот выбор устарел")
            && alert.payload_json.contains("show_alert")
    );
    assert!(misis_rasp_core::database::get_user(&db, "telegram", 42)
        .unwrap()
        .unwrap()
        .subscription_key
        .is_none());
}

#[tokio::test]
async fn cancel_clears_search_audience_and_sticker_waiting_states() {
    use std::io::{Read, Write};
    use std::time::Duration;
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut buf = [0; 8192];
        let _ = stream.read(&mut buf).unwrap();
        let body = r#"{"ok":true,"result":[{"update_id":280,"callback_query":{"id":"s","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"schedule:find"}},{"update_id":281,"callback_query":{"id":"a","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:audience_setup"}},{"update_id":282,"callback_query":{"id":"p","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:pers_set_sticker"}},{"update_id":283,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/cancel"}},{"update_id":284,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"sticker":{"file_id":"unexpected"}}},{"update_id":285,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"312"}}]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    let parser =
        parser::ScheduleParser::new("", "", Duration::from_millis(100), 1, Duration::ZERO).unwrap();
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,created_at,last_seen_at) VALUES ('telegram',42,'teacher','teacher:1','Иванов','now','now')",[]).unwrap();
    let settings = misis_rasp_core::config::Settings::from_map(&Default::default()).unwrap();
    let groups = misis_rasp_core::group_catalog::GroupCatalog::default();
    let search = misis_rasp_core::search::SearchCatalog::new(
        &[],
        &[("312".into(), "http://college/raspAud/312".into())],
    );
    let context = misis_rasp_core::telegram_engine::EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let mut poll = misis_rasp_core::telegram_poll::TelegramLongPoll::new(25);
    let result = misis_rasp_core::telegram_engine::poll_iteration_with_parser(
        &mut poll,
        &api,
        &mut Default::default(),
        &mut db,
        &context,
        Some(&parser),
    )
    .await;
    assert!(matches!(
        result,
        Ok(misis_rasp_core::telegram_engine::PollOutcome::Unsupported {
            processed: 5,
            update_id: Some(285)
        }) | Ok(misis_rasp_core::telegram_engine::PollOutcome::Processed(6))
    ));
    if let Some(row) = misis_rasp_core::outbox::get(&db, "telegram-update:285:0").unwrap() {
        assert!(!row
            .payload_json
            .contains("Сайт расписания временно недоступен"));
        assert!(!row.payload_json.contains("Расписание для 312"));
    }
    let user = misis_rasp_core::database::get_user(&db, "telegram", 42)
        .unwrap()
        .unwrap();
    assert!(user.audience_subscription_key.is_none() && user.custom_sticker_file_id.is_none());
    server.join().unwrap();
}

#[test]
fn teacher_audience_subscription_is_atomic_and_replay_safe() {
    use misis_rasp_core::{
        config::Settings,
        group_catalog::GroupCatalog,
        search::SearchCatalog,
        telegram_engine::{EngineContext, HandleOutcome, TelegramEngine},
    };
    use serde_json::json;
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,subscription_url,created_at,last_seen_at) VALUES ('telegram',42,'teacher','teacher:1','Иванов','http://college/raspprep/1','now','now')",[]).unwrap();
    let settings = Settings::from_map(&Default::default()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::new(&[], &[("312".into(), "http://college/raspAud/312".into())]);
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let mut engine = TelegramEngine::default();
    let setup = json!({"update_id":230,"callback_query":{"id":"cb","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:audience_setup"}});
    assert!(matches!(
        engine.handle(&mut db, &setup, &context).unwrap(),
        HandleOutcome::Handled
    ));
    let missing = json!({"update_id":231,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"несуществующий кабинет"}});
    assert!(matches!(
        engine.handle(&mut db, &missing, &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert!(misis_rasp_core::outbox::get(&db, "telegram-update:231:0")
        .unwrap()
        .unwrap()
        .payload_json
        .contains("Кабинет не найден"));
    let input = json!({"update_id":232,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"312"}});
    db.execute_batch("CREATE TRIGGER fail_audience BEFORE INSERT ON delivery_outbox BEGIN SELECT RAISE(ABORT,'failure'); END;").unwrap();
    assert!(engine.handle(&mut db, &input, &context).is_err());
    assert!(misis_rasp_core::database::get_user(&db, "telegram", 42)
        .unwrap()
        .unwrap()
        .audience_subscription_key
        .is_none());
    db.execute_batch("DROP TRIGGER fail_audience").unwrap();
    assert!(matches!(
        engine.handle(&mut db, &input, &context).unwrap(),
        HandleOutcome::Handled
    ));
    assert!(matches!(
        engine.handle(&mut db, &input, &context).unwrap(),
        HandleOutcome::Handled
    ));
    let user = misis_rasp_core::database::get_user(&db, "telegram", 42)
        .unwrap()
        .unwrap();
    assert_eq!(user.subscription_key.as_deref(), Some("teacher:1"));
    assert_eq!(
        user.audience_subscription_key.as_deref(),
        Some("audience:312")
    );
    let count: i64 = db
        .query_row(
            "SELECT count(*) FROM account_subscriptions WHERE subscription_key='audience:312'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}

#[test]
fn non_teacher_cannot_start_audience_subscription() {
    use misis_rasp_core::{
        config::Settings,
        group_catalog::GroupCatalog,
        search::SearchCatalog,
        telegram_engine::{EngineContext, HandleOutcome, TelegramEngine},
    };
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    let settings = Settings::from_map(&Default::default()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::default();
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let setup = serde_json::json!({"update_id":240,"callback_query":{"id":"cb","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:audience_setup"}});
    assert!(matches!(
        TelegramEngine::default()
            .handle(&mut db, &setup, &context)
            .unwrap(),
        HandleOutcome::Handled
    ));
    let row = misis_rasp_core::outbox::get(&db, "telegram-update:240:0")
        .unwrap()
        .unwrap();
    assert!(row.payload_json.contains("show_alert") && row.payload_json.contains("преподавателя"));
}

#[test]
fn returning_to_menu_cancels_audience_input_without_changing_subscription() {
    use misis_rasp_core::{
        config::Settings,
        group_catalog::GroupCatalog,
        search::SearchCatalog,
        telegram_engine::{EngineContext, HandleOutcome, TelegramEngine},
    };
    use serde_json::json;
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,subscription_url,created_at,last_seen_at) VALUES ('telegram',42,'teacher','teacher:1','Иванов','http://college/raspprep/1','now','now')",[]).unwrap();
    let settings = Settings::from_map(&Default::default()).unwrap();
    let groups = GroupCatalog::default();
    let search = SearchCatalog::new(&[], &[("312".into(), "http://college/raspAud/312".into())]);
    let context = EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let callback = |id: i64, data: &str| json!({"update_id":id,"callback_query":{"id":format!("cb{id}"),"from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":data}});
    let mut engine = TelegramEngine::default();
    assert!(matches!(
        engine
            .handle(&mut db, &callback(260, "settings:audience_setup"), &context)
            .unwrap(),
        HandleOutcome::Handled
    ));
    assert!(matches!(
        engine
            .handle(&mut db, &callback(261, "menu:start"), &context)
            .unwrap(),
        HandleOutcome::Handled
    ));
    let input = json!({"update_id":262,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"312"}});
    let _ = engine.handle(&mut db, &input, &context).unwrap();
    assert!(misis_rasp_core::database::get_user(&db, "telegram", 42)
        .unwrap()
        .unwrap()
        .audience_subscription_key
        .is_none());
}

#[tokio::test]
async fn audience_input_takes_precedence_over_pending_search_in_polling() {
    use std::io::{Read, Write};
    use std::time::Duration;
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut buf = [0; 8192];
        let _ = stream.read(&mut buf).unwrap();
        let body = r#"{"ok":true,"result":[{"update_id":250,"callback_query":{"id":"s","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"schedule:find"}},{"update_id":251,"callback_query":{"id":"a","from":{"id":42},"message":{"message_id":7,"chat":{"id":42}},"data":"settings:audience_setup"}},{"update_id":252,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"312"}}]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    let parser =
        parser::ScheduleParser::new("", "", Duration::from_millis(100), 1, Duration::ZERO).unwrap();
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,subscription_url,created_at,last_seen_at) VALUES ('telegram',42,'teacher','teacher:1','Иванов','http://college/raspprep/1','now','now')",[]).unwrap();
    let settings = misis_rasp_core::config::Settings::from_map(&Default::default()).unwrap();
    let groups = misis_rasp_core::group_catalog::GroupCatalog::default();
    let search = misis_rasp_core::search::SearchCatalog::new(
        &[],
        &[("312".into(), "http://college/raspAud/312".into())],
    );
    let context = misis_rasp_core::telegram_engine::EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now: chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap(),
        timestamp: 100.0,
    };
    let mut poll = misis_rasp_core::telegram_poll::TelegramLongPoll::new(25);
    let result = misis_rasp_core::telegram_engine::poll_iteration_with_parser(
        &mut poll,
        &api,
        &mut Default::default(),
        &mut db,
        &context,
        Some(&parser),
    )
    .await;
    assert!(matches!(
        result,
        Ok(misis_rasp_core::telegram_engine::PollOutcome::Processed(3))
    ));
    assert_eq!(
        misis_rasp_core::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap()
            .audience_subscription_key
            .as_deref(),
        Some("audience:312")
    );
    server.join().unwrap();
}

#[tokio::test]
async fn http_retry_then_parses_college_page() {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::time::Duration;
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let thread = std::thread::spawn(move || {
        for status in ["503 Service Unavailable", "200 OK"] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 2048];
            let _ = stream.read(&mut request).unwrap();
            let body = if status.starts_with("200") {
                "<div id='titleF'>Group</div>"
            } else {
                "temporary"
            };
            write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        }
    });
    let parser = parser::ScheduleParser::new(
        &format!("http://{address}/rasp"),
        "",
        Duration::from_secs(5),
        2,
        Duration::ZERO,
    )
    .unwrap();
    let reference = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let (snapshot, _) = parser.parse(1, reference).await.unwrap();
    assert_eq!(snapshot.group_name, "Group");
    thread.join().unwrap();
}
#[tokio::test]
async fn transport_sends_telegram_and_vk_payloads_to_local_api() {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for index in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 8192];
            let length = stream.read(&mut request).unwrap();
            let request = String::from_utf8_lossy(&request[..length]);
            assert!(if index == 0 {
                request.starts_with("POST /bottest/sendMessage ")
            } else {
                request.starts_with("POST /messages.send ")
            });
            let body = if index == 0 {
                r#"{"ok":true,"result":{"message_id":7}}"#
            } else {
                r#"{"response":42}"#
            };
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let base = format!("http://{addr}");
    let transport = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "test".into(),
        "5.199",
    )
    .unwrap();
    let tg = transport
        .send(
            "telegram",
            "sendMessage",
            &serde_json::json!({"chat_id":1,"text":"x"}),
        )
        .await
        .unwrap();
    assert_eq!(tg["message_id"], 7);
    let vk = transport
        .send(
            "vk",
            "messages.send",
            &serde_json::json!({"peer_id":1,"message":"x","random_id":1}),
        )
        .await
        .unwrap();
    assert_eq!(vk, 42);
    server.join().unwrap();
}
#[tokio::test]
async fn queued_vk_message_falls_back_without_keyboard() {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for index in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 8192];
            let _ = stream.read(&mut request).unwrap();
            let body = if index == 0 {
                r#"{"error":{"error_code":911,"error_msg":"keyboard"}}"#
            } else {
                r#"{"response":42}"#
            };
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    db.execute_batch(misis_rasp_core::outbox::SCHEMA).unwrap();
    db.execute_batch("CREATE TABLE delivery_events(message_id TEXT,campaign_type TEXT,platform TEXT,user_id INTEGER,via_broker INTEGER,status TEXT,attempt INTEGER,error_text TEXT,created_at TEXT);").unwrap();
    let messages = misis_rasp_core::notification::prepare_notification(
        "vk",
        2,
        "hello",
        "notification",
        "event",
        None,
    );
    misis_rasp_core::notification::enqueue_prepared(&mut db, &messages, 100.0).unwrap();
    let base = format!("http://{addr}");
    let transport = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "".into(),
        "test".into(),
        "5.199",
    )
    .unwrap();
    let outcome = misis_rasp_core::notification::drain_once(&mut db, &transport, "vk", 100.0, 50)
        .await
        .unwrap();
    assert_eq!(
        outcome,
        vec![(
            messages[0].id.clone(),
            misis_rasp_core::notification::ProcessOutcome::Sent
        )]
    );
    assert_eq!(
        misis_rasp_core::outbox::status(&db, &messages[0].id)
            .unwrap()
            .as_deref(),
        Some("sent")
    );
    let status: String = db
        .query_row("SELECT status FROM delivery_events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(status, "sent");
    server.join().unwrap();
}

#[tokio::test]
async fn telegram_plain_http_413_fails_one_message_without_blocking_peer_or_disabling_user() {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for (status, body) in [
            ("413 Payload Too Large", "too large"),
            ("200 OK", r#"{"ok":true,"result":{"message_id":7}}"#),
        ] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 8192];
            let _ = stream.read(&mut request).unwrap();
            write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,created_at,last_seen_at) VALUES ('telegram',42,'now','now')",[]).unwrap();
    let first = misis_rasp_core::notification::prepare_notification(
        "telegram",
        42,
        "one",
        "notification",
        "first",
        None,
    );
    let second = misis_rasp_core::notification::prepare_notification(
        "telegram",
        42,
        "two",
        "notification",
        "second",
        None,
    );
    misis_rasp_core::notification::enqueue_prepared(&mut db, &first, 100.0).unwrap();
    misis_rasp_core::notification::enqueue_prepared(&mut db, &second, 100.0).unwrap();
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    let first_result =
        misis_rasp_core::notification::drain_once(&mut db, &api, "telegram", 100.0, 50)
            .await
            .unwrap();
    assert_eq!(
        first_result[0].1,
        misis_rasp_core::notification::ProcessOutcome::Failed
    );
    let second_result =
        misis_rasp_core::notification::drain_once(&mut db, &api, "telegram", 101.0, 50)
            .await
            .unwrap();
    assert_eq!(
        second_result[0].1,
        misis_rasp_core::notification::ProcessOutcome::Sent
    );
    let user = misis_rasp_core::database::get_user(&db, "telegram", 42)
        .unwrap()
        .unwrap();
    assert!(user.homework_notifications_enabled && !user.delivery_disabled_auto);
    server.join().unwrap();
}
#[tokio::test]
async fn vk_poll_uses_saved_position_after_server_refresh() {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for index in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut bytes = [0; 8192];
            let n = stream.read(&mut bytes).unwrap();
            let req = String::from_utf8_lossy(&bytes[..n]);
            let body = if index == 0 {
                assert!(req.starts_with("POST /groups.getLongPollServer "));
                format!(
                    r#"{{"response":{{"server":"http://{addr}/poll","key":"test","ts":"100"}}}}"#
                )
            } else {
                assert!(req.contains("ts=42"));
                r#"{"ts":"43","updates":[{"event_id":"e1"}]}"#.into()
            };
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "".into(),
        "test".into(),
        "5.199",
    )
    .unwrap();
    let mut poll = misis_rasp_core::vk_poll::VkLongPoll::new(1, 25, Some("42".into())).unwrap();
    match poll.poll_once(&api).await.unwrap() {
        misis_rasp_core::vk_poll::PollResult::Batch { ts, updates } => {
            assert_eq!(ts, "43");
            assert_eq!(updates.len(), 1);
        }
        _ => panic!("expected batch"),
    };
    server.join().unwrap();
}
#[tokio::test]
async fn telegram_poll_reads_updates_without_advancing_before_ack() {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut bytes = [0; 8192];
        let n = stream.read(&mut bytes).unwrap();
        assert!(String::from_utf8_lossy(&bytes[..n]).starts_with("POST /bottest/getUpdates "));
        let body = r#"{"ok":true,"result":[{"update_id":42,"message":{}}]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    let mut poll = misis_rasp_core::telegram_poll::TelegramLongPoll::new(25);
    let updates = poll.fetch(&api).await.unwrap();
    assert_eq!(poll.next_offset(), None);
    assert!(poll.acknowledge(&updates[0]));
    assert_eq!(poll.next_offset(), Some(43));
    server.join().unwrap();
}
#[tokio::test]
async fn catalog_keeps_cached_college_if_only_university_is_reachable() {
    use std::{
        io::{Read, Write},
        time::Duration,
    };
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for index in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut b = [0; 8192];
            let _ = stream.read(&mut b).unwrap();
            let (status, body) = if index == 0 {
                ("503 Service Unavailable", "bad")
            } else {
                (
                    "200 OK",
                    r#"{"groups":[{"value":2,"content":"Университет"}]}"#,
                )
            };
            write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let base = format!("http://{addr}");
    let parser = misis_rasp_core::parser::ScheduleParser::new(
        &base,
        &base,
        Duration::from_secs(5),
        1,
        Duration::ZERO,
    )
    .unwrap();
    let cached = vec![misis_rasp_core::group_catalog::GroupInfo {
        department_id: 1,
        department_code: "A".into(),
        department_name: String::new(),
        group_name: "Колледж".into(),
        schedule_id: Some(1),
        url: String::new(),
    }];
    let result = misis_rasp_core::group_catalog::fetch_catalog(&parser, &base, &base, &cached)
        .await
        .unwrap();
    assert!(result.college_failed);
    assert!(!result.university_failed);
    assert_eq!(result.groups.len(), 2);
    server.join().unwrap();
}
#[tokio::test]
async fn retry_catalog_fetches_only_failed_departments() {
    use std::{
        io::{Read, Write},
        time::Duration,
    };
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for index in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut b = [0; 8192];
            let n = stream.read(&mut b).unwrap();
            let request = String::from_utf8_lossy(&b[..n]);
            let (status, body) = if index == 0 {
                assert!(request.starts_with("GET /group/1 "));
                ("200 OK", "<a href='/rasp/10'>Группа</a>")
            } else {
                assert!(request.starts_with("GET /group/2 "));
                ("503 Service Unavailable", "bad")
            };
            write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let base = format!("http://{addr}");
    let parser = misis_rasp_core::parser::ScheduleParser::new(
        &base,
        "",
        Duration::from_secs(5),
        1,
        Duration::ZERO,
    )
    .unwrap();
    let result = misis_rasp_core::group_catalog::retry_departments(
        &parser,
        &base,
        &[(1, "A".into()), (2, "B".into())],
        &[],
    )
    .await;
    assert!(result.recovered);
    assert_eq!(result.groups.len(), 1);
    assert_eq!(result.remaining, vec![(2, "B".into())]);
    server.join().unwrap();
}
#[tokio::test]
async fn telegram_poll_only_acknowledges_events_saved_in_outbox() {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut b = [0; 8192];
        let _ = stream.read(&mut b).unwrap();
        let body = r#"{"ok":true,"result":[{"update_id":20,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42,"first_name":"Ivan"},"text":"/start"}},{"update_id":21,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42,"first_name":"Ivan"},"text":"/unknown"}}]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    let mut poll = misis_rasp_core::telegram_poll::TelegramLongPoll::new(25);
    let mut engine = misis_rasp_core::telegram_engine::TelegramEngine::default();
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    let settings = misis_rasp_core::config::Settings::from_map(&Default::default()).unwrap();
    let groups = misis_rasp_core::group_catalog::GroupCatalog::default();
    let search = misis_rasp_core::search::SearchCatalog::default();
    let now = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let context = misis_rasp_core::telegram_engine::EngineContext {
        settings: &settings,
        groups: &groups,
        search: &search,
        group_snapshots: &[],
        now,
        timestamp: 100.0,
    };
    let result = misis_rasp_core::telegram_engine::poll_iteration(
        &mut poll,
        &api,
        &mut engine,
        &mut db,
        &context,
    )
    .await;
    assert!(matches!(
        result,
        Ok(misis_rasp_core::telegram_engine::PollOutcome::Unsupported {
            processed: 1,
            update_id: Some(21)
        })
    ));
    assert_eq!(poll.next_offset(), Some(21));
    assert!(misis_rasp_core::outbox::get(&db, "telegram-update:20:0")
        .unwrap()
        .is_some());
    server.join().unwrap();
}

#[tokio::test]
async fn telegram_fetches_missing_schedule_before_queuing_response() {
    use std::io::{Read, Write};
    use std::time::Duration;
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for index in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 8192];
            let n = stream.read(&mut request).unwrap();
            let request = String::from_utf8_lossy(&request[..n]);
            let body = if index == 0 {
                assert!(request.starts_with("POST /bottest/getUpdates "));
                r#"{"ok":true,"result":[{"update_id":30,"callback_query":{"id":"cb","from":{"id":42},"message":{"chat":{"id":42}},"data":"schedule:today"}}]}"#
            } else {
                assert!(request.starts_with("GET /rasp/123 "));
                r#"<div id="titleF">ИСП-25</div><div class="titleDate">8 ноября</div><div class="rasp"><table><tr><th>Номер</th></tr><tr><td>1</td><td>Физика</td><td>Иванов</td><td>101</td></tr></table></div>"#
            };
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    let parser = parser::ScheduleParser::new(
        &format!("{base}/rasp"),
        "",
        Duration::from_secs(5),
        1,
        Duration::ZERO,
    )
    .unwrap();
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,group_name,schedule_id,created_at,last_seen_at) VALUES ('telegram',42,'group','group:123','ИСП-25','ИСП-25',123,'now','now')", []).unwrap();
    let settings = misis_rasp_core::config::Settings::from_map(&Default::default()).unwrap();
    let groups = misis_rasp_core::group_catalog::GroupCatalog::default();
    let search = misis_rasp_core::search::SearchCatalog::default();
    let context = misis_rasp_core::telegram_engine::EngineContext {
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
    let mut poll = misis_rasp_core::telegram_poll::TelegramLongPoll::new(25);
    let result = misis_rasp_core::telegram_engine::poll_iteration_with_parser(
        &mut poll,
        &api,
        &mut Default::default(),
        &mut db,
        &context,
        Some(&parser),
    )
    .await;
    assert!(matches!(
        result,
        Ok(misis_rasp_core::telegram_engine::PollOutcome::Processed(1))
    ));
    assert!(misis_rasp_core::outbox::get(&db, "telegram-update:30:0")
        .unwrap()
        .unwrap()
        .payload_json
        .contains("Физика"));
    server.join().unwrap();
}

#[tokio::test]
async fn university_variant_checks_the_active_button_and_target_week() {
    use std::io::{Read, Write};
    use std::time::Duration;
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for active in [2, 1] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 8192];
            let n = stream.read(&mut request).unwrap();
            assert!(String::from_utf8_lossy(&request[..n]).starts_with("GET /group/123/2 "));
            let weekdays = [
                "Понедельник",
                "Вторник",
                "Среда",
                "Четверг",
                "Пятница",
                "Суббота",
                "Воскресенье",
            ];
            let rows = weekdays.iter().map(|day| format!(r#"<tr><th class="table-weekdays">{day}</th><td class="table-empty">—</td></tr>"#)).collect::<String>();
            let body = format!(
                r#"<a id="title-value">Расписание для Группа</a><div id="schedule-title"><button class="search-button-active" onclick="open('/group/123/{active}')"></button><div class="misc-schedule"><span>На этой неделе учимся по числителю (показано по знаменателю)</span></div></div><table id="schedule-table"><tr><th><span class="table-lesson-num">1</span></th></tr>{rows}</table>"#
            );
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let parser =
        parser::ScheduleParser::new("", "", Duration::from_secs(5), 1, Duration::ZERO).unwrap();
    let target = chrono::NaiveDate::from_ymd_opt(2026, 11, 11).unwrap();
    let url = format!("http://{addr}/group/123/1");
    let snapshot = parser
        .parse_university_variant(&url, 2, target)
        .await
        .unwrap();
    assert!(snapshot.days.iter().any(|day| day.date_iso == "2026-11-11"));
    assert!(parser
        .parse_university_variant(&url, 2, target)
        .await
        .is_err());
    assert!(parser
        .parse_university_variant(&url, 0, target)
        .await
        .is_err());
    server.join().unwrap();
}

#[tokio::test]
async fn telegram_search_queues_preview_without_changing_subscription() {
    use std::io::{Read, Write};
    use std::time::Duration;
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = [0; 8192];
        let _ = stream.read(&mut request).unwrap();
        let body = r#"{"ok":true,"result":[{"update_id":40,"callback_query":{"id":"cb","from":{"id":42},"message":{"chat":{"id":42}},"data":"schedule:find"}},{"update_id":41,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"ИСП-25"}}]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    let parser =
        parser::ScheduleParser::new("", "", Duration::from_millis(100), 1, Duration::ZERO).unwrap();
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,created_at,last_seen_at) VALUES ('telegram',42,'group','group:1','Старая','now','now')", []).unwrap();
    let snapshot = ScheduleSnapshot {
        group_name: "ИСП-25".into(),
        fetched_at: "2026-11-08T12:00:00".into(),
        days: vec![DaySchedule {
            date_label: "8 ноября".into(),
            date_iso: "2026-11-08".into(),
            week_variant: None,
            current_week_variant: None,
            lessons: vec![Lesson {
                number: 1,
                subject: "Физика".into(),
                teacher: "Иванов".into(),
                classroom: "101".into(),
            }],
        }],
    };
    misis_rasp_core::database::save_snapshot(
        &mut db,
        &misis_rasp_core::database::SnapshotInput {
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
    let groups = misis_rasp_core::group_catalog::GroupCatalog::new(vec![
        misis_rasp_core::group_catalog::GroupInfo {
            department_id: 1,
            department_code: "ИСП".into(),
            department_name: "Отделение".into(),
            group_name: "ИСП-25".into(),
            schedule_id: Some(123),
            url: "http://127.0.0.1:1/rasp/123".into(),
        },
    ]);
    let settings = misis_rasp_core::config::Settings::from_map(&Default::default()).unwrap();
    let search = misis_rasp_core::search::SearchCatalog::default();
    let context = misis_rasp_core::telegram_engine::EngineContext {
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
    let mut poll = misis_rasp_core::telegram_poll::TelegramLongPoll::new(25);
    let result = misis_rasp_core::telegram_engine::poll_iteration_with_parser(
        &mut poll,
        &api,
        &mut Default::default(),
        &mut db,
        &context,
        Some(&parser),
    )
    .await;
    assert!(matches!(
        result,
        Ok(misis_rasp_core::telegram_engine::PollOutcome::Processed(2))
    ));
    assert!(misis_rasp_core::outbox::get(&db, "telegram-update:41:0")
        .unwrap()
        .unwrap()
        .payload_json
        .contains("Физика"));
    assert_eq!(
        misis_rasp_core::database::get_user(&db, "telegram", 42)
            .unwrap()
            .unwrap()
            .subscription_key
            .as_deref(),
        Some("group:1")
    );
    server.join().unwrap();
}

#[tokio::test]
async fn failed_search_prompt_does_not_leave_input_state() {
    use std::io::{Read, Write};
    use std::time::Duration;
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for body in [
            r#"{"ok":true,"result":[{"update_id":50,"callback_query":{"id":"cb","from":{"id":42},"message":{"chat":{"id":42}},"data":"schedule:find"}}]}"#,
            r#"{"ok":true,"result":[{"update_id":51,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"ИСП-25"}}]}"#,
        ] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 8192];
            let _ = stream.read(&mut request).unwrap();
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    let parser =
        parser::ScheduleParser::new("", "", Duration::from_millis(100), 1, Duration::ZERO).unwrap();
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,created_at,last_seen_at) VALUES ('telegram',42,'group','group:1','Группа','now','now')",[]).unwrap();
    db.execute_batch("CREATE TRIGGER fail_search BEFORE INSERT ON delivery_outbox BEGIN SELECT RAISE(ABORT,'failure'); END;").unwrap();
    let settings = misis_rasp_core::config::Settings::from_map(&Default::default()).unwrap();
    let groups = misis_rasp_core::group_catalog::GroupCatalog::default();
    let search = misis_rasp_core::search::SearchCatalog::default();
    let context = misis_rasp_core::telegram_engine::EngineContext {
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
    let mut poll = misis_rasp_core::telegram_poll::TelegramLongPoll::new(25);
    let mut engine = misis_rasp_core::telegram_engine::TelegramEngine::default();
    assert!(
        misis_rasp_core::telegram_engine::poll_iteration_with_parser(
            &mut poll,
            &api,
            &mut engine,
            &mut db,
            &context,
            Some(&parser)
        )
        .await
        .is_err()
    );
    assert_eq!(poll.next_offset(), None);
    db.execute_batch("DROP TRIGGER fail_search").unwrap();
    let result = misis_rasp_core::telegram_engine::poll_iteration_with_parser(
        &mut poll,
        &api,
        &mut engine,
        &mut db,
        &context,
        Some(&parser),
    )
    .await;
    assert!(matches!(
        result,
        Ok(misis_rasp_core::telegram_engine::PollOutcome::Unsupported {
            processed: 0,
            update_id: Some(51)
        })
    ));
    server.join().unwrap();
}

#[tokio::test]
async fn group_command_checks_telegram_administrator_and_preserves_subscription_on_api_error() {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for (i,body) in [r#"{"ok":true,"result":[{"update_id":80,"message":{"chat":{"id":-100,"type":"supergroup","title":"Учебная"},"from":{"id":42},"text":"/group_clear"}}]}"#,r#"{"ok":false,"error_code":503,"description":"unavailable"}"#].into_iter().enumerate() {
            let (mut stream,_) = listener.accept().unwrap(); let mut buf=[0;8192]; let n=stream.read(&mut buf).unwrap();
            assert!(String::from_utf8_lossy(&buf[..n]).starts_with(if i==0 {"POST /bottest/getUpdates "} else {"POST /bottest/getChatMember "}));
            write!(stream,"HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
        }
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    misis_rasp_core::database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO users(platform,user_id,subscription_type,subscription_key,subscription_title,created_at,last_seen_at) VALUES ('telegram',-100,'group','group:123','ИСП-25','now','now')",[]).unwrap();
    let settings = misis_rasp_core::config::Settings::from_map(&Default::default()).unwrap();
    let groups = misis_rasp_core::group_catalog::GroupCatalog::default();
    let search = misis_rasp_core::search::SearchCatalog::default();
    let context = misis_rasp_core::telegram_engine::EngineContext {
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
    let mut poll = misis_rasp_core::telegram_poll::TelegramLongPoll::new(25);
    assert!(misis_rasp_core::telegram_engine::poll_iteration(
        &mut poll,
        &api,
        &mut Default::default(),
        &mut db,
        &context
    )
    .await
    .is_err());
    assert_eq!(poll.next_offset(), None);
    assert_eq!(
        misis_rasp_core::database::get_user(&db, "telegram", -100)
            .unwrap()
            .unwrap()
            .subscription_key
            .as_deref(),
        Some("group:123")
    );
    server.join().unwrap();
}

#[tokio::test]
async fn telegram_group_admin_check_accepts_only_administrator_or_creator() {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for status in ["member", "administrator", "creator"] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0; 4096];
            let n = stream.read(&mut buf).unwrap();
            assert!(String::from_utf8_lossy(&buf[..n]).starts_with("POST /bottest/getChatMember "));
            let body = format!(r#"{{"ok":true,"result":{{"status":"{status}"}}}}"#);
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    let base = format!("http://{addr}");
    let api = misis_rasp_core::notification::ApiTransport::new(
        &base,
        &base,
        "test".into(),
        "".into(),
        "5.199",
    )
    .unwrap();
    assert!(
        !misis_rasp_core::telegram_group::check_admin(&api, -100, 42)
            .await
            .unwrap()
    );
    assert!(misis_rasp_core::telegram_group::check_admin(&api, -100, 42)
        .await
        .unwrap());
    assert!(misis_rasp_core::telegram_group::check_admin(&api, -100, 42)
        .await
        .unwrap());
    server.join().unwrap();
}

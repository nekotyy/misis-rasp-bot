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

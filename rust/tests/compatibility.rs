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

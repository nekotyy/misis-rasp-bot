use misis_rasp_core::{
    config::Settings,
    database,
    integrations::telegram_runtime,
    notification::ApiTransport,
    telegram_engine::{PollOutcome, TelegramEngine},
    telegram_poll::TelegramLongPoll,
};
use std::io::{Read, Write};
use tokio::sync::Mutex;

#[tokio::test]
async fn network_wait_does_not_hold_database_and_event_is_saved_before_ack() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let (received_tx, received_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut buf = [0; 8192];
        let _ = stream.read(&mut buf).unwrap();
        received_tx.send(()).unwrap();
        release_rx
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
        let body = r#"{"ok":true,"result":[{"update_id":20,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/start"}}]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });
    let base = format!("http://{addr}");
    let api = ApiTransport::new(&base, &base, "fake".into(), "".into(), "5.131").unwrap();
    let mut conn = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut conn).unwrap();
    let db = Mutex::new(conn);
    let settings = Settings::from_map(&Default::default()).unwrap();
    let mut poll = TelegramLongPoll::new(25);
    let mut engine = TelegramEngine::default();
    let operation = telegram_runtime::poll_once(&mut poll, &api, &mut engine, &db, &settings, None);
    let check = async {
        received_rx.await.unwrap();
        assert!(
            db.try_lock().is_ok(),
            "SQLite was locked while waiting for Telegram"
        );
        release_tx.send(()).unwrap();
    };
    let (result, ()) = tokio::join!(operation, check);
    assert!(matches!(result, Ok(PollOutcome::Processed(1))));
    assert_eq!(poll.next_offset(), Some(21));
    let db = db.lock().await;
    assert!(misis_rasp_core::outbox::get(&db, "telegram-update:20:0")
        .unwrap()
        .is_some());
    server.join().unwrap();
}

#[tokio::test]
async fn supervisor_stops_on_unhandled_event_without_advancing_offset() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut buf = [0; 8192];
        let _ = stream.read(&mut buf).unwrap();
        let body = r#"{"ok":true,"result":[{"update_id":21,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":"/unknown"}}]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });
    let base = format!("http://{addr}");
    let api = ApiTransport::new(&base, &base, "fake".into(), "".into(), "5.131").unwrap();
    let mut conn = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut conn).unwrap();
    let db = Mutex::new(conn);
    let settings = Settings::from_map(&Default::default()).unwrap();
    let mut poll = TelegramLongPoll::new(25);
    let mut engine = TelegramEngine::default();
    let (_shutdown_tx, mut shutdown) = tokio::sync::watch::channel(false);
    let reason = telegram_runtime::run_until_shutdown(
        &mut poll,
        &api,
        &mut engine,
        &db,
        &settings,
        None,
        &mut shutdown,
    )
    .await;
    assert_eq!(
        reason,
        telegram_runtime::RuntimeStop::Unsupported {
            update_id: Some(21)
        }
    );
    assert_eq!(poll.next_offset(), None);
    server.join().unwrap();
}

#[tokio::test]
async fn supervisor_honors_shutdown_before_network_access() {
    let api = ApiTransport::new(
        "http://127.0.0.1:1",
        "http://127.0.0.1:1",
        "fake".into(),
        "".into(),
        "5.131",
    )
    .unwrap();
    let db = Mutex::new(rusqlite::Connection::open_in_memory().unwrap());
    let settings = Settings::from_map(&Default::default()).unwrap();
    let mut poll = TelegramLongPoll::new(25);
    let mut engine = TelegramEngine::default();
    let (_shutdown_tx, mut shutdown) = tokio::sync::watch::channel(true);
    let reason = telegram_runtime::run_until_shutdown(
        &mut poll,
        &api,
        &mut engine,
        &db,
        &settings,
        None,
        &mut shutdown,
    )
    .await;
    assert_eq!(reason, telegram_runtime::RuntimeStop::Shutdown);
    assert_eq!(poll.next_offset(), None);
}

#[tokio::test]
async fn supervisor_cancels_pending_long_poll_on_shutdown() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let (received_tx, received_rx) = tokio::sync::oneshot::channel();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(std::time::Duration::from_secs(5)))
            .unwrap();
        let mut buf = [0; 8192];
        let _ = stream.read(&mut buf).unwrap();
        received_tx.send(()).unwrap();
        let _ = stream.read(&mut buf);
    });
    let base = format!("http://{addr}");
    let api = ApiTransport::new(&base, &base, "fake".into(), "".into(), "5.131").unwrap();
    let db = Mutex::new(rusqlite::Connection::open_in_memory().unwrap());
    let settings = Settings::from_map(&Default::default()).unwrap();
    let mut poll = TelegramLongPoll::new(25);
    let mut engine = TelegramEngine::default();
    let (shutdown_tx, mut shutdown) = tokio::sync::watch::channel(false);
    let run = telegram_runtime::run_until_shutdown(
        &mut poll,
        &api,
        &mut engine,
        &db,
        &settings,
        None,
        &mut shutdown,
    );
    let stop = async {
        received_rx.await.unwrap();
        shutdown_tx.send(true).unwrap();
    };
    let (reason, ()) = tokio::join!(run, stop);
    assert_eq!(reason, telegram_runtime::RuntimeStop::Shutdown);
    assert_eq!(poll.next_offset(), None);
    server.join().unwrap();
}

#[tokio::test]
async fn retry_after_wait_can_be_cancelled_without_an_early_retry() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let (answered_tx, answered_rx) = tokio::sync::oneshot::channel();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut buf = [0; 8192];
        let _ = stream.read(&mut buf).unwrap();
        let body = r#"{"ok":false,"error_code":429,"description":"rate limited","parameters":{"retry_after":120}}"#;
        write!(
            stream,
            "HTTP/1.1 429 Too Many Requests\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
        answered_tx.send(()).unwrap();
        listener.set_nonblocking(true).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(300));
        assert!(listener.accept().is_err(), "retried before retry_after");
    });
    let base = format!("http://{addr}");
    let api = ApiTransport::new(&base, &base, "fake".into(), "".into(), "5.131").unwrap();
    let db = Mutex::new(rusqlite::Connection::open_in_memory().unwrap());
    let settings = Settings::from_map(&Default::default()).unwrap();
    let mut poll = TelegramLongPoll::new(25);
    let mut engine = TelegramEngine::default();
    let (shutdown_tx, mut shutdown) = tokio::sync::watch::channel(false);
    let run = telegram_runtime::run_until_shutdown(
        &mut poll,
        &api,
        &mut engine,
        &db,
        &settings,
        None,
        &mut shutdown,
    );
    let stop = async {
        answered_rx.await.unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        shutdown_tx.send(true).unwrap();
    };
    let (reason, ()) = tokio::join!(run, stop);
    assert_eq!(reason, telegram_runtime::RuntimeStop::Shutdown);
    server.join().unwrap();
}

#[tokio::test]
async fn immediately_empty_poll_does_not_spin_network_requests() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let (answered_tx, answered_rx) = tokio::sync::oneshot::channel();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut buf = [0; 8192];
        let _ = stream.read(&mut buf).unwrap();
        let body = r#"{"ok":true,"result":[]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
        answered_tx.send(()).unwrap();
        listener.set_nonblocking(true).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(180));
        assert!(
            listener.accept().is_err(),
            "immediately repeated an empty poll"
        );
    });
    let base = format!("http://{addr}");
    let api = ApiTransport::new(&base, &base, "fake".into(), "".into(), "5.131").unwrap();
    let db = Mutex::new(rusqlite::Connection::open_in_memory().unwrap());
    let settings = Settings::from_map(&Default::default()).unwrap();
    let mut poll = TelegramLongPoll::new(25);
    let mut engine = TelegramEngine::default();
    let (shutdown_tx, mut shutdown) = tokio::sync::watch::channel(false);
    let run = telegram_runtime::run_until_shutdown(
        &mut poll,
        &api,
        &mut engine,
        &db,
        &settings,
        None,
        &mut shutdown,
    );
    let stop = async {
        answered_rx.await.unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(120)).await;
        shutdown_tx.send(true).unwrap();
    };
    let (reason, ()) = tokio::join!(run, stop);
    assert_eq!(reason, telegram_runtime::RuntimeStop::Shutdown);
    server.join().unwrap();
}

#[tokio::test]
async fn conflicting_poll_receiver_stops_without_retry_or_ack() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut buf = [0; 8192];
        let _ = stream.read(&mut buf).unwrap();
        let body = r#"{"ok":false,"error_code":409,"description":"conflict"}"#;
        write!(
            stream,
            "HTTP/1.1 409 Conflict\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });
    let base = format!("http://{addr}");
    let api = ApiTransport::new(&base, &base, "fake".into(), "".into(), "5.131").unwrap();
    let db = Mutex::new(rusqlite::Connection::open_in_memory().unwrap());
    let settings = Settings::from_map(&Default::default()).unwrap();
    let mut poll = TelegramLongPoll::new(25);
    let mut engine = TelegramEngine::default();
    let (_shutdown_tx, mut shutdown) = tokio::sync::watch::channel(false);
    let reason = telegram_runtime::run_until_shutdown(
        &mut poll,
        &api,
        &mut engine,
        &db,
        &settings,
        None,
        &mut shutdown,
    )
    .await;
    assert_eq!(
        reason,
        telegram_runtime::RuntimeStop::AuthenticationOrConflict
    );
    assert_eq!(poll.next_offset(), None);
    server.join().unwrap();
}

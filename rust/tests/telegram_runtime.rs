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

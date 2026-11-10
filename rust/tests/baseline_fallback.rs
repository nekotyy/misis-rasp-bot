use chrono::NaiveDate;
use misis_rasp_core::{
    config::Settings, database, notification::ApiTransport, scheduler::Scheduler,
};
use std::{
    collections::HashMap,
    io::{Read, Write},
    sync::Arc,
};

#[test]
fn baseline_lookup_is_scoped_by_source_date_and_snapshot_type() {
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO schedule_snapshots (snapshot_type,source_key,snapshot_hash,content_json,fetched_at,created_at) VALUES ('daily_baseline','group:1','h','{}','2026-11-10','2026-11-10T00:00:01')", []).unwrap();
    db.execute("INSERT INTO schedule_snapshots (snapshot_type,source_key,snapshot_hash,content_json,fetched_at,created_at) VALUES ('current','group:2','h','{}','2026-11-10','2026-11-10T00:00:01'), ('daily_baseline','group:3','h','{}','2026-11-09','2026-11-09T23:59:59')", []).unwrap();
    let today = NaiveDate::from_ymd_opt(2026, 11, 10).unwrap();
    let keys = database::daily_baseline_source_keys(&db, today).unwrap();
    assert!(keys.contains("group:1"));
    assert!(!keys.contains("group:2"));
    assert!(!keys.contains("group:3"));
    assert!(
        !database::daily_baseline_source_keys(&db, today.succ_opt().unwrap())
            .unwrap()
            .contains("group:1")
    );
}

#[tokio::test]
async fn fallback_fetches_only_source_missing_todays_baseline() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = [0; 8192];
        let n = stream.read(&mut request).unwrap();
        assert!(String::from_utf8_lossy(&request[..n]).starts_with("GET /2 "));
        let body = r#"<div id="titleF">Группа 2</div><div class="titleDate">10 ноября</div><div class="rasp"><table><tr><th>Номер</th></tr><tr><td>1</td><td>Физика</td><td>Иванов</td><td>101</td></tr></table></div>"#;
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body).unwrap();
    });
    let base = format!("http://{addr}");
    let settings = Settings::from_map(&HashMap::from([(
        "SCHEDULE_URL".into(),
        format!("{base}/600"),
    )]))
    .unwrap();
    let api = ApiTransport::new(&base, &base, "".into(), "".into(), "5.131").unwrap();
    let scheduler = Scheduler::new(settings, Arc::new(api)).unwrap();
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    for id in [1, 2] {
        db.execute("INSERT INTO users (platform,user_id,subscription_type,subscription_key,subscription_title,schedule_id,group_name,created_at,last_seen_at) VALUES ('telegram',?,'group',?,?,?,?,'2026-11-10','2026-11-10')", rusqlite::params![id, format!("group:{id}"), format!("Группа {id}"), id, format!("Группа {id}")]).unwrap();
    }
    db.execute("INSERT INTO schedule_snapshots (snapshot_type,source_key,snapshot_hash,content_json,fetched_at,created_at) VALUES ('daily_baseline','group:1','h','{}','2026-11-10','2026-11-10T00:00:01')", []).unwrap();
    let now = NaiveDate::from_ymd_opt(2026, 11, 10)
        .unwrap()
        .and_hms_opt(5, 0, 0)
        .unwrap();
    let saved = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        scheduler.save_missing_daily_baselines(&mut db, now),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(saved, 1);
    assert!(database::daily_baseline_source_keys(&db, now.date())
        .unwrap()
        .contains("group:2"));
    let count: i64 = db.query_row("SELECT COUNT(*) FROM schedule_snapshots WHERE snapshot_type='daily_baseline' AND source_key='group:1'", [], |r| r.get(0)).unwrap();
    assert_eq!(count, 1);
    server.join().unwrap();
}

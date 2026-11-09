use misis_rasp_core::web::login_guard::{self, BLOCK_SECONDS};

#[test]
fn blocking_is_persistent_shared_between_keys_and_expires() {
    let path = tempfile::tempdir().unwrap();
    let db_path = path.path().join("auth.sqlite");
    let mut db = rusqlite::Connection::open(&db_path).unwrap();
    login_guard::ensure_schema(&db).unwrap();
    let first = vec![
        "ip:a".to_string(),
        "device:a".to_string(),
        "ip:a".to_string(),
    ];
    let second = vec!["ip:b".to_string(), "device:a".to_string()];
    login_guard::register_failure(&mut db, &first, 100).unwrap();
    login_guard::register_failure(&mut db, &first, 101).unwrap();
    assert!(!login_guard::is_blocked(&mut db, &first, 101).unwrap());
    login_guard::register_failure(&mut db, &second, 102).unwrap();
    drop(db);
    let mut reopened = rusqlite::Connection::open(&db_path).unwrap();
    assert!(login_guard::is_blocked(&mut reopened, &["device:a".into()], 103).unwrap());
    assert!(login_guard::is_blocked(&mut reopened, &["ip:b".into()], 103).unwrap());
    assert!(!login_guard::is_blocked(&mut reopened, &["ip:other".into()], 103).unwrap());
    assert!(!login_guard::is_blocked(&mut reopened, &second, 102 + BLOCK_SECONDS).unwrap());
}

#[test]
fn reset_and_expired_failures_do_not_affect_other_keys() {
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    login_guard::ensure_schema(&db).unwrap();
    let a = vec![" a ".to_string()];
    let b = vec!["b".to_string()];
    login_guard::register_failure(&mut db, &a, 1).unwrap();
    login_guard::register_failure(&mut db, &b, 1).unwrap();
    login_guard::reset(&mut db, &a).unwrap();
    let count: i64 = db
        .query_row(
            "SELECT count(*) FROM web_login_guard WHERE fingerprint_key='b'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
    login_guard::register_failure(&mut db, &a, BLOCK_SECONDS + 2).unwrap();
    let count: i64 = db
        .query_row(
            "SELECT count(*) FROM web_login_guard WHERE fingerprint_key='b'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
}

#[test]
fn login_audit_persists_bounded_fields_without_a_password() {
    let db = rusqlite::Connection::open_in_memory().unwrap();
    login_guard::ensure_schema(&db).unwrap();
    let long_login = "я".repeat(140);
    let attempt = login_guard::LoginAttempt {
        requested_login: &long_login,
        ip: " 127.0.0.1 ",
        user_agent: " test ",
        device_id_hash: "device-hash",
        fingerprint_hash: "fingerprint-hash",
        outcome: "failed",
        reason: "invalid_credentials",
    };
    login_guard::record_attempt(&db, &attempt, 123).unwrap();
    let (created, length, ip, outcome): (i64, i64, String, String) = db
        .query_row(
            "SELECT created_at,length(requested_login),ip,outcome FROM web_login_attempts",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();
    assert_eq!(
        (created, length, ip.as_str(), outcome.as_str()),
        (123, 128, "127.0.0.1", "failed")
    );
}

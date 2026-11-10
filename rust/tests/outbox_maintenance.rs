#![cfg(target_os = "linux")]

use chrono::Utc;
use misis_rasp_core::{database, storage::outbox_maintenance::cleanup_batch};
use rusqlite::Connection;
use std::{
    fs::{self, File, FileTimes},
    time::{Duration, UNIX_EPOCH},
};

fn insert(db: &Connection, id: &str, payload: &str, created: f64, lease: f64) {
    db.execute("INSERT INTO delivery_outbox (delivery_id,platform,peer_key,method,payload_json,created_at,updated_at,expires_at,next_attempt_at,lease_until) VALUES (?,'telegram','1','sendDocument',?,?,0,1,0,?)", (id, payload, created, lease)).unwrap();
}

#[test]
fn cleanup_preserves_shared_and_leased_files_and_can_resume() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = Connection::open(temp.path().join("bot.db")).unwrap();
    database::initialize(&mut db).unwrap();
    let root = temp.path().join("outbox_files");
    fs::create_dir(&root).unwrap();
    let shared = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let orphan = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    for name in [shared, orphan] {
        let path = root.join(name);
        fs::write(&path, b"attachment").unwrap();
        File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_times(FileTimes::new().set_modified(UNIX_EPOCH + Duration::from_secs(1)))
            .unwrap();
    }
    let now = Utc::now();
    let payload = serde_json::json!({"name":shared}).to_string();
    insert(&db, "old", &payload, 1.0, 0.0);
    insert(
        &db,
        "leased",
        &payload,
        1.0,
        now.timestamp() as f64 + 1000.0,
    );
    let result = cleanup_batch(&mut db, now, 1).unwrap();
    assert_eq!(result.removed_rows, 1);
    assert_eq!(result.removed_files, 1);
    assert!(root.join(shared).exists());
    assert!(!root.join(orphan).exists());
    db.execute("UPDATE delivery_outbox SET lease_until=0", [])
        .unwrap();
    let result = cleanup_batch(&mut db, now, 1).unwrap();
    assert_eq!((result.removed_rows, result.removed_files), (1, 1));
    assert!(!root.join(shared).exists());
    assert_eq!(cleanup_batch(&mut db, now, 1).unwrap().removed_rows, 0);
}

#[test]
fn invalid_payload_prevents_purging_rows() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = Connection::open(temp.path().join("bot.db")).unwrap();
    database::initialize(&mut db).unwrap();
    insert(&db, "broken", "not json", 1.0, 0.0);
    assert!(cleanup_batch(&mut db, Utc::now(), 500).is_err());
    assert_eq!(
        db.query_row("SELECT count(*) FROM delivery_outbox", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn failed_file_cleanup_can_be_retried_after_rows_were_removed() {
    use std::os::unix::fs::symlink;

    let temp = tempfile::tempdir().unwrap();
    let mut db = Connection::open(temp.path().join("bot.db")).unwrap();
    database::initialize(&mut db).unwrap();
    let name = "cccccccccccccccccccccccccccccccc";
    let actual = temp.path().join("actual");
    let root = temp.path().join("outbox_files");
    fs::create_dir(&actual).unwrap();
    let path = actual.join(name);
    fs::write(&path, b"attachment").unwrap();
    File::options()
        .write(true)
        .open(&path)
        .unwrap()
        .set_times(FileTimes::new().set_modified(UNIX_EPOCH + Duration::from_secs(1)))
        .unwrap();
    insert(
        &db,
        "old",
        &serde_json::json!({"name": name}).to_string(),
        1.0,
        0.0,
    );
    symlink(&actual, &root).unwrap();
    let now = Utc::now();
    assert!(cleanup_batch(&mut db, now, 500).is_err());
    assert!(path.exists());
    assert_eq!(
        db.query_row("SELECT count(*) FROM delivery_outbox", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    fs::remove_file(&root).unwrap();
    fs::rename(&actual, &root).unwrap();
    let result = cleanup_batch(&mut db, now, 500).unwrap();
    assert_eq!((result.removed_rows, result.removed_files), (0, 1));
    assert!(!root.join(name).exists());
}

#![cfg(target_os = "linux")]

use misis_rasp_core::{database, storage::orphan_files::cleanup_orphan_files};
use rusqlite::Connection;
use std::{
    fs::{self, File, FileTimes},
    os::unix::fs::symlink,
    path::Path,
    time::{Duration, UNIX_EPOCH},
};

fn setup() -> (tempfile::TempDir, Connection) {
    let temp = tempfile::tempdir().unwrap();
    let mut db = Connection::open(temp.path().join("bot.db")).unwrap();
    database::initialize(&mut db).unwrap();
    (temp, db)
}

fn old_file(path: &Path) {
    fs::write(path, b"temporary test data").unwrap();
    File::options()
        .write(true)
        .open(path)
        .unwrap()
        .set_times(FileTimes::new().set_modified(UNIX_EPOCH + Duration::from_secs(1)))
        .unwrap();
}

fn insert_payload(db: &Connection, payload: &str) {
    db.execute("INSERT INTO delivery_outbox (delivery_id,platform,peer_key,method,payload_json,created_at,updated_at,expires_at,next_attempt_at) VALUES ('test','telegram','1','sendDocument',?,0,0,1,0)", [payload]).unwrap();
}

#[test]
fn removes_only_old_unreferenced_regular_files_inside_outbox() {
    let (temp, mut db) = setup();
    let root = temp.path().join("outbox_files");
    fs::create_dir(&root).unwrap();
    let orphan = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let referenced = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    let recent = "cccccccccccccccccccccccccccccccc";
    let link = "dddddddddddddddddddddddddddddddd";
    old_file(&root.join(orphan));
    old_file(&root.join(referenced));
    fs::write(root.join(recent), b"new").unwrap();
    old_file(&root.join("not-an-outbox-name"));
    let outside = temp.path().join("outside.txt");
    old_file(&outside);
    symlink(&outside, root.join(link)).unwrap();
    insert_payload(
        &db,
        &serde_json::json!({"document":{"__outbox_file__":"local","name":referenced}}).to_string(),
    );
    db.execute(
        "UPDATE delivery_outbox SET status='processing', lease_until=?",
        [chrono::Utc::now().timestamp() as f64 + 120.0],
    )
    .unwrap();

    assert_eq!(
        cleanup_orphan_files(&mut db, chrono::Utc::now(), 100).unwrap(),
        1
    );
    assert!(!root.join(orphan).exists());
    assert!(root.join(referenced).exists());
    assert!(root.join(recent).exists());
    assert!(root.join("not-an-outbox-name").exists());
    assert!(fs::symlink_metadata(root.join(link))
        .unwrap()
        .file_type()
        .is_symlink());
    assert_eq!(fs::read(outside).unwrap(), b"temporary test data");
}

#[test]
fn malformed_payload_and_database_write_lock_prevent_deletion() {
    let (temp, mut db) = setup();
    let root = temp.path().join("outbox_files");
    fs::create_dir(&root).unwrap();
    let file = root.join("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    old_file(&file);
    insert_payload(&db, "invalid json");
    assert!(cleanup_orphan_files(&mut db, chrono::Utc::now(), 100).is_err());
    assert!(file.exists());
    db.execute("DELETE FROM delivery_outbox", []).unwrap();

    let mut other = database::connect_existing(&temp.path().join("bot.db")).unwrap();
    other.busy_timeout(Duration::from_millis(10)).unwrap();
    db.execute("BEGIN IMMEDIATE", []).unwrap();
    assert!(cleanup_orphan_files(&mut other, chrono::Utc::now(), 100).is_err());
    assert!(file.exists());
    db.execute("ROLLBACK", []).unwrap();
}

#[test]
fn rejects_symlinked_outbox_root_and_obeys_batch_limit() {
    let (temp, mut db) = setup();
    let external = temp.path().join("external");
    fs::create_dir(&external).unwrap();
    let name = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    old_file(&external.join(name));
    let root = temp.path().join("outbox_files");
    symlink(&external, &root).unwrap();
    assert!(cleanup_orphan_files(&mut db, chrono::Utc::now(), 10).is_err());
    assert!(external.join(name).exists());
    fs::remove_file(&root).unwrap();
    fs::create_dir(&root).unwrap();
    old_file(&root.join(name));
    old_file(&root.join("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"));
    assert_eq!(
        cleanup_orphan_files(&mut db, chrono::Utc::now(), 1).unwrap(),
        1
    );
    assert_eq!(fs::read_dir(&root).unwrap().count(), 1);
    assert_eq!(
        cleanup_orphan_files(&mut db, chrono::Utc::now(), 1).unwrap(),
        1
    );
    assert_eq!(fs::read_dir(&root).unwrap().count(), 0);
}

#[test]
fn refuses_in_memory_database_without_touching_files() {
    let mut db = Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    assert!(cleanup_orphan_files(&mut db, chrono::Utc::now(), 1).is_err());
}

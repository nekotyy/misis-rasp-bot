use misis_rasp_core::database;
use tokio::sync::Mutex;

#[tokio::test]
async fn background_connection_does_not_block_independent_web_connection() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("test.sqlite");
    let mut first = rusqlite::Connection::open(&path).unwrap();
    database::initialize(&mut first).unwrap();
    first
        .execute("CREATE TABLE isolation_probe (value INTEGER)", [])
        .unwrap();
    let web = database::connect_existing(&path).unwrap();
    let mode: String = web
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .unwrap();
    let timeout: i64 = web
        .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
        .unwrap();
    assert_eq!(mode.to_lowercase(), "wal");
    assert_eq!(timeout, 30_000);
    let background = Mutex::new(first);
    let held = background.lock().await;
    web.execute("INSERT INTO isolation_probe VALUES (42)", [])
        .unwrap();
    let value: i64 = held
        .query_row("SELECT value FROM isolation_probe", [], |r| r.get(0))
        .unwrap();
    assert_eq!(value, 42);
}

#[test]
fn opening_an_uninitialized_path_does_not_create_a_database() {
    let dir = tempfile::tempdir().unwrap();
    let missing = dir.path().join("missing.sqlite");
    assert!(database::connect_existing(&missing).is_err());
    assert!(!missing.exists());
}

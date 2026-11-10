use misis_rasp_core::database;
use rusqlite::Connection;

fn insert_logs(db: &Connection) {
    for timestamp in ["2026-01-01T00:00:00", "2026-11-19T00:00:00"] {
        db.execute("INSERT INTO delivery_events (platform,user_id,status,created_at) VALUES ('telegram',1,'sent',?)", [timestamp]).unwrap();
        db.execute("INSERT INTO change_events (snapshot_hash,message,changed_dates_json,payload_json,created_at) VALUES ('hash','change','[]','{}',?)", [timestamp]).unwrap();
        db.execute("INSERT INTO system_errors (component,error_type,message,created_at) VALUES ('test','error','detail',?)", [timestamp]).unwrap();
    }
    db.execute("INSERT INTO schedule_snapshots (snapshot_type,source_key,snapshot_hash,content_json,fetched_at,created_at) VALUES ('current','group:1','hash','{}','2026-01-01','2026-01-01T00:00:00')", []).unwrap();
}

#[test]
fn cleanup_removes_only_old_logs_and_preserves_snapshot_inside_outer_transaction() {
    let mut db = Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    insert_logs(&db);
    let tx = db.transaction().unwrap();
    assert_eq!(
        database::cleanup_database(&tx, 90, "2026-11-20T12:00:00").unwrap(),
        3
    );
    for table in [
        "delivery_events",
        "change_events",
        "system_errors",
        "schedule_snapshots",
    ] {
        let count: i64 = tx
            .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 1, "{table}");
    }
    tx.rollback().unwrap();
    let restored: i64 = db
        .query_row("SELECT COUNT(*) FROM delivery_events", [], |row| row.get(0))
        .unwrap();
    assert_eq!(restored, 2);
}

#[test]
fn cleanup_rolls_back_all_deletions_when_a_table_fails() {
    let mut db = Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    insert_logs(&db);
    db.execute_batch("CREATE TRIGGER reject_change_cleanup BEFORE DELETE ON change_events BEGIN SELECT RAISE(ABORT, 'blocked'); END;").unwrap();
    assert!(database::cleanup_database(&db, 90, "2026-11-20T12:00:00").is_err());
    let remaining: i64 = db
        .query_row("SELECT COUNT(*) FROM delivery_events", [], |row| row.get(0))
        .unwrap();
    assert_eq!(remaining, 2);
}

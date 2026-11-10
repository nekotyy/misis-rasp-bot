use misis_rasp_core::{database, outbox};
use rusqlite::Connection;

fn insert_payload(db: &Connection, id: &str, payload: &str) {
    db.execute("INSERT INTO delivery_outbox (delivery_id,platform,peer_key,method,payload_json,created_at,updated_at,expires_at,next_attempt_at) VALUES (?,'telegram','1','sendDocument',?,0,0,1,0)", [id, payload]).unwrap();
}

#[test]
fn finds_escaped_nested_names_and_conservatively_keeps_json_keys() {
    let mut db = Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    let first = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let second = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    let payload = format!(
        r#"{{"media":[{{"__outbox_file__":"local","name":"\u0061{}"}}],"{second}":true}}"#,
        &first[1..]
    );
    insert_payload(&db, "one", &payload);
    let refs = outbox::referenced_attachment_names(&db).unwrap();
    assert!(refs.contains(first));
    assert!(refs.contains(second));
    assert!(outbox::file_in_use(&db, first).unwrap());
    assert!(!outbox::file_in_use(&db, "cccccccccccccccccccccccccccccccc").unwrap());
}

#[test]
fn malformed_payload_or_unsafe_filename_prevents_deletion() {
    let mut db = Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    insert_payload(&db, "bad", "not-json");
    assert!(outbox::referenced_attachment_names(&db).is_err());
    assert!(outbox::file_in_use(&db, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").unwrap());
    assert!(outbox::file_in_use(&db, "../../secret").unwrap());
    assert!(!outbox::valid_attachment_name(
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ));
    let missing_schema = Connection::open_in_memory().unwrap();
    assert!(outbox::file_in_use(&missing_schema, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").is_err());
}

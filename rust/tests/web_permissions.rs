use misis_rasp_core::{
    database,
    web::{
        permissions::{authenticate, load_user},
        web_security::hash_password,
    },
};

#[test]
fn permissions_are_scoped_to_existing_users_and_known_actions() {
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO web_users (login,password_hash,permissions_json,created_at,updated_at) VALUES ('editor','unused',?,0,0)", [r#"["stats_users","unrecognized","manage_bot_admin",42]"#]).unwrap();
    let editor = load_user(&db, " editor ", "root").unwrap().unwrap();
    assert!(editor.can("stats_users"));
    assert!(editor.can("manage_bot_admin"));
    assert!(!editor.can("manage_web_users"));
    assert!(!editor.can("unrecognized"));
    assert!(load_user(&db, "missing", "root").unwrap().is_none());
    let root = load_user(&db, "root", "root").unwrap().unwrap();
    assert!(root.is_superuser);
    assert!(root.can("manage_web_users"));
}

#[test]
fn malformed_permissions_fail_closed() {
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    db.execute("INSERT INTO web_users (login,password_hash,permissions_json,created_at,updated_at) VALUES ('broken','unused','not json',0,0)", []).unwrap();
    let broken = load_user(&db, "broken", "root").unwrap().unwrap();
    assert!(!broken.can("manage_bot_admin"));
}

#[test]
fn superuser_name_cannot_fall_back_to_a_database_password() {
    let mut db = rusqlite::Connection::open_in_memory().unwrap();
    database::initialize(&mut db).unwrap();
    let hash = hash_password("database-password").unwrap();
    db.execute("INSERT INTO web_users (login,password_hash,permissions_json,created_at,updated_at) VALUES ('root',?,'[]',0,0)", [&hash]).unwrap();
    assert!(authenticate(
        &db,
        "root",
        "database-password",
        "root",
        "superuser-password"
    )
    .unwrap()
    .is_none());
    assert!(
        authenticate(
            &db,
            "root",
            "superuser-password",
            "root",
            "superuser-password"
        )
        .unwrap()
        .unwrap()
        .is_superuser
    );
    assert!(authenticate(&db, "root", "", "root", "").unwrap().is_none());
    assert!(
        authenticate(&db, "missing", "password", "root", "superuser-password")
            .unwrap()
            .is_none()
    );
    assert!(authenticate(
        &db,
        "root",
        "database-password",
        "other-root",
        "superuser-password"
    )
    .unwrap()
    .is_some());
}

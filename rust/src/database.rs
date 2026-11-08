use crate::UserRecord;
use rusqlite::{params, Connection, OptionalExtension};
use std::{path::Path, time::Duration};

pub fn connect(path: &Path) -> rusqlite::Result<Connection> {
    let db = Connection::open(path)?;
    db.busy_timeout(Duration::from_secs(30))?;
    Ok(db)
}
pub fn upsert_user(db: &Connection, user: &UserRecord, now: &str) -> rusqlite::Result<()> {
    db.execute("INSERT INTO users (platform,user_id,username,full_name,subscription_type,subscription_key,subscription_title,subscription_url,audience_subscription_key,audience_subscription_title,audience_subscription_url,group_name,schedule_id,is_admin,is_editor,homework_notifications_enabled,delivery_disabled_auto,created_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name,subscription_type=COALESCE(excluded.subscription_type,users.subscription_type),subscription_key=COALESCE(excluded.subscription_key,users.subscription_key),subscription_title=COALESCE(excluded.subscription_title,users.subscription_title),subscription_url=COALESCE(excluded.subscription_url,users.subscription_url),university_subgroup=CASE WHEN excluded.subscription_key IS NOT NULL AND excluded.subscription_key IS NOT users.subscription_key THEN NULL ELSE users.university_subgroup END,audience_subscription_key=COALESCE(excluded.audience_subscription_key,users.audience_subscription_key),audience_subscription_title=COALESCE(excluded.audience_subscription_title,users.audience_subscription_title),audience_subscription_url=COALESCE(excluded.audience_subscription_url,users.audience_subscription_url),group_name=COALESCE(excluded.group_name,users.group_name),schedule_id=COALESCE(excluded.schedule_id,users.schedule_id),is_admin=excluded.is_admin,is_editor=COALESCE(users.is_editor,excluded.is_editor),homework_notifications_enabled=CASE WHEN users.delivery_disabled_auto=1 THEN 1 ELSE users.homework_notifications_enabled END,delivery_disabled_auto=0,last_seen_at=excluded.last_seen_at",params![user.platform,user.user_id,user.username,user.full_name,user.subscription_type,user.subscription_key,user.subscription_title,user.subscription_url,user.audience_subscription_key,user.audience_subscription_title,user.audience_subscription_url,user.group_name,user.schedule_id,user.is_admin,user.is_editor,true,false,now,now])?;
    Ok(())
}
pub fn get_user(
    db: &Connection,
    platform: &str,
    user_id: i64,
) -> rusqlite::Result<Option<UserRecord>> {
    db.query_row("SELECT platform,user_id,username,full_name,subscription_type,subscription_key,subscription_title,subscription_url,audience_subscription_key,audience_subscription_title,audience_subscription_url,group_name,schedule_id,is_admin,is_editor,homework_notifications_enabled,delivery_disabled_auto,created_at,last_seen_at,custom_sticker_file_id,university_subgroup FROM users WHERE platform=? AND user_id=? LIMIT 1",params![platform,user_id],|r| Ok(UserRecord {
        platform:r.get(0)?, user_id:r.get(1)?, username:r.get(2)?,full_name:r.get(3)?,subscription_type:r.get(4)?,subscription_key:r.get(5)?,subscription_title:r.get(6)?,subscription_url:r.get(7)?,audience_subscription_key:r.get(8)?,audience_subscription_title:r.get(9)?,audience_subscription_url:r.get(10)?,group_name:r.get(11)?,schedule_id:r.get(12)?,is_admin:r.get(13)?,is_editor:r.get(14)?,homework_notifications_enabled:r.get(15)?,delivery_disabled_auto:r.get(16)?,created_at:r.get(17)?,last_seen_at:r.get(18)?,custom_sticker_file_id:r.get(19)?,university_subgroup:r.get(20)?,
    })).optional()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn upsert_preserves_preferences_and_restores_delivery() {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE users(platform TEXT,user_id INTEGER,username TEXT,full_name TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,audience_subscription_key TEXT,audience_subscription_title TEXT,audience_subscription_url TEXT,group_name TEXT,schedule_id INTEGER,is_admin INTEGER,is_editor INTEGER,homework_notifications_enabled INTEGER,delivery_disabled_auto INTEGER,created_at TEXT,last_seen_at TEXT,custom_sticker_file_id TEXT,university_subgroup INTEGER,PRIMARY KEY(platform,user_id));").unwrap();
        let mut user = UserRecord {
            platform: "telegram".into(),
            user_id: 1,
            username: None,
            full_name: None,
            subscription_type: Some("group".into()),
            subscription_key: Some("group:-1".into()),
            subscription_title: None,
            subscription_url: None,
            audience_subscription_key: None,
            audience_subscription_title: None,
            audience_subscription_url: None,
            group_name: None,
            schedule_id: Some(-1),
            is_admin: false,
            is_editor: true,
            homework_notifications_enabled: true,
            delivery_disabled_auto: false,
            created_at: String::new(),
            last_seen_at: String::new(),
            custom_sticker_file_id: None,
            university_subgroup: None,
        };
        upsert_user(&db, &user, "one").unwrap();
        db.execute("UPDATE users SET university_subgroup=2,delivery_disabled_auto=1,homework_notifications_enabled=0",[]).unwrap();
        user.subscription_key = None;
        user.subscription_type = None;
        user.is_editor = false;
        upsert_user(&db, &user, "two").unwrap();
        let saved = get_user(&db, "telegram", 1).unwrap().unwrap();
        assert_eq!(saved.subscription_key.as_deref(), Some("group:-1"));
        assert_eq!(saved.university_subgroup, Some(2));
        assert!(
            saved.homework_notifications_enabled
                && saved.is_editor
                && !saved.delivery_disabled_auto
        );
        user.subscription_key = Some("group:-2".into());
        upsert_user(&db, &user, "three").unwrap();
        assert_eq!(
            get_user(&db, "telegram", 1)
                .unwrap()
                .unwrap()
                .university_subgroup,
            None
        );
    }
}

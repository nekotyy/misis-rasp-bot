use crate::UserRecord;
use rusqlite::{params, Connection, OptionalExtension};
use std::{path::Path, time::Duration};

pub fn connect(path: &Path) -> rusqlite::Result<Connection> {
    let db = Connection::open(path)?;
    db.busy_timeout(Duration::from_secs(30))?;
    Ok(db)
}

/// Открывает уже инициализированную БД без создания нового файла.
pub fn connect_existing(path: &Path) -> rusqlite::Result<Connection> {
    let db = Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE)?;
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

pub fn set_subscription(
    db: &mut Connection,
    platform: &str,
    user_id: i64,
    sub: &crate::subscription::Subscription,
    now: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    set_subscription_in_transaction(&tx, platform, user_id, sub, now)?;
    tx.commit()?;
    Ok(())
}
pub fn set_subscription_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    platform: &str,
    user_id: i64,
    sub: &crate::subscription::Subscription,
    now: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    tx.execute("UPDATE users SET subscription_type=?,subscription_key=?,subscription_title=?,subscription_url=?,group_name=?,schedule_id=?,university_subgroup=CASE WHEN subscription_key IS ? THEN university_subgroup ELSE NULL END WHERE platform=? AND user_id=?",params![sub.subscription_type,sub.subscription_key,sub.subscription_title,sub.subscription_url,sub.group_name,sub.schedule_id,sub.subscription_key,platform,user_id])?;
    let account_id = crate::account::get_or_create_account(tx, platform, user_id, None, None, now)?;
    crate::account::add_subscription(
        tx,
        &account_id,
        &crate::account::AccountSubscription {
            subscription_type: &sub.subscription_type,
            key: &sub.subscription_key,
            title: Some(&sub.subscription_title),
            url: Some(&sub.subscription_url),
            group_name: sub.group_name.as_deref(),
            schedule_id: sub.schedule_id,
            subgroup: 0,
            primary: true,
            notifications: true,
        },
        now,
    )?;
    Ok(())
}

pub fn set_notifications_enabled(
    db: &mut Connection,
    platform: &str,
    user_id: i64,
    enabled: bool,
    now: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    set_notifications_enabled_in_transaction(&tx, platform, user_id, enabled, now)?;
    tx.commit()?;
    Ok(())
}
pub fn set_notifications_enabled_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    platform: &str,
    user_id: i64,
    enabled: bool,
    now: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    tx.execute("UPDATE users SET homework_notifications_enabled=?,delivery_disabled_auto=CASE WHEN ?=1 THEN 0 ELSE delivery_disabled_auto END WHERE platform=? AND user_id=?",params![enabled,enabled,platform,user_id])?;
    let account: Option<String> = tx
        .query_row(
            "SELECT account_id FROM account_identities WHERE platform=? AND user_id=?",
            params![platform, user_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(account) = account {
        crate::account::update_preferences(
            tx,
            &account,
            &crate::account::Preferences {
                notifications_enabled: Some(enabled),
                ..Default::default()
            },
            now,
        )?;
    }
    Ok(())
}
pub fn set_university_subgroup(
    db: &mut Connection,
    platform: &str,
    user_id: i64,
    schedule_id: i64,
    subgroup: Option<i64>,
    now: &str,
) -> Result<bool, Box<dyn std::error::Error>> {
    if subgroup.is_some_and(|value| !(1..=99).contains(&value)) {
        return Err("Недопустимая подгруппа".into());
    }
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let changed = tx.execute("UPDATE users SET university_subgroup=? WHERE platform=? AND user_id=? AND subscription_type='group' AND schedule_id=? AND schedule_id<0",params![subgroup,platform,user_id,schedule_id])? != 0;
    if changed {
        let primary:Option<(String,i64)> = tx.query_row("SELECT i.account_id,s.id FROM account_identities AS i JOIN account_subscriptions AS s ON s.account_id=i.account_id AND s.is_primary=1 WHERE i.platform=? AND i.user_id=? LIMIT 1",params![platform,user_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if let Some((account, id)) = primary {
            crate::account::update_subscription(
                &tx,
                &account,
                id,
                None,
                Some(subgroup.unwrap_or(0)),
                None,
                now,
            )?;
        }
    }
    tx.commit()?;
    Ok(changed)
}

pub struct SnapshotInput<'a> {
    pub snapshot_type: &'a str,
    pub hash: &'a str,
    pub snapshot: &'a crate::ScheduleSnapshot,
    pub schedule_id: Option<i64>,
    pub group_name: Option<&'a str>,
    pub source_type: Option<&'a str>,
    pub source_key: Option<&'a str>,
    pub source_title: Option<&'a str>,
    pub source_url: Option<&'a str>,
    pub archive_origin: Option<&'a str>,
    pub archive_dates: Option<&'a std::collections::HashSet<String>>,
    pub archive_projected: bool,
    pub timezone: chrono_tz::Tz,
    pub created_at: &'a str,
}
pub fn save_snapshot(
    db: &mut Connection,
    input: &SnapshotInput<'_>,
) -> Result<i64, Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let id = save_snapshot_in_transaction(&tx, input)?;
    tx.commit()?;
    Ok(id)
}
pub fn save_snapshot_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    input: &SnapshotInput<'_>,
) -> Result<i64, Box<dyn std::error::Error>> {
    let title = input
        .source_title
        .filter(|s| !s.is_empty())
        .or(input.group_name.filter(|s| !s.is_empty()))
        .unwrap_or(&input.snapshot.group_name);
    let group = input
        .group_name
        .filter(|s| !s.is_empty())
        .or(input.source_title.filter(|s| !s.is_empty()))
        .unwrap_or(&input.snapshot.group_name);
    tx.execute("INSERT INTO schedule_snapshots(snapshot_type,source_type,source_key,source_title,source_url,group_name,schedule_id,snapshot_hash,content_json,fetched_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",params![input.snapshot_type,input.source_type,input.source_key,title,input.source_url,group,input.schedule_id,input.hash,serde_json::to_string(input.snapshot)?,input.snapshot.fetched_at,input.created_at])?;
    let id = tx.last_insert_rowid();
    if let (Some(origin), Some(source_key), Some(source_type)) = (
        input.archive_origin.filter(|s| !s.is_empty()),
        input.source_key.filter(|s| !s.is_empty()),
        input.source_type.filter(|s| !s.is_empty()),
    ) {
        crate::archive::archive_accepted_snapshot(
            tx,
            &crate::archive::ProjectionInput {
                snapshot: input.snapshot,
                source_key,
                source_type,
                origin,
                timezone: input.timezone,
                snapshot_id: id,
                dates: input.archive_dates,
            },
        )?;
    }
    if input.archive_projected || input.archive_origin.is_some_and(|s| !s.is_empty()) {
        tx.execute(
            "INSERT INTO schedule_archive_projected_snapshots(snapshot_id) VALUES (?)",
            [id],
        )?;
    }
    Ok(id)
}

pub struct StoredSnapshot {
    pub source_type: Option<String>,
    pub source_key: Option<String>,
    pub source_title: Option<String>,
    pub source_url: Option<String>,
    pub group_name: Option<String>,
    pub schedule_id: Option<i64>,
    pub hash: String,
    pub content: crate::ScheduleSnapshot,
    pub fetched_at: String,
    pub created_at: String,
}
type SnapshotRow = (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<i64>,
    String,
    String,
    String,
    String,
);
pub fn latest_snapshot(
    db: &Connection,
    snapshot_type: &str,
    schedule_id: Option<i64>,
    source_key: Option<&str>,
) -> Result<Option<StoredSnapshot>, Box<dyn std::error::Error>> {
    let columns="SELECT source_type,source_key,source_title,source_url,group_name,schedule_id,snapshot_hash,content_json,fetched_at,created_at FROM schedule_snapshots WHERE snapshot_type=?";
    let (sql, scope): (String, Option<rusqlite::types::Value>) = if let Some(key) = source_key {
        (
            format!("{columns} AND source_key=? ORDER BY id DESC LIMIT 1"),
            Some(rusqlite::types::Value::Text(key.to_owned())),
        )
    } else if let Some(id) = schedule_id {
        (
            format!("{columns} AND schedule_id=? ORDER BY id DESC LIMIT 1"),
            Some(id.into()),
        )
    } else {
        (format!("{columns} ORDER BY id DESC LIMIT 1"), None)
    };
    let mut values = vec![rusqlite::types::Value::Text(snapshot_type.into())];
    if let Some(scope) = scope {
        values.push(scope);
    }
    let result: Option<SnapshotRow> = db
        .query_row(&sql, rusqlite::params_from_iter(values), |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get(3)?,
                r.get(4)?,
                r.get(5)?,
                r.get(6)?,
                r.get(7)?,
                r.get(8)?,
                r.get(9)?,
            ))
        })
        .optional()?;
    result
        .map(
            |(
                source_type,
                source_key,
                source_title,
                source_url,
                group_name,
                schedule_id,
                hash,
                content,
                fetched_at,
                created_at,
            )| {
                Ok(StoredSnapshot {
                    source_type,
                    source_key,
                    source_title,
                    source_url,
                    group_name,
                    schedule_id,
                    hash,
                    content: serde_json::from_str(&content)?,
                    fetched_at,
                    created_at,
                })
            },
        )
        .transpose()
}

pub struct ChangeInput<'a> {
    pub hash: &'a str,
    pub summary: &'a crate::format::ChangeSummary,
    pub schedule_id: Option<i64>,
    pub group_name: Option<&'a str>,
    pub source_type: Option<&'a str>,
    pub source_key: Option<&'a str>,
    pub source_title: Option<&'a str>,
    pub source_url: Option<&'a str>,
    pub created_at: &'a str,
}
pub fn record_change(
    db: &Connection,
    input: &ChangeInput<'_>,
) -> Result<bool, Box<dyn std::error::Error>> {
    let title = input
        .source_title
        .filter(|s| !s.is_empty())
        .or(input.group_name);
    Ok(db.execute("INSERT OR IGNORE INTO change_events(source_type,source_key,source_title,source_url,group_name,schedule_id,snapshot_hash,message,changed_dates_json,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",params![input.source_type,input.source_key,title,input.source_url,input.group_name,input.schedule_id,input.hash,input.summary.message,serde_json::to_string(&input.summary.changed_dates)?,serde_json::to_string(&input.summary.payload)?,input.created_at])? != 0)
}

fn user_from_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<UserRecord> {
    Ok(UserRecord {
        platform: r.get(0)?,
        user_id: r.get(1)?,
        username: r.get(2)?,
        full_name: r.get(3)?,
        subscription_type: r.get(4)?,
        subscription_key: r.get(5)?,
        subscription_title: r.get(6)?,
        subscription_url: r.get(7)?,
        audience_subscription_key: r.get(8)?,
        audience_subscription_title: r.get(9)?,
        audience_subscription_url: r.get(10)?,
        group_name: r.get(11)?,
        schedule_id: r.get(12)?,
        is_admin: r.get(13)?,
        is_editor: r.get(14)?,
        homework_notifications_enabled: r.get(15)?,
        delivery_disabled_auto: r.get(16)?,
        created_at: r.get(17)?,
        last_seen_at: r.get(18)?,
        custom_sticker_file_id: r.get(19)?,
        university_subgroup: r.get(20)?,
    })
}
pub fn users_for_notifications(
    db: &Connection,
    platform: &str,
    schedule_id: Option<i64>,
    subscription_key: Option<&str>,
    audience: &str,
) -> rusqlite::Result<Vec<UserRecord>> {
    use rusqlite::types::Value;
    let mut primary=String::from("SELECT u.platform,u.user_id,u.username,u.full_name,u.subscription_type,u.subscription_key,u.subscription_title,u.subscription_url,u.audience_subscription_key,u.audience_subscription_title,u.audience_subscription_url,u.group_name,u.schedule_id,u.is_admin,u.is_editor,u.homework_notifications_enabled,u.delivery_disabled_auto,u.created_at,u.last_seen_at,u.custom_sticker_file_id,u.university_subgroup FROM users u WHERE u.platform=? AND u.homework_notifications_enabled=1 AND u.delivery_disabled_auto=0");
    let mut extra=String::from("SELECT u.platform,u.user_id,u.username,u.full_name,s.subscription_type,s.subscription_key,s.subscription_title,s.subscription_url,u.audience_subscription_key,u.audience_subscription_title,u.audience_subscription_url,s.group_name,s.schedule_id,u.is_admin,u.is_editor,s.notifications_enabled,u.delivery_disabled_auto,u.created_at,u.last_seen_at,u.custom_sticker_file_id,s.university_subgroup FROM account_subscriptions s JOIN account_identities ai ON ai.account_id=s.account_id JOIN accounts a ON a.id=s.account_id JOIN users u ON u.platform=ai.platform AND u.user_id=ai.user_id WHERE ai.platform=? AND s.is_primary=0 AND s.notifications_enabled=1 AND a.notifications_enabled=1 AND u.delivery_disabled_auto=0");
    let mut values = vec![Value::Text(platform.into())];
    if let Some(id) = schedule_id {
        primary.push_str(" AND u.schedule_id=?");
        extra.push_str(" AND s.schedule_id=?");
        values.push(Value::Integer(id));
    }
    if let Some(key) = subscription_key {
        primary.push_str(" AND u.subscription_key=?");
        extra.push_str(" AND s.subscription_key=?");
        values.push(Value::Text(key.into()));
    }
    if audience == "teachers" {
        primary.push_str(" AND u.subscription_type='teacher'");
        extra.push_str(" AND s.subscription_type='teacher'");
    }
    if audience == "students" {
        primary.push_str(" AND COALESCE(u.subscription_type,'group')!='teacher'");
        extra.push_str(" AND COALESCE(s.subscription_type,'group')!='teacher'");
    }
    primary.push_str(" ORDER BY u.platform,u.created_at");
    let mut stmt = db.prepare(&primary)?;
    let mut result = stmt
        .query_map(rusqlite::params_from_iter(values.iter()), user_from_row)?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut seen: std::collections::HashSet<i64> = result.iter().map(|user| user.user_id).collect();
    let mut stmt = db.prepare(&extra)?;
    for user in stmt.query_map(rusqlite::params_from_iter(values.iter()), user_from_row)? {
        let user = user?;
        if seen.insert(user.user_id) {
            result.push(user);
        }
    }
    Ok(result)
}
pub fn university_notification_subgroups(
    db: &Connection,
    key: &str,
) -> rusqlite::Result<Vec<Option<i64>>> {
    let mut stmt=db.prepare("SELECT DISTINCT university_subgroup FROM users WHERE subscription_key=? AND subscription_type='group' AND schedule_id<0 AND homework_notifications_enabled=1 ORDER BY university_subgroup")?;
    let rows = stmt.query_map([key], |r| r.get(0))?;
    rows.collect()
}

pub fn latest_group_snapshots(
    db: &Connection,
    snapshot_type: &str,
) -> Result<Vec<StoredSnapshot>, Box<dyn std::error::Error>> {
    let mut stmt=db.prepare("SELECT s.source_type,s.source_key,s.source_title,s.source_url,s.group_name,s.schedule_id,s.snapshot_hash,s.content_json,s.fetched_at,s.created_at FROM schedule_snapshots AS s INNER JOIN (SELECT source_key,MAX(id) AS max_id FROM schedule_snapshots WHERE snapshot_type=? AND source_type='group' AND source_key IS NOT NULL GROUP BY source_key) AS latest ON latest.max_id=s.id ORDER BY s.source_key")?;
    let rows = stmt.query_map([snapshot_type], |r| {
        Ok((
            r.get::<_, Option<String>>(0)?,
            r.get::<_, Option<String>>(1)?,
            r.get::<_, Option<String>>(2)?,
            r.get::<_, Option<String>>(3)?,
            r.get::<_, Option<String>>(4)?,
            r.get::<_, Option<i64>>(5)?,
            r.get::<_, String>(6)?,
            r.get::<_, String>(7)?,
            r.get::<_, String>(8)?,
            r.get::<_, String>(9)?,
        ))
    })?;
    rows.map(|row| {
        let (
            source_type,
            source_key,
            source_title,
            source_url,
            group_name,
            schedule_id,
            hash,
            content,
            fetched_at,
            created_at,
        ) = row?;
        Ok(StoredSnapshot {
            source_type,
            source_key,
            source_title,
            source_url,
            group_name,
            schedule_id,
            hash,
            content: serde_json::from_str(&content)?,
            fetched_at,
            created_at,
        })
    })
    .collect()
}

pub fn set_audience_subscription(
    db: &mut Connection,
    platform: &str,
    user_id: i64,
    key: &str,
    title: &str,
    url: &str,
    now: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    set_audience_subscription_in_transaction(&tx, platform, user_id, key, title, url, now)?;
    tx.commit()?;
    Ok(())
}
pub fn set_audience_subscription_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    platform: &str,
    user_id: i64,
    key: &str,
    title: &str,
    url: &str,
    now: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    tx.execute("UPDATE users SET audience_subscription_key=?,audience_subscription_title=?,audience_subscription_url=? WHERE platform=? AND user_id=?",params![key,title,url,platform,user_id])?;
    let account = crate::account::get_or_create_account(tx, platform, user_id, None, None, now)?;
    crate::account::add_subscription(
        tx,
        &account,
        &crate::account::AccountSubscription {
            subscription_type: "audience",
            key,
            title: Some(title),
            url: Some(url),
            group_name: Some(title),
            schedule_id: None,
            subgroup: 0,
            primary: false,
            notifications: true,
        },
        now,
    )?;
    Ok(())
}
pub fn clear_audience_subscription(
    db: &mut Connection,
    platform: &str,
    user_id: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    clear_audience_in_transaction(&tx, platform, user_id)?;
    tx.commit()?;
    Ok(())
}
pub fn clear_audience_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    platform: &str,
    user_id: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    tx.execute("UPDATE users SET audience_subscription_key=NULL,audience_subscription_title=NULL,audience_subscription_url=NULL WHERE platform=? AND user_id=?",params![platform,user_id])?;
    let account: Option<String> = tx
        .query_row(
            "SELECT account_id FROM account_identities WHERE platform=? AND user_id=?",
            params![platform, user_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(account) = account {
        let ids = {
            let mut stmt=tx.prepare("SELECT id FROM account_subscriptions WHERE account_id=? AND subscription_type='audience'")?;
            let rows = stmt.query_map([&account], |r| r.get::<_, i64>(0))?;
            rows.collect::<rusqlite::Result<Vec<_>>>()?
        };
        for id in ids {
            crate::account::remove_subscription(tx, &account, id)?;
        }
    }
    Ok(())
}

pub fn clear_user_subscription(
    db: &mut Connection,
    platform: &str,
    user_id: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    clear_user_subscription_in_transaction(&tx, platform, user_id)?;
    tx.commit()?;
    Ok(())
}
pub fn clear_user_subscription_in_transaction(
    tx: &rusqlite::Transaction<'_>,
    platform: &str,
    user_id: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    tx.execute("UPDATE users SET subscription_type=NULL,subscription_key=NULL,subscription_title=NULL,subscription_url=NULL,audience_subscription_key=NULL,audience_subscription_title=NULL,audience_subscription_url=NULL,group_name=NULL,schedule_id=NULL,university_subgroup=NULL WHERE platform=? AND user_id=?",params![platform,user_id])?;
    let account: Option<String> = tx
        .query_row(
            "SELECT account_id FROM account_identities WHERE platform=? AND user_id=?",
            params![platform, user_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(account) = account {
        let ids = {
            let mut stmt = tx.prepare(
                "SELECT id FROM account_subscriptions WHERE account_id=? AND is_primary=1",
            )?;
            let rows = stmt.query_map([&account], |r| r.get::<_, i64>(0))?;
            rows.collect::<rusqlite::Result<Vec<_>>>()?
        };
        for id in ids {
            crate::account::remove_subscription(tx, &account, id)?;
        }
    }
    Ok(())
}

pub const SCHEMA_HEAD: &str = "20261105_0007";
pub fn initialize(db: &mut Connection) -> Result<(), Box<dyn std::error::Error>> {
    db.busy_timeout(Duration::from_secs(30))?;
    let tables: i64 = db.query_row(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
        [],
        |r| r.get(0),
    )?;
    if tables == 0 {
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute_batch(include_str!("../../schema.sql"))?;
        tx.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            [SCHEMA_HEAD],
        )?;
        crate::telegram_engine::ensure_schema(&tx)?;
        tx.commit()?;
    } else {
        let version: Option<String> = db
            .query_row("SELECT version_num FROM alembic_version LIMIT 1", [], |r| {
                r.get(0)
            })
            .optional()
            .map_err(|_| "Существующая база не содержит совместимой версии миграций")?;
        if version.as_deref() != Some(SCHEMA_HEAD) {
            return Err("Версия существующей базы требует миграции; данные не изменены".into());
        }
        verify_schema(db)?;
        crate::telegram_engine::ensure_schema(db)?;
    }
    db.execute_batch("PRAGMA journal_mode=WAL;")?;
    Ok(())
}

fn verify_schema(db: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    let expected = Connection::open_in_memory()?;
    expected.execute_batch(include_str!("../../schema.sql"))?;
    let mut tables=expected.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")?;
    let names = tables
        .query_map([], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for table in names {
        let exists: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
            [&table],
            |r| r.get(0),
        )?;
        if !exists {
            return Err(format!(
                "В существующей базе отсутствует таблица {table}; данные не изменены"
            )
            .into());
        }
        let mut expected_columns = expected.prepare("SELECT name FROM pragma_table_info(?)")?;
        let expected_columns = expected_columns
            .query_map([&table], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut actual_columns = db.prepare("SELECT name FROM pragma_table_info(?)")?;
        let actual_columns = actual_columns
            .query_map([&table], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<std::collections::HashSet<_>>>()?;
        if let Some(missing) = expected_columns
            .iter()
            .find(|column| !actual_columns.contains(*column))
        {
            return Err(format!(
                "В таблице {table} отсутствует поле {missing}; данные не изменены"
            )
            .into());
        }
    }
    let mut unique=expected.prepare("SELECT name FROM sqlite_master WHERE type='index' AND upper(sql) LIKE 'CREATE UNIQUE INDEX%'")?;
    let unique = unique
        .query_map([], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for index in unique {
        let exists: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name=?)",
            [&index],
            |r| r.get(0),
        )?;
        if !exists {
            return Err(format!(
                "В существующей базе отсутствует индекс {index}; данные не изменены"
            )
            .into());
        }
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct FeedbackMessage {
    pub id: i64,
    pub created_at: String,
    pub platform: String,
    pub user_id: i64,
    pub user_name: Option<String>,
    pub message_type: String,
    pub message_text: String,
    pub status: String,
    pub reply_text: Option<String>,
    pub replied_at: Option<String>,
}

pub fn create_feedback_message(
    tx: &rusqlite::Transaction<'_>,
    platform: &str,
    user_id: i64,
    user_name: Option<&str>,
    message_type: &str,
    message_text: &str,
    now: &str,
) -> rusqlite::Result<i64> {
    tx.execute(
        "INSERT INTO feedback_messages (created_at, platform, user_id, user_name, message_type, message_text, status) VALUES (?, ?, ?, ?, ?, ?, 'open')",
        rusqlite::params![now, platform, user_id, user_name, message_type, message_text],
    )?;
    Ok(tx.last_insert_rowid())
}

pub fn get_feedback_message(
    conn: &Connection,
    feedback_id: i64,
) -> rusqlite::Result<Option<FeedbackMessage>> {
    conn.query_row(
        "SELECT id, created_at, platform, user_id, user_name, message_type, message_text, status, reply_text, replied_at FROM feedback_messages WHERE id = ?",
        [feedback_id],
        |row| {
            Ok(FeedbackMessage {
                id: row.get(0)?,
                created_at: row.get(1)?,
                platform: row.get(2)?,
                user_id: row.get(3)?,
                user_name: row.get(4)?,
                message_type: row.get(5)?,
                message_text: row.get(6)?,
                status: row.get(7)?,
                reply_text: row.get(8)?,
                replied_at: row.get(9)?,
            })
        },
    )
    .optional()
}

pub fn answer_feedback_message(
    tx: &rusqlite::Transaction<'_>,
    feedback_id: i64,
    reply_text: &str,
    now: &str,
) -> rusqlite::Result<bool> {
    let rows = tx.execute(
        "UPDATE feedback_messages SET status = 'answered', reply_text = ?, replied_at = ? WHERE id = ?",
        rusqlite::params![reply_text, now, feedback_id],
    )?;
    Ok(rows > 0)
}

pub fn get_latest_open_feedback(
    conn: &Connection,
    platform: &str,
    user_id: i64,
) -> rusqlite::Result<Option<FeedbackMessage>> {
    conn.query_row(
        "SELECT id, created_at, platform, user_id, user_name, message_type, message_text, status, reply_text, replied_at FROM feedback_messages WHERE platform = ? AND user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
        rusqlite::params![platform, user_id],
        |row| {
            Ok(FeedbackMessage {
                id: row.get(0)?,
                created_at: row.get(1)?,
                platform: row.get(2)?,
                user_id: row.get(3)?,
                user_name: row.get(4)?,
                message_type: row.get(5)?,
                message_text: row.get(6)?,
                status: row.get(7)?,
                reply_text: row.get(8)?,
                replied_at: row.get(9)?,
            })
        },
    )
    .optional()
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct StarDonation {
    pub id: i64,
    pub user_id: i64,
    pub username: Option<String>,
    pub full_name: Option<String>,
    pub stars: i64,
    pub charge_id: String,
    pub refunded: bool,
    pub created_at: String,
    pub refunded_at: Option<String>,
}

pub fn record_star_donation(
    tx: &rusqlite::Transaction<'_>,
    user_id: i64,
    username: Option<&str>,
    full_name: Option<&str>,
    stars: i64,
    charge_id: &str,
    now: &str,
) -> rusqlite::Result<i64> {
    tx.execute(
        "INSERT INTO star_donations (user_id, username, full_name, stars, charge_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        rusqlite::params![user_id, username, full_name, stars, charge_id, now],
    )?;
    Ok(tx.last_insert_rowid())
}

pub fn get_star_donation(conn: &Connection, query: &str) -> rusqlite::Result<Option<StarDonation>> {
    let raw = query.trim();
    if let Ok(id) = raw.parse::<i64>() {
        conn.query_row(
            "SELECT id, user_id, username, full_name, stars, charge_id, refunded, created_at, refunded_at FROM star_donations WHERE id = ? OR charge_id = ?",
            rusqlite::params![id, raw],
            |row| {
                Ok(StarDonation {
                    id: row.get(0)?,
                    user_id: row.get(1)?,
                    username: row.get(2)?,
                    full_name: row.get(3)?,
                    stars: row.get(4)?,
                    charge_id: row.get(5)?,
                    refunded: row.get::<_, i64>(6)? != 0,
                    created_at: row.get(7)?,
                    refunded_at: row.get(8)?,
                })
            },
        )
        .optional()
    } else {
        conn.query_row(
            "SELECT id, user_id, username, full_name, stars, charge_id, refunded, created_at, refunded_at FROM star_donations WHERE charge_id = ?",
            rusqlite::params![raw],
            |row| {
                Ok(StarDonation {
                    id: row.get(0)?,
                    user_id: row.get(1)?,
                    username: row.get(2)?,
                    full_name: row.get(3)?,
                    stars: row.get(4)?,
                    charge_id: row.get(5)?,
                    refunded: row.get::<_, i64>(6)? != 0,
                    created_at: row.get(7)?,
                    refunded_at: row.get(8)?,
                })
            },
        )
        .optional()
    }
}

pub fn refund_star_donation(
    tx: &rusqlite::Transaction<'_>,
    donation_id: i64,
    now: &str,
) -> rusqlite::Result<bool> {
    let rows = tx.execute(
        "UPDATE star_donations SET refunded = 1, refunded_at = ? WHERE id = ? AND refunded = 0",
        rusqlite::params![now, donation_id],
    )?;
    Ok(rows > 0)
}

#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct MiniappStatsSummary {
    pub total_unique_users: i64,
    pub total_tg_unique: i64,
    pub total_vk_unique: i64,
    pub dau_today: i64,
    pub wau_7d: i64,
    pub mau_30d: i64,
    pub total_sessions: i64,
    pub sessions_today: i64,
    pub sessions_7d: i64,
    pub sessions_30d: i64,
    pub active_online_15m: i64,
    pub page_views_schedule: i64,
    pub page_views_history: i64,
    pub page_views_profile: i64,
}

pub fn collect_miniapp_stats(
    conn: &Connection,
    now: chrono::NaiveDateTime,
) -> rusqlite::Result<MiniappStatsSummary> {
    let today_date = now.date();
    let today_start = today_date
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let seven_days_ago = (now - chrono::Duration::days(7))
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let thirty_days_ago = (now - chrono::Duration::days(30))
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let fifteen_mins_ago = (now - chrono::Duration::minutes(15))
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string();

    let mut summary = MiniappStatsSummary::default();

    let users_query = "
        WITH all_users AS (
            SELECT platform, user_id, start_time AS ts FROM miniapp_telemetry_sessions
            UNION
            SELECT platform, user_id, created_at AS ts FROM miniapp_sessions
            UNION
            SELECT platform, user_id, created_at AS ts FROM account_identities
        )
        SELECT
            COUNT(DISTINCT platform || ':' || user_id) AS total_unique,
            COUNT(DISTINCT CASE WHEN platform = 'telegram' THEN user_id END) AS tg_unique,
            COUNT(DISTINCT CASE WHEN platform = 'vk' THEN user_id END) AS vk_unique,
            COUNT(DISTINCT CASE WHEN ts >= ? THEN platform || ':' || user_id END) AS dau,
            COUNT(DISTINCT CASE WHEN ts >= ? THEN platform || ':' || user_id END) AS wau,
            COUNT(DISTINCT CASE WHEN ts >= ? THEN platform || ':' || user_id END) AS mau
        FROM all_users
    ";
    let _ = conn.query_row(
        users_query,
        rusqlite::params![today_start, seven_days_ago, thirty_days_ago],
        |row| {
            summary.total_unique_users = row.get(0).unwrap_or(0);
            summary.total_tg_unique = row.get(1).unwrap_or(0);
            summary.total_vk_unique = row.get(2).unwrap_or(0);
            summary.dau_today = row.get(3).unwrap_or(0);
            summary.wau_7d = row.get(4).unwrap_or(0);
            summary.mau_30d = row.get(5).unwrap_or(0);
            Ok(())
        },
    );

    let sess_query = "
        WITH combined_sessions AS (
            SELECT id, start_time AS ts, last_ping_time FROM miniapp_telemetry_sessions
            UNION ALL
            SELECT token_hash AS id, created_at AS ts, created_at AS last_ping_time
            FROM miniapp_sessions
            WHERE token_hash NOT IN (SELECT id FROM miniapp_telemetry_sessions)
        )
        SELECT
            COUNT(*) AS total_sessions,
            COUNT(CASE WHEN ts >= ? THEN 1 END) AS sessions_today,
            COUNT(CASE WHEN ts >= ? THEN 1 END) AS sessions_7d,
            COUNT(CASE WHEN ts >= ? THEN 1 END) AS sessions_30d,
            COUNT(CASE WHEN last_ping_time >= ? THEN 1 END) AS active_online_15m
        FROM combined_sessions
    ";
    let _ = conn.query_row(
        sess_query,
        rusqlite::params![
            today_start,
            seven_days_ago,
            thirty_days_ago,
            fifteen_mins_ago
        ],
        |row| {
            summary.total_sessions = row.get(0).unwrap_or(0);
            summary.sessions_today = row.get(1).unwrap_or(0);
            summary.sessions_7d = row.get(2).unwrap_or(0);
            summary.sessions_30d = row.get(3).unwrap_or(0);
            summary.active_online_15m = row.get(4).unwrap_or(0);
            Ok(())
        },
    );

    let views_query = "
        SELECT
            COALESCE(SUM(page_views_schedule), 0),
            COALESCE(SUM(page_views_history), 0),
            COALESCE(SUM(page_views_profile), 0)
        FROM miniapp_telemetry_sessions
    ";
    let _ = conn.query_row(views_query, [], |row| {
        summary.page_views_schedule = row.get(0).unwrap_or(0);
        summary.page_views_history = row.get(1).unwrap_or(0);
        summary.page_views_profile = row.get(2).unwrap_or(0);
        Ok(())
    });

    Ok(summary)
}

#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct AdminStatusSummary {
    pub total_users: i64,
    pub telegram_users: i64,
    pub vk_users: i64,
    pub telegram_personal: i64,
    pub telegram_chats: i64,
    pub vk_personal: i64,
    pub vk_chats: i64,
    pub group_subscriptions: i64,
    pub teacher_subscriptions: i64,
    pub outbox_pending: i64,
    pub outbox_sent: i64,
    pub outbox_failed: i64,
}

pub fn list_all_users(conn: &Connection) -> rusqlite::Result<Vec<UserRecord>> {
    let mut stmt = conn.prepare(
        "SELECT platform, user_id, username, full_name, subscription_type, subscription_key,
                subscription_title, subscription_url, audience_subscription_key,
                audience_subscription_title, audience_subscription_url, group_name,
                schedule_id, is_admin, is_editor, homework_notifications_enabled,
                delivery_disabled_auto, created_at, last_seen_at, custom_sticker_file_id,
                university_subgroup
         FROM users
         ORDER BY platform, user_id ASC",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok(UserRecord {
            platform: row.get(0)?,
            user_id: row.get(1)?,
            username: row.get(2)?,
            full_name: row.get(3)?,
            subscription_type: row.get(4)?,
            subscription_key: row.get(5)?,
            subscription_title: row.get(6)?,
            subscription_url: row.get(7)?,
            audience_subscription_key: row.get(8)?,
            audience_subscription_title: row.get(9)?,
            audience_subscription_url: row.get(10)?,
            group_name: row.get(11)?,
            schedule_id: row.get(12)?,
            is_admin: row.get::<_, i64>(13)? != 0,
            is_editor: row.get::<_, i64>(14)? != 0,
            homework_notifications_enabled: row.get::<_, i64>(15)? != 0,
            delivery_disabled_auto: row.get::<_, i64>(16)? != 0,
            created_at: row.get(17)?,
            last_seen_at: row.get(18)?,
            custom_sticker_file_id: row.get(19)?,
            university_subgroup: row.get(20)?,
        })
    })?;
    rows.collect()
}

pub fn collect_admin_status(conn: &Connection) -> rusqlite::Result<AdminStatusSummary> {
    let mut summary = AdminStatusSummary::default();
    let _ = conn.query_row(
        "SELECT
            COUNT(*),
            COUNT(CASE WHEN platform = 'telegram' THEN 1 END),
            COUNT(CASE WHEN platform = 'vk' THEN 1 END),
            COUNT(CASE WHEN platform = 'telegram' AND user_id > 0 THEN 1 END),
            COUNT(CASE WHEN platform = 'telegram' AND user_id < 0 THEN 1 END),
            COUNT(CASE WHEN platform = 'vk' AND user_id < 2000000000 THEN 1 END),
            COUNT(CASE WHEN platform = 'vk' AND user_id >= 2000000000 THEN 1 END),
            COUNT(CASE WHEN subscription_type = 'group' THEN 1 END),
            COUNT(CASE WHEN subscription_type = 'teacher' THEN 1 END)
         FROM users",
        [],
        |row| {
            summary.total_users = row.get(0)?;
            summary.telegram_users = row.get(1)?;
            summary.vk_users = row.get(2)?;
            summary.telegram_personal = row.get(3)?;
            summary.telegram_chats = row.get(4)?;
            summary.vk_personal = row.get(5)?;
            summary.vk_chats = row.get(6)?;
            summary.group_subscriptions = row.get(7)?;
            summary.teacher_subscriptions = row.get(8)?;
            Ok(())
        },
    );
    let _ = conn.query_row(
        "SELECT
            COUNT(CASE WHEN status = 'pending' THEN 1 END),
            COUNT(CASE WHEN status = 'sent' THEN 1 END),
            COUNT(CASE WHEN status = 'failed' THEN 1 END)
         FROM delivery_outbox",
        [],
        |row| {
            summary.outbox_pending = row.get(0)?;
            summary.outbox_sent = row.get(1)?;
            summary.outbox_failed = row.get(2)?;
            Ok(())
        },
    );
    Ok(summary)
}

/// Удаляет только старые технические журналы; `now` передаётся в UTC.
/// Снимки расписания, очередь доставки и отметки событий очищаются отдельно.
pub fn cleanup_database(
    conn: &Connection,
    max_age_days: i64,
    now: &str,
) -> rusqlite::Result<usize> {
    let invalid = || rusqlite::Error::InvalidParameterName("invalid cleanup date or age".into());
    let age = chrono::Duration::try_days(max_age_days.max(1))
        .filter(|_| max_age_days >= 0)
        .ok_or_else(invalid)?;
    let cutoff = chrono::NaiveDateTime::parse_from_str(now, "%Y-%m-%dT%H:%M:%S")
        .map_err(|_| invalid())?
        .checked_sub_signed(age)
        .ok_or_else(invalid)?
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string();
    // SAVEPOINT работает также внутри транзакции админской команды.
    conn.execute("SAVEPOINT rust_cleanup_logs", [])?;
    let result = (|| -> rusqlite::Result<usize> {
        let mut deleted = 0;
        deleted += conn.execute(
            "DELETE FROM delivery_events WHERE datetime(created_at) < datetime(?)",
            [&cutoff],
        )?;
        deleted += conn.execute(
            "DELETE FROM change_events WHERE datetime(created_at) < datetime(?)",
            [&cutoff],
        )?;
        deleted += conn.execute(
            "DELETE FROM system_errors WHERE datetime(created_at) < datetime(?)",
            [&cutoff],
        )?;
        Ok(deleted)
    })();
    match result {
        Ok(deleted) => {
            conn.execute("RELEASE SAVEPOINT rust_cleanup_logs", [])?;
            Ok(deleted)
        }
        Err(error) => {
            conn.execute("ROLLBACK TO SAVEPOINT rust_cleanup_logs", [])?;
            conn.execute("RELEASE SAVEPOINT rust_cleanup_logs", [])?;
            Err(error)
        }
    }
}

#[derive(Debug, Clone)]
pub struct SourceRow {
    pub source_type: String,
    pub source_key: String,
    pub source_title: String,
    pub source_url: String,
    pub schedule_id: Option<i64>,
    pub group_name: Option<String>,
    pub users_count: i64,
}

/// Источники с эталоном за локальную дату; один проход по истории вместо N запросов.
pub fn daily_baseline_source_keys(
    conn: &Connection,
    date: chrono::NaiveDate,
) -> rusqlite::Result<std::collections::HashSet<String>> {
    let prefix = format!("{}%", date.format("%Y-%m-%d"));
    let mut stmt = conn.prepare("SELECT DISTINCT source_key FROM schedule_snapshots WHERE snapshot_type = 'daily_baseline' AND source_key IS NOT NULL AND created_at LIKE ?")?;
    let keys = stmt.query_map([prefix], |row| row.get(0))?.collect();
    keys
}

pub fn get_active_sources(conn: &Connection) -> rusqlite::Result<Vec<SourceRow>> {
    let mut stmt = conn.prepare(
        "SELECT source_type, source_key, source_title, source_url, schedule_id, group_name, SUM(users_count)
         FROM (
             SELECT
                 COALESCE(subscription_type, '') AS source_type,
                 COALESCE(subscription_key, '') AS source_key,
                 COALESCE(subscription_title, '') AS source_title,
                 COALESCE(subscription_url, '') AS source_url,
                 schedule_id,
                 group_name,
                 COUNT(*) AS users_count
             FROM users
             WHERE subscription_key IS NOT NULL
             GROUP BY subscription_type, subscription_key, subscription_title, subscription_url, schedule_id, group_name

             UNION ALL

             SELECT
                 'audience' AS source_type,
                 COALESCE(audience_subscription_key, '') AS source_key,
                 COALESCE(audience_subscription_title, '') AS source_title,
                 COALESCE(audience_subscription_url, '') AS source_url,
                 NULL AS schedule_id,
                 audience_subscription_title AS group_name,
                 COUNT(*) AS users_count
             FROM users
             WHERE audience_subscription_key IS NOT NULL
             GROUP BY audience_subscription_key, audience_subscription_title, audience_subscription_url

             UNION ALL

             SELECT
                 COALESCE(subscription_type, '') AS source_type,
                 COALESCE(subscription_key, '') AS source_key,
                 COALESCE(subscription_title, '') AS source_title,
                 COALESCE(subscription_url, '') AS source_url,
                 schedule_id,
                 group_name,
                 COUNT(*) AS users_count
             FROM account_subscriptions
             WHERE is_primary = 0 AND subscription_key IS NOT NULL
             GROUP BY subscription_type, subscription_key, subscription_title, subscription_url, schedule_id, group_name
         )
         GROUP BY source_type, source_key, source_title, source_url, schedule_id, group_name
         ORDER BY source_title"
    )?;

    let rows = stmt.query_map([], |row| {
        Ok(SourceRow {
            source_type: row.get(0)?,
            source_key: row.get(1)?,
            source_title: row.get(2)?,
            source_url: row.get(3)?,
            schedule_id: row.get(4)?,
            group_name: row.get(5)?,
            users_count: row.get(6)?,
        })
    })?;

    let mut result = Vec::new();
    for row in rows {
        let r = row?;
        if !r.source_key.is_empty() && !r.source_title.is_empty() {
            result.push(r);
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn upsert_preserves_preferences_and_restores_delivery() {
        let mut db = Connection::open_in_memory().unwrap();
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
        let sub = crate::subscription::group_subscription("Новая", Some(-8));
        assert!(set_subscription(&mut db, "telegram", 1, &sub, "now").is_err());
        assert_eq!(
            get_user(&db, "telegram", 1)
                .unwrap()
                .unwrap()
                .subscription_key
                .as_deref(),
            Some("group:-1")
        );
        db.execute_batch("CREATE TABLE accounts(id TEXT PRIMARY KEY,theme TEXT,accent TEXT,density TEXT,reduced_motion INTEGER,notifications_enabled INTEGER,created_at TEXT,updated_at TEXT); CREATE TABLE account_identities(account_id TEXT,platform TEXT,user_id INTEGER,username TEXT,full_name TEXT,created_at TEXT,last_seen_at TEXT,PRIMARY KEY(platform,user_id)); CREATE TABLE account_subscriptions(id INTEGER PRIMARY KEY,account_id TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,group_name TEXT,schedule_id INTEGER,university_subgroup INTEGER,is_primary INTEGER,notifications_enabled INTEGER,created_at TEXT,updated_at TEXT,UNIQUE(account_id,subscription_type,subscription_key));").unwrap();
        set_subscription(&mut db, "telegram", 1, &sub, "now").unwrap();
        assert_eq!(
            get_user(&db, "telegram", 1)
                .unwrap()
                .unwrap()
                .subscription_key
                .as_deref(),
            Some("group:-8")
        );
        let count:i64=db.query_row("SELECT count(*) FROM account_subscriptions WHERE subscription_key='group:-8' AND is_primary=1",[],|r|r.get(0)).unwrap();
        assert_eq!(count, 1);
        set_audience_subscription(&mut db, "telegram", 1, "audience:10", "101", "url", "now")
            .unwrap();
        assert_eq!(
            get_user(&db, "telegram", 1)
                .unwrap()
                .unwrap()
                .audience_subscription_key
                .as_deref(),
            Some("audience:10")
        );
        clear_audience_subscription(&mut db, "telegram", 1).unwrap();
        assert_eq!(
            get_user(&db, "telegram", 1)
                .unwrap()
                .unwrap()
                .audience_subscription_key,
            None
        );
        let rooms: i64 = db
            .query_row(
                "SELECT count(*) FROM account_subscriptions WHERE subscription_type='audience'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(rooms, 0);
        set_audience_subscription(&mut db, "telegram", 1, "audience:10", "101", "url", "now")
            .unwrap();
        crate::telegram_ui::apply_selected_subscription(&mut db, "telegram", 1, &sub, "now")
            .unwrap();
        assert!(get_user(&db, "telegram", 1)
            .unwrap()
            .unwrap()
            .audience_subscription_key
            .is_none());
        assert!(crate::telegram_ui::apply_selected_subscription(
            &mut db, "telegram", 999, &sub, "now"
        )
        .is_err());
        let missing: i64 = db
            .query_row(
                "SELECT count(*) FROM account_identities WHERE user_id=999",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(missing, 0);

        assert!(set_university_subgroup(&mut db, "telegram", 1, -8, Some(2), "then").unwrap());
        assert_eq!(
            get_user(&db, "telegram", 1)
                .unwrap()
                .unwrap()
                .university_subgroup,
            Some(2)
        );
        assert!(set_university_subgroup(&mut db, "telegram", 1, -8, Some(100), "then").is_err());
        assert_eq!(
            users_for_notifications(&db, "telegram", Some(-8), Some("group:-8"), "students")
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            university_notification_subgroups(&db, "group:-8").unwrap(),
            vec![Some(2)]
        );
        db.execute("INSERT INTO account_subscriptions(account_id,subscription_type,subscription_key,subscription_title,subscription_url,group_name,schedule_id,university_subgroup,is_primary,notifications_enabled,created_at,updated_at) SELECT account_id,'group','group:-9','Extra','url','Extra',-9,1,0,1,'now','now' FROM account_identities WHERE platform='telegram' AND user_id=1",[]).unwrap();
        let extra =
            users_for_notifications(&db, "telegram", Some(-9), Some("group:-9"), "students")
                .unwrap();
        assert_eq!(extra.len(), 1);
        assert_eq!(extra[0].university_subgroup, Some(1));
        set_notifications_enabled(&mut db, "telegram", 1, false, "then").unwrap();
        assert!(
            users_for_notifications(&db, "telegram", Some(-8), Some("group:-8"), "students")
                .unwrap()
                .is_empty()
        );
        assert!(
            !get_user(&db, "telegram", 1)
                .unwrap()
                .unwrap()
                .homework_notifications_enabled
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
        clear_user_subscription(&mut db, "telegram", 1).unwrap();
        assert_eq!(
            get_user(&db, "telegram", 1)
                .unwrap()
                .unwrap()
                .subscription_key
                .as_deref(),
            Some("group:-9")
        );
    }

    #[test]
    fn feedback_crud_roundtrip() {
        let mut db = Connection::open_in_memory().unwrap();
        initialize(&mut db).unwrap();
        let tx = db.transaction().unwrap();
        let id = create_feedback_message(
            &tx,
            "telegram",
            42,
            Some("Student (@student)"),
            "bugreport",
            "Расписание не грузится",
            "2026-11-09T12:00:00",
        )
        .unwrap();
        tx.commit().unwrap();
        assert!(id > 0);

        let fb = get_feedback_message(&db, id).unwrap().unwrap();
        assert_eq!(fb.id, id);
        assert_eq!(fb.user_id, 42);
        assert_eq!(fb.message_type, "bugreport");
        assert_eq!(fb.message_text, "Расписание не грузится");
        assert_eq!(fb.status, "open");
        assert_eq!(fb.reply_text, None);

        let latest = get_latest_open_feedback(&db, "telegram", 42)
            .unwrap()
            .unwrap();
        assert_eq!(latest.id, id);

        let tx = db.transaction().unwrap();
        assert!(answer_feedback_message(
            &tx,
            id,
            "Исправлено в новой версии",
            "2026-11-09T13:00:00"
        )
        .unwrap());
        tx.commit().unwrap();

        let updated = get_feedback_message(&db, id).unwrap().unwrap();
        assert_eq!(updated.status, "answered");
        assert_eq!(
            updated.reply_text.as_deref(),
            Some("Исправлено в новой версии")
        );
        assert_eq!(updated.replied_at.as_deref(), Some("2026-11-09T13:00:00"));

        assert!(get_latest_open_feedback(&db, "telegram", 42)
            .unwrap()
            .is_none());
    }

    #[test]
    fn star_donation_and_miniapp_stats_roundtrip() {
        let mut db = Connection::open_in_memory().unwrap();
        initialize(&mut db).unwrap();

        let tx = db.transaction().unwrap();
        let id = record_star_donation(
            &tx,
            77,
            Some("donor"),
            Some("Донатер"),
            100,
            "charge_123",
            "2026-11-09T12:00:00",
        )
        .unwrap();
        tx.commit().unwrap();
        assert!(id > 0);

        let d = get_star_donation(&db, "1").unwrap().unwrap();
        assert_eq!(d.user_id, 77);
        assert_eq!(d.stars, 100);
        assert_eq!(d.charge_id, "charge_123");
        assert!(!d.refunded);

        let d_by_charge = get_star_donation(&db, "charge_123").unwrap().unwrap();
        assert_eq!(d_by_charge.id, id);

        let tx = db.transaction().unwrap();
        assert!(refund_star_donation(&tx, id, "2026-11-09T13:00:00").unwrap());
        tx.commit().unwrap();

        let refunded = get_star_donation(&db, "charge_123").unwrap().unwrap();
        assert!(refunded.refunded);
        assert_eq!(refunded.refunded_at.as_deref(), Some("2026-11-09T13:00:00"));

        let acc_id = {
            let tx = db.transaction().unwrap();
            let acc = crate::account::get_or_create_account(
                &tx,
                "telegram",
                77,
                None,
                None,
                "2026-11-09T11:00:00",
            )
            .unwrap();
            tx.commit().unwrap();
            acc
        };
        db.execute(
            "INSERT INTO miniapp_sessions (token_hash, account_id, platform, user_id, expires_at, created_at) VALUES ('tok1', ?, 'telegram', 77, '2026-11-10T11:30:00', '2026-11-09T11:30:00')",
            [&acc_id],
        )
        .unwrap();

        let stats = collect_miniapp_stats(
            &db,
            chrono::NaiveDate::from_ymd_opt(2026, 11, 9)
                .unwrap()
                .and_hms_opt(12, 0, 0)
                .unwrap(),
        )
        .unwrap();
        assert_eq!(stats.total_unique_users, 1);
        assert_eq!(stats.total_tg_unique, 1);
        assert_eq!(stats.total_sessions, 1);
        assert_eq!(stats.sessions_today, 1);

        upsert_user(
            &db,
            &UserRecord {
                platform: "telegram".into(),
                user_id: 77,
                username: Some("user77".into()),
                full_name: Some("User 77".into()),
                subscription_type: Some("group".into()),
                subscription_key: Some("group:1".into()),
                subscription_title: Some("ИСП-25-1".into()),
                subscription_url: None,
                audience_subscription_key: None,
                audience_subscription_title: None,
                audience_subscription_url: None,
                group_name: Some("ИСП-25-1".into()),
                schedule_id: Some(1),
                is_admin: false,
                is_editor: false,
                homework_notifications_enabled: true,
                delivery_disabled_auto: false,
                created_at: "2026-11-09T11:00:00".into(),
                last_seen_at: "2026-11-09T11:00:00".into(),
                custom_sticker_file_id: None,
                university_subgroup: None,
            },
            "2026-11-09T11:00:00",
        )
        .unwrap();

        let users = list_all_users(&db).unwrap();
        assert_eq!(users.len(), 1);
        assert_eq!(users[0].user_id, 77);

        let admin_stats = collect_admin_status(&db).unwrap();
        assert_eq!(admin_stats.total_users, 1);
        assert_eq!(admin_stats.telegram_users, 1);
        assert_eq!(admin_stats.vk_users, 0);

        let cleaned = cleanup_database(&db, 7, "2026-11-20T12:00:00").unwrap();
        assert_eq!(cleaned, 0);

        let active_sources = get_active_sources(&db).unwrap();
        assert_eq!(active_sources.len(), 1);
        assert_eq!(active_sources[0].source_key, "group:1");
        assert_eq!(active_sources[0].source_type, "group");
        assert_eq!(active_sources[0].users_count, 1);
    }
}

#[cfg(test)]
mod snapshot_tests {
    use super::*;
    #[test]
    fn snapshot_and_projection_commit_together() {
        let mut db = Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE schedule_snapshots(id INTEGER PRIMARY KEY,snapshot_type TEXT,source_type TEXT,source_key TEXT,source_title TEXT,source_url TEXT,group_name TEXT,schedule_id INTEGER,snapshot_hash TEXT,content_json TEXT,fetched_at TEXT,created_at TEXT);").unwrap();
        let snapshot = crate::ScheduleSnapshot {
            group_name: "Группа".into(),
            fetched_at: "2026-11-08T12:00:00".into(),
            days: vec![],
        };
        let input = SnapshotInput {
            snapshot_type: "current",
            hash: "hash",
            snapshot: &snapshot,
            schedule_id: Some(1),
            group_name: None,
            source_type: Some("group"),
            source_key: Some("group:1"),
            source_title: None,
            source_url: None,
            archive_origin: None,
            archive_dates: None,
            archive_projected: true,
            timezone: chrono_tz::UTC,
            created_at: "now",
        };
        assert!(save_snapshot(&mut db, &input).is_err());
        let count: i64 = db
            .query_row("SELECT count(*) FROM schedule_snapshots", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 0);
        db.execute_batch(
            "CREATE TABLE schedule_archive_projected_snapshots(snapshot_id INTEGER PRIMARY KEY);",
        )
        .unwrap();
        let id = save_snapshot(&mut db, &input).unwrap();
        let marker: i64 = db
            .query_row(
                "SELECT snapshot_id FROM schedule_archive_projected_snapshots",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(id, marker);
        let stored = latest_snapshot(&db, "current", Some(999), Some("group:1"))
            .unwrap()
            .unwrap();
        assert_eq!(stored.hash, "hash");
        assert_eq!(stored.content, snapshot);
        assert_eq!(latest_group_snapshots(&db, "current").unwrap().len(), 1);

        assert!(latest_snapshot(&db, "current", Some(999), None)
            .unwrap()
            .is_none());
    }
}

#[cfg(test)]
mod change_tests {
    use super::*;
    #[test]
    fn repeated_change_is_deduplicated_per_day() {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE change_events(source_type TEXT,source_key TEXT,source_title TEXT,source_url TEXT,group_name TEXT,schedule_id INTEGER,snapshot_hash TEXT,message TEXT,changed_dates_json TEXT,payload_json TEXT,created_at TEXT); CREATE UNIQUE INDEX dedup ON change_events(source_key,snapshot_hash,substr(created_at,1,10));").unwrap();
        let summary = crate::format::ChangeSummary {
            changed_dates: vec!["дата".into()],
            message: "изменение".into(),
            payload: Default::default(),
            telegram_message: String::new(),
            vk_message: String::new(),
        };
        let input = ChangeInput {
            hash: "abc",
            summary: &summary,
            schedule_id: Some(1),
            group_name: Some("group"),
            source_type: Some("group"),
            source_key: Some("group:1"),
            source_title: None,
            source_url: None,
            created_at: "2026-11-08T12:00:00",
        };
        assert!(record_change(&db, &input).unwrap());
        assert!(!record_change(&db, &input).unwrap());
    }
}
#[cfg(test)]
mod initialization_tests {
    use super::*;
    #[test]
    fn fresh_schema_is_compatible_and_existing_rows_survive_restart() {
        let mut db = Connection::open_in_memory().unwrap();
        initialize(&mut db).unwrap();
        let version: String = db
            .query_row("SELECT version_num FROM alembic_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_HEAD);
        db.execute("INSERT INTO users(platform,user_id,created_at,last_seen_at) VALUES ('telegram',1,'now','now')",[]).unwrap();
        initialize(&mut db).unwrap();
        let count: i64 = db
            .query_row("SELECT count(*) FROM users", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
        assert!(crate::outbox::status(&db, "missing").unwrap().is_none());
    }
    #[test]
    fn missing_unique_index_is_rejected_before_marker_creation() {
        let mut db = Connection::open_in_memory().unwrap();
        initialize(&mut db).unwrap();
        db.execute_batch("DROP INDEX idx_change_events_source_key_snapshot_hash_day; DROP TABLE telegram_processed_updates;").unwrap();
        assert!(initialize(&mut db).is_err());
        let marker:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='telegram_processed_updates')",[],|r|r.get(0)).unwrap();
        assert!(!marker);
    }
    #[test]
    fn unknown_database_is_rejected_without_mutation() {
        let mut db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE users(id INTEGER PRIMARY KEY); INSERT INTO users VALUES (1);",
        )
        .unwrap();
        assert!(initialize(&mut db).is_err());
        let value: i64 = db
            .query_row("SELECT id FROM users", [], |r| r.get(0))
            .unwrap();
        assert_eq!(value, 1);
        let marker:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='telegram_processed_updates')",[],|r|r.get(0)).unwrap();
        assert!(!marker);
    }
}

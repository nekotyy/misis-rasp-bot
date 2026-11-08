use rusqlite::{params, OptionalExtension, Transaction};

pub fn get_or_create_account(
    db: &Transaction<'_>,
    platform: &str,
    user_id: i64,
    username: Option<&str>,
    full_name: Option<&str>,
    now: &str,
) -> Result<String, Box<dyn std::error::Error>> {
    let existing: Option<String> = db
        .query_row(
            "SELECT account_id FROM account_identities WHERE platform=? AND user_id=?",
            params![platform, user_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(id) = existing {
        db.execute("UPDATE account_identities SET username=COALESCE(?,username),full_name=COALESCE(?,full_name),last_seen_at=? WHERE platform=? AND user_id=?",params![username,full_name,now,platform,user_id])?;
        return Ok(id);
    }
    let mut random = [0_u8; 8];
    getrandom::getrandom(&mut random).map_err(|_| "Не удалось получить случайный идентификатор")?;
    let id = format!(
        "acc_{}",
        random
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    );
    db.execute("INSERT INTO accounts (id,theme,accent,density,reduced_motion,notifications_enabled,created_at,updated_at) VALUES (?,'system','default','comfortable',0,1,?,?)",params![id,now,now])?;
    db.execute("INSERT INTO account_identities (account_id,platform,user_id,username,full_name,created_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",params![id,platform,user_id,username,full_name,now,now])?;
    db.execute("INSERT OR IGNORE INTO account_subscriptions (account_id,subscription_type,subscription_key,subscription_title,subscription_url,group_name,schedule_id,university_subgroup,is_primary,notifications_enabled,created_at,updated_at) SELECT ?,COALESCE(subscription_type,'group'),subscription_key,subscription_title,subscription_url,group_name,schedule_id,COALESCE(university_subgroup,0),1,CASE WHEN homework_notifications_enabled THEN 1 ELSE 0 END,?,? FROM users WHERE platform=? AND user_id=? AND subscription_key IS NOT NULL AND subscription_key != ''",params![id,now,now,platform,user_id])?;
    Ok(id)
}

pub struct AccountSubscription<'a> {
    pub subscription_type: &'a str,
    pub key: &'a str,
    pub title: Option<&'a str>,
    pub url: Option<&'a str>,
    pub group_name: Option<&'a str>,
    pub schedule_id: Option<i64>,
    pub subgroup: i64,
    pub primary: bool,
    pub notifications: bool,
}
pub fn add_subscription(
    tx: &Transaction<'_>,
    account_id: &str,
    sub: &AccountSubscription<'_>,
    now: &str,
) -> rusqlite::Result<i64> {
    let mut primary = sub.primary;
    if sub.subscription_type == "group" && !primary {
        let exists: Option<i64> = tx.query_row("SELECT 1 FROM account_subscriptions WHERE account_id=? AND subscription_type='group' AND is_primary=1 LIMIT 1",[account_id],|r|r.get(0)).optional()?;
        primary = exists.is_none();
    }
    if primary {
        tx.execute("UPDATE account_subscriptions SET is_primary=0 WHERE account_id=? AND subscription_type=?",params![account_id,sub.subscription_type])?;
    }
    tx.execute("INSERT INTO account_subscriptions (account_id,subscription_type,subscription_key,subscription_title,subscription_url,group_name,schedule_id,university_subgroup,is_primary,notifications_enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,subscription_type,subscription_key) DO UPDATE SET subscription_title=excluded.subscription_title,subscription_url=excluded.subscription_url,group_name=excluded.group_name,schedule_id=excluded.schedule_id,university_subgroup=excluded.university_subgroup,is_primary=CASE WHEN excluded.is_primary=1 THEN 1 ELSE account_subscriptions.is_primary END,notifications_enabled=excluded.notifications_enabled,updated_at=excluded.updated_at",params![account_id,sub.subscription_type,sub.key,sub.title,sub.url,sub.group_name,sub.schedule_id,sub.subgroup,primary,sub.notifications,now,now])?;
    if primary {
        sync_primary(tx, account_id)?;
    }
    tx.query_row("SELECT id FROM account_subscriptions WHERE account_id=? AND subscription_type=? AND subscription_key=?",params![account_id,sub.subscription_type,sub.key],|r|r.get(0))
}
pub fn sync_primary(tx: &Transaction<'_>, account_id: &str) -> rusqlite::Result<()> {
    tx.execute("UPDATE users SET (subscription_type,subscription_key,subscription_title,subscription_url,group_name,schedule_id,university_subgroup) = (SELECT s.subscription_type,s.subscription_key,s.subscription_title,s.subscription_url,s.group_name,s.schedule_id,CASE WHEN users.subscription_key IS s.subscription_key THEN NULLIF(s.university_subgroup,0) ELSE NULL END FROM account_subscriptions AS s WHERE s.account_id=? AND s.is_primary=1 LIMIT 1) WHERE (platform,user_id) IN (SELECT platform,user_id FROM account_identities WHERE account_id=?)",params![account_id,account_id])?;
    Ok(())
}

#[derive(Default)]
pub struct Preferences<'a> {
    pub theme: Option<&'a str>,
    pub accent: Option<&'a str>,
    pub density: Option<&'a str>,
    pub reduced_motion: Option<bool>,
    pub notifications_enabled: Option<bool>,
}
pub fn update_preferences(
    tx: &Transaction<'_>,
    account_id: &str,
    prefs: &Preferences<'_>,
    now: &str,
) -> Result<bool, Box<dyn std::error::Error>> {
    if prefs
        .theme
        .is_some_and(|v| !matches!(v, "system" | "light" | "dark"))
    {
        return Err("Некорректная тема".into());
    }
    if prefs
        .density
        .is_some_and(|v| !matches!(v, "comfortable" | "compact"))
    {
        return Err("Некорректная плотность".into());
    }
    let changed = tx.execute("UPDATE accounts SET theme=COALESCE(?,theme),accent=COALESCE(?,accent),density=COALESCE(?,density),reduced_motion=COALESCE(?,reduced_motion),notifications_enabled=COALESCE(?,notifications_enabled),updated_at=? WHERE id=?",params![prefs.theme,prefs.accent,prefs.density,prefs.reduced_motion,prefs.notifications_enabled,now,account_id])?;
    if changed != 0 {
        if let Some(enabled) = prefs.notifications_enabled {
            tx.execute("UPDATE users SET homework_notifications_enabled=? WHERE (platform,user_id) IN (SELECT platform,user_id FROM account_identities WHERE account_id=?)",params![enabled,account_id])?;
        }
    }
    Ok(changed != 0)
}
pub fn update_subscription(
    tx: &Transaction<'_>,
    account_id: &str,
    id: i64,
    primary: Option<bool>,
    subgroup: Option<i64>,
    notifications: Option<bool>,
    now: &str,
) -> rusqlite::Result<bool> {
    let row: Option<(String,bool)> = tx.query_row("SELECT subscription_type,is_primary FROM account_subscriptions WHERE id=? AND account_id=?",params![id,account_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let Some((kind, was_primary)) = row else {
        return Ok(false);
    };
    if primary == Some(true) && !was_primary {
        tx.execute("UPDATE account_subscriptions SET is_primary=0 WHERE account_id=? AND subscription_type=?",params![account_id,kind])?;
    }
    tx.execute("UPDATE account_subscriptions SET is_primary=COALESCE(?,is_primary),university_subgroup=COALESCE(?,university_subgroup),notifications_enabled=COALESCE(?,notifications_enabled),updated_at=? WHERE id=? AND account_id=?",params![primary,subgroup,notifications,now,id,account_id])?;
    if primary == Some(true) || (was_primary && subgroup.is_some()) {
        sync_primary(tx, account_id)?;
    }
    Ok(true)
}
pub fn remove_subscription(
    tx: &Transaction<'_>,
    account_id: &str,
    id: i64,
) -> rusqlite::Result<bool> {
    let row: Option<(String,bool)> = tx.query_row("SELECT subscription_type,is_primary FROM account_subscriptions WHERE id=? AND account_id=?",params![id,account_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    let Some((kind, primary)) = row else {
        return Ok(false);
    };
    tx.execute(
        "DELETE FROM account_subscriptions WHERE id=? AND account_id=?",
        params![id, account_id],
    )?;
    if primary {
        let next: Option<i64> = tx.query_row("SELECT id FROM account_subscriptions WHERE account_id=? AND subscription_type=? ORDER BY id LIMIT 1",params![account_id,kind],|r|r.get(0)).optional()?;
        if let Some(next) = next {
            tx.execute(
                "UPDATE account_subscriptions SET is_primary=1 WHERE id=?",
                [next],
            )?;
        }
        sync_primary(tx, account_id)?;
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn creates_identity_and_migrates_existing_user_once() {
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE accounts(id TEXT PRIMARY KEY,theme TEXT,accent TEXT,density TEXT,reduced_motion INTEGER,notifications_enabled INTEGER,created_at TEXT,updated_at TEXT); CREATE TABLE account_identities(account_id TEXT,platform TEXT,user_id INTEGER,username TEXT,full_name TEXT,created_at TEXT,last_seen_at TEXT,PRIMARY KEY(platform,user_id)); CREATE TABLE account_subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,account_id TEXT,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,group_name TEXT,schedule_id INTEGER,university_subgroup INTEGER,is_primary INTEGER,notifications_enabled INTEGER,created_at TEXT,updated_at TEXT, UNIQUE(account_id,subscription_type,subscription_key)); CREATE TABLE users(platform TEXT,user_id INTEGER,subscription_type TEXT,subscription_key TEXT,subscription_title TEXT,subscription_url TEXT,group_name TEXT,schedule_id INTEGER,university_subgroup INTEGER,homework_notifications_enabled INTEGER); INSERT INTO users VALUES ('telegram',1,'group','group:-1','Group','url','Group',-1,2,0);").unwrap();
        let tx = db.transaction().unwrap();
        let id = get_or_create_account(&tx, "telegram", 1, None, None, "now").unwrap();
        assert!(id.starts_with("acc_") && id.len() == 20);
        assert_eq!(
            get_or_create_account(&tx, "telegram", 1, Some("name"), None, "later").unwrap(),
            id
        );
        let row:(String,i64,i64)=tx.query_row("SELECT subscription_key,university_subgroup,notifications_enabled FROM account_subscriptions",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
        assert_eq!(row, ("group:-1".into(), 2, 0));
        let count: i64 = tx
            .query_row("SELECT count(*) FROM accounts", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
        tx.execute("INSERT INTO users (platform,user_id,subscription_key,university_subgroup) VALUES ('vk',2,'group:-1',2)",[]).unwrap();
        tx.execute(
            "INSERT INTO account_identities(account_id,platform,user_id) VALUES (?,'vk',2)",
            [&id],
        )
        .unwrap();
        let sub = AccountSubscription {
            subscription_type: "group",
            key: "group:-2",
            title: Some("Другие"),
            url: Some("url"),
            group_name: Some("Другие"),
            schedule_id: Some(-2),
            subgroup: 0,
            primary: true,
            notifications: true,
        };
        add_subscription(&tx, &id, &sub, "later").unwrap();
        let (key,subgroup):(String,Option<i64>)=tx.query_row("SELECT subscription_key,university_subgroup FROM users WHERE platform='telegram' AND user_id=1",[],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();
        assert_eq!(key, "group:-2");
        assert_eq!(subgroup, None);
        let vk_key: String = tx
            .query_row(
                "SELECT subscription_key FROM users WHERE platform='vk' AND user_id=2",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(vk_key, "group:-2");
        let new_id:i64=tx.query_row("SELECT id FROM account_subscriptions WHERE account_id=? AND subscription_key='group:-2'",[&id],|r|r.get(0)).unwrap();
        assert!(update_subscription(&tx, &id, new_id, None, Some(1), None, "new").unwrap());
        let prefs = Preferences {
            theme: Some("dark"),
            notifications_enabled: Some(false),
            ..Default::default()
        };
        assert!(update_preferences(&tx, &id, &prefs, "new").unwrap());
        let flags: Vec<i64> = {
            let mut q = tx
                .prepare("SELECT homework_notifications_enabled FROM users ORDER BY platform")
                .unwrap();
            q.query_map([], |r| r.get(0))
                .unwrap()
                .collect::<Result<_, _>>()
                .unwrap()
        };
        assert_eq!(flags, vec![0, 0]);
        assert!(update_preferences(
            &tx,
            &id,
            &Preferences {
                theme: Some("invalid"),
                ..Default::default()
            },
            "new"
        )
        .is_err());
        assert!(remove_subscription(&tx, &id, new_id).unwrap());
        let current: String = tx
            .query_row(
                "SELECT subscription_key FROM users WHERE platform='telegram'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(current, "group:-1");
    }
}

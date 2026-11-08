use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::Value;

pub struct Delivery<'a> {
    pub id: &'a str,
    pub platform: &'a str,
    pub peer: &'a str,
    pub method: &'a str,
    pub payload: &'a Value,
    pub metadata: &'a Value,
    pub ttl: f64,
}
pub fn enqueue(db: &mut Connection, items: &[Delivery<'_>], now: f64) -> rusqlite::Result<()> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    for item in items {
        tx.execute("INSERT INTO delivery_outbox (delivery_id,platform,peer_key,method,payload_json,metadata_json,created_at,updated_at,expires_at,next_attempt_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(delivery_id) DO NOTHING", params![item.id,item.platform,item.peer,item.method,item.payload.to_string(),item.metadata.to_string(),now,now,now+item.ttl,now])?;
    }
    tx.commit()
}
fn expire(db: &Connection, now: f64) -> rusqlite::Result<()> {
    db.execute("UPDATE delivery_outbox SET status='expired',updated_at=?,lease_token=NULL,lease_until=0 WHERE status IN ('pending','processing') AND expires_at<=? AND lease_until<=?",params![now,now,now])?;
    Ok(())
}
pub fn reserve_due(
    db: &mut Connection,
    platform: &str,
    now: f64,
    limit: usize,
) -> rusqlite::Result<Vec<String>> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    expire(&tx, now)?;
    let ids = {
        let mut stmt = tx.prepare("SELECT o.delivery_id FROM delivery_outbox AS o WHERE o.platform=? AND o.status IN ('pending','processing') AND o.next_attempt_at<=? AND o.next_publish_at<=? AND o.lease_until<=? AND o.expires_at>? AND NOT EXISTS (SELECT 1 FROM delivery_outbox AS earlier WHERE earlier.platform=o.platform AND earlier.peer_key=o.peer_key AND earlier.id<o.id AND earlier.status IN ('pending','processing')) ORDER BY o.id LIMIT ?")?;
        let result = stmt
            .query_map(params![platform, now, now, now, now, limit as i64], |r| {
                r.get(0)
            })?
            .collect::<rusqlite::Result<Vec<String>>>()?;
        result
    };
    for id in &ids {
        tx.execute(
            "UPDATE delivery_outbox SET next_publish_at=? WHERE delivery_id=?",
            params![now + 30.0, id],
        )?;
    }
    tx.commit()?;
    Ok(ids)
}
pub fn claim(
    db: &mut Connection,
    id: &str,
    token: &str,
    now: f64,
    lease_seconds: f64,
) -> rusqlite::Result<bool> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    expire(&tx, now)?;
    let changed = tx.execute("UPDATE delivery_outbox AS o SET status='processing',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE delivery_id=? AND status IN ('pending','processing') AND next_attempt_at<=? AND lease_until<=? AND expires_at>? AND NOT EXISTS (SELECT 1 FROM delivery_outbox AS earlier WHERE earlier.platform=o.platform AND earlier.peer_key=o.peer_key AND earlier.id<o.id AND earlier.status IN ('pending','processing'))",params![token,now+lease_seconds,now,id,now,now,now])?;
    tx.commit()?;
    Ok(changed != 0)
}
pub fn finish(
    db: &Connection,
    id: &str,
    token: &str,
    now: f64,
    result: &Value,
    error: Option<&Value>,
    retry_delay: Option<f64>,
) -> rusqlite::Result<bool> {
    let status = if retry_delay.is_some() {
        "pending"
    } else if error.is_some() {
        "failed"
    } else {
        "sent"
    };
    Ok(db.execute("UPDATE delivery_outbox SET status=?,updated_at=?,next_attempt_at=?,next_publish_at=0,lease_token=NULL,lease_until=0,result_json=?,error_json=? WHERE delivery_id=? AND status='processing' AND lease_token=?",params![status,now,now+retry_delay.unwrap_or(0.0),result.to_string(),error.map(Value::to_string),id,token])? != 0)
}
pub fn status(db: &Connection, id: &str) -> rusqlite::Result<Option<String>> {
    db.query_row(
        "SELECT status FROM delivery_outbox WHERE delivery_id=?",
        [id],
        |r| r.get(0),
    )
    .optional()
}

pub fn publication_failed(db: &Connection, id: &str, now: f64) -> rusqlite::Result<()> {
    db.execute(
        "UPDATE delivery_outbox SET next_publish_at=? WHERE delivery_id=? AND status='pending'",
        params![now + 5.0, id],
    )?;
    Ok(())
}
pub fn renew(db: &Connection, id: &str, token: &str, now: f64) -> rusqlite::Result<bool> {
    Ok(db.execute("UPDATE delivery_outbox SET lease_until=? WHERE delivery_id=? AND status='processing' AND lease_token=?",params![now+120.0,id,token])? != 0)
}
pub fn release(db: &Connection, id: &str, token: &str) -> rusqlite::Result<()> {
    db.execute("UPDATE delivery_outbox SET status='pending',lease_token=NULL,lease_until=0,next_publish_at=0 WHERE delivery_id=? AND status='processing' AND lease_token=?",params![id,token])?;
    Ok(())
}
pub fn retention_cutoff(
    now: chrono::DateTime<chrono::Utc>,
) -> Option<chrono::DateTime<chrono::Utc>> {
    now.checked_sub_months(chrono::Months::new(3))
}
pub fn purge(
    db: &mut Connection,
    now: chrono::DateTime<chrono::Utc>,
    limit: usize,
) -> Result<Vec<(String, String)>, Box<dyn std::error::Error>> {
    let stamp = now.timestamp() as f64 + f64::from(now.timestamp_subsec_micros()) / 1_000_000.0;
    let cutoff = retention_cutoff(now)
        .ok_or("Дата вне допустимого диапазона")?
        .timestamp() as f64
        + f64::from(now.timestamp_subsec_micros()) / 1_000_000.0;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let rows = {
        let mut stmt = tx.prepare("SELECT delivery_id,payload_json FROM delivery_outbox WHERE created_at<? AND lease_until<=? ORDER BY id LIMIT ?")?;
        let result = stmt
            .query_map(params![cutoff, stamp, limit as i64], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        result
    };
    for (id, _) in &rows {
        tx.execute("DELETE FROM delivery_outbox WHERE delivery_id=?", [id])?;
    }
    tx.commit()?;
    Ok(rows)
}

pub const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS delivery_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT NOT NULL UNIQUE,
 platform TEXT NOT NULL, peer_key TEXT NOT NULL, method TEXT NOT NULL,
 payload_json TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
 next_attempt_at REAL NOT NULL, next_publish_at REAL NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0, result_json TEXT, error_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON delivery_outbox(platform,status,next_attempt_at,next_publish_at);
CREATE INDEX IF NOT EXISTS idx_outbox_peer ON delivery_outbox(platform,peer_key,status,id);
CREATE INDEX IF NOT EXISTS idx_outbox_retention ON delivery_outbox(created_at);
"#;
pub fn counts(db: &Connection) -> rusqlite::Result<std::collections::BTreeMap<String, i64>> {
    let mut stmt = db.prepare("SELECT status,count(*) FROM delivery_outbox GROUP BY status")?;
    let rows = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
    rows.collect()
}
pub fn file_in_use(db: &Connection, name: &str) -> rusqlite::Result<bool> {
    Ok(db
        .query_row(
            "SELECT 1 FROM delivery_outbox WHERE payload_json LIKE ? LIMIT 1",
            [format!("%\"{name}\"%")],
            |r| r.get::<_, i64>(0),
        )
        .optional()?
        .is_some())
}

#[derive(Debug)]
pub struct DeliveryRow {
    pub id: i64,
    pub delivery_id: String,
    pub platform: String,
    pub peer_key: String,
    pub method: String,
    pub payload_json: String,
    pub metadata_json: String,
    pub status: String,
    pub attempts: i64,
    pub created_at: f64,
    pub updated_at: f64,
    pub expires_at: f64,
    pub next_attempt_at: f64,
    pub next_publish_at: f64,
    pub lease_token: Option<String>,
    pub lease_until: f64,
    pub result_json: Option<String>,
    pub error_json: Option<String>,
}
pub fn get(db: &Connection, id: &str) -> rusqlite::Result<Option<DeliveryRow>> {
    db.query_row("SELECT id,delivery_id,platform,peer_key,method,payload_json,metadata_json,status,attempts,created_at,updated_at,expires_at,next_attempt_at,next_publish_at,lease_token,lease_until,result_json,error_json FROM delivery_outbox WHERE delivery_id=?",[id],|r| Ok(DeliveryRow { id:r.get(0)?,delivery_id:r.get(1)?,platform:r.get(2)?,peer_key:r.get(3)?,method:r.get(4)?,payload_json:r.get(5)?,metadata_json:r.get(6)?,status:r.get(7)?,attempts:r.get(8)?,created_at:r.get(9)?,updated_at:r.get(10)?,expires_at:r.get(11)?,next_attempt_at:r.get(12)?,next_publish_at:r.get(13)?,lease_token:r.get(14)?,lease_until:r.get(15)?,result_json:r.get(16)?,error_json:r.get(17)? })).optional()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn deduplicates_and_preserves_peer_order_and_leases() {
        let mut db = Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE delivery_outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,delivery_id TEXT UNIQUE NOT NULL,platform TEXT NOT NULL,peer_key TEXT NOT NULL,method TEXT NOT NULL,payload_json TEXT NOT NULL,metadata_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,updated_at REAL NOT NULL,expires_at REAL NOT NULL,next_attempt_at REAL NOT NULL,next_publish_at REAL NOT NULL DEFAULT 0,lease_token TEXT,lease_until REAL NOT NULL DEFAULT 0,result_json TEXT,error_json TEXT);").unwrap();
        let payload = serde_json::json!({"text":"hello"});
        let metadata = serde_json::json!({});
        let make = |id| Delivery {
            id,
            platform: "telegram",
            peer: "1",
            method: "sendMessage",
            payload: &payload,
            metadata: &metadata,
            ttl: 1000.0,
        };
        enqueue(&mut db, &[make("a"), make("b"), make("a")], 10.0).unwrap();
        assert_eq!(
            reserve_due(&mut db, "telegram", 10.0, 50).unwrap(),
            vec!["a"]
        );
        assert!(claim(&mut db, "a", "worker1", 10.0, 120.0).unwrap());
        assert!(!claim(&mut db, "a", "worker2", 10.0, 120.0).unwrap());
        assert!(!claim(&mut db, "b", "worker2", 10.0, 120.0).unwrap());
        assert!(!finish(&db, "a", "wrong", 11.0, &Value::Null, None, None).unwrap());
        assert!(finish(&db, "a", "worker1", 11.0, &Value::Null, None, None).unwrap());
        assert_eq!(status(&db, "a").unwrap().as_deref(), Some("sent"));
        assert_eq!(
            reserve_due(&mut db, "telegram", 11.0, 50).unwrap(),
            vec!["b"]
        );
    }
}

#[cfg(test)]
mod retention_tests {
    use super::*;
    use chrono::TimeZone;
    #[test]
    fn retention_uses_calendar_months() {
        let now = chrono::Utc.with_ymd_and_hms(2024, 5, 31, 12, 0, 0).unwrap();
        assert_eq!(
            retention_cutoff(now).unwrap(),
            chrono::Utc.with_ymd_and_hms(2024, 2, 29, 12, 0, 0).unwrap()
        );
    }
}

#[cfg(test)]
mod cleanup_tests {
    use super::*;
    use chrono::{TimeZone, Utc};
    #[test]
    fn purge_only_removes_old_unleased_rows() {
        let mut db = Connection::open_in_memory().unwrap();
        db.execute_batch(SCHEMA).unwrap();
        let payload = serde_json::json!({"text":"x"});
        let metadata = serde_json::json!({});
        let item = |id| Delivery {
            id,
            platform: "telegram",
            peer: id,
            method: "sendMessage",
            payload: &payload,
            metadata: &metadata,
            ttl: 100000000.0,
        };
        let old = Utc
            .with_ymd_and_hms(2024, 1, 1, 0, 0, 0)
            .unwrap()
            .timestamp() as f64;
        let now = Utc.with_ymd_and_hms(2024, 5, 31, 12, 0, 0).unwrap();
        enqueue(&mut db, &[item("old"), item("leased")], old).unwrap();
        assert!(claim(&mut db, "leased", "worker", now.timestamp() as f64, 120.0).unwrap());
        let removed = purge(&mut db, now, 500).unwrap();
        assert_eq!(removed.len(), 1);
        assert_eq!(removed[0].0, "old");
        assert_eq!(
            status(&db, "leased").unwrap().as_deref(),
            Some("processing")
        );
    }
}

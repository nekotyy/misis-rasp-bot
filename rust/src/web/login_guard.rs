//! SQLite-ограничитель попыток входа, совместимый с Python-таблицами.
//! Пока не подключён к маршрутам: Rust-админка остаётся закрытой.

use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use std::collections::HashSet;

pub const MAX_ATTEMPTS: i64 = 3;
pub const BLOCK_SECONDS: i64 = 6 * 60 * 60;

pub fn ensure_schema(db: &Connection) -> rusqlite::Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS web_login_guard (
            fingerprint_key TEXT PRIMARY KEY, fail_count INTEGER NOT NULL,
            blocked_until INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_web_login_guard_blocked_until ON web_login_guard(blocked_until);
        CREATE TABLE IF NOT EXISTS web_login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
            requested_login TEXT NOT NULL, ip TEXT NOT NULL, user_agent TEXT NOT NULL,
            device_id_hash TEXT NOT NULL, fingerprint_hash TEXT NOT NULL,
            outcome TEXT NOT NULL, reason TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_web_login_attempts_created_at ON web_login_attempts(created_at DESC);",
    )
}

fn keys(values: &[String]) -> Vec<&str> {
    let mut seen = HashSet::new();
    values
        .iter()
        .map(|v| v.trim())
        .filter(|v| !v.is_empty() && seen.insert(*v))
        .collect()
}

fn cleanup(tx: &Transaction<'_>, now: i64) -> rusqlite::Result<()> {
    tx.execute(
        "DELETE FROM web_login_guard WHERE blocked_until > 0 AND blocked_until <= ?",
        [now],
    )?;
    tx.execute(
        "DELETE FROM web_login_guard WHERE blocked_until = 0 AND updated_at <= ?",
        [now.saturating_sub(BLOCK_SECONDS)],
    )?;
    Ok(())
}

pub fn is_blocked(
    db: &mut Connection,
    fingerprints: &[String],
    now: i64,
) -> rusqlite::Result<bool> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    cleanup(&tx, now)?;
    let mut blocked = false;
    for key in keys(fingerprints) {
        blocked |= tx.query_row("SELECT EXISTS(SELECT 1 FROM web_login_guard WHERE fingerprint_key = ? AND blocked_until > ?)", params![key, now], |row| row.get::<_, bool>(0))?;
    }
    tx.commit()?;
    Ok(blocked)
}

pub fn register_failure(
    db: &mut Connection,
    fingerprints: &[String],
    now: i64,
) -> rusqlite::Result<()> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    cleanup(&tx, now)?;
    let fingerprints = keys(fingerprints);
    let mut blocked_until = 0_i64;
    for key in &fingerprints {
        let previous: Option<(i64, i64)> = tx
            .query_row(
                "SELECT fail_count, blocked_until FROM web_login_guard WHERE fingerprint_key = ?",
                [key],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let (count, previous_block) = previous.unwrap_or((0, 0));
        let count = count.saturating_add(1);
        if count >= MAX_ATTEMPTS {
            blocked_until = blocked_until
                .max(now.saturating_add(BLOCK_SECONDS))
                .max(previous_block);
        }
        tx.execute("INSERT INTO web_login_guard (fingerprint_key,fail_count,blocked_until,updated_at) VALUES (?,?,?,?) ON CONFLICT(fingerprint_key) DO UPDATE SET fail_count=excluded.fail_count,updated_at=excluded.updated_at", params![key, count, previous_block, now])?;
    }
    if blocked_until > now {
        for key in fingerprints {
            tx.execute("UPDATE web_login_guard SET blocked_until = ?, updated_at = ? WHERE fingerprint_key = ?", params![blocked_until, now, key])?;
        }
    }
    tx.commit()
}

pub fn reset(db: &mut Connection, fingerprints: &[String]) -> rusqlite::Result<()> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    for key in keys(fingerprints) {
        tx.execute(
            "DELETE FROM web_login_guard WHERE fingerprint_key = ?",
            [key],
        )?;
    }
    tx.commit()
}

pub struct LoginAttempt<'a> {
    pub requested_login: &'a str,
    pub ip: &'a str,
    pub user_agent: &'a str,
    pub device_id_hash: &'a str,
    pub fingerprint_hash: &'a str,
    pub outcome: &'a str,
    pub reason: &'a str,
}

pub fn record_attempt(
    db: &Connection,
    attempt: &LoginAttempt<'_>,
    now: i64,
) -> rusqlite::Result<()> {
    let bounded = |value: &str, limit| value.chars().take(limit).collect::<String>();
    db.execute(
        "INSERT INTO web_login_attempts (created_at,requested_login,ip,user_agent,device_id_hash,fingerprint_hash,outcome,reason) VALUES (?,?,?,?,?,?,?,?)",
        params![now, bounded(attempt.requested_login.trim(), 128), bounded(attempt.ip.trim(), 128), bounded(attempt.user_agent.trim(), 512), bounded(attempt.device_id_hash, 96), bounded(attempt.fingerprint_hash, 96), bounded(attempt.outcome, 32), bounded(attempt.reason, 64)],
    )?;
    Ok(())
}

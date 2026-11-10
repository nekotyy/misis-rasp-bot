//! Ограниченная очистка старых неиспользуемых вложений outbox.
//!
//! Пока вызывается только тестами; подключение к фоновому процессу требует
//! отдельной проверки хранения и восстановления данных.

use chrono::{DateTime, Utc};
use rusqlite::Connection;

#[cfg(target_os = "linux")]
pub fn cleanup_orphan_files(
    db: &mut Connection,
    now: DateTime<Utc>,
    max_files: usize,
) -> Result<usize, Box<dyn std::error::Error>> {
    cleanup_with_hook(db, now, max_files, || {})
}

#[cfg(target_os = "linux")]
fn cleanup_with_hook(
    db: &mut Connection,
    now: DateTime<Utc>,
    max_files: usize,
    before_final_check: impl FnOnce(),
) -> Result<usize, Box<dyn std::error::Error>> {
    use rustix::fs::{open, openat, unlinkat, AtFlags, Dir, Mode, OFlags};
    use rustix::io::Errno;
    use std::{
        fs::File,
        os::unix::fs::MetadataExt,
        time::{Duration, UNIX_EPOCH},
    };

    if max_files == 0 {
        return Ok(0);
    }
    // Путь берём у самого SQLite, чтобы случайно не сверять ссылки одной БД
    // с файлами рядом с другой БД.
    let database_path = db
        .path()
        .filter(|path| !path.is_empty())
        .ok_or("database is not a file")?;
    let parent = std::path::Path::new(database_path)
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .ok_or("database path has no parent")?;
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let parent_fd = open(parent, flags, Mode::empty())?;
    let root_fd = match openat(&parent_fd, "outbox_files", flags, Mode::empty()) {
        Ok(fd) => fd,
        Err(Errno::NOENT) => return Ok(0),
        Err(error) => return Err(Box::new(error)),
    };
    let cutoff = crate::outbox::retention_cutoff(now)
        .ok_or("invalid retention date")?
        .timestamp();
    let cutoff = UNIX_EPOCH
        .checked_add(Duration::from_secs(cutoff.try_into()?))
        .ok_or("invalid retention date")?;
    let old_regular_identity =
        |name: &str| -> Result<Option<(u64, u64)>, Box<dyn std::error::Error>> {
            let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
            let fd = match openat(&root_fd, name, flags, Mode::empty()) {
                Ok(fd) => fd,
                Err(Errno::NOENT | Errno::LOOP) => return Ok(None),
                Err(error) => return Err(Box::new(error)),
            };
            let metadata = File::from(fd).metadata()?;
            if metadata.is_file() && metadata.modified()? < cutoff {
                Ok(Some((metadata.dev(), metadata.ino())))
            } else {
                Ok(None)
            }
        };

    // Предварительный проход выполняется без блокировки писателей SQLite.
    let initial_refs = crate::outbox::referenced_attachment_names(db)?;
    let mut candidates = Vec::new();
    let mut entries = Dir::read_from(&root_fd)?;
    while let Some(entry) = entries.read() {
        let entry = entry?;
        let Ok(name) = entry.file_name().to_str() else {
            continue;
        };
        if crate::outbox::valid_attachment_name(name) && !initial_refs.contains(name) {
            let Some(identity) = old_regular_identity(name)? else {
                continue;
            };
            candidates.push((name.to_owned(), identity));
            if candidates.len() >= max_files.min(1000) {
                break;
            }
        }
    }
    if candidates.is_empty() {
        return Ok(0);
    }
    before_final_check();

    // Финальная проверка и удаление происходят при write-блокировке SQLite,
    // чтобы новая запись не появилась между проверкой ссылки и unlinkat.
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let live_refs = crate::outbox::referenced_attachment_names(&tx)?;
    let mut removed = 0;
    for (name, identity) in candidates {
        if !live_refs.contains(&name) && old_regular_identity(&name)? == Some(identity) {
            match unlinkat(&root_fd, name.as_str(), AtFlags::empty()) {
                Ok(()) => removed += 1,
                Err(Errno::NOENT) => {}
                Err(error) => return Err(Box::new(error)),
            }
        }
    }
    tx.commit()?;
    Ok(removed)
}

#[cfg(not(target_os = "linux"))]
pub fn cleanup_orphan_files(
    _db: &mut Connection,
    _now: DateTime<Utc>,
    _max_files: usize,
) -> Result<usize, Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "secure outbox cleanup is available only on Linux",
    )
    .into())
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::{
        fs::{self, File, FileTimes},
        time::{Duration, UNIX_EPOCH},
    };

    fn old_file(path: &std::path::Path, contents: &[u8]) {
        fs::write(path, contents).unwrap();
        File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_times(FileTimes::new().set_modified(UNIX_EPOCH + Duration::from_secs(1)))
            .unwrap();
    }

    #[test]
    fn replacement_after_scan_is_not_removed() {
        let temp = tempfile::tempdir().unwrap();
        let mut db = Connection::open(temp.path().join("bot.db")).unwrap();
        crate::database::initialize(&mut db).unwrap();
        let root = temp.path().join("outbox_files");
        fs::create_dir(&root).unwrap();
        let target = root.join("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        let replacement = root.join("replacement");
        old_file(&target, b"old");
        old_file(&replacement, b"new");
        let removed = cleanup_with_hook(&mut db, Utc::now(), 100, || {
            fs::rename(&replacement, &target).unwrap();
        })
        .unwrap();
        assert_eq!(removed, 0);
        assert_eq!(fs::read(target).unwrap(), b"new");
    }

    #[test]
    fn reference_added_after_scan_prevents_unlink() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("bot.db");
        let mut db = Connection::open(&db_path).unwrap();
        crate::database::initialize(&mut db).unwrap();
        let root = temp.path().join("outbox_files");
        fs::create_dir(&root).unwrap();
        let name = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let target = root.join(name);
        old_file(&target, b"data");
        let removed = cleanup_with_hook(&mut db, Utc::now(), 100, || {
            let other = Connection::open(&db_path).unwrap();
            let payload = serde_json::json!({"document":{"__outbox_file__":"local","name":name}}).to_string();
            other.execute("INSERT INTO delivery_outbox (delivery_id,platform,peer_key,method,payload_json,created_at,updated_at,expires_at,next_attempt_at) VALUES ('new','telegram','1','sendDocument',?,0,0,1,0)", [payload]).unwrap();
        }).unwrap();
        assert_eq!(removed, 0);
        assert!(target.exists());
    }
}

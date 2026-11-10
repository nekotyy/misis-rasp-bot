//! Ограниченная очистка записей и вложений очереди доставки.
//!
//! Отдельные этапы допускают повтор после прерывания: файлы удалённых записей
//! остаются сиротами и будут найдены следующим запуском.

#[cfg(target_os = "linux")]
use chrono::{DateTime, Utc};
#[cfg(target_os = "linux")]
use rusqlite::Connection;

#[derive(Debug, PartialEq, Eq)]
pub struct MaintenanceResult {
    pub removed_rows: usize,
    pub removed_files: usize,
}

/// Одна ограниченная порция; планировщик пока не вызывает эту функцию.
/// Ошибка очистки файлов может возникнуть после фиксации удаления записей:
/// вызывающий код должен повторить обслуживание, а не считать его откатом.
#[cfg(target_os = "linux")]
pub fn cleanup_batch(
    db: &mut Connection,
    now: DateTime<Utc>,
    limit: usize,
) -> Result<MaintenanceResult, Box<dyn std::error::Error>> {
    if limit == 0 {
        return Ok(MaintenanceResult {
            removed_rows: 0,
            removed_files: 0,
        });
    }
    // Повреждённая очередь должна оставаться доступной для восстановления.
    super::outbox::referenced_attachment_names(db)?;
    let limit = limit.min(500);
    let removed_rows = super::outbox::purge(db, now, limit)?.len();
    let removed_files = super::orphan_files::cleanup_orphan_files(db, now, limit)?;
    Ok(MaintenanceResult {
        removed_rows,
        removed_files,
    })
}

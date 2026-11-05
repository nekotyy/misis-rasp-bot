"""SQLite-схема будущего архива расписания миниаппа."""

# Нулевой вариант недели означает отсутствие вузовского варианта. Ссылки на
# снимки намеренно не являются внешними ключами: технические снимки очищаются
# раньше, чем должны исчезать архивные дни и их опорные версии.
# Эта константа закреплена за миграцией 20261105_0004: последующие изменения
# схемы оформляются новой миграцией, а не правкой исторической константы.
SCHEDULE_ARCHIVE_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schedule_day_archive (
    source_key TEXT NOT NULL,
    source_type TEXT NOT NULL,
    date_iso TEXT NOT NULL,
    week_variant INTEGER NOT NULL DEFAULT 0 CHECK (week_variant IN (0, 1, 2)),
    content_json TEXT NOT NULL,
    completeness TEXT NOT NULL CHECK (completeness IN ('unknown', 'complete')),
    origin TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    snapshot_id INTEGER,
    PRIMARY KEY (source_key, date_iso, week_variant)
);

CREATE INDEX IF NOT EXISTS idx_schedule_day_archive_date
ON schedule_day_archive(date_iso);

CREATE TABLE IF NOT EXISTS schedule_day_references (
    source_key TEXT NOT NULL,
    date_iso TEXT NOT NULL,
    week_variant INTEGER NOT NULL DEFAULT 0 CHECK (week_variant IN (0, 1, 2)),
    reference_kind TEXT NOT NULL CHECK (reference_kind IN ('first_seen', 'day_start')),
    content_json TEXT NOT NULL,
    completeness TEXT NOT NULL CHECK (completeness IN ('unknown', 'complete')),
    origin TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    snapshot_id INTEGER,
    PRIMARY KEY (source_key, date_iso, week_variant, reference_kind)
);
"""

"""SQLite-схема общих аккаунтов, связей и подписок миниаппа."""

ACCOUNTS_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    theme TEXT NOT NULL DEFAULT 'system' CHECK (theme IN ('system', 'light', 'dark')),
    accent TEXT NOT NULL DEFAULT 'default',
    density TEXT NOT NULL DEFAULT 'comfortable' CHECK (density IN ('comfortable', 'compact')),
    reduced_motion INTEGER NOT NULL DEFAULT 0 CHECK (reduced_motion IN (0, 1)),
    notifications_enabled INTEGER NOT NULL DEFAULT 1 CHECK (notifications_enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_identities (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('telegram', 'vk')),
    user_id INTEGER NOT NULL,
    username TEXT,
    full_name TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (platform, user_id)
);

CREATE INDEX IF NOT EXISTS idx_account_identities_account
ON account_identities(account_id);

CREATE TABLE IF NOT EXISTS account_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    subscription_type TEXT NOT NULL,
    subscription_key TEXT NOT NULL,
    subscription_title TEXT,
    subscription_url TEXT,
    group_name TEXT,
    schedule_id INTEGER,
    university_subgroup INTEGER NOT NULL DEFAULT 0,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    notifications_enabled INTEGER NOT NULL DEFAULT 1 CHECK (notifications_enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, subscription_type, subscription_key)
);

CREATE INDEX IF NOT EXISTS idx_account_subscriptions_key
ON account_subscriptions(subscription_key);

CREATE INDEX IF NOT EXISTS idx_account_subscriptions_account
ON account_subscriptions(account_id);

CREATE TABLE IF NOT EXISTS account_link_requests (
    token_hash TEXT PRIMARY KEY,
    initiator_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    initiator_platform TEXT NOT NULL,
    initiator_user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    confirmed_target_account_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS miniapp_sessions (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_miniapp_sessions_account
ON miniapp_sessions(account_id);

CREATE TABLE IF NOT EXISTS miniapp_telemetry_sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT,
    platform TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    theme TEXT DEFAULT 'system',
    scale INTEGER DEFAULT 100,
    start_time TEXT NOT NULL,
    last_ping_time TEXT NOT NULL,
    duration_seconds INTEGER DEFAULT 0,
    active_group TEXT,
    last_page TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telemetry_sessions_user
ON miniapp_telemetry_sessions(platform, user_id, start_time);

CREATE INDEX IF NOT EXISTS idx_telemetry_sessions_start
ON miniapp_telemetry_sessions(start_time);

CREATE TABLE IF NOT EXISTS miniapp_telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    account_id TEXT,
    platform TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_data TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telemetry_events_type_time
ON miniapp_telemetry_events(event_type, created_at);
"""

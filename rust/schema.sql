CREATE TABLE account_identities (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('telegram', 'vk')),
    user_id INTEGER NOT NULL,
    username TEXT,
    full_name TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (platform, user_id)
);

CREATE TABLE account_link_requests (
    token_hash TEXT PRIMARY KEY,
    initiator_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    initiator_platform TEXT NOT NULL,
    initiator_user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    confirmed_target_account_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE account_subscriptions (
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

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    theme TEXT NOT NULL DEFAULT 'system' CHECK (theme IN ('system', 'light', 'dark')),
    accent TEXT NOT NULL DEFAULT 'default',
    density TEXT NOT NULL DEFAULT 'comfortable' CHECK (density IN ('comfortable', 'compact')),
    reduced_motion INTEGER NOT NULL DEFAULT 0 CHECK (reduced_motion IN (0, 1)),
    notifications_enabled INTEGER NOT NULL DEFAULT 1 CHECK (notifications_enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL,
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

CREATE TABLE change_events (
	id INTEGER NOT NULL,
	source_type TEXT,
	source_key TEXT,
	source_title TEXT,
	source_url TEXT,
	group_name TEXT,
	schedule_id INTEGER,
	snapshot_hash TEXT NOT NULL,
	message TEXT NOT NULL,
	changed_dates_json TEXT NOT NULL,
	payload_json TEXT NOT NULL,
	created_at TEXT NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE daily_lesson_counter_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_date_iso TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    UNIQUE(target_date_iso, group_name)
                );

CREATE TABLE delivery_events (
	id INTEGER NOT NULL,
	message_id TEXT,
	campaign_type TEXT DEFAULT 'notification' NOT NULL,
	platform TEXT NOT NULL,
	user_id INTEGER NOT NULL,
	via_broker INTEGER DEFAULT '0' NOT NULL,
	status TEXT NOT NULL,
	attempt INTEGER DEFAULT '1' NOT NULL,
	error_text TEXT,
	created_at TEXT NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE delivery_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    peer_key TEXT NOT NULL,
    method TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL,
    next_publish_at REAL NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    result_json TEXT,
    error_json TEXT
);

CREATE TABLE feedback_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT,
                    message_type TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    reply_text TEXT,
                    replied_at TEXT
                );

CREATE TABLE groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER,
                    group_name TEXT NOT NULL,
                    department_id INTEGER NOT NULL DEFAULT 0,
                    department_code TEXT NOT NULL DEFAULT '',
                    department_name TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

CREATE TABLE lesson_counter_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    counter_id INTEGER NOT NULL,
                    schedule_id INTEGER,
                    date_iso TEXT NOT NULL,
                    lesson_number INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    teacher TEXT NOT NULL,
                    classroom TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(counter_id, date_iso, lesson_number),
                    FOREIGN KEY(counter_id) REFERENCES lesson_counters(id) ON DELETE CASCADE
                );

CREATE TABLE lesson_counters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER,
                    subject TEXT NOT NULL,
                    teacher TEXT NOT NULL,
                    subject_norm TEXT NOT NULL,
                    teacher_norm TEXT NOT NULL,
                    passed_count INTEGER NOT NULL DEFAULT 0,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(schedule_id, subject_norm, teacher_norm)
                );

CREATE TABLE miniapp_sessions (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE miniapp_telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    account_id TEXT,
    platform TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_data TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE miniapp_telemetry_sessions (
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

CREATE TABLE ocr_status_snapshot (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

CREATE TABLE schedule_archive_backfill (
    name TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE schedule_archive_projected_snapshots (
    snapshot_id INTEGER PRIMARY KEY
);

CREATE TABLE schedule_day_archive (
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

CREATE TABLE schedule_day_references (
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

CREATE TABLE schedule_snapshots (
	id INTEGER NOT NULL,
	snapshot_type TEXT NOT NULL,
	source_type TEXT,
	source_key TEXT,
	source_title TEXT,
	source_url TEXT,
	group_name TEXT,
	schedule_id INTEGER,
	snapshot_hash TEXT NOT NULL,
	content_json TEXT NOT NULL,
	fetched_at TEXT NOT NULL,
	created_at TEXT NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE search_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

CREATE TABLE star_donations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    stars INTEGER NOT NULL,
                    charge_id TEXT NOT NULL,
                    refunded INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    refunded_at TEXT
                );

CREATE TABLE system_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL
                );

CREATE TABLE users (
	platform TEXT NOT NULL,
	user_id INTEGER NOT NULL,
	username TEXT,
	full_name TEXT,
	subscription_type TEXT,
	subscription_key TEXT,
	subscription_title TEXT,
	subscription_url TEXT,
	group_name TEXT,
	schedule_id INTEGER,
	is_admin INTEGER DEFAULT '0' NOT NULL,
	is_editor INTEGER DEFAULT '0' NOT NULL,
	homework_notifications_enabled INTEGER DEFAULT '1' NOT NULL,
	delivery_disabled_auto INTEGER DEFAULT '0' NOT NULL,
	created_at TEXT NOT NULL,
	last_seen_at TEXT NOT NULL, university_subgroup INTEGER, audience_subscription_key TEXT, audience_subscription_title TEXT, audience_subscription_url TEXT, custom_sticker_file_id TEXT,
	PRIMARY KEY (platform, user_id)
);

CREATE TABLE web_users (
                    login TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    permissions_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

CREATE INDEX idx_account_identities_account
ON account_identities(account_id);

CREATE INDEX idx_account_subscriptions_account
ON account_subscriptions(account_id);

CREATE INDEX idx_account_subscriptions_key
ON account_subscriptions(subscription_key);

CREATE UNIQUE INDEX idx_change_events_source_key_snapshot_hash_day
                ON change_events(source_key, snapshot_hash, substr(created_at, 1, 10))
                ;

CREATE INDEX idx_delivery_events_platform_status_created
                ON delivery_events(platform, status, created_at)
                ;

CREATE INDEX idx_delivery_events_platform_user_id
                ON delivery_events(platform, user_id, id)
                ;

CREATE INDEX idx_delivery_events_status_broker ON delivery_events (status, via_broker);

CREATE INDEX idx_delivery_events_status_campaign ON delivery_events (status, campaign_type);

CREATE INDEX idx_feedback_platform_user ON feedback_messages(platform, user_id);

CREATE UNIQUE INDEX idx_groups_group_name ON groups(group_name);

CREATE UNIQUE INDEX idx_groups_schedule_id ON groups(schedule_id) WHERE schedule_id IS NOT NULL;

CREATE UNIQUE INDEX idx_lesson_counter_events_once
                ON lesson_counter_events(counter_id, date_iso, lesson_number)
                ;

CREATE UNIQUE INDEX idx_lesson_counters_source
                ON lesson_counters(schedule_id, subject_norm, teacher_norm)
                ;

CREATE INDEX idx_miniapp_sessions_account
ON miniapp_sessions(account_id);

CREATE INDEX idx_outbox_due
    ON delivery_outbox(platform, status, next_attempt_at, next_publish_at);

CREATE INDEX idx_outbox_peer
    ON delivery_outbox(platform, peer_key, status, id);

CREATE INDEX idx_outbox_retention ON delivery_outbox(created_at);

CREATE INDEX idx_schedule_day_archive_date
ON schedule_day_archive(date_iso);

CREATE UNIQUE INDEX idx_search_targets_kind_url ON search_targets(kind, url);

CREATE INDEX idx_telemetry_events_type_time
ON miniapp_telemetry_events(event_type, created_at);

CREATE INDEX idx_telemetry_sessions_start
ON miniapp_telemetry_sessions(start_time);

CREATE INDEX idx_telemetry_sessions_user
ON miniapp_telemetry_sessions(platform, user_id, start_time);

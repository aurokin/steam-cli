PRAGMA foreign_keys = OFF;

ALTER TABLE account_data_consents RENAME TO account_data_consents_v18;

CREATE TABLE account_data_consents (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    consent_kind TEXT NOT NULL CHECK (consent_kind IN (
        'owned_persistence', 'wishlist_persistence', 'activity_persistence',
        'review_persistence'
    )),
    disclosure_version TEXT NOT NULL,
    backups_acknowledged INTEGER NOT NULL CHECK (backups_acknowledged = 1),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (account_id, consent_kind)
);

INSERT INTO account_data_consents SELECT * FROM account_data_consents_v18;
DROP TABLE account_data_consents_v18;

PRAGMA foreign_keys = ON;

CREATE TABLE review_sync_demand (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    targeted INTEGER NOT NULL CHECK (targeted IN (0, 1)),
    evaluated INTEGER NOT NULL DEFAULT 0 CHECK (evaluated IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN (
        'running', 'ready', 'unevaluated', 'failed', 'expired'
    )),
    error_code TEXT,
    observed_at TEXT,
    PRIMARY KEY (sync_run_id, appid),
    UNIQUE (sync_run_id, ordinal)
);

CREATE INDEX review_sync_demand_account_app_idx
    ON review_sync_demand(account_id, appid, sync_run_id);

CREATE TABLE review_observations (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    review_score INTEGER NOT NULL CHECK (review_score BETWEEN 0 AND 10),
    total_positive INTEGER NOT NULL CHECK (total_positive >= 0),
    total_negative INTEGER NOT NULL CHECK (total_negative >= 0),
    total_reviews INTEGER NOT NULL CHECK (total_reviews >= 0),
    request_filter TEXT NOT NULL CHECK (request_filter = 'all'),
    language TEXT NOT NULL CHECK (language = 'all'),
    day_range INTEGER NOT NULL CHECK (day_range = 365),
    review_type TEXT NOT NULL CHECK (review_type = 'all'),
    purchase_type TEXT NOT NULL CHECK (purchase_type = 'all'),
    num_per_page INTEGER NOT NULL CHECK (num_per_page = 1),
    off_topic_activity_filtered INTEGER NOT NULL CHECK (off_topic_activity_filtered = 1),
    source_locator TEXT NOT NULL CHECK (source_locator = 'steam_store_appreviews'),
    human_reference_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (sync_run_id, appid),
    CHECK (total_positive + total_negative = total_reviews)
);

CREATE TABLE review_current (
    appid INTEGER PRIMARY KEY REFERENCES steam_apps(appid),
    provider TEXT NOT NULL CHECK (provider = 'steam_store'),
    review_score INTEGER NOT NULL CHECK (review_score BETWEEN 0 AND 10),
    total_positive INTEGER NOT NULL CHECK (total_positive >= 0),
    total_negative INTEGER NOT NULL CHECK (total_negative >= 0),
    total_reviews INTEGER NOT NULL CHECK (total_reviews >= 0),
    request_filter TEXT NOT NULL CHECK (request_filter = 'all'),
    language TEXT NOT NULL CHECK (language = 'all'),
    day_range INTEGER NOT NULL CHECK (day_range = 365),
    review_type TEXT NOT NULL CHECK (review_type = 'all'),
    purchase_type TEXT NOT NULL CHECK (purchase_type = 'all'),
    num_per_page INTEGER NOT NULL CHECK (num_per_page = 1),
    off_topic_activity_filtered INTEGER NOT NULL CHECK (off_topic_activity_filtered = 1),
    source_locator TEXT NOT NULL CHECK (source_locator = 'steam_store_appreviews'),
    human_reference_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    promoted_sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    CHECK (total_positive + total_negative = total_reviews)
);

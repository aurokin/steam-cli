PRAGMA foreign_keys = OFF;

ALTER TABLE account_data_consents RENAME TO account_data_consents_v11;

CREATE TABLE account_data_consents (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    consent_kind TEXT NOT NULL CHECK (
        consent_kind IN ('owned_persistence', 'wishlist_persistence')
    ),
    disclosure_version TEXT NOT NULL,
    backups_acknowledged INTEGER NOT NULL CHECK (backups_acknowledged = 1),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (account_id, consent_kind)
);

INSERT INTO account_data_consents
SELECT * FROM account_data_consents_v11;
DROP TABLE account_data_consents_v11;

PRAGMA foreign_keys = ON;

CREATE TABLE wishlist_sync_metadata (
    sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    support_level TEXT NOT NULL,
    item_list_retrieved_at TEXT NOT NULL,
    item_list_reported_count INTEGER NOT NULL CHECK (item_list_reported_count >= 0),
    item_count_retrieved_at TEXT NOT NULL,
    item_count_reported_count INTEGER NOT NULL CHECK (item_count_reported_count >= 0),
    validation_method TEXT NOT NULL CHECK (validation_method = 'sequential_count_match')
);

CREATE TABLE wishlist_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    priority INTEGER NOT NULL CHECK (priority >= 0),
    date_added INTEGER NOT NULL CHECK (date_added >= 0),
    observed_at TEXT NOT NULL,
    UNIQUE (sync_run_id, appid)
);

CREATE INDEX wishlist_observations_account_app_idx
    ON wishlist_observations(account_id, appid, observed_at);

CREATE TABLE wishlist_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    priority INTEGER NOT NULL CHECK (priority >= 0),
    date_added INTEGER NOT NULL CHECK (date_added >= 0),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, appid)
);

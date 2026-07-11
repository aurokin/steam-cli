ALTER TABLE sync_runs ADD COLUMN account_id INTEGER
    REFERENCES accounts(id) ON DELETE CASCADE;

CREATE INDEX sync_runs_account_capability_status_idx
    ON sync_runs(account_id, capability, status, started_at);

CREATE TABLE account_data_consents (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    consent_kind TEXT NOT NULL CHECK (consent_kind = 'owned_persistence'),
    disclosure_version TEXT NOT NULL,
    backups_acknowledged INTEGER NOT NULL CHECK (backups_acknowledged = 1),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (account_id, consent_kind)
);

CREATE TABLE owned_sync_metadata (
    sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    retrieved_at TEXT NOT NULL,
    include_appinfo INTEGER NOT NULL CHECK (include_appinfo IN (0, 1)),
    include_played_free_games INTEGER NOT NULL
        CHECK (include_played_free_games IN (0, 1)),
    support_level TEXT NOT NULL
);

CREATE TABLE owned_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    name TEXT,
    playtime_forever_minutes INTEGER
        CHECK (playtime_forever_minutes IS NULL OR playtime_forever_minutes >= 0),
    inclusion_basis TEXT NOT NULL
        CHECK (inclusion_basis IN ('visible_owned', 'played_free')),
    observed_at TEXT NOT NULL,
    UNIQUE (sync_run_id, appid)
);

CREATE INDEX owned_observations_account_app_idx
    ON owned_observations(account_id, appid, observed_at);

CREATE TABLE owned_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    name TEXT,
    playtime_forever_minutes INTEGER
        CHECK (playtime_forever_minutes IS NULL OR playtime_forever_minutes >= 0),
    inclusion_basis TEXT NOT NULL
        CHECK (inclusion_basis IN ('visible_owned', 'played_free')),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, appid)
);

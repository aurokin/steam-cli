ALTER TABLE sync_runs ADD COLUMN account_id INTEGER
    REFERENCES accounts(id) ON DELETE CASCADE;

CREATE INDEX sync_runs_account_capability_status_idx
    ON sync_runs(account_id, capability, status, started_at);

CREATE TABLE game_entities (
    id INTEGER PRIMARY KEY,
    entity_kind TEXT NOT NULL CHECK (entity_kind = 'application'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE external_game_identities (
    provider TEXT NOT NULL,
    identity_kind TEXT NOT NULL CHECK (identity_kind = 'application_appid'),
    external_id TEXT NOT NULL,
    game_entity_id INTEGER NOT NULL REFERENCES game_entities(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (provider, identity_kind, external_id)
);

CREATE INDEX external_game_identities_entity_idx
    ON external_game_identities(game_entity_id);

INSERT INTO game_entities(id, entity_kind, created_at, updated_at)
SELECT appid, 'application', updated_at, updated_at
FROM steam_apps
ORDER BY appid;

INSERT INTO external_game_identities(
    provider, identity_kind, external_id, game_entity_id, created_at
)
SELECT 'steam', 'application_appid', CAST(appid AS TEXT), appid, updated_at
FROM steam_apps
ORDER BY appid;

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
    provider TEXT NOT NULL,
    support_level TEXT NOT NULL,
    include_appinfo INTEGER NOT NULL CHECK (include_appinfo = 1),
    base_include_played_free_games INTEGER NOT NULL
        CHECK (base_include_played_free_games = 0),
    base_retrieved_at TEXT NOT NULL,
    base_reported_count INTEGER NOT NULL CHECK (base_reported_count >= 0),
    expanded_include_played_free_games INTEGER NOT NULL
        CHECK (expanded_include_played_free_games = 1),
    expanded_retrieved_at TEXT NOT NULL,
    expanded_reported_count INTEGER NOT NULL CHECK (expanded_reported_count >= 0),
    classification_method TEXT NOT NULL
        CHECK (classification_method = 'sequential_set_difference')
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

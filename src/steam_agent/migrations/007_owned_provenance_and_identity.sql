ALTER TABLE owned_sync_metadata RENAME TO owned_sync_metadata_v6;

CREATE TABLE owned_sync_metadata (
    sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    support_level TEXT NOT NULL,
    include_appinfo INTEGER NOT NULL CHECK (include_appinfo IN (0, 1)),
    base_include_played_free_games INTEGER NOT NULL
        CHECK (base_include_played_free_games = 0),
    base_retrieved_at TEXT NOT NULL,
    base_reported_count INTEGER NOT NULL CHECK (base_reported_count >= 0),
    expanded_include_played_free_games INTEGER NOT NULL
        CHECK (expanded_include_played_free_games = 1),
    expanded_retrieved_at TEXT NOT NULL,
    expanded_reported_count INTEGER NOT NULL CHECK (expanded_reported_count >= 0),
    classification_method TEXT NOT NULL CHECK (
        classification_method IN (
            'sequential_set_difference',
            'legacy_v6_inferred_pair'
        )
    )
);

INSERT INTO owned_sync_metadata(
    sync_run_id, account_id, provider, support_level, include_appinfo,
    base_include_played_free_games, base_retrieved_at, base_reported_count,
    expanded_include_played_free_games, expanded_retrieved_at,
    expanded_reported_count, classification_method
)
SELECT
    legacy.sync_run_id,
    legacy.account_id,
    runs.provider,
    legacy.support_level,
    legacy.include_appinfo,
    0,
    legacy.retrieved_at,
    (
        SELECT COUNT(*) FROM owned_observations AS observations
        WHERE observations.sync_run_id = legacy.sync_run_id
          AND observations.inclusion_basis = 'visible_owned'
    ),
    1,
    legacy.retrieved_at,
    (
        SELECT COUNT(*) FROM owned_observations AS observations
        WHERE observations.sync_run_id = legacy.sync_run_id
    ),
    'legacy_v6_inferred_pair'
FROM owned_sync_metadata_v6 AS legacy
JOIN sync_runs AS runs ON runs.id = legacy.sync_run_id;

DROP TABLE owned_sync_metadata_v6;

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

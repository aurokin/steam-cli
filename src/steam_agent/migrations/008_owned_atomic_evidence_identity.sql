ALTER TABLE owned_sync_metadata RENAME TO owned_sync_metadata_v7;

CREATE TABLE owned_sync_metadata (
    sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    support_level TEXT NOT NULL,
    include_appinfo INTEGER CHECK (include_appinfo IN (0, 1)),
    base_include_played_free_games INTEGER
        CHECK (base_include_played_free_games IN (0, 1)),
    base_retrieved_at TEXT,
    base_reported_count INTEGER CHECK (
        base_reported_count IS NULL OR base_reported_count >= 0
    ),
    expanded_include_played_free_games INTEGER
        CHECK (expanded_include_played_free_games IN (0, 1)),
    expanded_retrieved_at TEXT NOT NULL,
    expanded_reported_count INTEGER CHECK (
        expanded_reported_count IS NULL OR expanded_reported_count >= 0
    ),
    classification_method TEXT NOT NULL CHECK (
        classification_method IN (
            'sequential_set_difference',
            'legacy_v6_inferred_pair',
            'legacy_single_snapshot'
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
    legacy.provider,
    legacy.support_level,
    legacy.include_appinfo,
    CASE
        WHEN legacy.classification_method = 'sequential_set_difference'
        THEN legacy.base_include_played_free_games
        ELSE NULL
    END,
    CASE
        WHEN legacy.classification_method = 'sequential_set_difference'
        THEN legacy.base_retrieved_at
        ELSE NULL
    END,
    CASE
        WHEN legacy.classification_method = 'sequential_set_difference'
        THEN legacy.base_reported_count
        ELSE NULL
    END,
    CASE
        WHEN legacy.classification_method = 'sequential_set_difference'
        THEN legacy.expanded_include_played_free_games
        ELSE (
            SELECT CAST(json_extract(evidence.context_json,
                '$.include_played_free_games') AS INTEGER)
            FROM owned_observations AS observations
            JOIN evidence ON evidence.id = observations.evidence_id
            WHERE observations.sync_run_id = legacy.sync_run_id
              AND json_type(evidence.context_json,
                  '$.include_played_free_games') IN ('true', 'false', 'integer')
            LIMIT 1
        )
    END,
    legacy.expanded_retrieved_at,
    CASE
        WHEN legacy.classification_method = 'sequential_set_difference'
        THEN legacy.expanded_reported_count
        ELSE NULL
    END,
    CASE
        WHEN legacy.classification_method = 'sequential_set_difference'
        THEN 'sequential_set_difference'
        WHEN (
            SELECT CAST(json_extract(evidence.context_json,
                '$.include_played_free_games') AS INTEGER)
            FROM owned_observations AS observations
            JOIN evidence ON evidence.id = observations.evidence_id
            WHERE observations.sync_run_id = legacy.sync_run_id
              AND json_type(evidence.context_json,
                  '$.include_played_free_games') IN ('true', 'false', 'integer')
            LIMIT 1
        ) = 1
        THEN 'legacy_v6_inferred_pair'
        ELSE 'legacy_single_snapshot'
    END
FROM owned_sync_metadata_v7 AS legacy;

DROP TABLE owned_sync_metadata_v7;

DELETE FROM owned_observations
WHERE sync_run_id <> COALESCE((
    SELECT MAX(runs.id) FROM sync_runs AS runs
    WHERE runs.account_id = owned_observations.account_id
      AND runs.capability = 'owned.visible.read'
      AND runs.status = 'complete'
      AND runs.promoted = 1
), -1);

DELETE FROM owned_sync_metadata
WHERE sync_run_id <> COALESCE((
    SELECT MAX(runs.id) FROM sync_runs AS runs
    WHERE runs.account_id = owned_sync_metadata.account_id
      AND runs.capability = 'owned.visible.read'
      AND runs.status = 'complete'
      AND runs.promoted = 1
), -1);

DELETE FROM evidence
WHERE capability = 'owned.visible.read'
  AND source_kind = 'steam_web_api'
  AND NOT EXISTS (
      SELECT 1 FROM owned_observations
      WHERE owned_observations.evidence_id = evidence.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM owned_current
      WHERE owned_current.evidence_id = evidence.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM installed_observations
      WHERE installed_observations.evidence_id = evidence.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM installed_current
      WHERE installed_current.evidence_id = evidence.id
  );

ALTER TABLE evidence ADD COLUMN account_id INTEGER
    REFERENCES accounts(id) ON DELETE CASCADE;

UPDATE evidence
SET account_id = (
    SELECT observations.account_id
    FROM owned_observations AS observations
    WHERE observations.evidence_id = evidence.id
    LIMIT 1
)
WHERE EXISTS (
    SELECT 1 FROM owned_observations AS observations
    WHERE observations.evidence_id = evidence.id
);

CREATE INDEX evidence_account_idx ON evidence(account_id);

ALTER TABLE game_entities ADD COLUMN stable_id TEXT;

UPDATE game_entities
SET stable_id = (
    SELECT steam_application_uuid_v5(identities.external_id)
    FROM external_game_identities AS identities
    WHERE identities.game_entity_id = game_entities.id
      AND identities.provider = 'steam'
      AND identities.identity_kind = 'application_appid'
);

CREATE UNIQUE INDEX game_entities_stable_id_idx ON game_entities(stable_id);

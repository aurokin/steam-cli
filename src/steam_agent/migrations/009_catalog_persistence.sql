CREATE TABLE catalog_sync_metadata (
    sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    support_level TEXT NOT NULL,
    demanded_count INTEGER NOT NULL CHECK (demanded_count >= 0)
);

CREATE TABLE catalog_stream_provenance (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    stream TEXT NOT NULL CHECK (stream IN ('games', 'non_games')),
    termination TEXT NOT NULL,
    scanned_through_appid INTEGER NOT NULL CHECK (scanned_through_appid >= 0),
    filter_context_json TEXT NOT NULL,
    PRIMARY KEY (sync_run_id, stream)
);

CREATE TABLE catalog_page_provenance (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    stream TEXT NOT NULL CHECK (stream IN ('games', 'non_games')),
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    requested_last_appid INTEGER NOT NULL CHECK (requested_last_appid >= 0),
    first_appid INTEGER CHECK (first_appid IS NULL OR first_appid > 0),
    last_appid INTEGER NOT NULL CHECK (last_appid >= 0),
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    have_more_results INTEGER NOT NULL CHECK (have_more_results IN (0, 1)),
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (sync_run_id, stream, page_number),
    FOREIGN KEY (sync_run_id, stream)
        REFERENCES catalog_stream_provenance(sync_run_id, stream) ON DELETE CASCADE
);

CREATE TABLE catalog_observations (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    classification TEXT NOT NULL
        CHECK (classification IN ('game', 'non_game', 'not_observed')),
    last_modified INTEGER CHECK (last_modified IS NULL OR last_modified >= 0),
    price_change_number INTEGER
        CHECK (price_change_number IS NULL OR price_change_number >= 0),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (sync_run_id, appid)
);

CREATE INDEX catalog_observations_app_idx
    ON catalog_observations(appid, sync_run_id);

CREATE TABLE catalog_current (
    appid INTEGER PRIMARY KEY REFERENCES steam_apps(appid),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    promoted_sync_run_id INTEGER NOT NULL
        REFERENCES sync_runs(id) ON DELETE CASCADE,
    classification TEXT NOT NULL
        CHECK (classification IN ('game', 'non_game', 'not_observed')),
    last_modified INTEGER CHECK (last_modified IS NULL OR last_modified >= 0),
    price_change_number INTEGER
        CHECK (price_change_number IS NULL OR price_change_number >= 0),
    observed_at TEXT NOT NULL
);

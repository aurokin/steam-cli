CREATE TABLE catalog_subject_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    machine_id TEXT NOT NULL CHECK (length(machine_id) BETWEEN 1 AND 256),
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    classification TEXT NOT NULL
        CHECK (classification IN ('game', 'non_game', 'not_observed')),
    last_modified INTEGER CHECK (last_modified IS NULL OR last_modified >= 0),
    price_change_number INTEGER
        CHECK (price_change_number IS NULL OR price_change_number >= 0),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, machine_id, appid),
    FOREIGN KEY (promoted_sync_run_id, appid)
        REFERENCES catalog_observations(sync_run_id, appid) ON DELETE CASCADE
);

CREATE INDEX catalog_subject_current_run_idx
    ON catalog_subject_current(promoted_sync_run_id, appid);

CREATE TABLE catalog_sync_subjects (
    sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    machine_id TEXT NOT NULL CHECK (
        length(machine_id) BETWEEN 1 AND 256
    )
);

CREATE INDEX catalog_sync_subjects_account_machine_idx
    ON catalog_sync_subjects(account_id, machine_id, sync_run_id);

CREATE TABLE catalog_sync_demand (
    sync_run_id INTEGER NOT NULL
        REFERENCES catalog_sync_subjects(sync_run_id) ON DELETE CASCADE,
    appid INTEGER NOT NULL CHECK (appid BETWEEN 1 AND 4294967295),
    PRIMARY KEY (sync_run_id, appid)
);

CREATE INDEX catalog_sync_demand_app_idx
    ON catalog_sync_demand(appid, sync_run_id);

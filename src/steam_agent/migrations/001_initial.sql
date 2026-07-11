CREATE TABLE machines (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    architecture TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE steam_apps (
    appid INTEGER PRIMARY KEY CHECK (appid > 0),
    name TEXT,
    app_type TEXT NOT NULL DEFAULT 'unknown',
    updated_at TEXT NOT NULL
);

CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    capability TEXT NOT NULL,
    machine_id TEXT REFERENCES machines(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    promoted INTEGER NOT NULL DEFAULT 0 CHECK (promoted IN (0, 1)),
    records_seen INTEGER NOT NULL DEFAULT 0 CHECK (records_seen >= 0),
    error_code TEXT,
    error_detail TEXT
);

CREATE INDEX sync_runs_capability_status_idx
    ON sync_runs(capability, status, started_at);

CREATE TABLE evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    capability TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    effective_at TEXT,
    support_level TEXT NOT NULL,
    context_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE (
        provider,
        capability,
        source_kind,
        source_locator,
        retrieved_at,
        context_json,
        content_hash
    )
);

CREATE TABLE installed_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    name TEXT,
    app_type TEXT NOT NULL DEFAULT 'unknown',
    library_root TEXT NOT NULL,
    install_dir TEXT NOT NULL,
    state TEXT NOT NULL,
    build_id TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    manifest_path TEXT,
    manifest_mtime TEXT,
    observed_at TEXT NOT NULL,
    UNIQUE (sync_run_id, appid)
);

CREATE INDEX installed_observations_machine_app_idx
    ON installed_observations(machine_id, appid, observed_at);

CREATE TABLE installed_current (
    machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    library_root TEXT NOT NULL,
    install_dir TEXT NOT NULL,
    state TEXT NOT NULL,
    build_id TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    manifest_path TEXT,
    manifest_mtime TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (machine_id, appid)
);

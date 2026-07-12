ALTER TABLE declared_app_observations RENAME TO declared_app_observations_v22;

CREATE TABLE declared_app_observations (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    country TEXT NOT NULL,
    language TEXT NOT NULL,
    schema_id TEXT NOT NULL CHECK (schema_id IN (
        'declared-app-facts/0.1', 'declared-app-facts/0.2'
    )),
    facts_json TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider = 'steam_store'),
    support_level TEXT NOT NULL CHECK (support_level = 'provisional'),
    source_locator TEXT NOT NULL CHECK (source_locator = 'steam_store_appdetails'),
    human_reference_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    promoted INTEGER NOT NULL DEFAULT 0 CHECK (promoted IN (0, 1)),
    PRIMARY KEY (sync_run_id, appid),
    FOREIGN KEY (sync_run_id, appid)
        REFERENCES declared_app_sync_demand(sync_run_id, appid) ON DELETE CASCADE
);

INSERT INTO declared_app_observations
SELECT * FROM declared_app_observations_v22;
DROP TABLE declared_app_observations_v22;

ALTER TABLE declared_app_current RENAME TO declared_app_current_v22;

CREATE TABLE declared_app_current (
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    country TEXT NOT NULL,
    language TEXT NOT NULL,
    schema_id TEXT NOT NULL CHECK (schema_id IN (
        'declared-app-facts/0.1', 'declared-app-facts/0.2'
    )),
    facts_json TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider = 'steam_store'),
    support_level TEXT NOT NULL CHECK (support_level = 'provisional'),
    source_locator TEXT NOT NULL CHECK (source_locator = 'steam_store_appdetails'),
    human_reference_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    promoted_sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    PRIMARY KEY (appid, country, language, provider)
);

INSERT INTO declared_app_current
SELECT * FROM declared_app_current_v22;
DROP TABLE declared_app_current_v22;

CREATE INDEX declared_app_current_context_idx
    ON declared_app_current(country, language, appid);

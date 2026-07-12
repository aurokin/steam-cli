ALTER TABLE account_data_consents RENAME TO account_data_consents_v22;

CREATE TABLE account_data_consents (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    consent_kind TEXT NOT NULL CHECK (consent_kind IN (
        'owned_persistence', 'wishlist_persistence', 'activity_persistence',
        'review_persistence', 'compatibility_persistence'
    )),
    disclosure_version TEXT NOT NULL,
    backups_acknowledged INTEGER NOT NULL CHECK (backups_acknowledged = 1),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (account_id, consent_kind)
);

INSERT INTO account_data_consents SELECT * FROM account_data_consents_v22;
DROP TABLE account_data_consents_v22;

CREATE TABLE declared_app_sync_demand (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    country TEXT NOT NULL CHECK (
        length(country) = 2 AND country = upper(country)
    ),
    language TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    targeted INTEGER NOT NULL CHECK (targeted IN (0, 1)),
    evaluated INTEGER NOT NULL DEFAULT 0 CHECK (evaluated IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN (
        'running', 'ready', 'not_found', 'unevaluated', 'failed'
    )),
    error_code TEXT,
    retry_at TEXT,
    observed_at TEXT,
    PRIMARY KEY (sync_run_id, appid),
    UNIQUE (sync_run_id, ordinal)
);

CREATE INDEX declared_app_demand_account_context_idx
    ON declared_app_sync_demand(
        account_id, machine_id, country, language, appid, sync_run_id
    );

CREATE TABLE declared_app_observations (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    country TEXT NOT NULL,
    language TEXT NOT NULL,
    schema_id TEXT NOT NULL CHECK (schema_id = 'declared-app-facts/0.1'),
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

CREATE TABLE declared_app_current (
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    country TEXT NOT NULL,
    language TEXT NOT NULL,
    schema_id TEXT NOT NULL CHECK (schema_id = 'declared-app-facts/0.1'),
    facts_json TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider = 'steam_store'),
    support_level TEXT NOT NULL CHECK (support_level = 'provisional'),
    source_locator TEXT NOT NULL CHECK (source_locator = 'steam_store_appdetails'),
    human_reference_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    promoted_sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    PRIMARY KEY (appid, country, language, provider)
);

CREATE INDEX declared_app_current_context_idx
    ON declared_app_current(country, language, appid);

CREATE TABLE price_sync_metadata (
    sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    country TEXT NOT NULL CHECK (length(country) = 2 AND country = upper(country)),
    provider TEXT NOT NULL CHECK (provider IN ('gg-deals', 'cheapshark')),
    scope TEXT NOT NULL CHECK (scope = 'wishlist'),
    wishlist_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    demand_count INTEGER NOT NULL CHECK (demand_count >= 0),
    evaluated_count INTEGER NOT NULL DEFAULT 0 CHECK (evaluated_count >= 0),
    requested_limit INTEGER CHECK (requested_limit IS NULL OR requested_limit > 0),
    rate_limit INTEGER,
    rate_remaining INTEGER,
    rate_reset_value INTEGER,
    retry_after_seconds INTEGER
);

CREATE TABLE price_sync_demand (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    country TEXT NOT NULL,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    demand_order INTEGER NOT NULL CHECK (demand_order >= 0),
    wishlist_priority INTEGER NOT NULL CHECK (wishlist_priority >= 0),
    wishlist_date_added INTEGER NOT NULL CHECK (wishlist_date_added >= 0),
    evaluated INTEGER NOT NULL DEFAULT 0 CHECK (evaluated IN (0, 1)),
    outcome TEXT CHECK (outcome IN ('observed', 'not_found')),
    PRIMARY KEY (sync_run_id, appid)
);

CREATE INDEX price_sync_demand_subject_idx
    ON price_sync_demand(account_id, country, appid, sync_run_id);

CREATE TABLE price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    country TEXT NOT NULL,
    provider TEXT NOT NULL,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    fact_kind TEXT NOT NULL CHECK (fact_kind IN ('offer', 'historical_low')),
    provider_product_id TEXT NOT NULL,
    product_mapping TEXT NOT NULL CHECK (product_mapping = 'exact'),
    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    regular_amount_minor INTEGER CHECK (regular_amount_minor >= 0),
    discount_percent INTEGER CHECK (discount_percent BETWEEN 0 AND 100),
    store_class TEXT NOT NULL CHECK (store_class IN ('official', 'keyshop', 'unknown')),
    comparability TEXT NOT NULL CHECK (comparability IN ('exact_product', 'normalized_game', 'unknown')),
    low_scope TEXT,
    effective_at TEXT,
    observed_at TEXT NOT NULL,
    fresh_until TEXT NOT NULL,
    hard_expires_at TEXT NOT NULL,
    provider_url TEXT NOT NULL,
    access_mode TEXT NOT NULL CHECK (access_mode = 'manual_only'),
    automation_supported INTEGER NOT NULL CHECK (automation_supported = 0),
    UNIQUE (sync_run_id, appid, fact_kind, ordinal)
);

CREATE INDEX price_observations_expiry_idx ON price_observations(hard_expires_at);

CREATE TABLE price_subject_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    country TEXT NOT NULL,
    provider TEXT NOT NULL,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    outcome TEXT NOT NULL CHECK (outcome IN ('observed', 'not_found')),
    observed_at TEXT NOT NULL,
    fresh_until TEXT NOT NULL,
    hard_expires_at TEXT NOT NULL,
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    PRIMARY KEY (account_id, country, provider, appid)
);

CREATE INDEX price_subject_current_expiry_idx
    ON price_subject_current(hard_expires_at);

CREATE TABLE price_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    country TEXT NOT NULL,
    provider TEXT NOT NULL,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    fact_kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    provider_product_id TEXT NOT NULL,
    product_mapping TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    regular_amount_minor INTEGER,
    discount_percent INTEGER,
    store_class TEXT NOT NULL,
    comparability TEXT NOT NULL,
    low_scope TEXT,
    effective_at TEXT,
    observed_at TEXT NOT NULL,
    fresh_until TEXT NOT NULL,
    hard_expires_at TEXT NOT NULL,
    provider_url TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    automation_supported INTEGER NOT NULL,
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    PRIMARY KEY (account_id, country, provider, appid, fact_kind, ordinal)
);

CREATE INDEX price_current_expiry_idx ON price_current(hard_expires_at);

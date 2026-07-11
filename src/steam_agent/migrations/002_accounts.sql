CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL COLLATE NOCASE UNIQUE,
    provider TEXT NOT NULL,
    provider_account_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (provider, provider_account_id)
);

CREATE INDEX accounts_provider_idx
    ON accounts(provider, alias);

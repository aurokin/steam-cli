CREATE TABLE credential_refs (
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    backend TEXT NOT NULL CHECK (backend IN ('os', 'file')),
    configured_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, kind, profile_id)
);

CREATE TABLE provider_probes (
    capability TEXT NOT NULL,
    account_alias TEXT NOT NULL COLLATE NOCASE
        REFERENCES accounts(alias) ON DELETE CASCADE,
    probe_state TEXT NOT NULL CHECK (
        probe_state IN (
            'ready',
            'authentication_failed',
            'data_inaccessible',
            'provider_unavailable',
            'rate_limited',
            'contract_changed',
            'invalid_request'
        )
    ),
    checked_at TEXT NOT NULL,
    retryable INTEGER NOT NULL CHECK (retryable IN (0, 1)),
    PRIMARY KEY (capability, account_alias)
);

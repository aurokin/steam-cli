CREATE TABLE provider_request_limits (
    provider TEXT NOT NULL,
    budget_scope TEXT NOT NULL,
    next_allowed_at TEXT NOT NULL,
    PRIMARY KEY (provider, budget_scope)
);

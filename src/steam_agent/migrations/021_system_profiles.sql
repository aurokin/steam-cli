CREATE TABLE machine_data_consents (
    machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    consent_kind TEXT NOT NULL CHECK (consent_kind = 'system_profile'),
    disclosure_version TEXT NOT NULL,
    backups_acknowledged INTEGER NOT NULL CHECK (backups_acknowledged = 1),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (machine_id, consent_kind)
);

CREATE TABLE system_profile_observations (
    sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id) ON DELETE CASCADE,
    machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    schema_id TEXT NOT NULL CHECK (schema_id = 'system-profile/0.1'),
    profile_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX system_profile_observations_machine_time_idx
    ON system_profile_observations(machine_id, observed_at DESC);

CREATE TABLE system_profile_current (
    machine_id TEXT PRIMARY KEY REFERENCES machines(id) ON DELETE CASCADE,
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    schema_id TEXT NOT NULL CHECK (schema_id = 'system-profile/0.1'),
    profile_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

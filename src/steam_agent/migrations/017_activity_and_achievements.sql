PRAGMA foreign_keys = OFF;

ALTER TABLE account_data_consents RENAME TO account_data_consents_v16;

CREATE TABLE account_data_consents (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    consent_kind TEXT NOT NULL CHECK (consent_kind IN (
        'owned_persistence', 'wishlist_persistence', 'activity_persistence'
    )),
    disclosure_version TEXT NOT NULL,
    backups_acknowledged INTEGER NOT NULL CHECK (backups_acknowledged = 1),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (account_id, consent_kind)
);

INSERT INTO account_data_consents SELECT * FROM account_data_consents_v16;
DROP TABLE account_data_consents_v16;

PRAGMA foreign_keys = ON;

CREATE TABLE activity_observations (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    playtime_forever_minutes INTEGER CHECK (playtime_forever_minutes IS NULL OR playtime_forever_minutes >= 0),
    playtime_2weeks_minutes INTEGER CHECK (playtime_2weeks_minutes IS NULL OR playtime_2weeks_minutes >= 0),
    playtime_windows_forever_minutes INTEGER CHECK (playtime_windows_forever_minutes IS NULL OR playtime_windows_forever_minutes >= 0),
    playtime_mac_forever_minutes INTEGER CHECK (playtime_mac_forever_minutes IS NULL OR playtime_mac_forever_minutes >= 0),
    playtime_linux_forever_minutes INTEGER CHECK (playtime_linux_forever_minutes IS NULL OR playtime_linux_forever_minutes >= 0),
    playtime_deck_forever_minutes INTEGER CHECK (playtime_deck_forever_minutes IS NULL OR playtime_deck_forever_minutes >= 0),
    playtime_disconnected_minutes INTEGER CHECK (playtime_disconnected_minutes IS NULL OR playtime_disconnected_minutes >= 0),
    last_played_unix INTEGER CHECK (last_played_unix IS NULL OR last_played_unix >= 0),
    recent_window_minutes INTEGER CHECK (recent_window_minutes IS NULL OR recent_window_minutes >= 0),
    observed_at TEXT NOT NULL,
    recent_observed_at TEXT,
    PRIMARY KEY (sync_run_id, appid)
);

CREATE TABLE activity_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    playtime_forever_minutes INTEGER CHECK (playtime_forever_minutes IS NULL OR playtime_forever_minutes >= 0),
    playtime_2weeks_minutes INTEGER CHECK (playtime_2weeks_minutes IS NULL OR playtime_2weeks_minutes >= 0),
    playtime_windows_forever_minutes INTEGER CHECK (playtime_windows_forever_minutes IS NULL OR playtime_windows_forever_minutes >= 0),
    playtime_mac_forever_minutes INTEGER CHECK (playtime_mac_forever_minutes IS NULL OR playtime_mac_forever_minutes >= 0),
    playtime_linux_forever_minutes INTEGER CHECK (playtime_linux_forever_minutes IS NULL OR playtime_linux_forever_minutes >= 0),
    playtime_deck_forever_minutes INTEGER CHECK (playtime_deck_forever_minutes IS NULL OR playtime_deck_forever_minutes >= 0),
    playtime_disconnected_minutes INTEGER CHECK (playtime_disconnected_minutes IS NULL OR playtime_disconnected_minutes >= 0),
    last_played_unix INTEGER CHECK (last_played_unix IS NULL OR last_played_unix >= 0),
    recent_window_minutes INTEGER CHECK (recent_window_minutes IS NULL OR recent_window_minutes >= 0),
    observed_at TEXT NOT NULL,
    recent_observed_at TEXT,
    PRIMARY KEY (account_id, appid)
);

CREATE INDEX activity_current_account_last_played_idx
    ON activity_current(account_id, last_played_unix DESC, appid);

CREATE TABLE achievement_sync_demand (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    targeted INTEGER NOT NULL CHECK (targeted IN (0, 1)),
    evaluated INTEGER NOT NULL DEFAULT 0 CHECK (evaluated IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN (
        'running', 'ready', 'profile_not_public', 'achievements_not_supported',
        'unevaluated', 'failed', 'expired'
    )),
    error_code TEXT,
    observed_at TEXT,
    PRIMARY KEY (sync_run_id, appid),
    UNIQUE (sync_run_id, ordinal)
);

CREATE TABLE achievement_player_observations (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    api_name TEXT NOT NULL,
    achieved INTEGER NOT NULL CHECK (achieved IN (0, 1)),
    unlock_time_unix INTEGER CHECK (unlock_time_unix IS NULL OR unlock_time_unix >= 0),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (sync_run_id, appid, api_name)
);

CREATE TABLE achievement_player_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    api_name TEXT NOT NULL,
    achieved INTEGER NOT NULL CHECK (achieved IN (0, 1)),
    unlock_time_unix INTEGER CHECK (unlock_time_unix IS NULL OR unlock_time_unix >= 0),
    observed_at TEXT NOT NULL,
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    PRIMARY KEY (account_id, appid, api_name)
);

CREATE TABLE achievement_schema_current (
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    language TEXT NOT NULL,
    api_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready', 'achievements_not_supported')),
    display_name TEXT,
    description TEXT,
    hidden INTEGER NOT NULL CHECK (hidden IN (0, 1)),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (appid, language, api_name)
);

CREATE TABLE achievement_schema_status (
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    language TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready', 'achievements_not_supported')),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (appid, language)
);

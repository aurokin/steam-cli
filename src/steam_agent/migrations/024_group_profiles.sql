CREATE TABLE group_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('account', 'synthetic')),
    account_id INTEGER UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
    alias TEXT COLLATE NOCASE UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (kind = 'account' AND account_id IS NOT NULL AND alias IS NULL)
        OR
        (kind = 'synthetic' AND account_id IS NULL AND alias IS NOT NULL)
    )
);

CREATE INDEX group_profiles_kind_idx ON group_profiles(kind, id);

CREATE TABLE group_profile_consents (
    profile_id INTEGER PRIMARY KEY REFERENCES group_profiles(id) ON DELETE CASCADE,
    disclosure_version TEXT NOT NULL,
    backups_acknowledged INTEGER NOT NULL CHECK (backups_acknowledged = 1),
    accepted_at TEXT NOT NULL
);

CREATE TABLE group_ownership_current (
    profile_id INTEGER NOT NULL REFERENCES group_profiles(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    state TEXT NOT NULL CHECK (state IN ('owned', 'not_owned', 'unknown')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, appid)
);

CREATE TABLE group_family_current (
    recipient_profile_id INTEGER NOT NULL
        REFERENCES group_profiles(id) ON DELETE CASCADE,
    source_profile_id INTEGER NOT NULL
        REFERENCES group_profiles(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    state TEXT NOT NULL CHECK (state IN ('available', 'unavailable', 'unknown')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (recipient_profile_id, source_profile_id, appid),
    CHECK (recipient_profile_id <> source_profile_id)
);

CREATE INDEX group_family_source_idx
    ON group_family_current(source_profile_id, appid, recipient_profile_id);

CREATE TABLE group_app_assertion_current (
    profile_id INTEGER NOT NULL REFERENCES group_profiles(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    fact_kind TEXT NOT NULL CHECK (fact_kind IN (
        'players_min', 'players_max', 'trait', 'policy'
    )),
    slug TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN (
        'known', 'present', 'absent', 'unknown'
    )),
    value_integer INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, appid, fact_kind, slug),
    CHECK (
        (
            fact_kind IN ('players_min', 'players_max')
            AND slug = ''
            AND state IN ('known', 'unknown')
            AND (
                (
                    state = 'known'
                    AND value_integer IS NOT NULL
                    AND value_integer BETWEEN 1 AND 4294967295
                )
                OR (state = 'unknown' AND value_integer IS NULL)
            )
        )
        OR
        (
            fact_kind IN ('trait', 'policy')
            AND length(slug) BETWEEN 6 AND 64
            AND slug GLOB 'user:*'
            AND state IN ('present', 'absent', 'unknown')
            AND value_integer IS NULL
        )
    )
);

CREATE INDEX group_app_assertion_app_idx
    ON group_app_assertion_current(appid, fact_kind, slug, profile_id);

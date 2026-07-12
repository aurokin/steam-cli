CREATE TABLE explicit_feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'rating', 'play_state', 'snooze', 'minimum_session_minutes',
        'remaining_minutes', 'trait'
    )),
    value_text TEXT,
    value_integer INTEGER,
    trait TEXT,
    recorded_at TEXT NOT NULL,
    CHECK (value_integer IS NULL OR value_integer >= 0),
    CHECK (trait IS NULL OR trait GLOB 'user:*')
);

CREATE INDEX explicit_feedback_events_account_app_idx
    ON explicit_feedback_events(account_id, appid, id);

CREATE TABLE explicit_feedback_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    rating TEXT CHECK (rating IS NULL OR rating IN ('liked', 'disliked', 'neutral')),
    play_state TEXT CHECK (
        play_state IS NULL OR play_state IN ('finished', 'user_abandoned', 'active')
    ),
    snoozed_until TEXT,
    minimum_session_minutes INTEGER CHECK (
        minimum_session_minutes IS NULL OR minimum_session_minutes >= 0
    ),
    remaining_minutes INTEGER CHECK (
        remaining_minutes IS NULL OR remaining_minutes >= 0
    ),
    updated_at TEXT NOT NULL,
    last_event_id INTEGER REFERENCES explicit_feedback_events(id) ON DELETE SET NULL,
    PRIMARY KEY (account_id, appid)
);

CREATE TABLE explicit_trait_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    trait TEXT NOT NULL CHECK (trait GLOB 'user:*'),
    value TEXT NOT NULL CHECK (value IN ('present', 'absent', 'unknown')),
    updated_at TEXT NOT NULL,
    last_event_id INTEGER NOT NULL
        REFERENCES explicit_feedback_events(id) ON DELETE CASCADE,
    PRIMARY KEY (account_id, appid, trait)
);

CREATE TABLE preference_rule_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    trait TEXT NOT NULL CHECK (trait GLOB 'user:*'),
    action TEXT NOT NULL CHECK (action IN ('set', 'remove')),
    kind TEXT CHECK (kind IS NULL OR kind IN ('prefer', 'avoid', 'require')),
    strength TEXT CHECK (strength IS NULL OR strength IN ('soft', 'hard')),
    weight INTEGER CHECK (weight IS NULL OR weight BETWEEN 0 AND 100),
    recorded_at TEXT NOT NULL
);

CREATE INDEX preference_rule_events_account_trait_idx
    ON preference_rule_events(account_id, trait, id);

CREATE TABLE preference_rules_current (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    trait TEXT NOT NULL CHECK (trait GLOB 'user:*'),
    kind TEXT NOT NULL CHECK (kind IN ('prefer', 'avoid', 'require')),
    strength TEXT NOT NULL CHECK (strength IN ('soft', 'hard')),
    weight INTEGER NOT NULL CHECK (weight BETWEEN 0 AND 100),
    updated_at TEXT NOT NULL,
    last_event_id INTEGER NOT NULL
        REFERENCES preference_rule_events(id) ON DELETE CASCADE,
    PRIMARY KEY (account_id, trait)
);

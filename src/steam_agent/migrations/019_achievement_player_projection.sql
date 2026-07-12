CREATE TABLE achievement_player_projection (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL REFERENCES steam_apps(appid),
    observed_at TEXT NOT NULL,
    promoted_sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    PRIMARY KEY (account_id, appid)
);

-- Existing non-empty projections already prove that a ready player response was
-- promoted. Empty ready responses cannot be reconstructed retrospectively; new
-- writes record them explicitly below.
INSERT INTO achievement_player_projection(
    account_id, appid, observed_at, promoted_sync_run_id
)
SELECT account_id, appid, MAX(observed_at), MAX(promoted_sync_run_id)
FROM achievement_player_current
GROUP BY account_id, appid;

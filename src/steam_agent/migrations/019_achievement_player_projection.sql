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

-- A ready response with no rows is still a valid player projection. Recover
-- the newest historical ready result for AppIDs that had no non-empty current
-- projection to backfill above.
INSERT OR IGNORE INTO achievement_player_projection(
    account_id, appid, observed_at, promoted_sync_run_id
)
SELECT demand.account_id, demand.appid, demand.observed_at, demand.sync_run_id
FROM achievement_sync_demand AS demand
JOIN sync_runs AS runs ON runs.id = demand.sync_run_id
WHERE demand.state = 'ready'
  AND demand.observed_at IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM achievement_sync_demand AS newer_demand
    JOIN sync_runs AS newer_runs ON newer_runs.id = newer_demand.sync_run_id
    WHERE newer_demand.account_id = demand.account_id
      AND newer_demand.appid = demand.appid
      AND newer_demand.state = 'ready'
      AND (
        newer_runs.started_at > runs.started_at OR
        (newer_runs.started_at = runs.started_at AND newer_runs.id > runs.id)
      )
  );

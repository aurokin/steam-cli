-- Version 019 originally allowed a per-item ready result from an unsuccessful
-- parent run to become last-good. Remove player rows and projection markers
-- whose parent run was not itself completed and promoted.
DELETE FROM achievement_player_current
WHERE EXISTS (
    SELECT 1
    FROM sync_runs AS runs
    WHERE runs.id = achievement_player_current.promoted_sync_run_id
      AND (runs.status <> 'complete' OR runs.promoted <> 1)
);

DELETE FROM achievement_player_projection
WHERE EXISTS (
    SELECT 1
    FROM sync_runs AS runs
    WHERE runs.id = achievement_player_projection.promoted_sync_run_id
      AND (runs.status <> 'complete' OR runs.promoted <> 1)
);

-- Preserve/recover valid non-empty current projections first so the empty
-- ready backfill below cannot replace them.
INSERT OR IGNORE INTO achievement_player_projection(
    account_id, appid, observed_at, promoted_sync_run_id
)
SELECT player.account_id, player.appid, MAX(player.observed_at),
       MAX(player.promoted_sync_run_id)
FROM achievement_player_current AS player
JOIN sync_runs AS runs ON runs.id = player.promoted_sync_run_id
WHERE runs.status = 'complete' AND runs.promoted = 1
GROUP BY player.account_id, player.appid;

-- Recover the newest valid ready result, including valid empty projections.
INSERT OR IGNORE INTO achievement_player_projection(
    account_id, appid, observed_at, promoted_sync_run_id
)
SELECT demand.account_id, demand.appid, demand.observed_at, demand.sync_run_id
FROM achievement_sync_demand AS demand
JOIN sync_runs AS runs ON runs.id = demand.sync_run_id
WHERE demand.state = 'ready'
  AND demand.observed_at IS NOT NULL
  AND runs.status = 'complete'
  AND runs.promoted = 1
  AND NOT EXISTS (
    SELECT 1
    FROM achievement_sync_demand AS newer_demand
    JOIN sync_runs AS newer_runs ON newer_runs.id = newer_demand.sync_run_id
    WHERE newer_demand.account_id = demand.account_id
      AND newer_demand.appid = demand.appid
      AND newer_demand.state = 'ready'
      AND newer_runs.status = 'complete'
      AND newer_runs.promoted = 1
      AND (
        newer_runs.started_at > runs.started_at OR
        (newer_runs.started_at = runs.started_at AND newer_runs.id > runs.id)
      )
  );

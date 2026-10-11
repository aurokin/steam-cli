"""Cache-only, batched wishlist name enrichment with attributable evidence."""

import sqlite3
from typing import TypedDict


class CachedNameSource(TypedDict):
    scope: str
    provider: str
    retrieved_at: str
    support_level: str
    evidence_id: int


class CachedName(TypedDict):
    name: str
    name_source: CachedNameSource


def read_cached_names(
    connection: sqlite3.Connection,
    *,
    account_id: int,
    machine_id: str,
    appids: tuple[int, ...],
) -> dict[int, CachedName]:
    """Prefer selected-account owned names, then selected-machine observations.

    Names carry their own evidence; wishlist membership cannot certify a title.
    No arbitrary shared app name is used without an attributable name source.
    """

    if not appids:
        return {}
    # A wishlist can exceed SQLite's variable limit. Join its current bounded
    # account projection once rather than issuing one statement per AppID.
    rows = connection.execute(
        """
        SELECT o.appid AS appid, o.name AS name, 'owned' AS scope,
               e.provider, e.retrieved_at, e.support_level, e.id AS evidence_id
        FROM wishlist_current AS w
        JOIN owned_current AS o ON o.appid = w.appid AND o.account_id = w.account_id
        JOIN evidence AS e ON e.id = o.evidence_id
        WHERE w.account_id = ? AND o.name IS NOT NULL AND o.name != ''
        UNION ALL
        SELECT i.appid AS appid, obs.name AS name, 'installed' AS scope,
               e.provider, e.retrieved_at, e.support_level, e.id AS evidence_id
        FROM wishlist_current AS w
        JOIN installed_current AS i ON i.appid = w.appid AND i.machine_id = ?
        JOIN installed_observations AS obs
          ON obs.sync_run_id = i.promoted_sync_run_id
         AND obs.machine_id = i.machine_id AND obs.appid = i.appid
        JOIN evidence AS e ON e.id = i.evidence_id
        WHERE w.account_id = ? AND obs.name IS NOT NULL AND obs.name != ''
        ORDER BY appid, scope DESC
        """,
        (account_id, machine_id, account_id),
    )
    requested = set(appids)
    result: dict[int, CachedName] = {}
    for row in rows:
        if row["appid"] in requested and row["appid"] not in result:
            result[row["appid"]] = {
                "name": row["name"],
                "name_source": {
                    "scope": row["scope"],
                    "provider": row["provider"],
                    "retrieved_at": row["retrieved_at"],
                    "support_level": row["support_level"],
                    "evidence_id": row["evidence_id"],
                },
            }
    return result

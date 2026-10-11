"""Pure cached core query projections; no acquisition or execution boundary."""

from dataclasses import dataclass
from typing import Any

from steam_agent.application import installed_item
from steam_agent.contracts import (
    CompletenessStatus, ErrorCode, WarningRecord, completeness,
)
from steam_agent.storage import InstalledSnapshot


@dataclass(frozen=True, slots=True)
class InstalledQuery:
    context: dict[str, Any]
    completeness: dict[str, Any]
    data: dict[str, Any]


def installed_games_query(
    installed_snapshot: InstalledSnapshot, *, machine_id: str, include_paths: bool = False,
) -> InstalledQuery:
    """Preserve last-good and running-scan truth from one coherent snapshot."""

    games = installed_snapshot.games
    latest = installed_snapshot.latest
    latest_complete = installed_snapshot.latest_complete
    query_completeness: dict[str, Any]
    snapshot: dict[str, Any]
    if latest is None:
        query_completeness = completeness(
            CompletenessStatus.UNAVAILABLE,
            missing_capabilities=["installed.read"],
            warnings=[
                WarningRecord(
                    code=ErrorCode.NOT_SYNCED,
                    message="Installed games have not been synchronized for this machine.",
                )
            ],
        )
        snapshot = {"last_attempt_status": None, "last_successful_sync_at": None}
    elif latest.status == "complete":
        query_completeness = completeness(CompletenessStatus.COMPLETE)
        snapshot = {
            "last_attempt_status": latest.status,
            "last_successful_sync_at": latest.completed_at,
        }
    elif latest.status == "running":
        running_warning = WarningRecord(
            code=ErrorCode.SYNC_IN_PROGRESS,
            message="An installed-games synchronization is currently in progress.",
        )
        if latest_complete is None:
            query_completeness = completeness(
                CompletenessStatus.UNAVAILABLE,
                missing_capabilities=["installed.read"],
                warnings=[running_warning],
            )
            successful_at = None
        else:
            query_completeness = completeness(
                CompletenessStatus.COMPLETE,
                warnings=[running_warning],
            )
            successful_at = latest_complete.completed_at
        snapshot = {
            "last_attempt_status": latest.status,
            "last_successful_sync_at": successful_at,
        }
    elif latest_complete is None:
        query_completeness = completeness(
            CompletenessStatus.UNAVAILABLE,
            missing_capabilities=["installed.read"],
            warnings=[
                WarningRecord(
                    code=ErrorCode.PARTIAL_SCAN,
                    message="The latest scan was incomplete and no last-good snapshot exists.",
                )
            ],
        )
        snapshot = {
            "last_attempt_status": latest.status,
            "last_successful_sync_at": None,
        }
    else:
        query_completeness = completeness(
            CompletenessStatus.PARTIAL,
            stale_capabilities=["installed.read"],
            warnings=[
                WarningRecord(
                    code=ErrorCode.STALE_LAST_GOOD,
                    message="The latest scan was incomplete; results use the last-good snapshot.",
                )
            ],
        )
        snapshot = {
            "last_attempt_status": latest.status,
            "last_successful_sync_at": latest_complete.completed_at,
        }
    return InstalledQuery(
        context={"machine_id": machine_id, "scopes": ["installed"]},
        completeness=query_completeness,
        data={
            "items": [installed_item(game, include_paths=include_paths) for game in games],
            "next_cursor": None,
            "snapshot": snapshot,
        },
    )

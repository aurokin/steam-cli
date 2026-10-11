"""Reconstruct operational evidence from an already-read installed snapshot."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from steam_agent.operations_observe import (
    InstalledAttempt,
    LocalOperationBatch,
    PromotedInstalledFact,
    observe_local_operations,
)

if TYPE_CHECKING:
    from steam_agent.storage import InstalledSnapshot


def installed_attempt(snapshot: InstalledSnapshot) -> InstalledAttempt | None:
    latest = snapshot.latest
    if latest is None:
        return None
    return InstalledAttempt(
        latest.status,
        latest.completed_at or latest.started_at,
        latest.id,
    )


def operation_batch(
    snapshot: InstalledSnapshot, *, appids: tuple[int, ...], now: datetime
) -> LocalOperationBatch:
    """Retain promoted presence; infer absence only from a complete projection."""

    current = {item.appid: item for item in snapshot.games}
    facts: list[PromotedInstalledFact] = []
    latest_complete = snapshot.latest_complete
    for appid in appids:
        item = current.get(appid)
        if item is not None:
            facts.append(
                PromotedInstalledFact(
                    appid=appid,
                    presence="present",
                    observed_at=item.observed_at,
                    evidence_ids=(item.evidence_id,),
                    promoted_sync_run_id=item.promoted_sync_run_id,
                    build_id=item.build_id,
                    size_on_disk_bytes=item.size_bytes,
                    manifest_source_modified_at=item.manifest_mtime,
                )
            )
        elif latest_complete is not None:
            facts.append(
                PromotedInstalledFact(
                    appid=appid,
                    presence="absent",
                    observed_at=(
                        latest_complete.completed_at or latest_complete.started_at
                    ),
                    evidence_ids=(f"sync-run:{latest_complete.id}",),
                    promoted_sync_run_id=latest_complete.id,
                )
            )
    return observe_local_operations(
        requested_appids=appids,
        installed_facts=tuple(facts),
        generated_at=now,
        latest_attempt=installed_attempt(snapshot),
    )

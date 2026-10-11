from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from steam_agent.operations_query import installed_attempt, operation_batch
from steam_agent.storage import InstalledGame, InstalledSnapshot, SyncRun, SyncStatus


NOW = datetime(2026, 7, 15, 18, tzinfo=timezone.utc)
OBSERVED = "2026-07-15T17:55:00Z"


def run(run_id: int, status: SyncStatus, *, at: str = OBSERVED) -> SyncRun:
    return SyncRun(
        id=run_id,
        provider="local_steam",
        capability="installed",
        machine_id="local",
        account_id=None,
        started_at=at,
        completed_at=None if status == "running" else at,
        status=status,
        promoted=status == "complete",
        records_seen=1,
        error_code=None,
        error_detail=None,
    )


def game() -> InstalledGame:
    return InstalledGame(
        machine_id="local",
        appid=480,
        name="Synthetic game",
        app_type="game",
        library_root="/synthetic/library",
        install_dir="/synthetic/library/steamapps/common/game",
        state="installed",
        build_id="123",
        size_bytes=0,
        manifest_path="/synthetic/library/steamapps/appmanifest_480.acf",
        manifest_mtime="2026-07-15T17:50:00Z",
        observed_at=OBSERVED,
        evidence_id=123,
        promoted_sync_run_id=10,
    )


@pytest.mark.parametrize("status", ("partial", "failed", "running"))
def test_incomplete_latest_attempt_retains_last_good_presence_and_absence(
    status: SyncStatus,
) -> None:
    complete = run(10, "complete")
    latest = run(11, status, at="2026-07-15T17:56:00Z")
    snapshot = InstalledSnapshot((game(),), latest, complete)

    batch = operation_batch(snapshot, appids=(620, 480), now=NOW)
    present, absent = batch.items

    assert present.appid == 480
    assert present.installed.state == "present"
    assert present.installed.observed_at == NOW - timedelta(minutes=5)
    assert present.installed.evidence_ids == (123,)
    assert present.build_id.value == "123"
    assert present.size_on_disk_bytes.value == 0
    assert present.size_on_disk_bytes.state == "known"
    assert present.manifest_source_modified_at.value == "2026-07-15T17:50:00Z"
    assert absent.appid == 620
    assert absent.installed.state == "absent"
    assert absent.installed.observed_at == NOW - timedelta(minutes=5)
    assert absent.installed.evidence_ids == ("sync-run:10",)
    assert absent.build_id.unknown_reason == "not_installed"
    assert absent.size_on_disk_bytes.unknown_reason == "not_installed"
    assert all(item.installed.freshness == "stale" for item in batch.items)
    assert all(
        item.installed.freshness_reason == "newer_incomplete_attempt"
        for item in batch.items
    )
    # Private scanner paths are intentionally excluded at the query boundary.
    assert "/synthetic/" not in repr(batch.to_dict())


def test_complete_empty_projection_establishes_fresh_absence() -> None:
    complete = run(10, "complete")
    snapshot = InstalledSnapshot((), complete, complete)

    item = operation_batch(snapshot, appids=(480,), now=NOW).items[0]

    assert item.installed.state == "absent"
    assert item.installed.freshness == "fresh"
    assert item.installed.evidence_ids == ("sync-run:10",)


def test_incomplete_scan_without_complete_projection_does_not_establish_absence() -> None:
    latest = run(11, "partial")
    snapshot = InstalledSnapshot((), latest, None)

    item = operation_batch(snapshot, appids=(480,), now=NOW).items[0]

    assert item.installed.state == "unknown"
    assert item.installed.freshness == "unknown"
    assert item.installed.unknown_reason == "no_promoted_fact"
    assert item.installed.evidence_ids == ()
    assert installed_attempt(InstalledSnapshot((), None, None)) is None

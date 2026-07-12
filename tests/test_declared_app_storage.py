from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Mapping

import pytest

from steam_agent.steam_declared_facts import (
    HttpResponse,
    SteamDeclaredFactsClient,
    declared_facts_payload,
)
from steam_agent.storage import Machine, Storage


T0 = "2026-07-10T12:00:00Z"
T1 = "2026-07-10T12:01:00Z"
T2 = "2026-07-10T12:02:00Z"
T3 = "2026-07-10T12:03:00Z"
FIXTURES = Path(__file__).parent / "fixtures" / "steam_declared_facts"


class FixtureTransport:
    def __init__(self, appid: int) -> None:
        self.appid = appid

    def request(self, **_: object) -> HttpResponse:
        names = {400: "legacy_shape.json", 620: "list_shape.json"}
        return HttpResponse(
            200,
            (FIXTURES / names[self.appid]).read_bytes(),
            {"Content-Type": "application/json"},
        )


@pytest.fixture
def configured(tmp_path: Path) -> tuple[Storage, int]:
    with Storage(tmp_path / "steam-agent.sqlite3") as database:
        database.upsert_machine(
            Machine("desktop", "Gaming PC", "linux", "x86_64"), observed_at=T0
        )
        account = database.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=T0,
        )
        database.record_compatibility_data_consent(
            account_id=account.id,
            disclosure_version="m5-v1",
            accepted_at=T0,
            backups_acknowledged=True,
        )
        yield database, account.id


def payload(appid: int) -> Mapping[str, object]:
    result = SteamDeclaredFactsClient(
        transport=FixtureTransport(appid)
    ).fetch(appid, country="US", language="english")
    assert result.facts is not None
    return declared_facts_payload(result.facts)


def begin(
    storage: Storage,
    account_id: int,
    appids: list[int],
    *,
    at: str = T0,
    maximum: int = 100,
):
    return storage.begin_declared_app_sync(
        account_id=account_id,
        machine_id="desktop",
        demanded_appids=appids,
        country="US",
        language="english",
        max_items=maximum,
        skip_fresh_terminal=True,
        started_at=at,
        disclosure_version="m5-v1",
    )


def demand_rows(storage: Storage, run_id: int) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in storage._connection.execute(  # noqa: SLF001
            """SELECT appid,ordinal,targeted,evaluated,state,error_code
               FROM declared_app_sync_demand WHERE sync_run_id=? ORDER BY ordinal""",
            (run_id,),
        )
    ]


def test_v02_migration_allows_legacy_and_expanded_projection_ids(
    tmp_path: Path,
) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        sql = storage._connection.execute(  # noqa: SLF001
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='declared_app_current'"
        ).fetchone()[0]

    assert "declared-app-facts/0.1" in sql
    assert "declared-app-facts/0.2" in sql


def test_scheduler_retains_complete_ordered_demand_and_cap_reason(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured

    run, candidates, targeted = begin(storage, account_id, [620, 400], maximum=1)

    assert candidates == (400, 620)
    assert targeted == (400,)
    assert demand_rows(storage, run.id) == [
        {
            "appid": 400,
            "ordinal": 0,
            "targeted": 1,
            "evaluated": 0,
            "state": "running",
            "error_code": None,
        },
        {
            "appid": 620,
            "ordinal": 1,
            "targeted": 0,
            "evaluated": 0,
            "state": "unevaluated",
            "error_code": "MAX_ITEMS_LIMIT",
        },
    ]
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    finished = storage.finish_declared_app_sync(run.id, completed_at=T1)
    assert finished.status == "partial"
    assert finished.error_code == "MAX_ITEMS_LIMIT"


def test_fresh_and_active_skips_remain_visible_and_next_cap_slice_converges(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    first, _, _ = begin(storage, account_id, [400], at=T0)
    active_skip, candidates, targeted = begin(storage, account_id, [400], at=T1)
    assert candidates == ()
    assert targeted == ()
    assert demand_rows(storage, active_skip.id)[0]["error_code"] == "ACTIVE_REQUEST"
    active_finished = storage.finish_declared_app_sync(
        active_skip.id, completed_at=T1
    )
    assert active_finished.status == "failed"
    assert active_finished.error_code == "ACTIVE_REQUEST"

    storage.record_declared_app_result(
        first.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.finish_declared_app_sync(first.id, completed_at=T1)
    second, candidates, targeted = begin(storage, account_id, [400, 620], at=T2)

    assert candidates == (620,)
    assert targeted == (620,)
    assert demand_rows(storage, second.id)[0]["error_code"] == "FRESH_LAST_GOOD"
    assert demand_rows(storage, second.id)[1]["state"] == "running"


def test_snapshot_requires_explicit_demand_and_preserves_missing_identity(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    run, _, _ = begin(storage, account_id, [400])
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.finish_declared_app_sync(run.id, completed_at=T1)

    with pytest.raises(ValueError, match="explicit appids"):
        storage.read_declared_app_snapshot(
            account_id=account_id,
            machine_id="desktop",
            country="US",
            language="english",
        )
    snapshot = storage.read_declared_app_snapshot(
        account_id=account_id,
        machine_id="desktop",
        country="US",
        language="english",
        appids=[620, 400],
    )

    assert [item["appid"] for item in snapshot["items"]] == [400, 620]
    assert snapshot["items"][0]["facts"] is not None
    assert snapshot["items"][1] == {"appid": 620, "facts": None}
    assert snapshot["latest_demand"][0]["appid"] == 400


def test_finish_reports_partial_and_preserves_ready_last_good(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    run, _, _ = begin(storage, account_id, [400, 620])
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=620,
        state="failed",
        error_code="PROVIDER_UNAVAILABLE",
        observed_at=T1,
    )

    finished = storage.finish_declared_app_sync(run.id, completed_at=T2)

    assert finished.status == "partial"
    assert finished.promoted is True
    assert finished.error_code == "DECLARED_APP_SYNC_PARTIAL"
    assert finished.records_seen == 2


def test_contract_drift_fails_run_sets_global_cooldown_and_is_not_negative_cached(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    run, _, _ = begin(storage, account_id, [400])
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=400,
        state="failed",
        error_code="PROVIDER_RESPONSE_INVALID",
        observed_at=T1,
    )
    finished = storage.finish_declared_app_sync(run.id, completed_at=T2)

    assert finished.status == "failed"
    limit = storage._connection.execute(  # noqa: SLF001
        """SELECT cooldown_until FROM provider_request_limits
           WHERE provider='steam-store-appdetails' AND budget_scope='global'"""
    ).fetchone()
    assert limit["cooldown_until"] == "2026-07-11T12:01:00Z"
    next_run, candidates, targeted = begin(storage, account_id, [400], at=T2)
    assert candidates == ()
    assert targeted == ()
    cooldown_demand = storage._connection.execute(  # noqa: SLF001
        """SELECT error_code,retry_at FROM declared_app_sync_demand
           WHERE sync_run_id=?""",
        (next_run.id,),
    ).fetchone()
    assert dict(cooldown_demand) == {
        "error_code": "PROVIDER_COOLDOWN",
        "retry_at": "2026-07-11T12:01:00Z",
    }
    cooldown_finished = storage.finish_declared_app_sync(
        next_run.id, completed_at=T2
    )
    assert cooldown_finished.status == "failed"
    assert cooldown_finished.error_code == "PROVIDER_COOLDOWN"
    recovered, candidates, targeted = begin(
        storage, account_id, [400], at="2026-07-11T12:01:00Z"
    )
    assert candidates == (400,)
    assert targeted == (400,)
    storage.finish_declared_app_sync(
        recovered.id, completed_at="2026-07-11T12:01:01Z"
    )


def test_terminal_cache_precedes_cooldown_but_expired_negative_does_not(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    ready, _, _ = begin(storage, account_id, [400], at=T0)
    storage.record_declared_app_result(
        ready.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.finish_declared_app_sync(ready.id, completed_at=T1)
    missing, _, _ = begin(storage, account_id, [570], at=T0)
    storage.record_declared_app_result(
        missing.id,
        account_id=account_id,
        appid=570,
        state="not_found",
        observed_at=T0,
    )
    storage.finish_declared_app_sync(missing.id, completed_at=T0)
    drift, _, _ = begin(storage, account_id, [620], at=T1)
    storage.record_declared_app_result(
        drift.id,
        account_id=account_id,
        appid=620,
        state="failed",
        error_code="PROVIDER_RESPONSE_INVALID",
        observed_at=T1,
    )
    storage.finish_declared_app_sync(drift.id, completed_at=T1)

    cached, candidates, targeted = begin(storage, account_id, [400, 570], at=T2)
    assert candidates == ()
    assert targeted == ()
    assert [row["error_code"] for row in demand_rows(storage, cached.id)] == [
        "FRESH_LAST_GOOD",
        "NOT_FOUND_CACHE",
    ]

    after_negative_expiry, candidates, targeted = begin(
        storage,
        account_id,
        [570],
        at="2026-07-11T12:00:01Z",
    )
    assert candidates == ()
    assert targeted == ()
    assert demand_rows(storage, after_negative_expiry.id)[0]["error_code"] == (
        "PROVIDER_COOLDOWN"
    )


def test_success_false_is_bounded_negative_cache(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    run, _, _ = begin(storage, account_id, [570])
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=570,
        state="not_found",
        observed_at=T1,
    )
    assert storage.finish_declared_app_sync(run.id, completed_at=T1).status == "complete"

    cached, candidates, targeted = begin(storage, account_id, [570], at=T2)
    assert candidates == ()
    assert targeted == ()
    assert demand_rows(storage, cached.id)[0]["error_code"] == "NOT_FOUND_CACHE"
    assert storage.finish_declared_app_sync(cached.id, completed_at=T2).status == "complete"


def test_future_not_found_observation_is_not_used_by_earlier_sync_clock(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    future, _, _ = begin(storage, account_id, [570], at=T0)
    storage.record_declared_app_result(
        future.id,
        account_id=account_id,
        appid=570,
        state="not_found",
        observed_at=T2,
    )
    storage.finish_declared_app_sync(future.id, completed_at=T2)

    earlier, candidates, targeted = begin(storage, account_id, [570], at=T1)

    assert candidates == (570,)
    assert targeted == (570,)
    assert demand_rows(storage, earlier.id)[0]["error_code"] is None


def test_not_found_refresh_preserves_last_good_as_partial_stale_evidence(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    ready, _, _ = begin(storage, account_id, [400], at=T0)
    storage.record_declared_app_result(
        ready.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.finish_declared_app_sync(ready.id, completed_at=T1)
    refresh, _, _ = storage.begin_declared_app_sync(
        account_id=account_id,
        machine_id="desktop",
        demanded_appids=[400],
        country="US",
        language="english",
        max_items=1,
        skip_fresh_terminal=False,
        started_at=T2,
        disclosure_version="m5-v1",
    )
    storage.record_declared_app_result(
        refresh.id,
        account_id=account_id,
        appid=400,
        state="not_found",
        observed_at=T2,
    )

    finished = storage.finish_declared_app_sync(refresh.id, completed_at=T2)
    snapshot = storage.read_declared_app_snapshot(
        account_id=account_id,
        machine_id="desktop",
        country="US",
        language="english",
        appids=[400],
    )
    assert finished.status == "partial"
    assert snapshot["items"][0]["facts"] is not None
    assert snapshot["latest_demand"][0]["error_code"] == (
        "NOT_FOUND_LAST_GOOD_PRESERVED"
    )


def test_snapshot_chunks_more_than_one_thousand_explicit_appids(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    snapshot = storage.read_declared_app_snapshot(
        account_id=account_id,
        machine_id="desktop",
        country="US",
        language="english",
        appids=list(range(1, 1202)),
    )

    assert len(snapshot["items"]) == 1201
    assert snapshot["items"][0] == {"appid": 1, "facts": None}
    assert snapshot["items"][-1] == {"appid": 1201, "facts": None}


def test_scheduler_rejects_unbounded_demand(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    with pytest.raises(ValueError, match="bounded maximum"):
        begin(storage, account_id, list(range(1, 10_002)))


def test_scheduler_rejects_empty_demand(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    with pytest.raises(ValueError, match="cannot be empty"):
        begin(storage, account_id, [])


def test_only_normalized_payload_is_persisted(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    run, _, _ = begin(storage, account_id, [400])
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )

    saved = storage._connection.execute(  # noqa: SLF001
        "SELECT facts_json FROM declared_app_observations"
    ).fetchone()["facts_json"]
    decoded = json.loads(saved)
    assert set(decoded) == {
        "appid",
        "categories",
        "context",
        "controller_support",
        "drm_notice",
        "external_account_notice",
        "languages",
        "platforms",
        "requirements",
        "schema_id",
        "source",
    }
    assert "<strong>" not in saved


def test_account_deletion_removes_private_lineage_but_retains_global_public_fact(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    run, _, _ = begin(storage, account_id, [400])
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.finish_declared_app_sync(run.id, completed_at=T1)

    removed = storage.delete_declared_app_data(account_id=account_id)

    assert removed["current_removed"] == 0
    current = storage._connection.execute(  # noqa: SLF001
        "SELECT promoted_sync_run_id FROM declared_app_current WHERE appid=400"
    ).fetchone()
    assert current is not None
    assert current["promoted_sync_run_id"] is None
    assert storage._connection.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM declared_app_sync_demand WHERE account_id=?",
        (account_id,),
    ).fetchone()[0] == 0
    assert storage.remove_account("primary") is True
    assert storage._connection.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM steam_apps WHERE appid=400"
    ).fetchone()[0] == 1
    storage._prune_declared_apps("2026-08-10T12:02:00Z")  # noqa: SLF001
    assert storage._connection.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM steam_apps WHERE appid=400"
    ).fetchone()[0] == 0


def test_provider_deletion_removes_global_fact_cooldown_and_all_private_lineage(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    run, _, _ = begin(storage, account_id, [400])
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.finish_declared_app_sync(run.id, completed_at=T1)
    storage.defer_provider_requests(
        provider="steam-store-appdetails",
        budget_scope="global",
        requested_at=T1,
        retry_after_seconds=60,
    )

    removed = storage.delete_declared_app_data()

    assert removed["current_removed"] == 1
    assert removed["sync_runs_removed"] == 1
    assert storage._connection.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM provider_request_limits WHERE provider=?",
        ("steam-store-appdetails",),
    ).fetchone()[0] == 0


def test_retention_prunes_global_current_after_thirty_days(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    run, _, _ = begin(storage, account_id, [400])
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.finish_declared_app_sync(run.id, completed_at=T1)

    later, candidates, targeted = begin(
        storage, account_id, [400], at="2026-08-10T12:02:00Z"
    )

    assert candidates == (400,)
    assert targeted == (400,)
    assert storage._connection.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM declared_app_current"
    ).fetchone()[0] == 0
    storage.finish_declared_app_sync(later.id, completed_at="2026-08-10T12:03:00Z")


def test_future_dated_current_does_not_suppress_refresh_after_clock_correction(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    future = "2100-07-11T12:01:00Z"
    first, _, _ = begin(storage, account_id, [400], at=future)
    storage.record_declared_app_result(
        first.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=future,
    )
    storage.finish_declared_app_sync(first.id, completed_at=future)

    second, candidates, targeted = begin(storage, account_id, [400], at=T2)

    assert candidates == (400,)
    assert targeted == (400,)
    assert storage._connection.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM sync_runs WHERE id=?", (first.id,)
    ).fetchone()[0] == 0
    storage.record_declared_app_result(
        second.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T2,
    )
    storage.finish_declared_app_sync(second.id, completed_at=T2)
    failure, _, targeted = storage.begin_declared_app_sync(
        account_id=account_id,
        machine_id="desktop",
        demanded_appids=[400],
        country="US",
        language="english",
        max_items=1,
        skip_fresh_terminal=False,
        started_at=T3,
        disclosure_version="m5-v1",
    )
    assert targeted == (400,)
    storage.record_declared_app_result(
        failure.id,
        account_id=account_id,
        appid=400,
        state="failed",
        error_code="NETWORK_ERROR",
        observed_at=T3,
    )
    storage.finish_declared_app_sync(failure.id, completed_at=T3)

    current = storage._connection.execute(  # noqa: SLF001
        """SELECT observed_at,promoted_sync_run_id FROM declared_app_current
           WHERE appid=400 AND country='US' AND language='english'"""
    ).fetchone()
    assert dict(current) == {
        "observed_at": T2,
        "promoted_sync_run_id": second.id,
    }
    snapshot = storage.read_declared_app_snapshot(
        account_id=account_id,
        machine_id="desktop",
        country="US",
        language="english",
        appids=[400],
        as_of=T3,
    )
    assert snapshot["items"][0]["observed_at"] == T2
    assert snapshot["latest_demand"][0]["sync_run_id"] == failure.id
    assert snapshot["latest_demand"][0]["state"] == "failed"
    assert storage._connection.execute(  # noqa: SLF001
        """SELECT COUNT(*) FROM declared_app_sync_demand
           WHERE appid=400 AND country='US' AND language='english'
             AND observed_at>?""",
        (T3,),
    ).fetchone()[0] == 0
    assert storage._connection.execute(  # noqa: SLF001
        """SELECT COUNT(*) FROM declared_app_observations
           WHERE appid=400 AND country='US' AND language='english'
             AND observed_at>?""",
        (T3,),
    ).fetchone()[0] == 0


def test_future_quarantine_is_exact_and_preserves_other_subjects(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    future = "2100-07-11T12:01:00Z"
    broad, _, _ = begin(storage, account_id, [400, 620], at=future)
    for appid in (400, 620):
        storage.record_declared_app_result(
            broad.id,
            account_id=account_id,
            appid=appid,
            state="ready",
            facts=payload(appid),
            observed_at=future,
        )
    storage.finish_declared_app_sync(broad.id, completed_at=future)

    corrective, _, targeted = begin(storage, account_id, [400], at=T2)

    assert targeted == (400,)
    assert storage._connection.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM sync_runs WHERE id=?", (broad.id,)
    ).fetchone()[0] == 1
    remaining = storage._connection.execute(  # noqa: SLF001
        """SELECT appid FROM declared_app_sync_demand
           WHERE sync_run_id=? ORDER BY appid""",
        (broad.id,),
    ).fetchall()
    assert [int(row[0]) for row in remaining] == [620]
    assert storage._connection.execute(  # noqa: SLF001
        """SELECT COUNT(*) FROM declared_app_observations
           WHERE sync_run_id=? AND appid=620""",
        (broad.id,),
    ).fetchone()[0] == 1
    assert storage._connection.execute(  # noqa: SLF001
        """SELECT COUNT(*) FROM declared_app_observations
           WHERE sync_run_id=? AND appid=400""",
        (broad.id,),
    ).fetchone()[0] == 0
    assert storage._connection.execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM declared_app_current WHERE appid=400"
    ).fetchone()[0] == 0
    other_current = storage._connection.execute(  # noqa: SLF001
        """SELECT observed_at,promoted_sync_run_id FROM declared_app_current
           WHERE appid=620 AND country='US' AND language='english'"""
    ).fetchone()
    assert dict(other_current) == {
        "observed_at": future,
        "promoted_sync_run_id": broad.id,
    }
    storage.finish_declared_app_sync(corrective.id, completed_at=T2)


def test_expired_declared_rows_are_not_exposed_by_cache_reads(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    run, _, _ = begin(storage, account_id, [400])
    storage.record_declared_app_result(
        run.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.finish_declared_app_sync(run.id, completed_at=T1)

    snapshot = storage.read_declared_app_snapshot(
        account_id=account_id,
        machine_id="desktop",
        country="US",
        language="english",
        appids=[400],
        as_of="2026-08-10T12:02:00Z",
    )

    assert snapshot["items"] == ({"appid": 400, "facts": None},)
    assert snapshot["latest"] is None
    assert snapshot["latest_demand"][0]["error_code"] == "NOT_SYNCED"


def test_opening_storage_prunes_expired_declared_private_lineage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "steam-agent.sqlite3"
    old = "2020-01-01T00:00:00Z"
    with Storage(path) as storage:
        storage.upsert_machine(
            Machine("desktop", "Gaming PC", "linux", "x86_64"),
            observed_at=old,
        )
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=old,
        )
        storage.record_compatibility_data_consent(
            account_id=account.id,
            disclosure_version="m5-v1",
            accepted_at=old,
            backups_acknowledged=True,
        )
        run, _, _ = begin(storage, account.id, [400], at=old)
        storage.record_declared_app_result(
            run.id,
            account_id=account.id,
            appid=400,
            state="ready",
            facts=payload(400),
            observed_at=old,
        )
        storage.finish_declared_app_sync(run.id, completed_at=old)

    with Storage(path) as reopened:
        assert reopened._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM declared_app_current"
        ).fetchone()[0] == 0
        assert reopened._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM declared_app_sync_demand"
        ).fetchone()[0] == 0


def test_declared_retention_chunks_orphan_reclaim_to_sqlite_variable_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "steam-agent.sqlite3"
    old = "2020-01-01T00:00:00Z"
    appids = tuple(range(1, 26))
    with Storage(path) as storage:
        storage._connection.executemany(  # noqa: SLF001 - retention fixture
            """INSERT INTO steam_apps(appid,name,app_type,updated_at)
               VALUES (?,NULL,'unknown',?)""",
            ((appid, old) for appid in appids),
        )
        storage._connection.executemany(  # noqa: SLF001 - retention fixture
            """INSERT INTO declared_app_current(
                 appid,country,language,schema_id,facts_json,provider,
                 support_level,source_locator,human_reference_url,observed_at,
                 promoted_sync_run_id
               ) VALUES (?,'US','english','declared-app-facts/0.1','{}',
                 'steam_store','provisional','steam_store_appdetails',?, ?,NULL)""",
            (
                (appid, f"https://store.steampowered.com/app/{appid}/", old)
                for appid in appids
            ),
        )
        storage._connection.commit()  # noqa: SLF001
        previous = storage._connection.setlimit(  # noqa: SLF001
            sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10
        )
        try:
            storage._prune_declared_apps("2026-07-12T00:00:00Z")  # noqa: SLF001
        finally:
            storage._connection.setlimit(  # noqa: SLF001
                sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous
            )

        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM declared_app_current"
        ).fetchone()[0] == 0
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM steam_apps"
        ).fetchone()[0] == 0


def test_retention_prunes_old_private_run_while_preserving_fresh_public_fact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "steam-agent.sqlite3"
    started = "2026-06-01T00:00:00Z"
    observed = "2026-07-20T00:00:00Z"
    pruned_at = "2026-08-01T00:00:00Z"
    with Storage(path) as storage:
        storage.upsert_machine(
            Machine("desktop", "Gaming PC", "linux", "x86_64"),
            observed_at=started,
        )
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=started,
        )
        storage.record_compatibility_data_consent(
            account_id=account.id,
            disclosure_version="m5-v1",
            accepted_at=started,
            backups_acknowledged=True,
        )
        run, _, _ = begin(storage, account.id, [400], at=started)
        storage.record_declared_app_result(
            run.id,
            account_id=account.id,
            appid=400,
            state="ready",
            facts=payload(400),
            observed_at=observed,
        )
        storage.finish_declared_app_sync(run.id, completed_at=observed)

        storage._prune_declared_apps(pruned_at)  # noqa: SLF001

        current = storage._connection.execute(  # noqa: SLF001
            """SELECT observed_at,promoted_sync_run_id
               FROM declared_app_current WHERE appid=400"""
        ).fetchone()
        assert dict(current) == {
            "observed_at": observed,
            "promoted_sync_run_id": None,
        }
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM sync_runs WHERE id=?", (run.id,)
        ).fetchone()[0] == 0
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM declared_app_sync_demand WHERE sync_run_id=?",
            (run.id,),
        ).fetchone()[0] == 0
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM declared_app_observations WHERE sync_run_id=?",
            (run.id,),
        ).fetchone()[0] == 0


def test_older_observation_cannot_replace_newer_last_good(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    newest, _, _ = begin(storage, account_id, [400], at=T0)
    storage.record_declared_app_result(
        newest.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T2,
    )
    storage.finish_declared_app_sync(newest.id, completed_at=T2)
    older, _, targeted = storage.begin_declared_app_sync(
        account_id=account_id,
        machine_id="desktop",
        demanded_appids=[400],
        country="US",
        language="english",
        max_items=1,
        skip_fresh_terminal=False,
        started_at=T1,
        disclosure_version="m5-v1",
    )
    assert targeted == (400,)
    storage.record_declared_app_result(
        older.id,
        account_id=account_id,
        appid=400,
        state="ready",
        facts=payload(400),
        observed_at=T1,
    )
    storage.finish_declared_app_sync(older.id, completed_at=T2)

    current = storage._connection.execute(  # noqa: SLF001
        """SELECT observed_at,promoted_sync_run_id FROM declared_app_current
           WHERE appid=400"""
    ).fetchone()
    assert dict(current) == {
        "observed_at": T2,
        "promoted_sync_run_id": newest.id,
    }


def test_active_target_is_global_but_demand_lineage_is_account_machine_scoped(
    configured: tuple[Storage, int],
) -> None:
    storage, first_account_id = configured
    storage.upsert_machine(
        Machine("deck", "Steam Deck", "linux", "x86_64"), observed_at=T0
    )
    second = storage.configure_steam_account(
        alias="family",
        steam_id64="76561198000000001",
        configured_at=T0,
    )
    storage.record_compatibility_data_consent(
        account_id=second.id,
        disclosure_version="m5-v1",
        accepted_at=T0,
        backups_acknowledged=True,
    )
    first, _, _ = begin(storage, first_account_id, [400])
    second_run, candidates, targeted = storage.begin_declared_app_sync(
        account_id=second.id,
        machine_id="deck",
        demanded_appids=[400],
        country="US",
        language="english",
        max_items=1,
        skip_fresh_terminal=True,
        started_at=T1,
        disclosure_version="m5-v1",
    )

    assert candidates == ()
    assert targeted == ()
    assert demand_rows(storage, second_run.id)[0]["error_code"] == "ACTIVE_REQUEST"
    second_snapshot = storage.read_declared_app_snapshot(
        account_id=second.id,
        machine_id="deck",
        country="US",
        language="english",
        appids=[400],
    )
    assert second_snapshot["latest"].id == second_run.id
    first_snapshot = storage.read_declared_app_snapshot(
        account_id=first_account_id,
        machine_id="desktop",
        country="US",
        language="english",
        appids=[400],
    )
    assert first_snapshot["latest"].id == first.id


def test_narrower_later_sync_does_not_mask_per_app_attempt_lineage(
    configured: tuple[Storage, int],
) -> None:
    storage, account_id = configured
    broad, _, _ = begin(storage, account_id, [400, 620], at=T0)
    for appid in (400, 620):
        storage.record_declared_app_result(
            broad.id,
            account_id=account_id,
            appid=appid,
            state="ready",
            facts=payload(appid),
            observed_at=T1,
        )
    storage.finish_declared_app_sync(broad.id, completed_at=T1)
    narrow, _, _ = begin(storage, account_id, [400], at=T2)
    storage.finish_declared_app_sync(narrow.id, completed_at=T2)

    snapshot = storage.read_declared_app_snapshot(
        account_id=account_id,
        machine_id="desktop",
        country="US",
        language="english",
        appids=[400, 620],
    )

    assert [row["appid"] for row in snapshot["latest_demand"]] == [400, 620]
    assert snapshot["latest_demand"][0]["sync_run_id"] == narrow.id
    assert snapshot["latest_demand"][0]["error_code"] == "FRESH_LAST_GOOD"
    assert snapshot["latest_demand"][1]["sync_run_id"] == broad.id
    assert snapshot["latest_demand"][1]["state"] == "ready"

"""Sparse compatibility reads preserve authoritative inventory metadata."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from steam_agent.compatibility import CompatibilityTarget
from steam_agent.compatibility_adapter import assess_compatibility_snapshot
from steam_agent.storage import EvidenceInput, InstalledObservation, Machine, OwnedObservation, Storage


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


@pytest.fixture
def inventories(tmp_path: Path):
    with Storage(tmp_path / "fixture.sqlite3", clock=lambda: NOW) as storage:
        accounts = []
        for alias, identifier in (("primary", "76561198000000000"), ("secondary", "76561198000000001")):
            account = storage.configure_steam_account(alias=alias, steam_id64=identifier, configured_at=NOW)
            accounts.append(account)
            storage.record_owned_data_consent(account_id=account.id, disclosure_version="fixture", accepted_at=NOW, backups_acknowledged=True)
            run = storage.begin_sync(provider="steam_web_api", capability="owned.visible.read", account_id=account.id, started_at=NOW)
            storage.complete_owned_snapshot(
                run.id,
                [OwnedObservation(appid, 0, "visible_owned", NOW, f"{alias} cached {appid}") for appid in (480, 620, 999999)],
                base_retrieved_at=NOW, expanded_retrieved_at=NOW,
                base_reported_count=3, expanded_reported_count=3, completed_at=NOW,
            )
        for machine_id in ("first", "second"):
            storage.upsert_machine(Machine(machine_id, "Synthetic", "linux"), observed_at=NOW)
            run = storage.begin_sync(provider="local_steam", capability="installed", machine_id=machine_id, started_at=NOW)
            for appid in (480, 620, 999999):
                storage.record_installed_observation(
                    run.id,
                    InstalledObservation(appid, "synthetic-library", "synthetic-game", NOW, name=f"{machine_id} cached {appid}"),
                    EvidenceInput("local_steam", "installed", "fixture", "fixture", NOW, "local_observed", {"appid": appid}),
                )
            storage.finish_installed_sync(run.id, status="complete", completed_at=NOW)
        # Failed newer attempts remain metadata; they cannot replace either
        # complete projection when a sparse compatibility query is requested.
        run = storage.begin_sync(provider="local_steam", capability="installed", machine_id="first", started_at=NOW)
        storage.finish_installed_sync(run.id, status="failed", completed_at=NOW)
        yield storage, accounts


@pytest.mark.parametrize("account_index,machine", [(0, "first"), (1, "second")])
def test_compatibility_reads_exact_games_without_changing_inventory_truth(
    inventories, account_index: int, machine: str,
) -> None:
    storage, accounts = inventories
    account = accounts[account_index]
    full_owned = storage.read_owned_snapshot(account.id)
    full_installed = storage.read_installed_snapshot(machine)
    selected = (404, 480, 620)
    statements: list[str] = []
    storage._connection.set_trace_callback(statements.append)  # noqa: SLF001
    try:
        bounded = storage.read_compatibility_snapshot(account.id, machine, "US", "english", selected, NOW)
    finally:
        storage._connection.set_trace_callback(None)  # noqa: SLF001
    assert bounded.owned == replace(
        full_owned, games=tuple(game for game in full_owned.games if game.appid in selected),
        stable_game_ids_by_appid=tuple(item for item in full_owned.stable_game_ids_by_appid if item[0] in selected),
    )
    assert bounded.installed == replace(full_installed, games=tuple(game for game in full_installed.games if game.appid in selected))
    target = CompatibilityTarget("machine", machine, "linux")
    assert assess_compatibility_snapshot(bounded, target=target) == assess_compatibility_snapshot(
        replace(bounded, owned=full_owned, installed=full_installed), target=target,
    )
    assert tuple(item.appid for item in bounded.requested) == selected
    assert 999999 not in {game.appid for game in bounded.owned.games}
    assert all(account.alias in game.name for game in bounded.owned.games)
    assert all(machine in game.name for game in bounded.installed.games)
    owned_queries = [query for query in statements if "FROM owned_current" in query]
    installed_queries = [query for query in statements if "FROM installed_current AS current" in query]
    assert len(owned_queries) == len(installed_queries) == 1
    assert "appid IN (404,480,620)" in owned_queries[0]
    assert "current.appid IN (404,480,620)" in installed_queries[0]


def test_compatibility_inventory_and_identity_chunks_honor_sqlite_variable_budget(inventories) -> None:
    storage, accounts = inventories
    run = storage.begin_sync(
        provider="steam_web_api", capability="owned.visible.read",
        account_id=accounts[0].id, started_at=NOW,
    )
    storage.complete_owned_snapshot(
        run.id,
        [OwnedObservation(appid, 0, "visible_owned", NOW, f"primary cached {appid}") for appid in range(1, 41)],
        base_retrieved_at=NOW, expanded_retrieved_at=NOW,
        base_reported_count=40, expanded_reported_count=40, completed_at=NOW,
    )
    full_owned = storage.read_owned_snapshot(accounts[0].id)
    full_installed = storage.read_installed_snapshot("first")
    selected = tuple(range(1, 1002))
    previous = storage._connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 24)  # noqa: SLF001
    try:
        bounded = storage.read_compatibility_snapshot(accounts[0].id, "first", "US", "english", selected, NOW)
    finally:
        storage._connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)  # noqa: SLF001
    assert bounded.owned == replace(
        full_owned, games=tuple(game for game in full_owned.games if game.appid in selected),
        stable_game_ids_by_appid=tuple(item for item in full_owned.stable_game_ids_by_appid if item[0] in selected),
    )
    assert bounded.installed == replace(full_installed, games=tuple(game for game in full_installed.games if game.appid in selected))
    assert len(bounded.requested) == 1001


def test_deck_context_still_omits_local_inventory_while_bounding_owned(inventories) -> None:
    storage, accounts = inventories
    bounded = storage.read_compatibility_snapshot(
        accounts[0].id, "first", "US", "english", (404,), NOW, include_local_target_evidence=False,
    )
    assert bounded.owned.games == ()
    assert bounded.owned.latest_complete is not None
    assert bounded.installed.games == ()
    assert bounded.installed.latest_complete is None

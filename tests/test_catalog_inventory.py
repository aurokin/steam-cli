from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from steam_agent.catalog_inventory import CatalogSyncError, sync_catalog
from steam_agent.credentials import SecretValue
from steam_agent.steam_store_catalog import (
    CatalogApp,
    CatalogPageProvenance,
    CatalogScan,
    CatalogStream,
)
from steam_agent.storage import Storage


START = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(seconds=1)
        return result


def scan(
    stream: CatalogStream,
    *,
    hits: tuple[CatalogApp, ...],
    absent: tuple[int, ...],
    state: str = "complete",
) -> CatalogScan:
    demanded = (10, 20, 30)
    return CatalogScan(
        stream=stream,
        max_results=123,
        state=state,  # type: ignore[arg-type]
        termination="demand_boundary" if state == "complete" else "provider_error",
        demanded_appids=demanded,
        hits=hits,
        confirmed_absent_appids=absent,
        unresolved_appids=() if state == "complete" else (30,),
        pages=(
            CatalogPageProvenance(
                page_number=1,
                requested_last_appid=0,
                first_appid=1,
                last_appid=40,
                item_count=3,
                have_more_results=True,
                retrieved_at="2026-07-11T12:00:00Z",
            ),
        ),
        scanned_through_appid=40 if state == "complete" else 20,
        error_code=None if state == "complete" else "PROVIDER_UNAVAILABLE",
        retryable=state != "complete",
    )


class Client:
    def __init__(self, *, partial_non_games: bool = False) -> None:
        self.partial_non_games = partial_non_games

    def scan_demanded_apps(
        self, *, api_key: SecretValue, demanded_appids: object, stream: object
    ) -> CatalogScan:
        selected = CatalogStream(stream)
        assert tuple(demanded_appids) == (10, 20, 30)  # type: ignore[arg-type]
        if selected is CatalogStream.GAMES:
            return scan(
                selected,
                hits=(CatalogApp(10, selected, 100, 7),),
                absent=(20, 30),
            )
        return scan(
            selected,
            hits=(CatalogApp(20, selected, None, None),),
            absent=(10, 30) if not self.partial_non_games else (10,),
            state="partial" if self.partial_non_games else "complete",
        )


def test_catalog_sync_promotes_demanded_classifications(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=START,
        )
        result = sync_catalog(
            storage,
            account_id=account.id,
            machine_id="local",
            demanded_appids=[30, 10, 20],
            api_key=SecretValue("credential-long-enough"),
            client=Client(),  # type: ignore[arg-type]
            clock=Clock(),
        )
        snapshot = storage.read_catalog_snapshot({10, 20, 30})

    assert result.run.status == "complete"
    assert result.game_count == 1
    assert result.non_game_count == 1
    assert result.not_observed_count == 1
    assert [(fact.appid, fact.classification) for fact in snapshot.facts] == [
        (10, "game"),
        (20, "non_game"),
        (30, "not_observed"),
    ]
    assert snapshot.facts[0].last_modified == 100
    assert snapshot.sources[0].provider == "steam_store_api"
    assert snapshot.sources[0].streams[0].filter_context["max_results"] == 123


def test_partial_catalog_scan_preserves_last_good(tmp_path: Path) -> None:
    clock = Clock()
    with Storage(tmp_path / "db.sqlite3") as storage:
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=START,
        )
        sync_catalog(
            storage,
            account_id=account.id,
            machine_id="local",
            demanded_appids=[10, 20, 30],
            api_key=SecretValue("credential-long-enough"),
            client=Client(),  # type: ignore[arg-type]
            clock=clock,
        )
        with pytest.raises(CatalogSyncError, match="PROVIDER_UNAVAILABLE"):
            sync_catalog(
                storage,
                account_id=account.id,
                machine_id="local",
                demanded_appids=[10, 20, 30],
                api_key=SecretValue("credential-long-enough"),
                client=Client(partial_non_games=True),  # type: ignore[arg-type]
                clock=clock,
            )
        snapshot = storage.read_catalog_snapshot({10, 20, 30})

    assert [(fact.appid, fact.classification) for fact in snapshot.facts] == [
        (10, "game"),
        (20, "non_game"),
        (30, "not_observed"),
    ]
    assert snapshot.latest is not None and snapshot.latest.status == "partial"

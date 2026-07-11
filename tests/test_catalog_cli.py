from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import steam_agent.cli as cli
from steam_agent.credentials import InMemoryCredentialStore, SecretValue
from steam_agent.steam_store_catalog import (
    CatalogApp,
    CatalogPageProvenance,
    CatalogScan,
    CatalogStream,
)
from steam_agent.storage import (
    CatalogFact,
    CatalogSnapshot,
    OwnedObservation,
    Storage,
    StorageError,
    SyncRun,
)


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def invoke(argv: list[str], capsys: object) -> tuple[int, dict[str, object], str]:
    code = cli.main(argv)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


class CatalogClient:
    def scan_demanded_apps(
        self, *, api_key: SecretValue, demanded_appids: object, stream: object
    ) -> CatalogScan:
        demanded = tuple(demanded_appids)  # type: ignore[arg-type]
        selected = CatalogStream(stream)
        hit_appid = 10 if selected is CatalogStream.GAMES else 20
        hits = (
            (CatalogApp(hit_appid, selected, 100, 7),) if hit_appid in demanded else ()
        )
        return CatalogScan(
            stream=selected,
            max_results=50000,
            state="complete",
            termination="demand_boundary",
            demanded_appids=demanded,
            hits=hits,
            confirmed_absent_appids=tuple(
                appid for appid in demanded if appid != hit_appid
            ),
            unresolved_appids=(),
            pages=(
                CatalogPageProvenance(
                    page_number=1,
                    requested_last_appid=0,
                    first_appid=1,
                    last_appid=max(demanded),
                    item_count=2,
                    have_more_results=True,
                    retrieved_at="2026-07-11T12:00:00Z",
                ),
            ),
            scanned_through_appid=max(demanded),
        )


def setup_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, object]:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)
    with Storage(database) as storage:
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
        storage.upsert_credential_reference(
            provider=ref.provider,
            kind=ref.kind,
            profile_id=ref.profile_id,
            backend="os",
            configured_at=NOW,
        )
        storage.record_owned_data_consent(
            account_id=account.id,
            disclosure_version="2026-07-11.m2",
            accepted_at=NOW,
            backups_acknowledged=True,
        )
        run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account.id,
            started_at=NOW,
        )
        storage.complete_owned_snapshot(
            run.id,
            (
                OwnedObservation(10, 1, "visible_owned", NOW, "Game"),
                OwnedObservation(20, 1, "visible_owned", NOW, "Tool"),
            ),
            base_retrieved_at=NOW,
            expanded_retrieved_at=NOW,
            base_reported_count=2,
            expanded_reported_count=2,
            completed_at=NOW,
        )
    credential_store = InMemoryCredentialStore()
    credential_store.put(ref, SecretValue("credential-long-enough"))
    monkeypatch.setattr(
        cli,
        "_credential_store",
        lambda backend, backend_locator=None: credential_store,
    )
    monkeypatch.setattr(cli, "_reserve_provider_request", lambda *args: True)
    monkeypatch.setattr(
        cli, "SteamStoreCatalogClient", lambda **kwargs: CatalogClient()
    )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    return data_dir, account


def test_catalog_sync_enriches_joined_truth(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = setup_profile(tmp_path, monkeypatch)
    common = ["--data-dir", str(data_dir)]

    code, synced, stderr = invoke(common + ["sync", "catalog"], capsys)
    assert code == 0
    assert stderr == ""
    assert synced["data"]["demanded_count"] == 2  # type: ignore[index]

    code, queried, stderr = invoke(
        common + ["games", "query", "--scope", "library"], capsys
    )
    assert code == 0
    assert stderr == ""
    assert queried["completeness"]["status"] == "partial"  # installed not synced
    items = {item["appid"]: item for item in queried["data"]["items"]}  # type: ignore[index]
    assert items[10]["catalog_classification"] == "game"
    assert items[10]["app_type"] == "game"
    assert items[20]["catalog_classification"] == "non_game"
    assert items[20]["app_type"] == "non_game"
    assert items[10]["identity"]["package"] is None
    assert items[10]["game_id"].startswith("game:")
    assert queried["data"]["snapshots"]["catalog"]["sources"]  # type: ignore[index]


def test_owned_only_item_has_no_installed_app_type_evidence(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = setup_profile(tmp_path, monkeypatch)
    common = ["--data-dir", str(data_dir)]
    invoke(common + ["sync", "catalog"], capsys)

    code, queried, stderr = invoke(
        common + ["games", "query", "--scope", "library"], capsys
    )

    assert code == 0
    assert stderr == ""
    items = {item["appid"]: item for item in queried["data"]["items"]}
    assert items[10]["app_type"] == "game"
    assert items[10]["app_types"]["installed"] is None


def test_empty_catalog_demand_does_not_require_a_credential(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, result, stderr = invoke(
        ["--data-dir", str(data_dir), "sync", "catalog"], capsys
    )

    assert code == 0
    assert stderr == ""
    assert result["data"]["demanded_count"] == 0  # type: ignore[index]
    assert result["data"]["page_count"] == 0  # type: ignore[index]


def test_catalog_completeness_uses_mixed_fact_ages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = NOW - timedelta(hours=1)
    stale = NOW - timedelta(hours=25)
    latest = SyncRun(
        id=3,
        provider="steam_store_api",
        capability="catalog.application.read",
        machine_id=None,
        account_id=None,
        started_at=fresh.isoformat().replace("+00:00", "Z"),
        completed_at=fresh.isoformat().replace("+00:00", "Z"),
        status="complete",
        promoted=True,
        records_seen=2,
        error_code=None,
        error_detail=None,
    )
    facts = (
        CatalogFact(
            10,
            "stable-10",
            "game",
            None,
            None,
            fresh.isoformat().replace("+00:00", "Z"),
            1,
            3,
        ),
        CatalogFact(
            20,
            "stable-20",
            "non_game",
            None,
            None,
            stale.isoformat().replace("+00:00", "Z"),
            2,
            2,
        ),
    )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    value, metadata = cli._catalog_completeness(
        CatalogSnapshot(facts=facts, sources=(), latest=latest),
        demanded_appids={10, 20},
    )

    assert value["status"] == "partial"
    assert value["stale_capabilities"] == ["catalog.application.read"]
    assert metadata["stale_fact_count"] == 1
    assert metadata["oldest_fact_observed_at"] == stale.isoformat().replace(
        "+00:00", "Z"
    )
    assert metadata["newest_fact_observed_at"] == fresh.isoformat().replace(
        "+00:00", "Z"
    )


def test_catalog_sync_can_repair_corrupt_prior_provenance(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, _ = setup_profile(tmp_path, monkeypatch)
    common = ["--data-dir", str(data_dir)]
    assert invoke(common + ["sync", "catalog"], capsys)[0] == 0
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage._connection.execute("DELETE FROM catalog_stream_provenance")
        storage._connection.commit()
        with pytest.raises(StorageError, match="provenance"):
            storage.read_library_snapshot(
                storage.get_account("primary").id,  # type: ignore[union-attr]
                "local",
            )

    code, repaired, stderr = invoke(common + ["sync", "catalog"], capsys)

    assert code == 0
    assert stderr == ""
    assert repaired["data"]["demanded_count"] == 2  # type: ignore[index]

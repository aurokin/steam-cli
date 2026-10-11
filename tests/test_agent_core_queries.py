from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

import steam_agent.cli as cli
from steam_agent.storage import (
    EvidenceInput,
    InstalledObservation,
    Machine,
    OwnedObservation,
    Storage,
    WishlistObservation,
)


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def setup_snapshots(data_dir: Path) -> None:
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = storage.configure_steam_account(alias="primary", steam_id64="76561198000000000", configured_at=NOW)
        storage.record_owned_data_consent(account_id=account.id, disclosure_version="test", accepted_at=NOW, backups_acknowledged=True)
        storage.record_wishlist_data_consent(account_id=account.id, disclosure_version="test", accepted_at=NOW, backups_acknowledged=True)
        storage.upsert_machine(Machine("local", "Fixture", "linux"), observed_at=NOW)
        owned = storage.begin_sync(provider="steam_web_api", capability="owned.visible.read", account_id=account.id, started_at=NOW)
        storage.complete_owned_snapshot(
            owned.id,
            [OwnedObservation(10, 0, "visible_owned", NOW, "Owned title"), OwnedObservation(20, None, "visible_owned", NOW, None)],
            base_retrieved_at=NOW, expanded_retrieved_at=NOW,
            base_reported_count=2, expanded_reported_count=2, completed_at=NOW,
        )
        installed = storage.begin_sync(provider="local_steam", capability="installed", machine_id="local", started_at=NOW)
        for appid in (10, 30):
            storage.record_installed_observation(
                installed.id,
                InstalledObservation(appid, "fixture-library", "fixture-game", NOW, name=f"Installed {appid}"),
                EvidenceInput("local_steam", "installed", "fixture", "fixture", NOW, "local_observed", {"appid": appid}),
            )
        storage.finish_installed_sync(installed.id, status="complete", completed_at=NOW)
        wishlist = storage.begin_sync(provider="steam_web_api", capability="wishlist.read", account_id=account.id, started_at=NOW)
        storage.complete_wishlist_snapshot(
            wishlist.id, [WishlistObservation(appid, 0, 100, NOW) for appid in (10, 20, 30, 40)],
            item_list_retrieved_at=NOW, item_count_retrieved_at=NOW,
            item_list_reported_count=4, item_count_reported_count=4, completed_at=NOW,
        )


def invoke(data_dir: Path, args: list[str], capsys: pytest.CaptureFixture[str]):
    code = cli.main(["--data-dir", str(data_dir), "games", "query", *args])
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


@pytest.mark.parametrize("scope,expected", [("installed", [10, 30]), ("owned", [10, 20]), ("wishlist", [10, 20, 30, 40]), ("library", [10, 20, 30])])
def test_exact_filter_retains_snapshot_and_completeness_without_provider_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], scope: str, expected: list[int],
) -> None:
    setup_snapshots(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    monkeypatch.setattr(cli, "_resolve_credential", lambda *args: pytest.fail("credential accessed"))
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: pytest.fail("provider constructed"))
    _, full = invoke(tmp_path, ["--scope", scope], capsys)
    _, filtered = invoke(tmp_path, ["--scope", scope, "--appid", "30", "--appid", "10", "--appid", "10"], capsys)
    assert [item["appid"] for item in full["data"]["items"]] == expected
    assert [item["appid"] for item in filtered["data"]["items"]] == [appid for appid in expected if appid in {10, 30}]
    assert filtered["context"]["appid_filter"] == [10, 30]
    assert filtered["completeness"] == full["completeness"]
    snapshot_key = "snapshots" if scope == "library" else "snapshot"
    assert filtered["data"][snapshot_key] == full["data"][snapshot_key]


@pytest.mark.parametrize("scope", ["owned", "wishlist"])
def test_filtered_empty_is_distinct_from_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], scope: str,
) -> None:
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    _, unavailable = invoke(tmp_path, ["--scope", scope, "--appid", "99"], capsys)
    assert unavailable["data"]["items"] == []
    assert unavailable["data"]["empty"] is False
    assert unavailable["completeness"]["status"] == "unavailable"
    setup_snapshots(tmp_path)
    _, absent = invoke(tmp_path, ["--scope", scope, "--appid", "99"], capsys)
    assert absent["data"]["items"] == []
    assert absent["data"]["empty"] is True
    if scope == "owned":
        monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(days=10))
        _, stale = invoke(tmp_path, ["--scope", scope, "--appid", "99"], capsys)
        assert stale["data"]["empty"] is False
        assert stale["completeness"]["status"] == "partial"


def test_optional_cached_names_are_attributed_batched_and_unknown_stays_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_snapshots(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    statements: list[str] = []
    original = cli.read_cached_names

    def traced(connection, **kwargs):
        connection.set_trace_callback(statements.append)
        return original(connection, **kwargs)

    monkeypatch.setattr(cli, "read_cached_names", traced)
    _, plain = invoke(tmp_path, ["--scope", "wishlist"], capsys)
    assert "name" not in plain["data"]["items"][0]
    assert statements == []
    _, named = invoke(tmp_path, ["--scope", "wishlist", "--include-names"], capsys)
    items = {item["appid"]: item for item in named["data"]["items"]}
    assert items[10]["name"] == "Owned title"
    assert items[10]["name_source"]["scope"] == "owned"
    assert items[10]["name_source"]["provider"] == "steam_web_api"
    assert items[30]["name"] == "Installed 30"
    assert items[30]["name_source"]["scope"] == "installed"
    assert items[20]["name"] is None and items[20]["name_source"] is None
    assert items[40]["name"] is None and items[40]["name_source"] is None
    assert named["data"]["source"] == plain["data"]["source"]
    assert len([statement for statement in statements if statement.lstrip().startswith("SELECT")]) == 1


@pytest.mark.parametrize("appid", ["0", "-1", "4294967296"])
def test_invalid_exact_filter_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str], appid: str) -> None:
    code, result = invoke(tmp_path, ["--scope", "installed", "--appid", appid], capsys)
    assert code == 2 and result["error"]["code"] == "INVALID_ARGUMENT"


def test_help_and_cache_cli_import_do_not_import_keyring() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import steam_agent.cli; assert 'keyring' not in sys.modules"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scope", ["installed", "owned", "wishlist"])
def test_exact_filter_preserves_last_good_after_incomplete_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], scope: str,
) -> None:
    setup_snapshots(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(seconds=2))
    _, before = invoke(tmp_path, ["--scope", scope, "--appid", "10"], capsys)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.get_account("primary")
        assert account is not None
        run = storage.begin_sync(
            provider="local_steam" if scope == "installed" else "steam_web_api",
            capability={"installed": "installed", "owned": "owned.visible.read", "wishlist": "wishlist.read"}[scope],
            machine_id="local" if scope == "installed" else None,
            account_id=account.id if scope != "installed" else None,
            started_at=NOW + timedelta(seconds=1),
        )
        if scope == "installed":
            storage.finish_installed_sync(run.id, status="partial", completed_at=NOW + timedelta(seconds=2), error_code="PARTIAL_SCAN")
        elif scope == "owned":
            storage.finish_owned_sync(run.id, status="failed", completed_at=NOW + timedelta(seconds=2), error_code="PROVIDER_UNAVAILABLE")
        else:
            storage.finish_wishlist_sync(run.id, completed_at=NOW + timedelta(seconds=2), error_code="PROVIDER_UNAVAILABLE")
    _, after = invoke(tmp_path, ["--scope", scope, "--appid", "10"], capsys)
    for key in ("appid", "evidence_ids", "observed_at"):
        assert after["data"]["items"][0][key] == before["data"]["items"][0][key]
    if scope == "owned":
        assert after["data"]["items"][0]["playtime_state"] == "unknown"
    assert after["data"]["snapshot"]["last_successful_sync_at"] == before["data"]["snapshot"]["last_successful_sync_at"]
    assert after["completeness"]["status"] == "partial"

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping

import pytest

import steam_agent.cli as cli
from steam_agent.acquisition_credentials import (
    AcquisitionCredential,
    resolve_acquisition_credential,
)
from steam_agent.credentials import CredentialError, SecretValue
from steam_agent.steam_web_api import HttpResponse, SteamWebApiClient
from steam_agent.steam_activity_api import SteamActivityApiClient
from steam_agent.steam_wishlist import SteamWishlistClient
from steam_agent.storage import Storage


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
CANARY = "synthetic-ephemeral-provider-key-canary"


class Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def request(
        self, *, host: str, path: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        assert host == "api.steampowered.com"
        assert headers["x-webapi-key"] == CANARY
        assert CANARY not in path
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"synthetic transport failure {CANARY}")
        if "GetPlayerAchievements" in path:
            payload = {"playerstats": {"steamID": "76561198000000000", "gameName": "Fixture", "success": True, "achievements": [{"apiname": "FIRST", "achieved": 1, "unlocktime": 100}]}}
        elif "GetSchemaForGame" in path:
            payload = {"game": {"availableGameStats": {"achievements": [{"name": "FIRST", "displayName": "First", "description": "Fixture", "hidden": 0}]}}}
        elif "GetOwnedGames" in path:
            response = {"game_count": 1, "games": [{"appid": 10, "name": "Cached", "playtime_forever": 0}]}
        elif "GetRecentlyPlayedGames" in path:
            response = {"total_count": 1, "games": [{"appid": 10, "playtime_forever": 30, "playtime_2weeks": 5}]}
        elif "GetWishlistItemCount" in path:
            response = {"count": 1}
        else:
            response = {"items": [{"appid": 10, "priority": 0, "date_added": 100}]}
        if "GetPlayerAchievements" not in path and "GetSchemaForGame" not in path:
            payload = {"response": response}
        return HttpResponse(200, json.dumps(payload).encode())


def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage.configure_steam_account(
            alias="primary", steam_id64="76561198000000000", configured_at=NOW
        )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    monkeypatch.setattr(cli, "_reserve_provider_request", lambda *args: True)
    return data_dir


def invoke(data_dir: Path, args: list[str], capsys: pytest.CaptureFixture[str]):
    code = cli.main(["--data-dir", str(data_dir), *args])
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err
    return code, json.loads(captured.out), captured.err


@pytest.mark.parametrize("scope", ["owned", "wishlist", "activity", "achievements"])
def test_default_stored_does_not_use_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], scope: str
) -> None:
    data_dir = configured(tmp_path, monkeypatch)
    monkeypatch.setenv("STEAM_WEB_API_KEY", CANARY)
    code, result, _ = invoke(data_dir, ["sync", scope, "--acknowledge-local-storage"], capsys)
    assert code == 1
    assert result["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("scope", ["owned", "wishlist"])
def test_explicit_environment_missing_has_no_store_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], scope: str
) -> None:
    data_dir = configured(tmp_path, monkeypatch)
    monkeypatch.delenv("STEAM_WEB_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_credential_store", lambda *args: pytest.fail("store accessed"))
    monkeypatch.setattr(cli, "_steam_credential_ref", lambda *args: pytest.fail("stored locator created"))
    code, result, _ = invoke(data_dir, ["sync", scope, "--credential-source", "process-env", "--acknowledge-local-storage"], capsys)
    assert code == 1
    assert result["error"]["code"] == "CREDENTIAL_NOT_FOUND"


def test_locked_stored_source_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = configured(tmp_path, monkeypatch)
    ref = cli._steam_credential_ref(data_dir / "steam-agent.sqlite3")
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage.upsert_credential_reference(
            provider=ref.provider, kind=ref.kind, profile_id=ref.profile_id,
            backend="os", configured_at=NOW,
        )
    monkeypatch.setenv("STEAM_WEB_API_KEY", CANARY)

    def locked(*args: object) -> None:
        raise CredentialError("CREDENTIAL_STORE_LOCKED")

    monkeypatch.setattr(cli, "_credential_store", locked)
    code, result, _ = invoke(data_dir, ["sync", "owned", "--acknowledge-local-storage"], capsys)
    assert code == 1
    assert result["error"]["code"] == "CREDENTIAL_STORE_LOCKED"


@pytest.mark.parametrize("scope", ["owned", "wishlist", "activity", "achievements"])
@pytest.mark.parametrize("fail", [False, True])
def test_environment_key_reaches_only_injected_transport_without_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture, scope: str, fail: bool,
) -> None:
    data_dir = configured(tmp_path, monkeypatch)
    monkeypatch.setenv("STEAM_WEB_API_KEY", CANARY)
    monkeypatch.setattr(cli, "_credential_store", lambda *args: pytest.fail("store accessed"))
    monkeypatch.setattr(cli, "_steam_credential_ref", lambda *args: pytest.fail("stored locator created"))
    transport = Transport(fail=fail)
    if scope == "owned":
        monkeypatch.setattr(cli, "_steam_web_api_client", lambda: SteamWebApiClient(transport=transport))
    elif scope == "wishlist":
        monkeypatch.setattr(cli, "_steam_wishlist_client", lambda: SteamWishlistClient(transport=transport))
    else:
        monkeypatch.setattr(cli, "_steam_activity_client", lambda: SteamActivityApiClient(transport=transport))
    # Keep a second connection open so a real WAL remains inspectable after sync.
    with Storage(data_dir / "steam-agent.sqlite3") as observer:
        args = ["sync", scope, "--credential-source", "process-env", "--acknowledge-local-storage"]
        if scope == "achievements":
            args += ["--appid", "10"]
        code, result, _ = invoke(data_dir, args, capsys)
        assert code == (1 if fail else 0)
        if fail:
            assert result["error"]["code"] == "INTERNAL_ERROR"
        else:
            assert result["data"]["sync_status"] == "complete"
            if scope == "activity":
                assert result["data"]["owned_count"] == 1
                assert result["data"]["recent_count"] == 1
            elif scope == "achievements":
                assert result["data"]["targeted_count"] == 1
                assert result["data"]["state_counts"] == {"ready": 1}
        assert transport.calls == (1 if fail else 2)
        assert observer._connection.execute("SELECT COUNT(*) FROM credential_refs").fetchone()[0] == 0  # noqa: SLF001
        assert not (data_dir / ".credential-operation.lock").exists()
        assert CANARY not in caplog.text
        for path in data_dir.rglob("*"):
            if path.is_file():
                assert CANARY.encode() not in path.read_bytes()


def test_resolution_and_secret_formatting_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEAM_WEB_API_KEY", CANARY)
    resolved = resolve_acquisition_credential(
        "process-env", stored=lambda: AcquisitionCredential("configured", SecretValue("other"))
    )
    assert resolved.secret is not None
    assert resolved.secret.reveal() == CANARY
    assert CANARY not in str(resolved) + repr(resolved) + str(resolved.secret)

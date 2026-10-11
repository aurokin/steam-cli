from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping

import pytest

import steam_agent.cli as cli
from steam_agent.steam_web_api import HttpResponse, SteamWebApiClient
from steam_agent.storage import Storage


SID = "76561198000000000"
OTHER_SID = "76561198000000001"
NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


@pytest.fixture
def no_steam(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("explicit account setup must not access Steam files, credentials or providers")

    for name in (
        "_account_steam_root", "discover_steam_root", "discover_local_accounts",
        "_credential_store", "_steam_web_api_client", "_steam_wishlist_client",
        "_steam_activity_client",
    ):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)


def invoke(data_dir: Path, arguments: list[str], capsys: pytest.CaptureFixture[str]):
    code = cli.main(["--data-dir", str(data_dir), *arguments])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert str(data_dir) not in captured.out
    return code, json.loads(captured.out)


@pytest.mark.parametrize("explicit_root", [False, True])
def test_explicit_setup_needs_no_steam_and_preserves_identifier_opt_in(
    no_steam: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], explicit_root: bool,
) -> None:
    data_dir = tmp_path / "data"
    args = ["accounts", "configure", "--steam-id64", SID, "--alias", "primary"]
    if explicit_root:
        args += ["--steam-root", str(tmp_path / "absent-steam")]
    code, configured = invoke(data_dir, args, capsys)
    assert code == 0
    assert configured["schema_version"] == "0.1"
    assert configured["data"] == {
        "alias": "primary", "provider": "steam", "configured": True,
        "source_kind": "explicit_steam_id", "identifier_included": False,
    }
    assert SID not in json.dumps(configured)
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = storage.get_account("primary")
        assert account is not None
        assert account.provider_account_id == SID
        assert account.source_kind == "explicit_steam_id"
    code, status = invoke(data_dir, ["accounts", "status"], capsys)
    assert code == 0
    assert status["data"]["configured"] is True
    assert SID not in json.dumps(status)
    _, explicit = invoke(data_dir, ["accounts", "status", "--include-identifiers"], capsys)
    assert explicit["data"]["steam_id64"] == SID
    _, capability = invoke(data_dir, ["owned", "capability"], capsys)
    assert capability["data"]["capability"]["identity"] == "configured"
    assert capability["data"]["capability"]["probe"] == "not_checked"


@pytest.mark.parametrize("identifier", ["0", "-1", "1.0", " 123", "١٢٣", "18446744073709551616", "PRIVATE-ID-CANARY"])
def test_invalid_explicit_id_is_sanitized_before_database_access(
    no_steam: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], identifier: str,
) -> None:
    monkeypatch.setattr(cli, "Storage", lambda *args, **kwargs: pytest.fail("invalid ID opened storage"))
    code, result = invoke(tmp_path / "data", ["accounts", "configure", "--steam-id64", identifier], capsys)
    assert code == 2
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert identifier not in json.dumps(result["error"], ensure_ascii=False)


def test_explicit_setup_canonicalizes_id_and_does_not_rebind_alias(
    no_steam: None, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "data"
    args = ["accounts", "configure", "--steam-id64", "000" + SID]
    assert invoke(data_dir, args, capsys)[0] == 0
    assert invoke(data_dir, ["accounts", "configure", "--steam-id64", SID], capsys)[0] == 0
    code, conflict = invoke(data_dir, ["accounts", "configure", "--steam-id64", OTHER_SID], capsys)
    assert code == 1
    assert conflict["error"]["code"] == "ACCOUNT_CONFLICT"
    assert SID not in json.dumps(conflict)
    assert OTHER_SID not in json.dumps(conflict)
    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        accounts = storage.list_accounts()
        assert len(accounts) == 1
        assert accounts[0].provider_account_id == SID


def test_explicit_setup_then_api_sync_and_cached_query_without_steam(
    no_steam: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "data"
    assert invoke(data_dir, ["accounts", "configure", "--steam-id64", SID], capsys)[0] == 0
    canary = "synthetic-steam-free-key-canary"

    class Transport:
        calls = 0

        def request(self, *, host: str, path: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
            assert host == "api.steampowered.com"
            assert "GetOwnedGames" in path
            assert headers["x-webapi-key"] == canary
            assert canary not in path
            self.calls += 1
            return HttpResponse(200, json.dumps({"response": {
                "game_count": 1,
                "games": [{"appid": 480, "name": "Synthetic game", "playtime_forever": 0}],
            }}).encode())

    transport = Transport()
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: SteamWebApiClient(transport=transport))
    monkeypatch.setattr(cli, "_reserve_provider_request", lambda *args: True)
    monkeypatch.setenv("STEAM_WEB_API_KEY", canary)
    code, synced = invoke(data_dir, [
        "sync", "owned", "--credential-source", "process-env", "--acknowledge-local-storage",
    ], capsys)
    assert code == 0
    assert synced["data"]["sync_status"] == "complete"
    assert transport.calls == 2
    code, games = invoke(data_dir, ["games", "query", "--scope", "owned", "--appid", "480"], capsys)
    assert code == 0
    assert games["data"]["items"][0]["appid"] == 480
    assert transport.calls == 2
    assert canary not in json.dumps(synced) + json.dumps(games)
    assert SID not in json.dumps(synced) + json.dumps(games)

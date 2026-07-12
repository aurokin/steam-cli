from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

import steam_agent.cli as cli
from steam_agent.credentials import InMemoryCredentialStore, SecretValue
from steam_agent.steam_activity_api import ActivityAcquisition, ActivityGame, ActivityList
from steam_agent.storage import Storage


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


class Client:
    calls = 0

    def fetch_activity(self, **_: object) -> ActivityAcquisition:
        self.calls += 1
        game = ActivityGame(10, 30, None, None, None, None, None, None, 1_720_000_000)
        return ActivityAcquisition(ActivityList("ready", (game,), 1), ActivityList("ready", (), 0))


def invoke(tmp_path, capsys, *args: str):
    code = cli.main(["--data-dir", str(tmp_path), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def configure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "steam-agent.sqlite3"
    ref = cli._steam_credential_ref(database)
    with Storage(database) as storage:
        storage.configure_steam_account(alias="primary", steam_id64="76561198000000000", configured_at=NOW)
        storage.upsert_credential_reference(provider=ref.provider, kind=ref.kind, profile_id=ref.profile_id, backend="os", configured_at=NOW)
    secrets = InMemoryCredentialStore()
    secrets.put(ref, SecretValue("credential-long-enough"))
    monkeypatch.setattr(cli, "_credential_store", lambda *args, **kwargs: secrets)
    monkeypatch.setattr(cli, "_reserve_provider_request", lambda *args: True)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)


def test_activity_sync_requires_new_disclosure_and_query_is_redacted(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(tmp_path, monkeypatch)
    client = Client()
    monkeypatch.setattr(cli, "_steam_activity_client", lambda: client)
    code, blocked, _ = invoke(tmp_path, capsys, "sync", "activity")
    assert code == 1 and blocked["error"]["code"] == "DATA_POLICY_ACKNOWLEDGMENT_REQUIRED"
    assert client.calls == 0
    code, synced, _ = invoke(tmp_path, capsys, "sync", "activity", "--acknowledge-local-storage")
    assert code == 0 and synced["data"]["owned_count"] == 1
    code, queried, _ = invoke(tmp_path, capsys, "activity", "query")
    assert code == 0 and queried["data"]["items"][0]["appid"] == 10
    encoded = json.dumps(queried)
    assert "765611" not in encoded and str(tmp_path) not in encoded


def test_fresh_achievement_query_is_truthfully_unavailable(tmp_path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(tmp_path, monkeypatch)
    code, result, _ = invoke(tmp_path, capsys, "achievements", "query")
    assert code == 0
    assert result["completeness"]["status"] == "unavailable"
    assert result["completeness"]["missing_capabilities"] == ["achievements.read"]

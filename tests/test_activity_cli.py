from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

import steam_agent.cli as cli
from steam_agent.activity import ACTIVITY_DISCLOSURE_VERSION
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


def test_stale_activity_is_partial_in_json_and_table(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_steam_activity_client", Client)
    code, _, _ = invoke(
        tmp_path, capsys, "sync", "activity", "--acknowledge-local-storage"
    )
    assert code == 0
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW + timedelta(hours=7))
    code, queried, _ = invoke(tmp_path, capsys, "activity", "query")
    assert code == 0
    assert queried["completeness"]["status"] == "partial"
    assert queried["completeness"]["stale_capabilities"] == ["activity.read"]
    assert queried["completeness"]["warnings"][0]["code"] == "STALE_LAST_GOOD"

    code = cli.main(
        [
            "--data-dir",
            str(tmp_path),
            "activity",
            "query",
            "--format",
            "table",
        ]
    )
    table = capsys.readouterr()
    assert code == 0 and table.err == ""
    assert table.out.startswith("COMPLETENESS\tpartial\nWARNING\tSTALE_LAST_GOOD\t")


def test_partial_achievement_subjects_are_not_reported_complete(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path, monkeypatch)
    database = tmp_path / "steam-agent.sqlite3"
    with Storage(database) as storage:
        account = storage.get_account("primary")
        assert account is not None
        storage.record_activity_data_consent(
            account_id=account.id,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
            accepted_at=NOW,
            backups_acknowledged=True,
        )
        run = storage.begin_achievement_sync(
            account_id=account.id,
            candidates=(10, 20),
            targeted=(10,),
            started_at=NOW,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.record_achievement_result(
            run.id,
            account_id=account.id,
            appid=10,
            state="failed",
            player=(),
            schema_state="achievements_not_supported",
            schema=(),
            observed_at=NOW,
            error_code="PROVIDER_UNAVAILABLE",
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
        storage.finish_achievement_sync(run.id, completed_at=NOW)

    code, queried, _ = invoke(tmp_path, capsys, "achievements", "query")
    assert code == 0
    assert queried["completeness"]["status"] == "partial"
    assert queried["completeness"]["stale_capabilities"] == ["achievements.read"]
    assert queried["completeness"]["warnings"] == [
        {
            "code": "PARTIAL_SCAN",
            "message": "Achievement evidence is unavailable for some requested subjects.",
        }
    ]
    assert [item["state"] for item in queried["data"]["items"]] == [
        "failed",
        "unevaluated",
    ]

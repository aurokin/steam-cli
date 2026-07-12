from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import steam_agent.cli as cli
from steam_agent.steam_declared_facts import (
    HttpResponse,
    SteamDeclaredFactsClient,
    SteamDeclaredFactsError,
)
from steam_agent.storage import Machine, OwnedObservation, Storage


NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "steam_declared_facts" / "legacy_shape.json"


class FixtureTransport:
    def request(self, **_: object) -> HttpResponse:
        return HttpResponse(
            200, FIXTURE.read_bytes(), {"Content-Type": "application/json"}
        )


class FailingClient:
    def __init__(self, code: str = "PROVIDER_RESPONSE_INVALID") -> None:
        self.calls = 0
        self.code = code

    def fetch(self, *_: object, **__: object):
        self.calls += 1
        raise SteamDeclaredFactsError(
            self.code, retryable=self.code == "RATE_LIMITED"
        )


class InterruptingClient:
    def fetch(self, *_: object, **__: object):
        raise KeyboardInterrupt


def invoke(tmp_path: Path, capsys, *args: str):
    arguments = list(args)
    if arguments[:2] == ["sync", "compatibility"] and "--language" not in arguments:
        arguments.extend(("--language", "english"))
    code = cli.main(["--data-dir", str(tmp_path), *arguments])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def configure(tmp_path: Path) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine("desktop", "Desktop", "linux", "x86_64"), observed_at=NOW
        )
        account = storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
        storage.record_owned_data_consent(
            account_id=account.id,
            disclosure_version="owned-visible-v1",
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
                OwnedObservation(
                    400, 0, "visible_owned", NOW, "Fixture Game"
                ),
            ),
            base_retrieved_at=NOW,
            expanded_retrieved_at=NOW,
            base_reported_count=1,
            expanded_reported_count=1,
            completed_at=NOW,
        )


def test_sync_requires_disclosure_then_persists_normalized_cache_only(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    client = SteamDeclaredFactsClient(transport=FixtureTransport())
    calls: list[int] = []
    original_fetch = client.fetch

    def fetch(appid: int, **context: str):
        calls.append(appid)
        return original_fetch(appid, **context)

    monkeypatch.setattr(client, "fetch", fetch)
    monkeypatch.setattr(cli, "_declared_facts_client", lambda: client)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, blocked, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--appid",
        "400",
        "--machine",
        "desktop",
        "--country",
        "US",
    )
    assert code == 1
    assert blocked["error"]["code"] == "DATA_POLICY_ACKNOWLEDGMENT_REQUIRED"
    assert calls == []

    code, synced, error = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--appid",
        "400",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--acknowledge-local-storage",
    )
    assert code == 0 and error == ""
    assert synced["completeness"]["status"] == "complete"
    assert synced["data"]["items"][0]["facts"]["appid"] == 400
    assert calls == [400]
    encoded = json.dumps(synced)
    assert "<strong>" not in encoded

    code, cached, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--appid",
        "400",
        "--machine",
        "desktop",
        "--country",
        "US",
    )
    assert code == 0
    assert cached["data"]["targeted"] == []
    assert cached["data"]["demand"][0]["error_code"] == "FRESH_LAST_GOOD"
    assert calls == [400]

    code, confirmation, _ = invoke(
        tmp_path,
        capsys,
        "data",
        "delete",
        "--provider",
        "steam-store-appdetails",
        "--account",
        "primary",
    )
    assert code == 1
    assert confirmation["error"]["code"] == "CONFIRMATION_REQUIRED"
    code, account_delete, _ = invoke(
        tmp_path,
        capsys,
        "data",
        "delete",
        "--provider",
        "steam-store-appdetails",
        "--account",
        "primary",
        "--yes",
    )
    assert code == 0
    assert account_delete["data"]["global_public_current_preserved"] is True
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM declared_app_current"
        ).fetchone()[0] == 1
    code, provider_delete, _ = invoke(
        tmp_path,
        capsys,
        "data",
        "delete",
        "--provider",
        "steam-store-appdetails",
        "--all",
        "--yes",
    )
    assert code == 0
    assert provider_delete["data"]["global_public_current_preserved"] is False
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM declared_app_current"
        ).fetchone()[0] == 0


def test_contract_drift_disables_transport_until_retry_time(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    client = FailingClient()
    monkeypatch.setattr(cli, "_declared_facts_client", lambda: client)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, failed, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--appid",
        "400",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--acknowledge-local-storage",
    )
    assert code == 0
    assert failed["completeness"]["status"] == "unavailable"
    assert client.calls == 1

    code, cooldown, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--appid",
        "400",
        "--machine",
        "desktop",
        "--country",
        "US",
    )
    assert code == 0
    assert cooldown["data"]["targeted"] == []
    assert cooldown["data"]["demand"][0]["error_code"] == "PROVIDER_COOLDOWN"
    assert cooldown["data"]["demand"][0]["retry_at"] == "2026-07-13T12:00:00Z"
    assert client.calls == 1


def test_unsynced_owned_scope_is_truthfully_unavailable_without_provider_call(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine("desktop", "Desktop", "linux", "x86_64"), observed_at=NOW
        )
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=NOW,
        )
    client = FailingClient()
    monkeypatch.setattr(cli, "_declared_facts_client", lambda: client)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, result, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--machine",
        "desktop",
        "--country",
        "US",
    )

    assert code == 0
    assert result["completeness"]["status"] == "unavailable"
    assert result["completeness"]["missing_capabilities"] == ["owned.visible.read"]
    assert result["completeness"]["warnings"][0]["code"] == "NOT_SYNCED"
    assert client.calls == 0


def test_stale_owned_last_good_makes_sync_partial(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage._connection.execute(  # noqa: SLF001
            """UPDATE sync_runs SET started_at=?,completed_at=?
               WHERE capability='owned.visible.read'""",
            ("2026-07-10T00:00:00Z", "2026-07-10T00:00:01Z"),
        )
        storage._connection.commit()  # noqa: SLF001
    monkeypatch.setattr(
        cli,
        "_declared_facts_client",
        lambda: SteamDeclaredFactsClient(transport=FixtureTransport()),
    )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, result, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--acknowledge-local-storage",
    )
    assert code == 0
    assert result["completeness"]["status"] == "partial"
    assert "owned.visible.read" in result["completeness"]["stale_capabilities"]
    assert any(
        warning["code"] == "STALE_LAST_GOOD"
        for warning in result["completeness"]["warnings"]
    )


def test_keyboard_interrupt_finishes_typed_recoverable_run(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    monkeypatch.setattr(cli, "_declared_facts_client", InterruptingClient)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, result, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--acknowledge-local-storage",
    )
    assert code == 1
    assert result["error"]["code"] == "INTERNAL_ERROR"
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        row = storage._connection.execute(  # noqa: SLF001
            """SELECT status,error_code FROM sync_runs
               WHERE capability='compatibility.declared.read'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    assert dict(row) == {
        "status": "failed",
        "error_code": "DECLARED_APP_SYNC_FAILED",
    }


def test_rate_limit_without_retry_after_uses_default_persisted_backoff(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    client = FailingClient("RATE_LIMITED")
    monkeypatch.setattr(cli, "_declared_facts_client", lambda: client)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    code, _, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--acknowledge-local-storage",
    )
    assert code == 0
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        retry_at = storage._connection.execute(  # noqa: SLF001
            """SELECT cooldown_until FROM provider_request_limits
               WHERE provider='steam-store-appdetails' AND budget_scope='global'"""
        ).fetchone()[0]
    assert retry_at == "2026-07-12T12:05:00Z"

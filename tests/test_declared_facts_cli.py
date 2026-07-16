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
FIXTURE = (
    Path(__file__).parent / "fixtures" / "steam_declared_facts" / "legacy_shape.json"
)


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
        raise SteamDeclaredFactsError(self.code, retryable=self.code == "RATE_LIMITED")


class InterruptingClient:
    def fetch(self, *_: object, **__: object):
        raise KeyboardInterrupt


def test_explicit_candidate_scope_reads_no_private_inventories() -> None:
    class NoInventoryStorage:
        def __getattr__(self, name: str):
            pytest.fail(f"unexpected private inventory read: {name}")

    assert cli._declared_scope_appids(  # noqa: SLF001
        NoInventoryStorage(),  # type: ignore[arg-type]
        account_id=1,
        machine_id="local",
        country="US",
        language="english",
        scope="appids",
        explicit=(400,),
    ) == (400,)


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
            (OwnedObservation(400, 0, "visible_owned", NOW, "Fixture Game"),),
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
        "--appid",
        "400",
        "--machine",
        "desktop",
        "--country",
        "us",
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
        "--appid",
        "400",
        "--machine",
        "desktop",
        "--country",
        "us",
        "--acknowledge-local-storage",
    )
    assert code == 0 and error == ""
    assert synced["completeness"]["status"] == "complete"
    assert synced["data"]["candidates"] == [400]
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

    code, assessed, error = invoke(
        tmp_path,
        capsys,
        "compatibility",
        "assess",
        "400",
        "--account",
        "primary",
        "--target",
        "machine:desktop",
        "--country",
        "us",
        "--language",
        "english",
    )
    assert code == 0 and error == ""
    assert assessed["context"]["country"] == "US"
    assert (
        "compatibility.declared.read"
        not in assessed["data"]["source_completeness"]["missing_capabilities"]
    )

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
        assert (
            storage._connection.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM declared_app_current"
            ).fetchone()[0]
            == 1
        )
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
        assert (
            storage._connection.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM declared_app_current"
            ).fetchone()[0]
            == 0
        )


def test_app_facts_explicit_sync_and_discovery_query_are_bounded_cache_only(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.get_account("primary")
        assert account is not None
        storage.record_compatibility_data_consent(
            account_id=account.id,
            disclosure_version="m5-declared-app-facts-v1",
            accepted_at=NOW,
            backups_acknowledged=True,
        )
    client = SteamDeclaredFactsClient(transport=FixtureTransport())
    monkeypatch.setattr(cli, "_declared_facts_client", lambda: client)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, blocked, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "app-facts",
        "--scope",
        "appids",
        "--appid",
        "400",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 1
    assert blocked["error"]["code"] == "DATA_POLICY_ACKNOWLEDGMENT_REQUIRED"

    code, synced, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "app-facts",
        "--scope",
        "appids",
        "--appid",
        "400",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
        "--acknowledge-local-storage",
    )
    assert code == 0
    assert synced["data"]["schema_id"] == "declared-app-facts/0.2"
    assert synced["context"]["scope"] == "appids"

    monkeypatch.setattr(
        cli, "_declared_facts_client", lambda: pytest.fail("query used network")
    )
    code, queried, error = invoke(
        tmp_path,
        capsys,
        "discovery",
        "query",
        "--scope",
        "appids",
        "--appid",
        "400",
        "--limit",
        "1",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
        "--require-mode",
        "online_co_op",
    )
    assert code == 0 and error == ""
    assert queried["data"]["network_used"] is False
    assert queried["data"]["items"][0]["mode_requirement"] == "unknown"
    assert queried["data"]["items"][0]["health"] == {"state": "unsupported"}
    assert queried["data"]["items"][0]["source"] == {
        "provider": "steam_store",
        "support_level": "provisional",
        "source_locator": "steam_store_appdetails",
        "human_reference_url": (
            "https://store.steampowered.com/app/400/?cc=US&l=english"
        ),
    }


def test_known_discovery_never_enumerates_another_accounts_explicit_demand(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage.configure_steam_account(
            alias="other", steam_id64="76561198000000001", configured_at=NOW
        )
    monkeypatch.setattr(
        cli,
        "_declared_facts_client",
        lambda: SteamDeclaredFactsClient(transport=FixtureTransport()),
    )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    invoke(
        tmp_path,
        capsys,
        "sync",
        "app-facts",
        "--scope",
        "appids",
        "--appid",
        "400",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
        "--acknowledge-local-storage",
    )

    code, same_context, _ = invoke(
        tmp_path,
        capsys,
        "discovery",
        "query",
        "--scope",
        "known",
        "--limit",
        "10",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 0
    assert same_context["data"]["candidate_count"] == 1

    code, result, _ = invoke(
        tmp_path,
        capsys,
        "discovery",
        "query",
        "--scope",
        "known",
        "--limit",
        "10",
        "--account",
        "other",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 0
    assert result["data"]["candidate_count"] == 0
    assert result["data"]["items"] == []


def test_discovery_reports_retained_declared_facts_as_stale(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    monkeypatch.setattr(
        cli,
        "_declared_facts_client",
        lambda: SteamDeclaredFactsClient(transport=FixtureTransport()),
    )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    assert (
        invoke(
            tmp_path,
            capsys,
            "sync",
            "app-facts",
            "--scope",
            "appids",
            "--appid",
            "400",
            "--account",
            "primary",
            "--machine",
            "desktop",
            "--country",
            "US",
            "--language",
            "english",
            "--acknowledge-local-storage",
        )[0]
        == 0
    )
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage._connection.execute(  # noqa: SLF001
            "UPDATE declared_app_current SET observed_at='2026-07-04T00:00:00Z'"
        )
        storage._connection.commit()  # noqa: SLF001

    code, result, _ = invoke(
        tmp_path,
        capsys,
        "discovery",
        "query",
        "--scope",
        "appids",
        "--appid",
        "400",
        "--limit",
        "1",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
    )

    assert code == 0
    assert result["completeness"]["status"] == "partial"
    assert result["completeness"]["stale_capabilities"] == ["discovery.declared.read"]
    assert result["data"]["items"][0]["freshness"] == "stale"


def test_discovery_reports_stale_owned_candidate_scope(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    monkeypatch.setattr(
        cli,
        "_declared_facts_client",
        lambda: SteamDeclaredFactsClient(transport=FixtureTransport()),
    )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    assert (
        invoke(
            tmp_path,
            capsys,
            "sync",
            "app-facts",
            "--scope",
            "library",
            "--account",
            "primary",
            "--machine",
            "desktop",
            "--country",
            "US",
            "--language",
            "english",
            "--acknowledge-local-storage",
        )[0]
        == 0
    )
    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        storage._connection.execute(  # noqa: SLF001
            """UPDATE sync_runs SET started_at=?, completed_at=?
               WHERE capability='owned.visible.read'""",
            ("2026-07-01T00:00:00Z", "2026-07-01T00:00:01Z"),
        )
        storage._connection.commit()  # noqa: SLF001

    code, result, _ = invoke(
        tmp_path,
        capsys,
        "discovery",
        "query",
        "--scope",
        "library",
        "--limit",
        "10",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
    )

    assert code == 0
    assert result["completeness"]["status"] == "partial"
    assert result["completeness"]["stale_capabilities"] == ["owned.visible.read"]


def test_app_facts_rechecks_bound_after_scope_expansion(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    monkeypatch.setattr(cli, "MAX_DECLARED_APP_DEMAND", 0)
    monkeypatch.setattr(
        cli, "_declared_facts_client", lambda: pytest.fail("oversized demand fetched")
    )

    code, result, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "app-facts",
        "--scope",
        "library",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
    )

    assert code == 2
    assert result["error"]["code"] == "INVALID_ARGUMENT"


def test_played_free_rows_do_not_enter_m6_known_or_library_scope(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
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
            (OwnedObservation(400, 1, "played_free", NOW, "Played Free"),),
            base_retrieved_at=NOW,
            expanded_retrieved_at=NOW,
            base_reported_count=0,
            expanded_reported_count=1,
            completed_at=NOW,
        )
    monkeypatch.setattr(
        cli, "_declared_facts_client", lambda: pytest.fail("played-free app fetched")
    )

    code, synced, _ = invoke(
        tmp_path,
        capsys,
        "sync",
        "app-facts",
        "--scope",
        "library",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 0
    assert synced["data"]["items"] == []

    code, queried, _ = invoke(
        tmp_path,
        capsys,
        "discovery",
        "query",
        "--scope",
        "known",
        "--limit",
        "10",
        "--account",
        "primary",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
    )
    assert code == 0
    assert queried["data"]["candidate_count"] == 0
    assert queried["completeness"]["status"] == "complete"


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


@pytest.mark.parametrize(
    "invalid_arguments",
    (
        ("--appid", "0"),
        ("--appid", "4294967296"),
        ("--language", "not-a-steam-language"),
        ("--max-items", "0"),
        ("--max-items", "101"),
    ),
)
def test_sync_validates_bounded_contract_before_unsynced_owned_return(
    tmp_path: Path,
    capsys,
    invalid_arguments: tuple[str, str],
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

    arguments = [
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--machine",
        "desktop",
        "--country",
        "US",
        "--language",
        "english",
        *invalid_arguments,
    ]
    # Replace the default when the invalid case is itself a language.
    if invalid_arguments[0] == "--language":
        del arguments[arguments.index("--language") : arguments.index("--language") + 2]
    code, result, _ = invoke(tmp_path, capsys, *arguments)

    assert code == 2
    assert result["error"]["code"] == "INVALID_ARGUMENT"


def test_declared_sync_source_gaps_are_classified_per_appid() -> None:
    gaps = cli._declared_sync_source_gaps(  # noqa: SLF001
        {
            "items": (
                {"appid": 400, "facts": {"schema_id": "declared-app-facts/0.1"}},
                {"appid": 620, "facts": None},
                {"appid": 730, "facts": {"schema_id": "declared-app-facts/0.1"}},
            ),
            "latest_demand": (
                {"appid": 400, "state": "ready", "error_code": None},
                {"appid": 620, "state": "failed", "error_code": "RATE_LIMITED"},
                {"appid": 730, "state": "failed", "error_code": "RATE_LIMITED"},
            ),
        }
    )

    assert gaps == [
        {"appid": 400, "missing_capabilities": [], "stale_capabilities": []},
        {
            "appid": 620,
            "missing_capabilities": ["compatibility.declared.read"],
            "stale_capabilities": [],
        },
        {
            "appid": 730,
            "missing_capabilities": [],
            "stale_capabilities": ["compatibility.declared.read"],
        },
    ]


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


@pytest.mark.parametrize("superseded", [False, True])
def test_empty_owned_last_good_retains_stale_or_superseded_completeness(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
    superseded: bool,
) -> None:
    path = tmp_path / "steam-agent.sqlite3"
    with Storage(path) as storage:
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
        complete = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account.id,
            started_at="2026-07-10T00:00:00Z" if not superseded else NOW,
        )
        storage.complete_owned_snapshot(
            complete.id,
            (),
            base_retrieved_at=NOW,
            expanded_retrieved_at=NOW,
            base_reported_count=0,
            expanded_reported_count=0,
            completed_at=("2026-07-10T00:00:01Z" if not superseded else NOW),
        )
        if superseded:
            failed = storage.begin_sync(
                provider="steam_web_api",
                capability="owned.visible.read",
                account_id=account.id,
                started_at="2026-07-12T12:00:01Z",
            )
            storage.finish_owned_sync(
                failed.id,
                status="failed",
                completed_at="2026-07-12T12:00:02Z",
                error_code="PROVIDER_UNAVAILABLE",
            )
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    code, result, error = invoke(
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
    assert error == ""
    assert result["data"]["items"] == []
    assert result["completeness"]["status"] == "partial"
    assert result["completeness"]["stale_capabilities"] == ["owned.visible.read"]
    assert result["completeness"]["warnings"] == [
        {
            "code": "STALE_LAST_GOOD",
            "message": (
                "Compatibility demand came from a stale or superseded "
                "last-good visible-owned snapshot."
            ),
        }
    ]


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


def test_sync_rejects_unicode_country_case_expansion(tmp_path: Path, capsys) -> None:
    configure(tmp_path)

    code, result, stderr = invoke(
        tmp_path,
        capsys,
        "sync",
        "compatibility",
        "--scope",
        "library",
        "--machine",
        "desktop",
        "--country",
        "ß",
    )

    assert code == 2
    assert stderr == ""
    assert result["error"]["code"] == "INVALID_ARGUMENT"


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

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import warnings
from threading import Event

import pytest

import steam_agent.cli as cli
from steam_agent.credentials import (
    CredentialRef,
    InMemoryCredentialStore,
    ProtectedFileStore,
    SecretValue,
)
from steam_agent.storage import Storage


def _invoke(argv: list[str], capsys: object) -> tuple[int, dict[str, object], str]:
    code = cli.main(argv)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


def _steam_root(tmp_path: Path, *, users: str) -> Path:
    root = tmp_path / "Steam"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "loginusers.vdf").write_text(users, encoding="utf-8")
    return root


def test_account_discovery_and_configuration_are_redacted(
    tmp_path: Path, capsys: object
) -> None:
    steam_id = "76561198000000000"
    root = _steam_root(
        tmp_path,
        users=(
            '"users" { "76561198000000000" { "AccountName" "private-name" '
            '"PersonaName" "private-persona" "MostRecent" "1" } }'
        ),
    )
    common = ["--data-dir", str(tmp_path / "data")]

    code, discovered, stderr = _invoke(
        common + ["accounts", "discover", "--steam-root", str(root)], capsys
    )
    assert code == 0
    assert stderr == ""
    assert discovered["data"]["candidate_count"] == 1  # type: ignore[index]
    assert steam_id not in json.dumps(discovered)
    assert "private-name" not in json.dumps(discovered)

    code, configured, stderr = _invoke(
        common
        + [
            "accounts",
            "configure",
            "--from-local-most-recent",
            "--alias",
            "primary",
            "--steam-root",
            str(root),
        ],
        capsys,
    )
    assert code == 0
    assert stderr == ""
    assert configured["data"]["configured"] is True  # type: ignore[index]
    assert steam_id not in json.dumps(configured)

    _, redacted, _ = _invoke(common + ["accounts", "status"], capsys)
    assert steam_id not in json.dumps(redacted)
    _, explicit, _ = _invoke(
        common + ["accounts", "status", "--include-identifiers"], capsys
    )
    assert explicit["data"]["steam_id64"] == steam_id  # type: ignore[index]


def test_ambiguous_account_can_be_selected_only_after_identifier_opt_in(
    tmp_path: Path, capsys: object
) -> None:
    first = "76561198000000000"
    second = "76561198000000001"
    root = _steam_root(
        tmp_path,
        users=(
            f'"users" {{ "{first}" {{ "MostRecent" "0" }} '
            f'"{second}" {{ "MostRecent" "0" }} }}'
        ),
    )
    common = ["--data-dir", str(tmp_path / "data")]

    _, redacted, _ = _invoke(
        common + ["accounts", "discover", "--steam-root", str(root)], capsys
    )
    assert redacted["data"]["primary_selection"] == "ambiguous"  # type: ignore[index]
    assert first not in json.dumps(redacted)

    _, explicit, _ = _invoke(
        common
        + [
            "accounts",
            "discover",
            "--steam-root",
            str(root),
            "--include-identifiers",
        ],
        capsys,
    )
    assert {item["steam_id64"] for item in explicit["data"]["candidates"]} == {  # type: ignore[index]
        first,
        second,
    }

    code, configured, _ = _invoke(
        common
        + [
            "accounts",
            "configure",
            "--steam-id64",
            second,
            "--steam-root",
            str(root),
        ],
        capsys,
    )
    assert code == 0
    assert second not in json.dumps(configured)

    code, conflict, stderr = _invoke(
        common
        + [
            "accounts",
            "configure",
            "--steam-id64",
            first,
            "--steam-root",
            str(root),
        ],
        capsys,
    )
    assert code == 1
    assert stderr == ""
    assert conflict["error"]["code"] == "ACCOUNT_CONFLICT"  # type: ignore[index]


def test_auth_status_and_owned_capability_do_not_touch_network(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def network_forbidden() -> object:
        raise AssertionError("read-only capability commands must not create a client")

    monkeypatch.setattr(cli, "_steam_web_api_client", network_forbidden)
    common = ["--data-dir", str(tmp_path / "data")]

    code, auth, stderr = _invoke(
        common + ["auth", "status", "steam-web-api"], capsys
    )
    assert code == 0
    assert stderr == ""
    assert auth["data"] == {  # type: ignore[index]
        "provider": "steam-web-api",
        "configured": False,
        "state": "missing",
        "backend": None,
        "protection": None,
        "secret_included": False,
    }

    code, owned, stderr = _invoke(common + ["owned", "capability"], capsys)
    assert code == 0
    assert stderr == ""
    capability = owned["data"]["capability"]  # type: ignore[index]
    assert capability["identity"] == "missing"
    assert capability["credential"] == "missing"
    assert capability["probe"] == "not_checked"


def test_credential_references_are_scoped_per_data_directory(tmp_path: Path) -> None:
    first = cli._steam_credential_ref(tmp_path / "one" / "steam-agent.sqlite3")
    second = cli._steam_credential_ref(tmp_path / "two" / "steam-agent.sqlite3")

    assert first != second
    assert first.profile_id.startswith("data-")
    assert str(tmp_path) not in first.profile_id


def test_provider_request_budget_is_global_across_data_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = tmp_path / "global" / "steam-agent.sqlite3"
    monkeypatch.setattr(cli, "_provider_budget_database_path", lambda: budget)
    requested = datetime(2026, 7, 10, tzinfo=timezone.utc)

    assert cli._reserve_provider_request(requested)
    assert not cli._reserve_provider_request(requested)


def test_provider_budget_path_ignores_data_directory_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_credentials = tmp_path / "fixed" / "credentials"
    monkeypatch.setenv("STEAM_AGENT_DATA_DIR", str(tmp_path / "overridden-data"))
    monkeypatch.setattr(cli, "default_credential_dir", lambda: fixed_credentials)

    assert cli._provider_budget_database_path() == (
        fixed_credentials.parent / "provider-request-budget.sqlite3"
    )


@pytest.mark.skipif(cli.os.name == "nt", reason="thread assertion targets flock")
def test_credential_operation_lock_serializes_same_data_profile(tmp_path: Path) -> None:
    database = tmp_path / "data" / "steam-agent.sqlite3"
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def first() -> None:
        with cli._credential_operation_lock(database):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second() -> None:
        assert first_entered.wait(timeout=2)
        with cli._credential_operation_lock(database):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first)
        second_future = executor.submit(second)
        assert first_entered.wait(timeout=2)
        assert not second_entered.wait(timeout=0.05)
        release_first.set()
        first_future.result(timeout=2)
        second_future.result(timeout=2)
    assert second_entered.is_set()


def test_owned_invalid_account_alias_is_a_typed_argument_error(
    tmp_path: Path, capsys: object
) -> None:
    code, value, stderr = _invoke(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "owned",
            "capability",
            "--account",
            "bad alias",
        ],
        capsys,
    )
    assert code == 2
    assert stderr == ""
    assert value["error"]["code"] == "INVALID_ARGUMENT"  # type: ignore[index]


def test_auth_set_requires_hidden_interactive_input(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        cli,
        "_credential_store",
        lambda backend, backend_locator=None: InMemoryCredentialStore(),
    )

    code, value, stderr = _invoke(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "auth",
            "set",
            "steam-web-api",
        ],
        capsys,
    )
    assert code == 1
    assert stderr == ""
    assert value["error"]["code"] == "INTERACTIVE_INPUT_REQUIRED"  # type: ignore[index]


def test_auth_set_refuses_getpass_echo_fallback_without_leaking_secret(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "never-echo-this-api-key"
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        cli,
        "_credential_store",
        lambda backend, backend_locator=None: InMemoryCredentialStore(),
    )

    def unsafe_getpass(prompt: str) -> str:
        warnings.warn("unsafe fallback", cli.getpass.GetPassWarning)
        return sentinel

    monkeypatch.setattr(cli.getpass, "getpass", unsafe_getpass)
    code, value, stderr = _invoke(
        ["--data-dir", str(tmp_path / "data"), "auth", "set", "steam-web-api"],
        capsys,
    )

    assert code == 1
    assert value["error"]["code"] == "INTERACTIVE_INPUT_REQUIRED"  # type: ignore[index]
    assert sentinel not in json.dumps(value) + stderr


def test_replacing_credential_invalidates_prior_ready_probe(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    credential_ref = cli._steam_credential_ref(database)
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=now,
        )
        storage.upsert_credential_reference(
            provider="steam",
            kind="web-api-key",
            profile_id=credential_ref.profile_id,
            backend="os",
            configured_at=now,
        )
        storage.save_provider_probe(
            capability="owned.visible.read",
            account_alias="primary",
            probe_state="ready",
            checked_at=now,
            retryable=False,
        )

    credential_store = InMemoryCredentialStore()
    credential_store.put(credential_ref, SecretValue("old-secret-long-enough"))
    prompts = iter(["new-secret-long-enough", "new-secret-long-enough"])
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: credential_store
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_hidden_input", lambda prompt: next(prompts))

    code, value, stderr = _invoke(
        ["--data-dir", str(data_dir), "auth", "set", "steam-web-api"], capsys
    )
    assert code == 0
    assert stderr == ""
    assert value["data"]["validated"] is False  # type: ignore[index]
    assert credential_store.resolve(credential_ref).reveal() == (  # type: ignore[union-attr]
        "new-secret-long-enough"
    )
    with Storage(database) as storage:
        assert storage.get_provider_probe(
            capability="owned.visible.read", account_alias="primary"
        ) is None


class _RefusesDeletionStore(InMemoryCredentialStore):
    def delete(self, ref: CredentialRef) -> bool:
        return False


def test_auth_remove_keeps_metadata_when_backend_does_not_confirm_deletion(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    credential_ref = cli._steam_credential_ref(database)
    with Storage(database) as storage:
        storage.upsert_credential_reference(
            provider=credential_ref.provider,
            kind=credential_ref.kind,
            profile_id=credential_ref.profile_id,
            backend="os",
            backend_locator="test-backend",
            configured_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
    credential_store = _RefusesDeletionStore()
    credential_store.put(credential_ref, SecretValue("undeleted-secret-long"))
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: credential_store
    )

    code, value, stderr = _invoke(
        [
            "--data-dir",
            str(data_dir),
            "auth",
            "remove",
            "steam-web-api",
            "--yes",
        ],
        capsys,
    )
    assert code == 1
    assert stderr == ""
    assert value["error"]["code"] == "CREDENTIAL_DELETE_FAILED"  # type: ignore[index]
    with Storage(database) as storage:
        assert storage.get_credential_reference(
            provider=credential_ref.provider,
            kind=credential_ref.kind,
            profile_id=credential_ref.profile_id,
        ) is not None


def test_failed_credential_metadata_update_restores_previous_secret(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    credential_ref = cli._steam_credential_ref(database)
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
        storage.upsert_credential_reference(
            provider="steam",
            kind="web-api-key",
            profile_id=credential_ref.profile_id,
            backend="os",
            configured_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
        storage.save_provider_probe(
            capability="owned.visible.read",
            account_alias="primary",
            probe_state="ready",
            checked_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
            retryable=False,
        )
    credential_store = InMemoryCredentialStore()
    credential_store.put(credential_ref, SecretValue("previous-secret-long"))
    prompts = iter(["replacement-secret-long", "replacement-secret-long"])
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: credential_store
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_hidden_input", lambda prompt: next(prompts))

    def fail_metadata(*args: object, **kwargs: object) -> object:
        raise sqlite3.OperationalError("canary database failure")

    monkeypatch.setattr(Storage, "upsert_credential_and_clear_probes", fail_metadata)
    code, value, stderr = _invoke(
        ["--data-dir", str(data_dir), "auth", "set", "steam-web-api"], capsys
    )

    assert code == 1
    assert stderr == ""
    assert value["error"]["code"] == "DATABASE_ERROR"  # type: ignore[index]
    assert credential_store.resolve(credential_ref).reveal() == (  # type: ignore[union-attr]
        "previous-secret-long"
    )
    assert "canary database failure" not in json.dumps(value)
    with Storage(database) as storage:
        assert storage.get_provider_probe(
            capability="owned.visible.read", account_alias="primary"
        ).probe_state == "ready"  # type: ignore[union-attr]


def test_auth_set_cleans_file_secret_when_put_fails_after_replace(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    credential_ref = cli._steam_credential_ref(database)
    store = ProtectedFileStore(tmp_path / "credentials", approved=True)
    original_fsync = store._fsync_directory
    calls = 0

    def fail_first_directory_fsync() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated post-replace failure")
        original_fsync()

    monkeypatch.setattr(store, "_fsync_directory", fail_first_directory_fsync)
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: store
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    prompts = iter(["new-file-secret-long", "new-file-secret-long"])
    monkeypatch.setattr(cli, "_hidden_input", lambda prompt: next(prompts))

    code, value, stderr = _invoke(
        [
            "--data-dir",
            str(data_dir),
            "auth",
            "set",
            "steam-web-api",
            "--backend",
            "file",
            "--yes-file-risk",
        ],
        capsys,
    )

    assert code == 1
    assert stderr == ""
    assert value["error"]["code"] == "CREDENTIAL_WRITE_FAILED"  # type: ignore[index]
    assert store.resolve(credential_ref) is None


@dataclass
class _ProbeResult:
    probe_state: str = "ready"
    retryable: bool = False


class _RecordingClient:
    def __init__(self) -> None:
        self.calls = 0

    def probe_visible_owned_games(
        self, *, steamid: str, api_key: SecretValue
    ) -> _ProbeResult:
        self.calls += 1
        assert steamid == "76561198000000000"
        assert api_key.reveal() == "test-secret-value-long-enough"
        return _ProbeResult()


def test_owned_probe_is_explicit_and_persists_only_coarse_state(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    credential_ref = cli._steam_credential_ref(database)
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=now,
        )
        storage.upsert_credential_reference(
            provider="steam",
            kind="web-api-key",
            profile_id=credential_ref.profile_id,
            backend="os",
            configured_at=now,
        )

    credential_store = InMemoryCredentialStore()
    credential_store.put(
        credential_ref, SecretValue("test-secret-value-long-enough")
    )
    client = _RecordingClient()
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: credential_store
    )
    monkeypatch.setattr(cli, "_steam_web_api_client", lambda: client)
    monkeypatch.setattr(
        cli,
        "_provider_budget_database_path",
        lambda: tmp_path / "global" / "steam-agent.sqlite3",
    )

    common = ["--data-dir", str(data_dir)]
    code, before, _ = _invoke(common + ["owned", "capability"], capsys)
    assert code == 0
    assert client.calls == 0
    assert before["data"]["capability"]["probe"] == "not_checked"  # type: ignore[index]

    code, after, stderr = _invoke(common + ["owned", "probe"], capsys)
    assert code == 0
    assert stderr == ""
    assert client.calls == 1
    assert after["data"]["capability"]["probe"] == "ready"  # type: ignore[index]
    assert "visible_game_count" not in json.dumps(after)

    with Storage(database) as storage:
        row = storage._connection.execute("SELECT * FROM provider_probes").fetchone()
        assert set(row.keys()) == {
            "capability",
            "account_alias",
            "probe_state",
            "checked_at",
            "retryable",
        }


def test_owned_capability_expires_old_probe_and_exposes_retryability(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "steam-agent.sqlite3"
    credential_ref = cli._steam_credential_ref(database)
    checked = datetime(2026, 7, 9, tzinfo=timezone.utc)
    with Storage(database) as storage:
        storage.configure_steam_account(
            alias="primary",
            steam_id64="76561198000000000",
            configured_at=checked,
        )
        storage.upsert_credential_reference(
            provider="steam",
            kind="web-api-key",
            profile_id=credential_ref.profile_id,
            backend="os",
            configured_at=checked,
        )
        storage.save_provider_probe(
            capability="owned.visible.read",
            account_alias="primary",
            probe_state="provider_unavailable",
            checked_at=checked,
            retryable=True,
        )
    credential_store = InMemoryCredentialStore()
    credential_store.put(credential_ref, SecretValue("credential-long-enough"))
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: credential_store
    )
    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime(2026, 7, 11, tzinfo=timezone.utc),
    )

    code, value, stderr = _invoke(
        ["--data-dir", str(data_dir), "owned", "capability"], capsys
    )
    capability = value["data"]["capability"]  # type: ignore[index]
    assert code == 0
    assert stderr == ""
    assert capability["probe"] == "stale"
    assert capability["probe_retryable"] is True
    assert value["completeness"]["status"] == "unavailable"  # type: ignore[index]

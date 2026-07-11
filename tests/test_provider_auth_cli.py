from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import steam_agent.cli as cli
from steam_agent.credentials import InMemoryCredentialStore
from steam_agent.storage import Storage


def _invoke(argv: list[str], capsys: object) -> tuple[int, dict[str, object], str]:
    code = cli.main(argv)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out), captured.err


@pytest.mark.parametrize(
    "provider", ["isthereanydeal", "steamgriddb", "gg-deals"]
)
def test_third_party_key_set_and_status_are_redacted(
    provider: str,
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = f"{provider}-secret-canary-value"
    store = InMemoryCredentialStore()
    prompts = iter([secret, secret])
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: store
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_hidden_input", lambda prompt: next(prompts))
    common = ["--data-dir", str(tmp_path / "data")]

    code, configured, stderr = _invoke(
        common + ["auth", "set", provider], capsys
    )
    assert code == 0
    assert stderr == ""
    assert configured["data"]["validated"] is False  # type: ignore[index]
    assert secret not in json.dumps(configured)

    code, status, stderr = _invoke(
        common + ["auth", "status", provider], capsys
    )
    assert code == 0
    assert stderr == ""
    assert status["data"]["configured"] is True  # type: ignore[index]
    assert secret not in json.dumps(status)


def test_provider_credentials_use_distinct_keychain_references(tmp_path: Path) -> None:
    database = tmp_path / "data" / "steam-agent.sqlite3"
    refs = {
        cli._provider_credential_ref(database, spec)
        for spec in cli._CREDENTIAL_PROVIDERS.values()
    }

    assert len(refs) == len(cli._CREDENTIAL_PROVIDERS)
    assert cli._steam_credential_ref(database) in refs


def test_auth_probe_requires_a_configured_key(
    tmp_path: Path, capsys: object
) -> None:
    code, value, stderr = _invoke(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "auth",
            "probe",
            "steamgriddb",
        ],
        capsys,
    )

    assert code == 1
    assert stderr == ""
    assert value["error"]["code"] == "AUTH_REQUIRED"  # type: ignore[index]


def test_auth_probe_success_is_explicit_and_non_retaining(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    store = InMemoryCredentialStore()
    secret = "steamgriddb-secret-canary-value"
    prompts = iter([secret, secret])
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: store
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_hidden_input", lambda prompt: next(prompts))
    monkeypatch.setattr(
        cli,
        "_provider_budget_database_path",
        lambda: tmp_path / "global" / "steam-agent.sqlite3",
    )

    class Client:
        def probe(self, *, provider: str, api_key: object) -> object:
            assert provider == "steamgriddb"
            assert api_key.reveal() == secret  # type: ignore[attr-defined]
            return SimpleNamespace(state="ready", retryable=False)

    monkeypatch.setattr(cli, "_provider_auth_client", Client)
    common = ["--data-dir", str(data_dir)]
    assert _invoke(common + ["auth", "set", "steamgriddb"], capsys)[0] == 0

    code, value, stderr = _invoke(
        common + ["auth", "probe", "steamgriddb"], capsys
    )

    assert code == 0
    assert stderr == ""
    assert value["data"] == {  # type: ignore[index]
        "provider": "steamgriddb",
        "validation_state": "ready",
        "validated": True,
        "retryable": False,
        "response_retained": False,
        "secret_included": False,
    }
    assert secret not in json.dumps(value)


def test_third_party_key_changes_preserve_steam_owned_probe(
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
            configured_at="2026-07-11T00:00:00Z",
        )
        storage.save_provider_probe(
            capability="owned.visible.read",
            account_alias="primary",
            probe_state="ready",
            checked_at="2026-07-11T00:00:00Z",
            retryable=False,
        )
    store = InMemoryCredentialStore()
    prompts = iter(["itad-secret-long-enough", "itad-secret-long-enough"])
    monkeypatch.setattr(
        cli, "_credential_store", lambda backend, backend_locator=None: store
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_hidden_input", lambda prompt: next(prompts))
    common = ["--data-dir", str(data_dir)]

    assert _invoke(common + ["auth", "set", "isthereanydeal"], capsys)[0] == 0
    assert _invoke(
        common + ["auth", "remove", "isthereanydeal", "--yes"], capsys
    )[0] == 0

    with Storage(database) as storage:
        assert storage.get_provider_probe(
            capability="owned.visible.read", account_alias="primary"
        ).probe_state == "ready"  # type: ignore[union-attr]

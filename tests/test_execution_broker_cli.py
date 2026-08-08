"""Broker CLI: init, request/confirm flow, policy denial, status output."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from steam_agent.execution.broker_main import main


def _plan(operation: str = "install") -> str:
    return json.dumps(
        {
            "schema": "operation-plan/0.1",
            "operation": operation,
            "idempotency_key": "a" * 16,
            "install_dir_name": "Spacewar",
            "target": {"appid": 480, "machine_id": "herb"},
        }
    )


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    assert (
        main(
            [
                "--state-dir",
                str(state),
                "init",
                "--library",
                str(tmp_path / "library"),
                "--steamcmd",
                str(tmp_path / "steamcmd.sh"),
                "--machine-id",
                "herb",
            ]
        )
        == 0
    )
    return state


def _grant_install(state_dir: Path) -> None:
    (state_dir / "policy.toml").write_text(
        '[grants]\ninstall = "confirm"\n', encoding="utf-8"
    )


def test_init_makes_state_dir_owner_only(state_dir: Path) -> None:
    assert (state_dir.stat().st_mode & 0o777) == 0o700


def test_init_writes_deny_all_policy(state_dir: Path) -> None:
    assert 'install = "deny"' in (state_dir / "policy.toml").read_text(
        encoding="utf-8"
    )


def test_request_denied_by_default_policy(
    state_dir: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
    assert "denies" in capsys.readouterr().err


def test_request_confirm_status_flow(state_dir: Path, monkeypatch, capsys) -> None:
    _grant_install(state_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 0
    request_output = json.loads(capsys.readouterr().out)
    assert request_output["state"] == "pending_confirmation"

    assert (
        main(
            [
                "--state-dir",
                str(state_dir),
                "confirm",
                request_output["nonce"],
                "--actor",
                "discord:owner",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "authorized"

    assert main(["--state-dir", str(state_dir), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["active"]["state"] == "authorized"
    assert status["active"]["appid"] == 480


def test_unsupported_operation_rejected(state_dir: Path, monkeypatch, capsys) -> None:
    _grant_install(state_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan("uninstall")))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
    assert "not executable" in capsys.readouterr().err


def test_unsafe_install_dir_name_rejected(
    state_dir: Path, monkeypatch, capsys
) -> None:
    _grant_install(state_dir)
    plan = json.loads(_plan())
    plan["install_dir_name"] = "../../outside"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan)))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
    assert "path component" in capsys.readouterr().err


def test_reconcile_works_despite_broken_policy(state_dir: Path, capsys) -> None:
    # Recovery must never be blocked behind policy repair.
    (state_dir / "policy.toml").write_text("not valid [ toml", encoding="utf-8")
    assert main(["--state-dir", str(state_dir), "reconcile"]) == 0
    assert "actions" in capsys.readouterr().out


def test_non_object_plan_rejected(state_dir: Path, monkeypatch, capsys) -> None:
    _grant_install(state_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO('"just a string"'))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
    assert "JSON object" in capsys.readouterr().err


def test_malformed_appid_rejected(state_dir: Path, monkeypatch, capsys) -> None:
    _grant_install(state_dir)
    plan = json.loads(_plan())
    plan["target"]["appid"] = "not-an-appid"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan)))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
    assert "appid" in capsys.readouterr().err


def test_non_string_install_dir_name_rejected(
    state_dir: Path, monkeypatch, capsys
) -> None:
    _grant_install(state_dir)
    plan = json.loads(_plan())
    plan["install_dir_name"] = 123
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan)))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
    assert "string" in capsys.readouterr().err


def test_foreign_machine_id_rejected(state_dir: Path, monkeypatch, capsys) -> None:
    _grant_install(state_dir)
    plan = json.loads(_plan())
    plan["target"]["machine_id"] = "haste"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan)))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
    assert "different machine" in capsys.readouterr().err


def test_policy_revocation_dead_ends_confirmation(
    state_dir: Path, monkeypatch, capsys
) -> None:
    _grant_install(state_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    main(["--state-dir", str(state_dir), "request", "--account", "o"])
    nonce = json.loads(capsys.readouterr().out)["nonce"]

    (state_dir / "policy.toml").write_text(
        '[grants]\ninstall = "deny"\n', encoding="utf-8"
    )
    assert (
        main(["--state-dir", str(state_dir), "confirm", nonce, "--actor", "a"]) == 2
    )
    assert "denies" in capsys.readouterr().err

    assert main(["--state-dir", str(state_dir), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["active"] is None  # aborted


def test_replayed_nonce_rejected(state_dir: Path, monkeypatch, capsys) -> None:
    _grant_install(state_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    main(["--state-dir", str(state_dir), "request", "--account", "o"])
    nonce = json.loads(capsys.readouterr().out)["nonce"]
    main(["--state-dir", str(state_dir), "confirm", nonce, "--actor", "a"])
    capsys.readouterr()
    assert (
        main(["--state-dir", str(state_dir), "confirm", nonce, "--actor", "a"]) == 2
    )

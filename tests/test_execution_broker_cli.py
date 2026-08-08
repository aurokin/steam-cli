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


def test_init_is_idempotent_after_partial_state(state_dir: Path, tmp_path: Path, capsys) -> None:
    capsys.readouterr()
    assert (
        main(
            [
                "--state-dir",
                str(state_dir),
                "init",
                "--library",
                str(tmp_path / "library"),
                "--steamcmd",
                str(tmp_path / "steamcmd.sh"),
            ]
        )
        == 0
    )
    config = json.loads((state_dir / "broker.json").read_text(encoding="utf-8"))
    assert config["machine_id"] == "herb"  # omitted flag preserves identity


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


def test_reinit_over_corrupt_config_requires_machine_id(
    state_dir: Path, tmp_path: Path, capsys
) -> None:
    (state_dir / "broker.json").write_text("{partial", encoding="utf-8")
    base = [
        "--state-dir",
        str(state_dir),
        "init",
        "--library",
        str(tmp_path / "library"),
        "--steamcmd",
        str(tmp_path / "steamcmd.sh"),
    ]
    # Omitting --machine-id must not silently retarget "herb" to "local".
    assert main(base) == 2
    assert "--machine-id" in capsys.readouterr().err
    assert main([*base, "--machine-id", "herb"]) == 0
    config = json.loads((state_dir / "broker.json").read_text(encoding="utf-8"))
    assert config["machine_id"] == "herb"


def test_corrupt_broker_config_fails_cleanly(state_dir: Path, capsys) -> None:
    (state_dir / "broker.json").write_text("{partial", encoding="utf-8")
    assert main(["--state-dir", str(state_dir), "status"]) == 2
    assert "corrupt" in capsys.readouterr().err


def test_reconcile_works_despite_broken_policy(state_dir: Path, capsys) -> None:
    # Recovery must never be blocked behind policy repair.
    (state_dir / "policy.toml").write_text("not valid [ toml", encoding="utf-8")
    assert main(["--state-dir", str(state_dir), "reconcile"]) == 0
    assert "actions" in capsys.readouterr().out


def test_empty_install_dir_name_accepted_as_unspecified(
    state_dir: Path, monkeypatch, capsys
) -> None:
    _grant_install(state_dir)
    plan = json.loads(_plan())
    plan["install_dir_name"] = ""  # same as absent for the executor
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan)))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "pending_confirmation"


def test_oversized_plan_rejected(state_dir: Path, monkeypatch, capsys) -> None:
    _grant_install(state_dir)
    plan = json.loads(_plan())
    plan["padding"] = "x" * (64 * 1024)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan)))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
    assert "size limit" in capsys.readouterr().err


def test_non_object_plan_rejected(state_dir: Path, monkeypatch, capsys) -> None:
    _grant_install(state_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO('"just a string"'))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
    assert "JSON object" in capsys.readouterr().err


def test_malformed_appid_rejected(state_dir: Path, monkeypatch, capsys) -> None:
    _grant_install(state_dir)
    for bad in ("not-an-appid", 2**64, 0, True):
        plan = json.loads(_plan())
        plan["target"]["appid"] = bad
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan)))
        assert (
            main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
        )
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


def test_run_with_unreadable_policy_aborts_operation(
    state_dir: Path, monkeypatch, capsys
) -> None:
    _grant_install(state_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    main(["--state-dir", str(state_dir), "request", "--account", "o"])
    nonce = json.loads(capsys.readouterr().out)["nonce"]
    main(["--state-dir", str(state_dir), "confirm", nonce, "--actor", "a"])
    capsys.readouterr()

    # An unreadable policy must dead-end the operation like a denial, not
    # leave it (and a possibly stopped client) waiting on policy repair.
    (state_dir / "policy.toml").write_text("not valid [ toml", encoding="utf-8")
    assert main(["--state-dir", str(state_dir), "run"]) == 2
    capsys.readouterr()
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

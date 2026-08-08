"""Broker CLI: init, request/confirm flow, policy denial, status output."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from steam_agent.execution.broker_main import main
from steam_agent.execution.ledger import ExecutionLedger


def _plan(operation: str = "install") -> str:
    return json.dumps(
        {
            "schema": "operation-plan/0.1",
            "operation": operation,
            "idempotency_key": "a" * 16,
            "install_dir_name": "Spacewar",
            "target": {"appid": 480, "machine_id": "machine-a"},
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
                "machine-a",
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
    assert config["machine_id"] == "machine-a"  # omitted flag preserves identity


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
    # Omitting --machine-id must not silently retarget "machine-a" to "local".
    assert main(base) == 2
    assert "--machine-id" in capsys.readouterr().err
    assert main([*base, "--machine-id", "machine-a"]) == 0
    config = json.loads((state_dir / "broker.json").read_text(encoding="utf-8"))
    assert config["machine_id"] == "machine-a"


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


def test_missing_idempotency_key_rejected(
    state_dir: Path, monkeypatch, capsys
) -> None:
    _grant_install(state_dir)
    for bad in (None, "", "short", 123):
        plan = json.loads(_plan())
        if bad is None:
            del plan["idempotency_key"]
        else:
            plan["idempotency_key"] = bad
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan)))
        assert (
            main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 2
        )
        assert "idempotency_key" in capsys.readouterr().err


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


def _grant_install_allow(state_dir: Path, min_free_gb: int) -> None:
    (state_dir / "policy.toml").write_text(
        f'[grants]\ninstall = "allow"\n[limits]\nmin_free_gb = {min_free_gb}\n',
        encoding="utf-8",
    )


def test_allow_grant_auto_authorizes(
    state_dir: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    _grant_install_allow(state_dir, 0)
    (tmp_path / "library" / "steamapps").mkdir(parents=True)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "authorized"
    assert "nonce" not in output

    ledger = ExecutionLedger(state_dir / "ledger.sqlite3")
    operation = ledger.get(output["operation_id"])
    ledger.close()
    assert operation.confirmation_actor is not None
    assert operation.confirmation_actor.startswith("policy:")


def test_allow_floor_failure_degrades_to_confirmable_pending(
    state_dir: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    _grant_install_allow(state_dir, 10**9)  # an impossible floor
    (tmp_path / "library" / "steamapps").mkdir(parents=True)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "pending_confirmation"
    assert "floor" in output["auto_confirm_denied"]

    # The degraded row is still explicitly confirmable.
    assert (
        main(
            ["--state-dir", str(state_dir), "confirm", output["nonce"], "--actor", "o"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "authorized"


def test_allow_unmeasurable_library_degrades(
    state_dir: Path, monkeypatch, capsys
) -> None:
    # The configured library was never created: the floor cannot be
    # measured, so auto-confirmation must degrade rather than pass.
    _grant_install_allow(state_dir, 0)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    assert main(["--state-dir", str(state_dir), "request", "--account", "o"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "pending_confirmation"
    assert "measured" in output["auto_confirm_denied"]


def test_allow_revoked_before_run_aborts(
    state_dir: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    _grant_install_allow(state_dir, 0)
    (tmp_path / "library" / "steamapps").mkdir(parents=True)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    main(["--state-dir", str(state_dir), "request", "--account", "o"])
    capsys.readouterr()

    (state_dir / "policy.toml").write_text(
        '[grants]\ninstall = "deny"\n', encoding="utf-8"
    )
    assert main(["--state-dir", str(state_dir), "run"]) == 2
    assert "denies" in capsys.readouterr().err
    assert main(["--state-dir", str(state_dir), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["active"] is None  # aborted


def test_status_includes_recent_terminal_rows(
    state_dir: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    _grant_install_allow(state_dir, 0)
    (tmp_path / "library" / "steamapps").mkdir(parents=True)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    main(["--state-dir", str(state_dir), "request", "--account", "o"])
    (state_dir / "policy.toml").write_text(
        '[grants]\ninstall = "deny"\n', encoding="utf-8"
    )
    main(["--state-dir", str(state_dir), "run"])  # aborts: terminal row
    capsys.readouterr()

    assert main(["--state-dir", str(state_dir), "status", "--limit", "3"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["active"] is None
    assert status["recent"][0]["state"] == "aborted"
    assert status["recent"][0]["appid"] == 480
    assert status["recent"][0]["detail"] == "policy revoked before execution"


def test_status_negative_limit_rejected(state_dir: Path, capsys) -> None:
    assert main(["--state-dir", str(state_dir), "status", "--limit", "-1"]) == 2
    assert "non-negative" in capsys.readouterr().err


def test_policy_verb_reports_effective_policy(state_dir: Path, capsys) -> None:
    _grant_install_allow(state_dir, 25)
    assert main(["--state-dir", str(state_dir), "policy"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["grants"] == {"install": "allow"}
    assert output["limits"] == {"min_free_gb": 25}
    assert len(output["version"]) == 16


def test_policy_verb_unreadable_fails(state_dir: Path, capsys) -> None:
    (state_dir / "policy.toml").write_text("not valid [ toml", encoding="utf-8")
    assert main(["--state-dir", str(state_dir), "policy"]) == 2
    assert "TOML" in capsys.readouterr().err


def test_confirm_minted_row_confirmable_after_flip_to_allow(
    state_dir: Path, monkeypatch, capsys
) -> None:
    _grant_install(state_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO(_plan()))
    main(["--state-dir", str(state_dir), "request", "--account", "o"])
    nonce = json.loads(capsys.readouterr().out)["nonce"]

    _grant_install_allow(state_dir, 0)
    assert (
        main(["--state-dir", str(state_dir), "confirm", nonce, "--actor", "o"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "authorized"

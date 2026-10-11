"""Agent request receipts, client-only provisioning, and independently gated stopping."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from steam_agent.execution import broker_main, client_executor, ledger as ledger_module
from steam_agent.execution.executor import ExecutionReport
from steam_agent.execution.ledger import ExecutionLedger
from steam_agent.execution.policy import PolicyError, load_policy
from steam_agent.execution.runtime import (
    ProcessRecord, ProcessScan, PsutilBackend, RuntimeObservation, RuntimeObserver, StopResult,
)


@pytest.fixture
def state(tmp_path: Path, capsys) -> Path:
    library = tmp_path / "library"
    (library / "steamapps").mkdir(parents=True)
    state = tmp_path / "state"
    assert (
        broker_main.main(
            [
                "--state-dir",
                str(state),
                "init",
                "--library",
                str(library),
                "--machine-id",
                "gaming",
            ]
        )
        == 0
    )
    capsys.readouterr()
    return state


def invoke(state: Path, *args: str) -> int:
    return broker_main.main(["--state-dir", str(state), *args])


def test_pending_retry_returns_same_receipt_and_nonce(state: Path, capsys) -> None:
    (state / "policy.toml").write_text(
        '[grants]\nstop="confirm"\n[stop]\nallowed_appids=[480]\n'
    )
    assert invoke(state, "stop", "480", "--request-id", "request-0001") == 0
    first = json.loads(capsys.readouterr().out)
    assert invoke(state, "stop", "480", "--request-id", "request-0001") == 0
    second = json.loads(capsys.readouterr().out)
    assert second["operation_id"] == first["operation_id"]
    assert second["nonce"] == first["nonce"]
    assert second["reused"] is True
    assert invoke(state, "stop", "481", "--request-id", "request-0001") == 2
    assert "different plan content" in capsys.readouterr().err


def test_allow_stop_needs_no_content_or_disk_floor_and_never_replays(
    state: Path, monkeypatch, capsys
) -> None:
    (state / "policy.toml").write_text(
        '[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\n'
    )
    calls: list[int] = []

    class Runtime:
        def observe(self, appid: int) -> RuntimeObservation:
            return RuntimeObservation("absent", ())

        def stop_app(self, appid: int, *, force: bool, before_request=None) -> StopResult:
            calls.append(appid)
            return StopResult("absent", "no_attributed_process", 0, force)

    monkeypatch.setattr(client_executor, "RuntimeObserver", Runtime)
    monkeypatch.setattr(
        broker_main.ClientExecutor,
        "_lock",
        lambda _: (state / "execution.lock").open("wb"),
    )
    assert invoke(state, "stop", "480", "--request-id", "stop-0001") == 0
    first = json.loads(capsys.readouterr().out)
    assert first["outcome"] == "dispatched"
    assert invoke(state, "stop", "480", "--request-id", "stop-0001") == 0
    second = json.loads(capsys.readouterr().out)
    assert second["state"] == "dispatched"
    assert calls == [480]
    ledger = ExecutionLedger(state / "ledger.sqlite3")
    try:
        assert [row[2] for row in ledger.events(first["operation_id"])] == [
            "pending_confirmation",
            "authorized",
            "dispatch_pending",
            "dispatched",
        ]
    finally:
        ledger.close()


@pytest.mark.parametrize("gate_change", ["expiry", "revocation", "unreadable_policy"])
def test_stop_rechecks_authorization_after_slow_dispatch_scan_without_replay(
    state: Path, monkeypatch, capsys, gate_change: str,
) -> None:
    policy_path = state / "policy.toml"
    policy_path.write_text('[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\n')
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(ledger_module, "_utcnow", lambda: clock[0])

    class SlowBackend:
        scans = 0
        requests: list[int] = []

        def scan(self) -> ProcessScan:
            self.scans += 1
            # The first scan belongs to ClientExecutor; the second is the
            # runtime's fresh dispatch scan after durable intent was written.
            if self.scans == 2:
                if gate_change == "expiry":
                    clock[0] += timedelta(seconds=2)
                elif gate_change == "revocation":
                    policy_path.write_text('[grants]\nstop="deny"\n')
                else:
                    policy_path.write_text("malformed policy")
            return ProcessScan((self.read(100),))

        def read(self, pid: int) -> ProcessRecord:
            return ProcessRecord(pid, 1.0, "synthetic-game", {"SteamAppId": "480"}, True)

        def request_stop(
            self, process: ProcessRecord, *, force: bool,
            before_request: Callable[[], bool] | None = None,
        ) -> bool:
            self.requests.append(process.pid)
            return True

    backend = SlowBackend()
    runtime = RuntimeObserver(platform="linux", backend=backend)
    monkeypatch.setattr(client_executor, "RuntimeObserver", lambda: runtime)
    monkeypatch.setattr(
        broker_main.ClientExecutor, "_lock",
        lambda _: (state / "execution.lock").open("wb"),
    )
    assert invoke(
        state, "stop", "480", "--request-id", "slow-stop-001", "--request-only",
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    if gate_change == "expiry":
        clock[0] += timedelta(seconds=ledger_module.DEFAULT_EXECUTION_WINDOW_SECONDS - 1)
    assert invoke(state, "run", "--operation-id", str(receipt["operation_id"])) == 1
    first = json.loads(capsys.readouterr().out)
    assert first["outcome"] == "unconfirmed"
    assert "stop_dispatch_gate_denied" in first["detail"]
    assert backend.requests == []
    assert invoke(state, "stop", "480", "--request-id", "slow-stop-001") == 1
    retry = json.loads(capsys.readouterr().out)
    assert retry["operation_id"] == first["operation_id"]
    assert retry["state"] == "unconfirmed"
    assert backend.scans == 2
    ledger = ExecutionLedger(state / "ledger.sqlite3")
    try:
        assert [row[2] for row in ledger.events(first["operation_id"])] == [
            "pending_confirmation", "authorized", "dispatch_pending", "unconfirmed",
        ]
    finally:
        ledger.close()


@pytest.mark.parametrize("change", [
    "expiry", "revocation", "unreadable_policy", "owner_changed", "close_failure",
])
def test_windows_partial_native_stop_is_audited_and_never_replayed(
    state: Path, monkeypatch, capsys, change: str,
) -> None:
    policy_path = state / "policy.toml"
    policy_path.write_text('[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\n')
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(ledger_module, "_utcnow", lambda: clock[0])
    closed: list[int] = []
    record = ProcessRecord(100, 1.0, "synthetic-game", {"SteamAppId": "480"}, True)

    class Windows:
        def windows(self) -> tuple[int, ...]:
            return (10, 12)

        def owner(self, window: int) -> int:
            return 999 if closed and window == 12 and change == "owner_changed" else 100

        def close(self, window: int) -> bool:
            if window == 12 and change == "close_failure":
                return False
            closed.append(window)
            if change == "expiry":
                clock[0] += timedelta(seconds=2)
            elif change == "revocation":
                policy_path.write_text('[grants]\nstop="deny"\n')
            elif change == "unreadable_policy":
                policy_path.write_text("malformed policy")
            return True

    class SyntheticProcess:
        def create_time(self) -> float:
            return record.create_time

        def kill(self) -> None:
            raise AssertionError("Graceful stop never escalates to force")

        def terminate(self) -> None:
            raise AssertionError("Windows graceful stop never terminates")

    backend = PsutilBackend(platform="win32", windows=Windows())
    monkeypatch.setattr(backend, "scan", lambda: ProcessScan((record,)))
    monkeypatch.setattr(backend, "read", lambda _: record)
    monkeypatch.setattr("steam_agent.execution.runtime.psutil.Process", lambda _: SyntheticProcess())
    runtime = RuntimeObserver(platform="win32", backend=backend)
    monkeypatch.setattr(client_executor, "RuntimeObserver", lambda: runtime)
    monkeypatch.setattr(
        broker_main.ClientExecutor, "_lock",
        lambda _: (state / "execution.lock").open("wb"),
    )
    assert invoke(
        state, "stop", "480", "--request-id", "windows-stop-001", "--request-only",
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    if change == "expiry":
        clock[0] += timedelta(seconds=ledger_module.DEFAULT_EXECUTION_WINDOW_SECONDS - 1)

    assert invoke(state, "run", "--operation-id", str(receipt["operation_id"])) == 1
    report = json.loads(capsys.readouterr().out)

    assert report["outcome"] == "unconfirmed"
    assert "requested 1 attributed processes" in report["detail"]
    assert closed == [10]
    reason = "stop_probe_failed" if change in {"owner_changed", "close_failure"} else "stop_dispatch_gate_denied"
    assert reason in report["detail"]
    assert invoke(state, "stop", "480", "--request-id", "windows-stop-001") == 1
    retry = json.loads(capsys.readouterr().out)
    assert retry["state"] == "unconfirmed"
    assert retry["operation_id"] == receipt["operation_id"]
    assert closed == [10]
    ledger = ExecutionLedger(state / "ledger.sqlite3")
    try:
        assert [row[2] for row in ledger.events(receipt["operation_id"])] == [
            "pending_confirmation", "authorized", "dispatch_pending", "unconfirmed",
        ]
    finally:
        ledger.close()


def test_force_requires_independent_explicit_permission(state: Path, capsys) -> None:
    (state / "policy.toml").write_text(
        '[grants]\nlaunch="confirm"\nstop="confirm"\n[launch]\nallowed_appids=[480]\n[stop]\nallowed_appids=[480]\n'
    )
    assert invoke(state, "stop", "480", "--force", "--request-id", "force-0001") == 2
    assert "force is not permitted" in capsys.readouterr().err
    assert invoke(state, "stop", "481", "--request-id", "stop-0002") == 2
    assert "not permitted" in capsys.readouterr().err


@pytest.mark.parametrize(
    "field", ["machine_id", "steam_login", "account_alias", "steam_executable"]
)
def test_config_rejects_nonstring_runtime_fields(
    state: Path, field: str, capsys
) -> None:
    config = json.loads((state / "broker.json").read_text())
    config[field] = 123
    (state / "broker.json").write_text(json.dumps(config))
    assert invoke(state, "status") == 2
    assert "corrupt" in capsys.readouterr().err


def test_reinit_preserves_account_mapping_and_executable(
    state: Path, tmp_path: Path, capsys
) -> None:
    library = tmp_path / "library"
    assert (
        invoke(
            state,
            "init",
            "--library",
            str(library),
            "--account",
            "primary",
            "--steam-login",
            "test_login",
            "--steam-executable",
            str(tmp_path / "Steam.exe"),
        )
        == 0
    )
    before = json.loads((state / "broker.json").read_text())
    assert invoke(state, "init", "--library", str(library)) == 0
    after = json.loads((state / "broker.json").read_text())
    assert before == after
    assert "test_login" not in capsys.readouterr().out


def test_stop_unknown_is_deferred_without_durable_dispatch(
    state: Path, monkeypatch, capsys
) -> None:
    (state / "policy.toml").write_text(
        '[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\n'
    )

    class Runtime:
        def observe(self, appid: int) -> RuntimeObservation:
            return RuntimeObservation("unknown", (), ("process_probe_failed",))

        def stop_app(self, appid: int, *, force: bool, before_request=None) -> StopResult:
            raise AssertionError("unknown must not stop")

    monkeypatch.setattr(client_executor, "RuntimeObserver", Runtime)
    monkeypatch.setattr(
        broker_main.ClientExecutor,
        "_lock",
        lambda _: (state / "execution.lock").open("wb"),
    )
    assert invoke(state, "stop", "480", "--request-id", "unknown-001") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "deferred"
    assert invoke(state, "inspect", str(result["operation_id"])) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["state"] == "authorized"
    assert not any(row[2] == "dispatch_pending" for row in receipt["events"])


def test_stop_policy_has_independent_force_and_unknown_keys_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        '[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\nallow_force=true\n'
    )
    policy = load_policy(path)
    assert policy.stop_permitted(480, force=True)
    assert policy.grant_for("launch") == "deny"
    assert not policy.stop_permitted(481)
    path.write_text(
        '[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\nkill_all=true\n'
    )
    with pytest.raises(PolicyError):
        load_policy(path)


def test_auto_launch_request_uses_existing_run_policy_path(
    state: Path, monkeypatch, capsys
) -> None:
    (state / "policy.toml").write_text(
        '[grants]\nlaunch="allow"\n[limits]\nmin_free_gb=0\n[launch]\nallowed_appids=[480]\n'
    )
    calls: list[int] = []

    def execute(executor, operation_id: int) -> ExecutionReport:
        calls.append(operation_id)
        executor._ledger.transition(operation_id, "dispatch_pending")
        executor._ledger.transition(operation_id, "dispatched")
        return ExecutionReport(operation_id, "dispatched", "launch request accepted")

    monkeypatch.setattr(broker_main.ClientExecutor, "execute", execute)
    assert invoke(state, "launch", "480", "--request-id", "launch-001") == 0
    first = json.loads(capsys.readouterr().out)
    assert invoke(state, "launch", "480", "--request-id", "launch-001") == 0
    second = json.loads(capsys.readouterr().out)
    assert calls == [first["operation_id"]]
    assert second["reused"] is True

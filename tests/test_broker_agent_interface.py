"""Versioned agent contracts and recoverable desktop jobs, entirely synthetic."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

from steam_agent.execution import broker_main
from steam_agent.execution.desktop_bridge import (
    DesktopSession, DesktopTaskSnapshot, _job_id, _worker_arguments,
    discover_desktop_jobs, dispatch_in_desktop, inspect_desktop_job, run_worker,
)
from steam_agent.execution.executor import ExecutionReport
from steam_agent.execution.ledger import ExecutionLedger
from steam_agent.execution.linux_session import LeaseGates
from steam_agent.execution.policy import load_policy
from steam_agent.execution.readiness import operation_preflight
from steam_agent.execution.agent_requests import DirectRequest

_SID = "synthetic-sid"


@pytest.fixture
def state(tmp_path: Path, capsys) -> Path:
    state = tmp_path / "state"
    library = tmp_path / "library"
    (library / "steamapps").mkdir(parents=True)
    assert broker_main.main([
        "--state-dir", str(state), "init", "--library", str(library),
        "--machine-id", "gaming",
    ]) == 0
    capsys.readouterr()
    return state


def invoke(state: Path, *args: str) -> int:
    return broker_main.main(["--state-dir", str(state), "--format", "json", *args])


def test_legacy_success_and_opt_in_receipt_reuse(state: Path, capsys) -> None:
    (state / "policy.toml").write_text('[grants]\nstop="confirm"\n[stop]\nallowed_appids=[480]\n')
    assert broker_main.main([
        "--state-dir", str(state), "stop", "480", "--request-id", "request-0001",
    ]) == 0
    legacy = json.loads(capsys.readouterr().out)
    assert set(legacy) == {"operation_id", "nonce", "state", "reused"}
    assert invoke(state, "stop", "480", "--request-id", "request-0001") == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["schema"] == "broker-result/0.1"
    assert selected["receipt"]["state"] == "pending_confirmation"
    assert selected["attempt"]["outcome"] == "receipt_reused"
    assert selected["retry"] == {"category": "confirmation_required", "automatic": False}
    assert selected["result"]["nonce"] == legacy["nonce"]
    assert selected["context"]["machine_id"] == "gaming"
    assert "account_alias" not in str(selected)


def test_expected_host_refuses_before_ledger_creation(state: Path, capsys) -> None:
    lock = state / "state.lock"
    before = lock.read_bytes() if lock.exists() else None
    assert invoke(state, "--expected-machine", "elsewhere", "stop", "480", "--request-id", "request-0001") == 2
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["attempt"] == {"outcome": "refused", "reason_code": "machine_mismatch"}
    assert result["receipt"] is None
    assert not (state / "ledger.sqlite3").exists()
    assert (lock.read_bytes() if lock.exists() else None) == before


def test_inspect_unknown_request_does_not_create_or_expire(state: Path, capsys) -> None:
    assert invoke(state, "inspect", "--request-id", "unknown-request") == 2
    assert json.loads(capsys.readouterr().out)["attempt"]["reason_code"] == "request_not_found"
    assert not (state / "ledger.sqlite3").exists()
    (state / "policy.toml").write_text('[grants]\nstop="confirm"\n[stop]\nallowed_appids=[480]\n')
    assert invoke(state, "stop", "480", "--request-id", "request-0001") == 0
    first = json.loads(capsys.readouterr().out)
    ledger = ExecutionLedger(state / "ledger.sqlite3")
    operation_id = first["receipt"]["operation_id"]
    ledger._connection.execute("UPDATE operations SET nonce_expires_at='2000-01-01T00:00:00Z'")
    before = ledger.events(operation_id)
    ledger.close()
    assert invoke(state, "inspect", "--request-id", "request-0001") == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["receipt"]["state"] == "pending_confirmation"
    ledger = ExecutionLedger(state / "ledger.sqlite3", readonly=True)
    assert ledger.events(operation_id) == before
    ledger.close()


def test_deferred_attempt_retains_actual_authorized_receipt(state: Path, capsys, monkeypatch) -> None:
    (state / "policy.toml").write_text('[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\n')

    class Deferred:
        def execute(self, operation_id):
            return ExecutionReport(operation_id, "deferred", "synthetic busy session")

    monkeypatch.setattr(broker_main, "_components", lambda _: (
        ExecutionLedger(state / "ledger.sqlite3"), Deferred(),
    ))
    assert invoke(state, "stop", "480", "--request-id", "request-0001") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["receipt"]["state"] == "authorized"
    assert result["attempt"]["outcome"] == "deferred"
    assert result["retry"] == {"category": "resume_original", "automatic": False}


def test_terminal_uncertainty_is_a_receipt_and_never_replays(state: Path, capsys, monkeypatch) -> None:
    (state / "policy.toml").write_text('[grants]\nstop="allow"\n[stop]\nallowed_appids=[480]\n')
    assert invoke(state, "stop", "480", "--request-id", "request-0001", "--request-only") == 0
    initial = json.loads(capsys.readouterr().out)
    ledger = ExecutionLedger(state / "ledger.sqlite3")
    operation_id = initial["receipt"]["operation_id"]
    ledger.transition(operation_id, "dispatch_pending")
    ledger.transition(operation_id, "unconfirmed", detail="synthetic uncertain acknowledgement")
    ledger.close()
    monkeypatch.setattr(broker_main, "_components", lambda _: pytest.fail("terminal receipt must not execute"))
    assert invoke(state, "stop", "480", "--request-id", "request-0001") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["receipt"]["state"] == "unconfirmed"
    assert result["receipt"]["dispatch_certainty"] == "uncertain"
    assert result["attempt"]["outcome"] == "receipt_reused"
    assert result["retry"] == {"category": "new_intent_required", "automatic": False}


class RecoveryDesktop:
    def __init__(self, *, delayed=False):
        self.delayed = delayed
        self.tasks = {}
        self.starts = 0
        self.removed = []

    def select_session(self):
        return DesktopSession(12, "synthetic-owner", _SID)

    def current_identity(self):
        return _SID, 12

    def protect_directory(self, path):
        pass

    def start(self, token, state_dir, session):
        self.starts += 1
        self.tasks[token] = DesktopTaskSnapshot(
            "steam-agent-desktop-bridge/0.1:" + token, session.user_sid, 3, 0,
            str(Path(sys.executable).resolve()), _worker_arguments(state_dir, token),
            str(state_dir), 0, "queued" if self.delayed else "done",
        )
        if not self.delayed:
            run_worker(state_dir, token, backend=self)

    def state(self, token):
        return "running" if self.delayed else "done"

    def remove(self, token):
        self.removed.append(token)
        return True

    def inspect_task(self, token, created_at):
        return self.tasks.get(token)

    def remove_completed(self, token, created_at, expected):
        actual = self.inspect_task(token, created_at)
        if actual != expected or actual.state != "done" or actual.instance_count:
            return False
        self.removed.append(token)
        del self.tasks[token]
        return True


def test_typed_policy_refusal_survives_desktop_worker(state: Path) -> None:
    result = dispatch_in_desktop([
        "stop", "480", "--request-id", "request-0001",
    ], state, backend=RecoveryDesktop())
    assert result.exit_code == 2
    assert result.payload["schema"] == "broker-result/0.1"
    assert result.payload["attempt"]["reason_code"] == "policy_denied"
    assert "desktop_worker_result_unknown" not in str(result.payload)
    assert str(state) not in str(result.payload)


def test_fresh_process_collects_late_worker_original_job_without_dispatch(state: Path) -> None:
    original = RecoveryDesktop(delayed=True)
    clock = iter([0.0, 31.0])
    result = dispatch_in_desktop([
        "stop", "480", "--request-id", "request-0001",
    ], state, backend=original, monotonic=lambda: next(clock), sleep=lambda _: None)
    job_id = result.payload["job_id"]
    token = next(iter(original.tasks))
    # A fresh backend has only native task state, no in-process dispatch cache.
    collector = RecoveryDesktop()
    collector.tasks = original.tasks.copy()
    queued = inspect_desktop_job(state, job_id, backend=collector, cleanup=True)
    assert queued["state"] == "queued" and queued["cleaned"] is False
    assert collector.starts == 0
    assert run_worker(state, token, backend=collector) == 0
    collector.tasks[token] = replace(collector.tasks[token], state="done")
    jobs = discover_desktop_jobs(state, backend=collector)
    assert jobs["jobs"][0]["request_id"] == "request-0001"
    collected = inspect_desktop_job(state, job_id, backend=collector, cleanup=True)
    assert collected["receipt"]["payload"]["attempt"]["reason_code"] == "policy_denied"
    assert collected["request_id"] == "request-0001"
    assert collected["cleaned"] is True
    assert collector.starts == 0
    assert not (state / "desktop-jobs" / token).exists()
    assert token not in str(jobs)


@pytest.mark.parametrize("command", [
    ["install", "480", "--request-id", "request-0001"],
    ["verify", "480", "--request-id", "request-0001"],
    ["launch", "480", "--request-id", "request-0001"],
    ["stop", "480", "--request-id", "request-0001"],
])
def test_desktop_timeout_envelope_preserves_original_job_recovery(
    state: Path, monkeypatch, capsys, command: list[str],
) -> None:
    desktop = RecoveryDesktop(delayed=True)
    clock = iter([0.0, 31.0])
    monkeypatch.setattr(
        "steam_agent.execution.desktop_bridge.dispatch_in_desktop",
        lambda argv, state_dir: dispatch_in_desktop(
            argv, state_dir, backend=desktop, monotonic=lambda: next(clock), sleep=lambda _: None,
        ),
    )

    assert invoke(state, "--desktop", *command) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["attempt"] == {
        "outcome": "transport_unknown", "reason_code": "desktop_worker_timeout",
    }
    assert result["receipt"] is None
    assert result["retry"] == {"category": "collect_original_job", "automatic": False}
    assert result["result"]["job_id"] == _job_id(next(iter(desktop.tasks)))
    assert desktop.starts == 1
    assert not (state / "ledger.sqlite3").exists()


def test_desktop_timeout_retains_existing_receipt_and_original_job_recovery(
    state: Path, monkeypatch, capsys,
) -> None:
    (state / "policy.toml").write_text('[grants]\nstop="confirm"\n[stop]\nallowed_appids=[480]\n')
    command = ["stop", "480", "--request-id", "request-0001"]
    assert invoke(state, *command, "--request-only") == 0
    initial = json.loads(capsys.readouterr().out)
    desktop = RecoveryDesktop(delayed=True)
    clock = iter([0.0, 31.0])
    monkeypatch.setattr(
        "steam_agent.execution.desktop_bridge.dispatch_in_desktop",
        lambda argv, state_dir: dispatch_in_desktop(
            argv, state_dir, backend=desktop, monotonic=lambda: next(clock), sleep=lambda _: None,
        ),
    )

    assert invoke(state, "--desktop", *command) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["receipt"] == initial["receipt"]
    assert result["attempt"] == {
        "outcome": "transport_unknown", "reason_code": "desktop_worker_timeout",
    }
    assert result["retry"] == {"category": "collect_original_job", "automatic": False}
    assert result["result"]["job_id"] == _job_id(next(iter(desktop.tasks)))
    assert desktop.starts == 1


@pytest.mark.parametrize("change", ["source", "principal", "action_path", "action_arguments", "running", "unknown", "instances"])
def test_collection_retains_foreign_running_and_unknown_jobs(state: Path, change: str) -> None:
    backend = RecoveryDesktop(delayed=True)
    clock = iter([0.0, 31.0])
    dispatch_in_desktop(["status"], state, backend=backend,
                        monotonic=lambda: next(clock), sleep=lambda _: None)
    token = next(iter(backend.tasks))
    task = replace(backend.tasks[token], state="done")
    if change in {"source", "principal", "action_path", "action_arguments"}:
        task = replace(task, **{change: "foreign"})
    elif change in {"running", "unknown"}:
        task = replace(task, state=change)
    else:
        task = replace(task, instance_count=1)
    backend.tasks[token] = task
    run_worker(state, token, backend=backend)
    result = inspect_desktop_job(state, _job_id(token), backend=backend, cleanup=True)
    assert result["cleaned"] is False
    assert backend.removed == []
    assert (state / "desktop-jobs" / token).exists()


def test_preflight_launch_ignores_download_gate_and_creates_no_ledger(state: Path) -> None:
    (state / "policy.toml").write_text('[grants]\nlaunch="confirm"\n[launch]\nallowed_appids=[480]\n')
    config = json.loads((state / "broker.json").read_text())

    class Session:
        def gates(self):
            return LeaseGates("pass", "pass", "unknown", "fail")

    def no_disk(*args):
        pytest.fail("launch preflight must not probe a content disk floor")

    result = operation_preflight(
        config, load_policy(state / "policy.toml"), DirectRequest("launch", 480, "preflight-only"),
        platform="win32", session=Session(), disk_floor=no_disk,
    )
    assert result["readiness"] == "ready"
    assert result["authorization"] == "not_granted"
    assert result["native_qualification"] == "unqualified"
    assert not (state / "ledger.sqlite3").exists()


def test_selected_parser_refusal_is_typed_and_redacted(state: Path, capsys) -> None:
    assert invoke(state, "stop", "private-invalid-appid", "--request-id", "request-0001") == 2
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["attempt"]["reason_code"] == "invalid_request"
    assert "private-invalid-appid" not in output.out + output.err
    assert not (state / "ledger.sqlite3").exists()


def test_version_available_without_initialized_broker(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        broker_main.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("steam-agent-broker 0.1.")


def test_native_reopen_checks_metadata_and_idle_state_without_cached_tasks(state: Path, monkeypatch) -> None:
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from steam_agent.execution.desktop_bridge import NativeTaskScheduler

    token = "b" * 32
    action = SimpleNamespace(
        Type=0, Path=str(Path(sys.executable).resolve()),
        Arguments=_worker_arguments(state, token), WorkingDirectory=str(state),
    )
    definition = SimpleNamespace(
        Actions=SimpleNamespace(Count=1, Item=lambda _: action),
        Triggers=SimpleNamespace(Count=0), Settings=SimpleNamespace(MultipleInstances=2),
        RegistrationInfo=SimpleNamespace(Source="steam-agent-desktop-bridge/0.1:" + token),
        Principal=SimpleNamespace(UserId="domain\\synthetic-owner", LogonType=3, RunLevel=0),
    )
    task = SimpleNamespace(
        State=3, LastRunTime=datetime.fromtimestamp(1001, timezone.utc), Definition=definition,
        GetInstances=lambda _: SimpleNamespace(Count=0),
    )
    deletions = []
    folder = SimpleNamespace(DeleteTask=lambda name, _: deletions.append(name))
    security = SimpleNamespace(
        LookupAccountName=lambda _, principal: (_SID, None, None),
        ConvertSidToStringSid=lambda sid: sid,
    )
    scheduler = NativeTaskScheduler()
    monkeypatch.setattr(scheduler, "_reopen_task", lambda _: (folder, task))
    monkeypatch.setattr(scheduler, "_windows", lambda: (None, None, security, None))
    snapshot = scheduler.inspect_task(token, 1000.5)
    assert snapshot is not None and snapshot.principal == _SID and snapshot.state == "done"
    assert scheduler._tasks == {}
    # Changed metadata between observation and cleanup is never deleted.
    action.Arguments = "foreign-action"
    assert scheduler.remove_completed(token, 1000.5, snapshot) is False
    assert deletions == []
    action.Arguments = _worker_arguments(state, token)
    task.State = 2
    assert scheduler.remove_completed(token, 1000.5, snapshot) is False
    task.State = 3
    assert scheduler.remove_completed(token, 1000.5, snapshot) is True
    assert deletions == ["steam-agent-desktop-" + token]


@pytest.mark.parametrize("code,legacy", [
    (0, {"operation_id": 42, "outcome": "dispatched", "detail": "synthetic dispatched"}),
    (1, {"operation_id": 42, "outcome": "deferred", "detail": "synthetic busy"}),
])
def test_desktop_output_respects_explicit_envelope_selection(state: Path, monkeypatch, capsys, code, legacy) -> None:
    from steam_agent.execution.desktop_bridge import DesktopResult

    envelope = {
        "schema": "broker-result/0.1",
        "context": {"broker_version": "synthetic-worker-version", "machine_id": "gaming"},
        "receipt": {"operation_id": 42, "state": "dispatched" if code == 0 else "authorized"},
        "attempt": {"outcome": legacy["outcome"], "reason_code": None},
        "retry": {"category": "none", "automatic": False}, "result": legacy,
    }
    monkeypatch.setattr(
        "steam_agent.execution.desktop_bridge.dispatch_in_desktop",
        lambda argv, state_dir: DesktopResult(code, envelope),
    )
    assert broker_main.main(["--state-dir", str(state), "--desktop", "runtime"]) == code
    assert json.loads(capsys.readouterr().out) == legacy
    assert invoke(state, "--desktop", "runtime") == code
    assert json.loads(capsys.readouterr().out) == envelope


@pytest.mark.parametrize("operation", ["launch", "install", "verify"])
def test_preflight_unknown_client_presence_cannot_claim_ready(state: Path, operation: str) -> None:
    policy_path = state / "policy.toml"
    policy_path.write_text(
        '[grants]\nlaunch="confirm"\ninstall="confirm"\nverify="confirm"\n'
        '[launch]\nallowed_appids=[480]\n'
    )
    config = json.loads((state / "broker.json").read_text())
    config.update({"steamcmd": "/synthetic/steamcmd", "steam_login": "synthetic-login"})

    class Session:
        def gates(self):
            return LeaseGates("pass", "pass", "pass", "unknown")

        def steamcmd_running(self):
            return False

    result = operation_preflight(
        config, load_policy(policy_path), DirectRequest(operation, 480, "preflight-only"),
        platform="linux", session=Session(), disk_floor=lambda *_: None,
    )
    assert result["activity_clear"] is None
    assert result["readiness"] == "unknown"
    assert result["authorization"] == "not_granted"


@pytest.mark.parametrize("writer_running,library_present", [(True, True), (False, False)])
def test_content_preflight_blocks_writer_or_missing_library(state: Path, writer_running: bool, library_present: bool) -> None:
    policy_path = state / "policy.toml"
    policy_path.write_text('[grants]\ninstall="confirm"\n')
    config = json.loads((state / "broker.json").read_text())
    config.update({"steamcmd": "/synthetic/steamcmd", "steam_login": "synthetic-login"})
    if not library_present:
        (Path(config["library"]) / "steamapps").rmdir()

    class Session:
        def gates(self):
            return LeaseGates("pass", "pass", "pass", "pass")

        def steamcmd_running(self):
            return writer_running

    result = operation_preflight(
        config, load_policy(policy_path), DirectRequest("install", 480, "preflight-only"),
        platform="linux", session=Session(), disk_floor=lambda *_: None,
    )
    assert result["readiness"] == "blocked"
    assert result["content_writer_absent"] is not writer_running
    assert result["content_library_available"] is library_present


def test_preflight_expected_host_refuses_before_session_probe(state: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(broker_main, "LinuxSession", lambda **_: pytest.fail("wrong host must not probe session"))
    assert invoke(state, "--expected-machine", "elsewhere", "preflight", "launch", "480") == 2
    assert json.loads(capsys.readouterr().out)["attempt"]["reason_code"] == "machine_mismatch"
    assert not (state / "ledger.sqlite3").exists()


@pytest.mark.parametrize("game_gate", ["pass", "fail", "unknown"])
def test_stop_preflight_stays_unknown_without_appid_attribution(state: Path, game_gate: str) -> None:
    policy_path = state / "policy.toml"
    policy_path.write_text('[grants]\nstop="confirm"\n[stop]\nallowed_appids=[480]\n')
    config = json.loads((state / "broker.json").read_text())

    class Session:
        def gates(self):
            return LeaseGates(game_gate, "pass", "pass", "pass")

    result = operation_preflight(
        config, load_policy(policy_path), DirectRequest("stop", 480, "preflight-only"),
        platform="linux", session=Session(), disk_floor=lambda *_: None,
    )
    assert result["activity_clear"] is None
    assert result["readiness"] == "unknown"
    assert result["authorization"] == "not_granted"

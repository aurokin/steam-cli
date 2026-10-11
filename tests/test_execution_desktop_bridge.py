"""Synthetic desktop bridge: no Windows tasks, credentials, or Steam actions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from steam_agent.execution.desktop_bridge import (
    DesktopBridgeError, DesktopSession, NativeTaskScheduler, _ScheduledTask,
    dispatch_in_desktop, run_worker,
)

_SID = "S-1-5-21-100-200-300-400"
_TOKEN = "a" * 32


class FakeDesktop:
    def __init__(self, *, running: bool = False, worker_session: int = 12) -> None:
        self.running = running
        self.worker_session = worker_session
        self.submissions = 0
        self.removed: list[str] = []
        self.protected: list[Path] = []
        self.executed: list[list[str]] = []

    def select_session(self):
        return DesktopSession(12, "synthetic-owner", _SID)

    def current_identity(self):
        return _SID, self.worker_session

    def protect_directory(self, path):
        self.protected.append(path)

    def broker(self, argv):
        self.executed.append(argv)
        print(json.dumps({"state": "dispatched", "operation_id": 42}))
        return 0

    def start(self, token, state_dir, session):
        self.submissions += 1
        if not self.running:
            run_worker(state_dir, token, backend=self, broker=self.broker)

    def state(self, token):
        return "running" if self.running else "done"

    def remove(self, token):
        assert not self.running
        self.removed.append(token)
        return True


def _job(state_dir: Path, *, argv=None, created=1000, expires=1120, session=12, sid=_SID):
    job = state_dir / "desktop-jobs" / _TOKEN
    job.mkdir(parents=True)
    (job / "request.json").write_text(json.dumps({
        "schema": "steam-agent-desktop-bridge/0.1", "job_token": _TOKEN,
        "argv": argv if argv is not None else ["status"],
        "created_at": created, "expires_at": expires,
        "session_id": session, "user_sid": sid,
    }), encoding="utf-8")
    return job


def test_bridge_returns_broker_receipt_and_cleans_only_finished_owned_job(tmp_path: Path) -> None:
    backend = FakeDesktop()

    result = dispatch_in_desktop(
        ["launch", "480", "--request-id", "synthetic-request"], tmp_path, backend=backend,
    )

    assert result.exit_code == 0
    assert result.payload == {"state": "dispatched", "operation_id": 42}
    assert backend.executed == [[
        "--state-dir", str(tmp_path), "--format", "json", "launch", "480",
        "--request-id", "synthetic-request",
    ]]
    assert len(backend.removed) == 1
    assert list((tmp_path / "desktop-jobs").iterdir()) == []


def test_timeout_preserves_running_task_and_job_without_claiming_action(tmp_path: Path) -> None:
    backend = FakeDesktop(running=True)
    clock = iter([0.0, 31.0])

    result = dispatch_in_desktop(
        ["stop", "480", "--request-id", "synthetic-request"], tmp_path, backend=backend,
        monotonic=lambda: next(clock), sleep=lambda _: None,
    )

    assert result.exit_code == 2
    assert result.payload["state"] == "unknown"
    assert result.payload["reason_code"] == "desktop_worker_timeout"
    assert backend.executed == []
    assert backend.removed == []
    assert len(list((tmp_path / "desktop-jobs").iterdir())) == 1


def test_finished_worker_without_receipt_preserves_original_job_for_recovery(tmp_path: Path) -> None:
    class FinishedWithoutReceipt(FakeDesktop):
        def start(self, token, state_dir, session):
            self.submissions += 1

    backend = FinishedWithoutReceipt()
    argv = ["launch", "480", "--request-id", "synthetic-request"]

    result = dispatch_in_desktop(argv, tmp_path, backend=backend)

    assert result.exit_code == 2
    assert result.payload["state"] == "unknown"
    assert result.payload["reason_code"] == "desktop_worker_no_receipt"
    assert result.payload["request_id"] == "synthetic-request"
    assert result.payload["retry"] == {"category": "collect_original_job", "automatic": False}
    assert backend.submissions == 1
    assert backend.removed == []
    jobs = list((tmp_path / "desktop-jobs").iterdir())
    assert len(jobs) == 1
    request = json.loads((jobs[0] / "request.json").read_text(encoding="utf-8"))
    assert request["argv"] == argv
    assert result.payload["job_id"] == hashlib.sha256(jobs[0].name.encode()).hexdigest()[:32]


def test_scheduler_ready_before_start_is_unknown_and_cannot_be_removed() -> None:
    class Registered:
        State = 3
        LastRunTime = "never"

        def GetInstances(self, flags):
            return type("Instances", (), {"Count": 0})()

    registered = Registered()
    scheduler = NativeTaskScheduler()
    scheduler._tasks[_TOKEN] = _ScheduledTask(registered, "never")

    assert scheduler.state(_TOKEN) == "unknown"
    assert scheduler.remove(_TOKEN) is False
    registered.LastRunTime = "started"
    assert scheduler.state(_TOKEN) == "done"


def test_scheduler_queued_then_ready_without_start_remains_unknown() -> None:
    class Registered:
        State = 2
        LastRunTime = "never"

        def GetInstances(self, flags):
            return SimpleNamespace(Count=0)

    registered = Registered()
    scheduler = NativeTaskScheduler()
    scheduler._tasks[_TOKEN] = _ScheduledTask(registered, "never")

    assert scheduler.state(_TOKEN) == "running"
    registered.State = 3

    assert scheduler.state(_TOKEN) == "unknown"
    assert scheduler.remove(_TOKEN) is False


@pytest.mark.parametrize("session,elevated,allowed", [(12,1,False), (12,0,True), (0,1,True)])
def test_native_identity_refuses_elevated_desktop_worker_without_native_calls(
    monkeypatch, session, elevated, allowed,
) -> None:
    class Token:
        def Close(self):
            pass

    security = SimpleNamespace(
        OpenProcessToken=lambda *args: Token(), TokenUser=1, TokenElevation=20,
        GetTokenInformation=lambda token, key: (_SID, 0) if key == 1 else elevated,
        ConvertSidToStringSid=lambda value: value,
    )
    scheduler = NativeTaskScheduler()
    scheduler._windows = lambda: (
        SimpleNamespace(GetCurrentProcess=lambda: 1), SimpleNamespace(TOKEN_QUERY=8), security, None,
    )
    monkeypatch.setattr("steam_agent.execution.desktop_bridge.windows_session_id", lambda _: session)

    if allowed:
        assert scheduler.current_identity() == (_SID, session)
    else:
        with pytest.raises(DesktopBridgeError):
            scheduler.current_identity()


def test_delayed_worker_start_keeps_job_until_receipt_and_completion(tmp_path: Path) -> None:
    class Delayed(FakeDesktop):
        def start(self, token, state_dir, session):
            self.submissions += 1
            self.pending = (token, state_dir)
            self.probes = 0

        def state(self, token):
            self.probes += 1
            if self.probes == 1:
                assert (self.pending[1] / "desktop-jobs" / token / "request.json").exists()
                return "unknown"  # task ready, worker not started yet
            run_worker(self.pending[1], token, backend=self, broker=self.broker)
            return "done"

    backend = Delayed()

    result = dispatch_in_desktop(["status"], tmp_path, backend=backend, sleep=lambda _: None)

    assert result.exit_code == 0
    assert result.payload["operation_id"] == 42
    assert len(backend.removed) == 1


@pytest.mark.parametrize("argv", [
    ["init", "--library", "synthetic"], ["confirm", "synthetic-nonce", "--actor", "owner"],
    ["request", "--account", "synthetic"], ["policy"], ["runtime", "--desktop"],
    ["--state-dir", "synthetic", "runtime"], ["runtime", "--appid", "bad"],
])
def test_bridge_rejects_unsupported_or_malformed_commands_before_submission(tmp_path: Path, argv) -> None:
    backend = FakeDesktop()

    result = dispatch_in_desktop(argv, tmp_path, backend=backend)

    assert result.exit_code == 2
    assert backend.submissions == 0
    assert not (tmp_path / "desktop-jobs").exists()


def test_unknown_session_does_not_register_any_task(tmp_path: Path) -> None:
    backend = FakeDesktop()

    def unavailable():
        raise DesktopBridgeError("synthetic private error")

    backend.select_session = unavailable
    result = dispatch_in_desktop(["runtime"], tmp_path, backend=backend)

    assert result.payload["state"] == "unknown"
    assert "private error" not in str(result.payload)
    assert backend.submissions == 0


@pytest.mark.parametrize("session,sid", [(0, _SID), (13, _SID), (12, "S-1-5-21-999")])
def test_worker_requires_exact_same_user_interactive_session(tmp_path: Path, session, sid) -> None:
    _job(tmp_path, session=session, sid=sid)
    backend = FakeDesktop()

    assert run_worker(tmp_path, _TOKEN, backend=backend, broker=backend.broker, now=lambda: 1001) == 2
    assert backend.executed == []


@pytest.mark.parametrize("created,expires,current", [(1000,1120,1120), (1000,1121,1001), (1000,1120,999)])
def test_worker_rejects_expired_future_or_overlong_job(tmp_path: Path, created, expires, current) -> None:
    _job(tmp_path, created=created, expires=expires)
    backend = FakeDesktop()

    assert run_worker(tmp_path, _TOKEN, backend=backend, broker=backend.broker, now=lambda: current) == 2
    assert backend.executed == []


def test_worker_bounded_output_sanitizes_unknown_failure_receipt(tmp_path: Path) -> None:
    job = _job(tmp_path)
    backend = FakeDesktop()

    def noisy_broker(argv):
        print("x" * (64 * 1024 + 1))
        return 0

    assert run_worker(tmp_path, _TOKEN, backend=backend, broker=noisy_broker, now=lambda: 1001) == 0
    result = json.loads((job / "result.json").read_text(encoding="utf-8"))
    assert result["exit_code"] == 2
    assert result["payload"]["state"] == "unknown"
    assert "x" * 100 not in str(result)


def test_worker_keeps_stderr_and_private_identifiers_out_of_receipt(tmp_path: Path) -> None:
    job = _job(tmp_path)
    backend = FakeDesktop()

    def private_broker(argv):
        import sys

        print("synthetic private diagnostic", file=sys.stderr)
        print(json.dumps({"state": "unconfirmed", "path": str(tmp_path), "username": "synthetic",
                          "detail": "see " + str(tmp_path / "private.log")}))
        return 1

    assert run_worker(tmp_path, _TOKEN, backend=backend, broker=private_broker, now=lambda: 1001) == 0
    result = json.loads((job / "result.json").read_text(encoding="utf-8"))
    assert result["exit_code"] == 1
    assert "path" not in result["payload"]
    assert "username" not in result["payload"]
    assert str(tmp_path) not in str(result)
    assert "private diagnostic" not in str(result)


def test_worker_rejects_oversized_input_without_broker_call(tmp_path: Path) -> None:
    job = _job(tmp_path)
    (job / "request.json").write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    backend = FakeDesktop()

    assert run_worker(tmp_path, _TOKEN, backend=backend, broker=backend.broker, now=lambda: 1001) == 2
    assert backend.executed == []


def test_native_private_directory_flags_preserve_protection_without_signed_overflow(tmp_path: Path, monkeypatch) -> None:
    from steam_agent.execution.desktop_bridge import NativeTaskScheduler

    class Security:
        DACL_SECURITY_INFORMATION = 4
        SDDL_REVISION_1 = 1

        def ConvertStringSecurityDescriptorToSecurityDescriptor(self, value, revision):
            assert value == "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;synthetic-sid)"
            return "descriptor"

        def SetFileSecurity(self, path, flags, descriptor):
            if not -(1 << 31) <= flags < (1 << 31):
                raise OverflowError("signed C long")
            assert flags & 0xFFFFFFFF == 0x80000004
            assert descriptor == "descriptor"

    scheduler = NativeTaskScheduler()
    monkeypatch.setattr(scheduler, "_windows", lambda: (None, None, Security(), None))
    monkeypatch.setattr(scheduler, "current_identity", lambda: ("synthetic-sid", 0))
    scheduler.protect_directory(tmp_path)

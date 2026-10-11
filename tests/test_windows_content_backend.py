"""Candidate Windows content integration without native calls or Steam effects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path, PureWindowsPath
import subprocess

import pytest

from steam_agent.execution import content_plane
from steam_agent.execution.content_backend import (
    EFFECT_SCHEMA, WindowsContentBackend, WindowsContentError,
)
from steam_agent.execution.content_plane import ContentProcessError, WindowsContentResult, WindowsSteamcmdAdapter
from steam_agent.execution.executor import Executor
from steam_agent.execution.ledger import ExecutionLedger
from steam_agent.execution.windows_filesystem import WindowsFileIdentity, WindowsFilesystem


def identity(name: str) -> WindowsFileIdentity:
    return WindowsFileIdentity(1, hashlib.sha256(name.encode()).hexdigest()[:32])


class IdentityFilesystem:
    def __init__(self):
        self.changed = False

    def identity(self, path, *, directory=True):
        return identity(str(path) + ("changed" if self.changed else ""))


@pytest.mark.parametrize("mode", ["success", "abort", "poll-error", "kill-error", "close-error"])
def test_windows_adapter_uses_private_cwd_and_whole_job_cleanup(tmp_path: Path, monkeypatch, mode: str) -> None:
    events = []
    seen = {}

    class Job:
        pid = 42
        returncode = None

        def __init__(self, argv, *, stdout, stderr, env, cwd):
            events.append("spawn-job")
            seen.update(argv=argv, env=env, cwd=cwd)
            self.killed = False
            stdout.write(b"Success! App fully installed\nERROR opening C:\\Users\\private-owner\\secret.log\nERROR opening \\\\server\\private-share\\secret.log\n")

        def wait(self, timeout=None):
            events.append("wait-entire-job")
            if self.killed or mode == "success" or mode == "close-error":
                self.returncode = 1 if self.killed else 0
                return self.returncode
            if mode == "poll-error":
                raise OSError("synthetic private path from probe")
            raise subprocess.TimeoutExpired("synthetic-owned-job", timeout)

        def kill(self):
            events.append("terminate-entire-job")
            if mode == "kill-error":
                raise OSError("synthetic private termination detail")
            self.killed = True

        def close(self):
            events.append("close-job-handles")
            if mode == "close-error":
                raise OSError("synthetic private close detail")

    monkeypatch.setattr(content_plane, "WindowsJobProcess", Job)
    monkeypatch.setattr(content_plane.os, "killpg", lambda *_: pytest.fail("Windows cannot use killpg"), raising=False)
    monkeypatch.setenv("STEAM_WEB_API_KEY", "synthetic-provider-canary")
    monkeypatch.setenv("HOME", "synthetic-desktop-home")
    installation = Path("C:\\private-cache")
    adapter = WindowsSteamcmdAdapter(
        steamcmd_executable=Path("C:\\private-cache\\steamcmd.exe"),
        private_installation=installation, client_installation=Path("C:\\client"),
        log_dir=tmp_path, filesystem=IdentityFilesystem(),
    )
    arguments = {
        "account": "synthetic-login", "appid": 480,
        "install_dir": Path("C:\\library\\steamapps\\common\\fixture"),
        "operation_id": 1, "abort_when": lambda: True,
    }
    if mode in {"poll-error", "kill-error", "close-error"}:
        with pytest.raises(ContentProcessError) as error:
            adapter.install(**arguments)
        assert "private" not in str(error.value)
    else:
        result = adapter.install(**arguments)
        assert result.outcome == ("installed" if mode == "success" else "failed")
        assert result.appid == 480 and result.operation_id == 1
        assert result.operation == "install"
        assert result.target_identity == filesystem_identity(arguments["install_dir"])
        log = result.log_path.read_text()
        assert "private-owner" not in log and "private-share" not in log and "server" not in log
        assert "synthetic-provider-canary" not in log
    assert seen["cwd"] == installation
    assert seen["env"]["HOME"] == "synthetic-desktop-home"
    assert "STEAM_WEB_API_KEY" not in seen["env"]
    assert events[-1] == "close-job-handles"
    if mode in {"abort", "poll-error", "kill-error"}:
        assert "terminate-entire-job" in events
    assert list(tmp_path.glob("*.tmp")) == []


def test_windows_adapter_refuses_shared_installation_and_replaced_private_executable(tmp_path: Path) -> None:
    filesystem = IdentityFilesystem()
    with pytest.raises(ContentProcessError, match="overlaps"):
        WindowsSteamcmdAdapter(
            steamcmd_executable=Path("C:\\client\\steamcmd.exe"),
            private_installation=Path("C:\\client"), client_installation=Path("C:\\client"),
            log_dir=tmp_path, filesystem=filesystem,
        )
    adapter = WindowsSteamcmdAdapter(
        steamcmd_executable=Path("C:\\private-cache\\steamcmd.exe"),
        private_installation=Path("C:\\private-cache"), client_installation=Path("C:\\client"),
        log_dir=tmp_path, filesystem=filesystem,
    )
    filesystem.changed = True
    with pytest.raises(ContentProcessError, match="identity changed"):
        adapter.install(account="synthetic-login", appid=480, install_dir=Path("C:\\library\\fixture"), operation_id=1)
    assert list(tmp_path.iterdir()) == []


class MemoryFiles:
    def __init__(self):
        self.files = {}
        self.identities = {}
        self.calls = []

    def identity(self, path, *, directory):
        self.calls.append(("identity", path))
        return self.identities.get(path, identity(str(path)))

    def exists(self, path):
        return path in self.files

    def read(self, path, *, limit):
        value = self.files[path]
        if len(value) > limit:
            raise OSError("fixture bounded read")
        return value

    def write_new(self, path, data):
        assert path not in self.files
        self.files[path] = data
        self.calls.append(("flushed-write", path))

    def move_new(self, source, destination):
        assert destination not in self.files
        self.files[destination] = self.files.pop(source)
        self.calls.append(("same-volume-move", destination))

    def replace(self, source, destination):
        assert destination in self.files
        self.files[destination] = self.files.pop(source)
        self.calls.append(("same-volume-replace", destination))


def backend():
    native = MemoryFiles()
    value = WindowsContentBackend(
        library=Path("C:\\library"), state_dir=Path("C:\\broker"),
        filesystem=WindowsFilesystem(native),
    )
    binding = value.bind_target(appid=480, operation_id=1, install_dir_name="Fixture")
    return value, binding, native


def filesystem_identity(path: Path) -> WindowsFileIdentity:
    return identity(str(path))


def job_result(binding, *, outcome="installed", operation="install", process_backend="windows-job/1"):
    return WindowsContentResult(
        outcome, Path("private-log"), binding.appid, binding.operation_id,
        operation, binding.target_identity, process_backend,
    )


def test_candidate_markers_bind_ntfs_ids_and_do_not_replay_prepared_effect() -> None:
    candidate, binding, native = backend()
    candidate.record_ownership(binding)
    receipt = candidate.begin_effect(binding, operation="install")
    assert receipt.schema == EFFECT_SCHEMA and receipt.state == "prepared"
    with pytest.raises(WindowsContentError, match="ALREADY_RECORDED"):
        candidate.begin_effect(binding, operation="install")
    finished = candidate.finish_effect(binding, result=job_result(binding, outcome="failed"))
    assert finished.state == "uncertain"
    with pytest.raises(WindowsContentError, match="TERMINAL"):
        candidate.finish_effect(binding, result=job_result(binding))
    assert "library_dev" not in str(native.files) and '"ino"' not in str(native.files)
    assert all(str(path).startswith("C:\\broker\\owned") for path in native.files)
    assert "C:\\library" not in str(receipt)


@pytest.mark.parametrize("legacy", [True, False])
def test_candidate_pending_legacy_and_unknown_marker_schema_refused(legacy: bool) -> None:
    candidate, binding, native = backend()
    if legacy:
        path = PureWindowsPath("C:\\broker\\owned\\480.json")
        native.files[path] = b'{"dev":1,"ino":2}'
        with pytest.raises(WindowsContentError, match="LEGACY_STATE"):
            candidate.record_ownership(binding)
    else:
        path = PureWindowsPath("C:\\broker\\owned\\windows-effect-1.json")
        native.files[path] = json.dumps({"schema": "future-effect/2"}).encode()
        with pytest.raises(WindowsContentError, match="UNSUPPORTED"):
            candidate.begin_effect(binding, operation="install")
    assert len(native.files) == 1


def test_candidate_pending_write_and_target_replacement_preserve_evidence() -> None:
    candidate, binding, native = backend()
    pending = PureWindowsPath("C:\\broker\\owned\\windows-effect-1.json.intent")
    native.files[pending] = b"interrupted write"
    with pytest.raises(WindowsContentError, match="MARKER_PENDING"):
        candidate.begin_effect(binding, operation="install")
    target = PureWindowsPath("C:\\library\\steamapps\\common\\Fixture")
    native.identities[target] = identity("replacement")
    with pytest.raises(WindowsContentError, match="IDENTITY_CHANGED"):
        candidate.record_ownership(binding)
    assert native.files == {pending: b"interrupted write"}


@pytest.mark.parametrize("name", ["CON", "trailing.", "trailing ", "named:stream", "..", "escaped\\target"])
def test_candidate_rejects_windows_unsafe_components(name: str) -> None:
    candidate, _, _ = backend()
    with pytest.raises(WindowsContentError, match="TARGET_INVALID"):
        candidate.bind_target(appid=480, operation_id=1, install_dir_name=name)


def test_candidate_adoption_requires_confirmed_job_and_exact_target_source() -> None:
    candidate, binding, _ = backend()
    candidate.begin_effect(binding, operation="install")
    with pytest.raises(WindowsContentError, match="EFFECT_UNCONFIRMED"):
        candidate.adopt(binding, source=Path("C:\\elsewhere\\appmanifest_480.acf"))
    candidate.finish_effect(binding, result=job_result(binding))
    with pytest.raises(WindowsContentError, match="SOURCE_UNBOUND"):
        candidate.adopt(binding, source=Path("C:\\elsewhere\\appmanifest_480.acf"))


@pytest.mark.parametrize("operation", ["install", "verify"])
def test_direct_windows_executor_and_recovery_never_enter_posix_content(tmp_path: Path, monkeypatch, operation: str) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger.sqlite3")
    operation_id, nonce = ledger.request(
        plan_key="synthetic-content-key", plan_document="{}", operation=operation,
        appid=480, account_alias="synthetic", machine_id="fixture", policy_version="fixture",
    )
    ledger.confirm(nonce=nonce, actor="fixture")

    class Session:
        def __getattr__(self, name):
            pytest.fail("Windows unqualified content cannot probe or mutate the client: " + name)

    executor = Executor(ledger=ledger, session=Session(), content=None,
                        library=tmp_path / "missing-library", state_dir=tmp_path)
    monkeypatch.setattr("steam_agent.execution.executor.sys.platform", "win32")
    result = executor._execute_locked(operation_id)
    assert result.outcome == "deferred" and "unqualified" in result.detail
    assert ledger.get(operation_id).state == "authorized"
    assert "deferred" in executor._reconcile_locked()[0]
    assert ledger.get(operation_id).state == "authorized"
    ledger.close()


def test_candidate_rejects_linux_and_unknown_job_result_contracts() -> None:
    from steam_agent.execution.content_plane import ContentResult

    candidate, binding, native = backend()
    candidate.begin_effect(binding, operation="install")
    before = dict(native.files)
    with pytest.raises(WindowsContentError, match="JOB_RESULT_REQUIRED"):
        candidate.finish_effect(binding, result=ContentResult("installed", Path("private-log")))
    with pytest.raises(WindowsContentError, match="JOB_RESULT_REQUIRED"):
        candidate.finish_effect(binding, result=job_result(binding, process_backend="future-job/2"))
    assert native.files == before


@pytest.mark.parametrize("mismatch", ["appid", "operation_id", "operation", "target_identity"])
def test_candidate_cannot_complete_receipt_with_another_job_result(mismatch: str) -> None:
    candidate, binding, native = backend()
    candidate.begin_effect(binding, operation="install")
    other = {
        "appid": 481, "operation_id": 2, "operation": "verify",
        "target_identity": identity("another-job-target"),
    }
    result = replace(job_result(binding), **{mismatch: other[mismatch]})
    before = dict(native.files)
    with pytest.raises(WindowsContentError, match="JOB_RESULT_UNBOUND"):
        candidate.finish_effect(binding, result=result)
    assert native.files == before
    assert candidate.inspect_effect(binding).state == "prepared"
    with pytest.raises(WindowsContentError, match="EFFECT_UNCONFIRMED"):
        candidate.adopt(binding, source=Path("C:\\elsewhere\\appmanifest_480.acf"))


def test_candidate_verify_result_and_replaced_target_binding() -> None:
    candidate, binding, native = backend()
    candidate.begin_effect(binding, operation="verify")
    with pytest.raises(WindowsContentError, match="JOB_RESULT_UNBOUND"):
        candidate.finish_effect(binding, result=job_result(binding))
    result = job_result(binding, operation="verify")
    target = PureWindowsPath("C:\\library\\steamapps\\common\\Fixture")
    native.identities[target] = identity("replacement")
    before = dict(native.files)
    with pytest.raises(WindowsContentError, match="IDENTITY_CHANGED"):
        candidate.finish_effect(binding, result=result)
    assert native.files == before
    del native.identities[target]
    assert candidate.finish_effect(binding, result=result).state == "completed"


def test_windows_adapter_confirms_target_before_returning_bound_verify_result(tmp_path: Path, monkeypatch) -> None:
    from steam_agent.execution.content_plane import ContentResult

    filesystem = IdentityFilesystem()
    adapter = WindowsSteamcmdAdapter(
        steamcmd_executable=Path("C:\\private-cache\\steamcmd.exe"),
        private_installation=Path("C:\\private-cache"), client_installation=Path("C:\\client"),
        log_dir=tmp_path, filesystem=filesystem,
    )
    monkeypatch.setattr(content_plane.SteamcmdAdapter, "install", lambda *_args, **_kwargs: ContentResult("installed", tmp_path / "log"))
    target = Path("C:\\library\\fixture")
    result = adapter.install(account="synthetic-login", appid=480, install_dir=target, operation_id=7, validate=True)
    assert result.operation == "verify" and result.operation_id == 7 and result.appid == 480
    assert result.target_identity == filesystem_identity(target)

    def replace_target(*_args, **_kwargs):
        filesystem.changed = True
        return ContentResult("installed", tmp_path / "log")

    monkeypatch.setattr(content_plane.SteamcmdAdapter, "install", replace_target)
    with pytest.raises(ContentProcessError, match="target identity changed"):
        adapter.install(account="synthetic-login", appid=480, install_dir=target, operation_id=8)

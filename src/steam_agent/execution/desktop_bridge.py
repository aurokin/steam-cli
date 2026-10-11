"""One-shot Windows interactive-token bridge for an existing owner's desktop.

The SSH process submits bounded broker argv to a fixed Python worker. Task
Scheduler selects the explicit same-user console session; no credentials,
elevation, fallback session, or arbitrary executable are accepted. Missing
receipts remain unknown and never imply that an operation did not run.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Callable, Literal, Protocol

import psutil

from steam_agent.execution.runtime import ProbeError, windows_session_id

_LIMIT = 64 * 1024
_JOB_TTL = 120
_POLL_TIMEOUT = 30
_TOKEN = re.compile(r"[0-9a-f]{32}")
_COMMANDS = frozenset({
    "runtime", "capabilities", "status", "inspect", "run", "preflight",
    "launch", "stop", "install", "verify",
})
_SOURCE = "steam-agent-desktop-bridge/0.1"


class DesktopBridgeError(RuntimeError):
    """The bridge cannot prove its session, job, task, or receipt."""


@dataclass(frozen=True, slots=True)
class DesktopResult:
    exit_code: int
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class DesktopSession:
    session_id: int
    user_id: str
    user_sid: str


class DesktopBackend(Protocol):
    def select_session(self) -> DesktopSession: ...
    def current_identity(self) -> tuple[str, int]: ...
    def protect_directory(self, path: Path) -> None: ...
    def start(self, token: str, state_dir: Path, session: DesktopSession) -> None: ...
    def state(self, token: str) -> Literal["running", "done", "unknown"]: ...
    def remove(self, token: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class DesktopTaskSnapshot:
    """Private exact task identity, read afresh by recovery and deletion."""

    source: str
    principal: str  # Canonical SID, independent of Task Scheduler name formatting.
    logon_type: int
    run_level: int
    action_path: str
    action_arguments: str
    working_directory: str
    instance_count: int
    state: Literal["queued", "running", "done", "unknown"]


class DesktopRecoveryBackend(Protocol):
    def current_identity(self) -> tuple[str, int]: ...
    def inspect_task(self, token: str, created_at: float) -> DesktopTaskSnapshot | None: ...
    def remove_completed(
        self, token: str, created_at: float, expected: DesktopTaskSnapshot,
    ) -> bool: ...


def _job_id(token: str) -> str:
    # Public recovery handles omit the private scheduler/job token.
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def _request_id(argv: list[str]) -> str | None:
    from steam_agent.execution.broker_main import build_parser

    parsed = build_parser().parse_args(argv)
    return getattr(parsed, "request_id", None)


def _worker_arguments(state_dir: Path, token: str) -> str:
    return subprocess.list2cmdline([
        "-m", "steam_agent.execution.desktop_bridge", "--state-dir", str(state_dir),
        "--job", token,
    ])


def _scheduler_run_epoch(value: datetime) -> float:
    # Task Scheduler supplies local wall time as a COM DATE. pywin32 attaches
    # UTC tzinfo without converting those fields. Use the host's local zone;
    # an ambiguous fall-back hour must not certify a possibly older run.
    local = value.replace(tzinfo=None)
    return min(local.replace(fold=0).timestamp(), local.replace(fold=1).timestamp())


def _owned_task(
    task: DesktopTaskSnapshot, request: dict[str, object], state_dir: Path, token: str,
) -> bool:
    return (
        task.source == f"{_SOURCE}:{token}"
        and task.principal == request.get("user_sid")
        and task.logon_type == 3 and task.run_level == 0
        and task.action_path == str(Path(sys.executable).resolve())
        and task.action_path == request.get("interpreter")
        and task.action_arguments == _worker_arguments(state_dir, token)
        and task.working_directory == str(state_dir)
    )


def inspect_desktop_job(
    state_dir: Path, job_id: str, *, backend: DesktopRecoveryBackend | None = None,
    cleanup: bool = False,
) -> dict[str, object]:
    """Collect only the original job; never submit or resume an operation.

    Completion, owner and exact action are checked again by the backend at
    deletion. A receipt alone does not prove that a queued task is safe to delete.
    """
    result: dict[str, object] = {
        "schema": "desktop-job/0.1", "job_id": job_id, "state": "unknown",
        "request_id": None, "receipt": None, "cleaned": False,
        "retry": {"category": "inspect_original_job", "automatic": False},
    }
    try:
        state_dir = state_dir.resolve(strict=True)
        root = state_dir / "desktop-jobs"
        if root.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise DesktopBridgeError("unsafe job")
        matches: list[Path] = []
        for index, path in enumerate(root.iterdir()):
            if index >= 1000:
                break
            if _TOKEN.fullmatch(path.name) and _job_id(path.name) == job_id:
                matches.append(path)
        if len(matches) != 1 or matches[0].is_symlink():
            return {**result, "reason_code": "desktop_job_not_found"}
        job_dir = matches[0]
        token = job_dir.name
        request = _read_json(job_dir / "request.json")
        created = request.get("created_at")
        expires = request.get("expires_at")
        argv = request.get("argv")
        if (request.get("schema") != _SOURCE or request.get("job_token") != token
                or isinstance(created, bool) or not isinstance(created, (float, int))
                or not 0 <= created <= 10**12
                or isinstance(expires, bool) or not isinstance(expires, (float, int))
                or not 0 <= expires <= 10**12 or not 0 < expires - created <= _JOB_TTL
                or not isinstance(request.get("session_id"), int) or isinstance(request.get("session_id"), bool)
                or not isinstance(request.get("user_id"), str) or not request.get("user_id")
                or not isinstance(argv, list) or not all(isinstance(item, str) for item in argv)):
            raise DesktopBridgeError("malformed job")
        checked = _checked_argv(argv)
        scheduler = backend or NativeTaskScheduler()
        sid, _ = scheduler.current_identity()
        if request.get("user_sid") != sid:
            return {**result, "reason_code": "desktop_job_foreign"}
        task = scheduler.inspect_task(token, float(created))
        if task is None or not _owned_task(task, request, state_dir, token):
            return {**result, "reason_code": "desktop_task_identity_unknown"}
        result["request_id"] = _request_id(checked)
        result["state"] = task.state
        result_path = job_dir / "result.json"
        if result_path.exists():
            received = _receipt(_read_json(result_path), token)
            result["receipt"] = {
                "exit_code": received.exit_code, "payload": _sanitize(received.payload, (str(state_dir), sid)),
            }
        if (cleanup and task.state == "done" and task.instance_count == 0
                and result["receipt"] is not None
                and scheduler.remove_completed(token, float(created), task)):
            shutil.rmtree(job_dir)
            result["cleaned"] = True
        return result
    except (DesktopBridgeError, OSError, ValueError, TypeError):
        return {**result, "reason_code": "desktop_job_unknown"}


def discover_desktop_jobs(
    state_dir: Path, *, backend: DesktopRecoveryBackend | None = None, limit: int = 20,
) -> dict[str, object]:
    """Bounded private-directory discovery without registration or cleanup."""
    if not 0 <= limit <= 100:
        raise DesktopBridgeError("job limit must be between 0 and 100")
    root = state_dir / "desktop-jobs"
    if root.is_symlink():
        raise DesktopBridgeError("unsafe job directory")
    if not root.exists():
        return {"schema": "desktop-jobs/0.1", "jobs": [], "truncated": False}
    # Cap directory traversal as well as returned results; an oversized private
    # directory cannot turn discovery into an unbounded scan.
    tokens: list[str] = []
    capped = False
    for index, path in enumerate(root.iterdir()):
        if index >= 1000:
            capped = True
            break
        if _TOKEN.fullmatch(path.name):
            tokens.append(path.name)
    tokens.sort()
    return {
        "schema": "desktop-jobs/0.1",
        "jobs": [inspect_desktop_job(state_dir, _job_id(token), backend=backend)
                 for token in tokens[:limit]],
        "truncated": len(tokens) > limit or capped,
    }


def _unknown(reason: str) -> DesktopResult:
    return DesktopResult(2, {"schema": "desktop-dispatch/0.1", "state": "unknown", "reason_code": reason})


def _checked_argv(argv: list[str]) -> list[str]:
    if (
        not argv
        or not all(isinstance(value, str) and "\0" not in value for value in argv)
        or "--desktop" in argv or any(value.startswith("--state-dir") for value in argv)
    ):
        raise DesktopBridgeError("unsupported desktop command")
    from steam_agent.execution.broker_main import build_parser

    # Parse a copy before creating a task; rejected verbs cannot consume stdin
    # or produce confirmation/configuration side effects in the desktop.
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        try:
            parsed = build_parser().parse_args(argv)
        except SystemExit:
            raise DesktopBridgeError("malformed desktop command") from None
    if parsed.command not in _COMMANDS:
        raise DesktopBridgeError("unsupported desktop command")
    return list(argv)


def _read_json(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise DesktopBridgeError("symlink job file")
    with path.open("rb") as handle:
        raw = handle.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise DesktopBridgeError("job file too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise DesktopBridgeError("malformed job file")
    return value


def _write_json(path: Path, value: dict[str, object]) -> None:
    raw = json.dumps(value, sort_keys=True).encode("utf-8")
    if len(raw) > _LIMIT:
        raise DesktopBridgeError("job file too large")
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _receipt(value: dict[str, object], token: str) -> DesktopResult:
    code = value.get("exit_code")
    payload = value.get("payload")
    if (
        value.get("job_token") != token
        or isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255
        or not isinstance(payload, dict)
    ):
        raise DesktopBridgeError("malformed desktop receipt")
    return DesktopResult(code, payload)


def dispatch_in_desktop(
    argv: list[str], state_dir: Path, *, backend: DesktopBackend | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], float] = time.time,
) -> DesktopResult:
    """Dispatch fixed broker argv and wait at most 30 seconds for a receipt.

    Running/unknown tasks and jobs without a validated receipt are retained.
    An owner inspects the original request ID or operation before considering
    a retry.
    """
    job_dir: Path | None = None
    started = False
    token = secrets.token_hex(16)
    scheduler = backend or NativeTaskScheduler()
    try:
        checked = _checked_argv(argv)
        session = scheduler.select_session()
        if session.session_id <= 0 or not session.user_id or not session.user_sid:
            return _unknown("interactive_session_unavailable")
        state_dir = state_dir.resolve(strict=True)
        root = state_dir / "desktop-jobs"
        if root.is_symlink():
            return _unknown("desktop_job_directory_unsafe")
        root.mkdir(mode=0o700, exist_ok=True)
        scheduler.protect_directory(root)
        job_dir = root / token
        job_dir.mkdir(mode=0o700)
        scheduler.protect_directory(job_dir)
        created = now()
        _write_json(job_dir / "request.json", {
            "schema": _SOURCE, "job_token": token, "argv": checked,
            "created_at": created, "expires_at": created + _JOB_TTL,
            "session_id": session.session_id, "user_sid": session.user_sid,
            "user_id": session.user_id,
            "interpreter": str(Path(sys.executable).resolve()),
        })
        # A registration/start failure can still have started a child. Once
        # submission begins, cleanup requires proof that the task is idle.
        started = True
        scheduler.start(token, state_dir, session)
        deadline = monotonic() + _POLL_TIMEOUT
        received: DesktopResult | None = None
        while True:
            result_path = job_dir / "result.json"
            if result_path.exists():
                received = _receipt(_read_json(result_path), token)
            state = scheduler.state(token)
            if state == "done":
                # A fast worker may publish between the first file probe and
                # the scheduler probe. Read again after proven completion.
                if result_path.exists():
                    received = _receipt(_read_json(result_path), token)
                if received is None:
                    return DesktopResult(2, {
                        **_unknown("desktop_worker_no_receipt").payload,
                        "job_id": _job_id(token),
                        "request_id": _request_id(checked),
                        "retry": {"category": "collect_original_job", "automatic": False},
                    })
                if scheduler.remove(token):
                    shutil.rmtree(job_dir)
                return received
            if monotonic() >= deadline:
                if received is not None:
                    return received
                return DesktopResult(2, {
                    **_unknown("desktop_worker_timeout").payload,
                    "job_id": _job_id(token),
                    "request_id": _request_id(checked),
                    "retry": {"category": "collect_original_job", "automatic": False},
                })
            sleep(0.1)
    except (DesktopBridgeError, OSError, ValueError, TypeError):
        # Never expose COM errors, usernames, private paths, or raw child stderr.
        if job_dir is not None and not started:
            shutil.rmtree(job_dir, ignore_errors=True)
        return _unknown("desktop_bridge_unavailable")


class _BoundedOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self._bytes = 0

    def write(self, value: str) -> int:
        self._bytes += len(value.encode("utf-8"))
        if self._bytes > _LIMIT:
            raise DesktopBridgeError("broker output too large")
        return super().write(value)


def _sanitize(value: object, private: tuple[str, ...]) -> object:
    if isinstance(value, dict):
        return {
            key: _sanitize(item, private) for key, item in value.items()
            if key not in {"path", "paths", "account", "account_alias", "steam_login", "username", "user_sid"}
        }
    if isinstance(value, list):
        return [_sanitize(item, private) for item in value]
    if isinstance(value, str):
        for item in private:
            if item:
                value = re.sub(re.escape(item), "<private>", value, flags=re.IGNORECASE)
        return re.sub(r"(?:[A-Za-z]:[\\/]|/)[^\s\"']+[\\/][^\s\"']*", "<path>", value)
    return value


def run_worker(
    state_dir: Path, token: str, *, backend: DesktopBackend | None = None,
    broker: Callable[[list[str]], int] | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    """Validate one private job in the selected interactive session and run it."""
    if not _TOKEN.fullmatch(token):
        return 2
    try:
        state_dir = state_dir.resolve(strict=True)
        root = state_dir / "desktop-jobs"
        job_dir = root / token
        if root.is_symlink() or job_dir.is_symlink():
            return 2
        request = _read_json(job_dir / "request.json")
        created = request.get("created_at")
        expires = request.get("expires_at")
        argv = request.get("argv")
        expected_session = request.get("session_id")
        if (
            request.get("schema") != _SOURCE or request.get("job_token") != token
            or isinstance(created, bool) or not isinstance(created, (int, float))
            or isinstance(expires, bool) or not isinstance(expires, (int, float))
            or not 0 < expires - created <= _JOB_TTL or not created <= now() < expires
            or not isinstance(argv, list) or not all(isinstance(item, str) for item in argv)
            or isinstance(expected_session, bool) or not isinstance(expected_session, int)
            or expected_session <= 0 or not isinstance(request.get("user_sid"), str)
        ):
            return 2
        scheduler = backend or NativeTaskScheduler()
        sid, session = scheduler.current_identity()
        if session <= 0 or session != expected_session or sid != request.get("user_sid"):
            return 2
        checked = _checked_argv(argv)
        if broker is None:
            from steam_agent.execution.broker_main import main

            broker = main
        output = _BoundedOutput()
        with redirect_stdout(output), redirect_stderr(_BoundedOutput()):
            try:
                selected = checked if any(value.startswith("--format") for value in checked) else [
                    "--format", "json", *checked,
                ]
                code = broker(["--state-dir", str(state_dir), *selected])
                parsed = json.loads(output.getvalue())
                if not isinstance(parsed, dict):
                    raise DesktopBridgeError("broker output is not an object")
                sanitized = _sanitize(parsed, (str(state_dir), sid))
                if not isinstance(sanitized, dict):
                    raise DesktopBridgeError("broker output is not an object")
                result = DesktopResult(code, sanitized)
            except (Exception, SystemExit):
                result = _unknown("desktop_worker_result_unknown")
        _write_json(job_dir / "result.json", {
            "job_token": token, "exit_code": result.exit_code, "payload": result.payload,
        })
        return 0
    except (DesktopBridgeError, OSError, ValueError, TypeError):
        return 2


@dataclass(slots=True)
class _ScheduledTask:
    registered: object
    last_run_before_dispatch: object
    running_instance: object | None = None
    observed_started: bool = False
    expected_identity: DesktopTaskSnapshot | None = None
    created_at: float | None = None


class NativeTaskScheduler:
    """Microsoft Task Scheduler COM with an existing same-user console token."""
    def __init__(self) -> None:
        self._tasks: dict[str, _ScheduledTask] = {}
        self._folder = None

    def _windows(self):
        if sys.platform != "win32":
            raise DesktopBridgeError("unsupported platform")
        try:
            import win32api
            import win32con
            import win32security
            import win32com.client
        except ImportError:
            raise DesktopBridgeError("desktop bridge dependencies unavailable") from None
        return win32api, win32con, win32security, win32com.client

    def current_identity(self) -> tuple[str, int]:
        api, constants, security, _ = self._windows()
        try:
            token = security.OpenProcessToken(api.GetCurrentProcess(), constants.TOKEN_QUERY)
            try:
                sid = security.GetTokenInformation(token, security.TokenUser)[0]
                elevated = security.GetTokenInformation(token, security.TokenElevation)
            finally:
                token.Close()
            session = windows_session_id(os.getpid())
            # Scheduler ignores LUA for some account/UAC configurations.
            # A desktop worker must verify the resulting token itself. The
            # SSH source may have a full token in session 0; it is not the
            # process that is authorized to operate on the desktop.
            if session > 0 and elevated:
                raise DesktopBridgeError("desktop worker is elevated")
            return security.ConvertSidToStringSid(sid), session
        except Exception:
            raise DesktopBridgeError("desktop identity unavailable") from None

    def select_session(self) -> DesktopSession:
        self._windows()
        try:
            import ctypes
            from ctypes import wintypes

            function = ctypes.WinDLL("kernel32", use_last_error=True).WTSGetActiveConsoleSessionId
            function.argtypes = []
            function.restype = wintypes.DWORD
            console = function()
            if console in {0, 0xFFFFFFFF}:
                raise DesktopBridgeError("interactive session unavailable")
            user = psutil.Process().username()
            matches: set[int] = set()
            for process in psutil.process_iter():
                try:
                    if process.name().casefold() != "explorer.exe":
                        continue
                    if windows_session_id(process.pid) != console:
                        continue
                    if process.username().casefold() == user.casefold():
                        matches.add(console)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            if matches != {console}:
                raise DesktopBridgeError("same-user console unavailable")
            sid, _ = self.current_identity()
            return DesktopSession(console, user, sid)
        except (psutil.Error, OSError, ProbeError):
            raise DesktopBridgeError("interactive session unavailable") from None

    def protect_directory(self, path: Path) -> None:
        _, _, security, _ = self._windows()
        sid, _ = self.current_identity()
        try:
            descriptor = security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{sid})", security.SDDL_REVISION_1,
            )
            security.SetFileSecurity(
                # pywin32 311 parses this DWORD through a signed C long.
                # Preserve the protected-DACL bit without overflowing it.
                str(path), (security.DACL_SECURITY_INFORMATION | 0x80000000) - (1 << 32), descriptor,
            )
        except Exception:
            raise DesktopBridgeError("private job directory unavailable") from None

    def start(self, token: str, state_dir: Path, session: DesktopSession) -> None:
        _, _, _, client = self._windows()
        try:
            service = client.Dispatch("Schedule.Service")
            service.Connect()
            folder = service.GetFolder("\\")
            definition = service.NewTask(0)
            definition.RegistrationInfo.Source = f"{_SOURCE}:{token}"
            definition.Principal.UserId = session.user_id
            definition.Principal.LogonType = 3  # INTERACTIVE_TOKEN, no password.
            definition.Principal.RunLevel = 0  # LUA, never elevation.
            definition.Settings.Enabled = True
            definition.Settings.AllowDemandStart = True
            definition.Settings.DisallowStartIfOnBatteries = False
            definition.Settings.StopIfGoingOnBatteries = False
            definition.Settings.MultipleInstances = 2  # Ignore new instances.
            definition.Settings.ExecutionTimeLimit = "PT2M"
            action = definition.Actions.Create(0)
            action.Path = str(Path(sys.executable).resolve())
            action.Arguments = _worker_arguments(state_dir, token)
            action.WorkingDirectory = str(state_dir)
            name = f"steam-agent-desktop-{token}"
            task = folder.RegisterTaskDefinition(name, definition, 2, session.user_id, None, 3)
            self._folder = folder
            scheduled = _ScheduledTask(
                task, task.LastRunTime,
                expected_identity=DesktopTaskSnapshot(
                    f"{_SOURCE}:{token}", session.user_sid, 3, 0,
                    str(Path(sys.executable).resolve()), _worker_arguments(state_dir, token),
                    str(state_dir), 0, "unknown",
                ),
                created_at=time.time(),
            )
            self._tasks[token] = scheduled
            scheduled.running_instance = task.RunEx(None, 4, session.session_id, session.user_id)
        except Exception:
            raise DesktopBridgeError("desktop task unavailable") from None

    def _reopen_task(self, token: str):
        if not _TOKEN.fullmatch(token):
            raise DesktopBridgeError("malformed task token")
        _, _, _, client = self._windows()
        service = client.Dispatch("Schedule.Service")
        service.Connect()
        folder = service.GetFolder("\\")
        return folder, folder.GetTask(f"steam-agent-desktop-{token}")

    def inspect_task(self, token: str, created_at: float) -> DesktopTaskSnapshot | None:
        """Reopen exact metadata without relying on this process's task cache."""
        try:
            _, task = self._reopen_task(token)
            definition = task.Definition
            if (definition.Actions.Count != 1 or definition.Triggers.Count != 0
                    or definition.Settings.MultipleInstances != 2):
                return None
            action = definition.Actions.Item(1)
            if action.Type != 0:
                return None
            instances = int(task.GetInstances(0).Count)
            state: Literal["queued", "running", "done", "unknown"] = "unknown"
            if instances or task.State == 4:
                state = "running"
            elif task.State == 2:
                state = "queued"
            elif task.State == 3 and _scheduler_run_epoch(task.LastRunTime) >= math.floor(created_at):
                state = "done"
            _, _, security, _ = self._windows()
            principal = str(definition.Principal.UserId)
            sid = (
                security.ConvertStringSidToSid(principal) if principal.startswith("S-1-")
                else security.LookupAccountName(None, principal)[0]
            )
            return DesktopTaskSnapshot(
                source=str(definition.RegistrationInfo.Source),
                principal=security.ConvertSidToStringSid(sid),
                logon_type=int(definition.Principal.LogonType),
                run_level=int(definition.Principal.RunLevel),
                action_path=str(action.Path), action_arguments=str(action.Arguments),
                working_directory=str(action.WorkingDirectory),
                instance_count=instances, state=state,
            )
        except Exception:
            return None

    def remove_completed(
        self, token: str, created_at: float, expected: DesktopTaskSnapshot,
    ) -> bool:
        # A second connection/read closes stale-snapshot mistakes. Unknown,
        # foreign, queued and running jobs always remain available to collect.
        current = self.inspect_task(token, created_at)
        if (current != expected or current is None or current.state != "done"
                or current.instance_count or current.source != f"{_SOURCE}:{token}"):
            return False
        try:
            folder, _ = self._reopen_task(token)
            # Reopen itself does not certify identity; repeat the metadata and
            # idle check immediately before the sole scheduler mutation.
            if self.inspect_task(token, created_at) != current:
                return False
            folder.DeleteTask(f"steam-agent-desktop-{token}", 0)
            return True
        except Exception:
            return False

    def state(self, token: str) -> Literal["running", "done", "unknown"]:
        scheduled = self._tasks.get(token)
        if scheduled is None:
            return "unknown"
        try:
            task = scheduled.registered
            task_state = task.State
            if task_state == 4:
                scheduled.observed_started = True
            if task.GetInstances(0).Count or task_state in {2, 4}:
                return "running"
            if task.LastRunTime != scheduled.last_run_before_dispatch:
                scheduled.observed_started = True
            # Ready also describes a task that has never begun. RunEx's
            # successful return alone must not let cleanup delete a queued
            # request before its worker starts.
            return "done" if task_state == 3 and scheduled.observed_started else "unknown"
        except Exception:
            return "unknown"

    def remove(self, token: str) -> bool:
        scheduled = self._tasks.get(token)
        if scheduled is None or self._folder is None or self.state(token) != "done":
            return False
        if scheduled.created_at is None or scheduled.expected_identity is None:
            return False
        current = self.inspect_task(token, scheduled.created_at)
        if current is None:
            return False
        from dataclasses import replace

        expected = replace(scheduled.expected_identity, state="done", instance_count=0)
        if current != expected or not self.remove_completed(token, scheduled.created_at, expected):
            return False
        del self._tasks[token]
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--job", required=True)
    arguments = parser.parse_args(argv)
    return run_worker(arguments.state_dir, arguments.job)


if __name__ == "__main__":
    raise SystemExit(main())

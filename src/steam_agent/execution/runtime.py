"""Observe and stop positively attributed OS processes, without Steam file writes.

Environment attribution is evidence of an OS process, never proof of game
readiness. A termination request also cannot prove that a game saved its state.
All process access and window operations are injectable for synthetic tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
import sys
import time
from typing import Literal, Protocol

import psutil

APP_ID_VARIABLES = ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID")
# Shared client/runtime infrastructure can inherit game environment variables.
# These names exclude targets; a name is never positive game attribution.
_INFRASTRUCTURE = frozenset({
    "steam", "steam.exe", "steamwebhelper", "steamwebhelper.exe",
    "steamservice.exe", "steamerrorreporter.exe", "steamcmd", "steamcmd.exe",
    "wineserver", "wineserver64", "reaper", "pressure-vessel-wrap",
    "steam-runtime-launcher-service", "steam-runtime-launch-client",
    "services.exe", "winedevice.exe", "explorer.exe",
})


class ProbeError(Exception):
    """A sanitized process probe failure; never include OS exception text."""


def _app_environment(environment: Mapping[str, str], *, platform: str) -> dict[str, str]:
    if platform != "win32":
        return {key: value for key, value in environment.items() if key in APP_ID_VARIABLES}
    # Windows environment names are case-insensitive; psutil can return them
    # uppercased. Keep our canonical evidence keys and reject ambiguous aliases.
    names = {key.casefold(): key for key in APP_ID_VARIABLES}
    selected: dict[str, str] = {}
    for key, value in environment.items():
        canonical = names.get(key.casefold())
        if canonical is None:
            continue
        if canonical in selected and selected[canonical] != value:
            raise ProbeError("process_probe_failed")
        selected[canonical] = value
    return selected


StopFailureReason = Literal[
    "stop_probe_failed", "stop_dispatch_gate_denied", "stop_dispatch_gate_unreadable",
]


class StopRequestError(ProbeError):
    """A failed dispatch may already have requested exit for this process."""

    def __init__(
        self, reason_code: StopFailureReason, *, requested: bool = False,
        probe_reason: Literal[
            "process_identity_changed", "window_identity_changed", "window_close_failed",
        ] | None = None,
    ) -> None:
        super().__init__(probe_reason or reason_code)
        self.reason_code = reason_code
        self.requested = requested
        self.probe_reason = probe_reason


def _check_stop_dispatch_gate(before_request: Callable[[], bool] | None) -> None:
    if before_request is None:
        return
    try:
        permitted = before_request()
    except Exception:
        raise StopRequestError("stop_dispatch_gate_unreadable") from None
    if permitted is not True:
        raise StopRequestError("stop_dispatch_gate_denied")


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    create_time: float
    name: str
    app_environment: Mapping[str, str]
    same_user: bool
    same_session: bool = True


@dataclass(frozen=True, slots=True)
class ProcessScan:
    processes: tuple[ProcessRecord, ...] = ()
    reason_codes: tuple[str, ...] = ()


class ProcessBackend(Protocol):
    def scan(self) -> ProcessScan: ...
    def read(self, pid: int) -> ProcessRecord | None: ...
    def request_stop(
        self, process: ProcessRecord, *, force: bool,
        before_request: Callable[[], bool] | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ObservedProcess:
    appid: int
    pid: int
    create_time: float
    evidence_variables: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "appid": self.appid, "pid": self.pid,
            "create_time": self.create_time,
            "evidence_variables": list(self.evidence_variables),
            "support_level": "os_process_observed",
        }


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    state: Literal["observed", "absent", "unknown"]
    processes: tuple[ObservedProcess, ...]
    reason_codes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "processes": [process.to_json() for process in self.processes],
            "reason_codes": list(self.reason_codes),
            "support_level": "os_process_observed",
        }


@dataclass(frozen=True, slots=True)
class StopResult:
    state: Literal["stopped", "absent", "deferred", "unknown"]
    reason_code: str
    process_count: int
    force: bool
    requested_count: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state, "reason_code": self.reason_code,
            "process_count": self.process_count, "force": self.force,
            "requested_count": self.requested_count,
        }


def _attribution(process: ProcessRecord) -> tuple[int | None, tuple[str, ...], bool]:
    if not process.same_user or not process.same_session:
        return None, (), False
    if process.name.casefold() in _INFRASTRUCTURE:
        return None, (), False
    values: dict[str, int] = {}
    for variable in APP_ID_VARIABLES:
        value = process.app_environment.get(variable)
        if value is None:
            continue
        # AppIDs are unsigned 32-bit decimals. Bound untrusted environment
        # text before integer conversion, including pathological zero padding.
        if len(value) > 10 or not value.isascii() or not value.isdecimal():
            return None, (), True
        number = int(value)
        if not 0 < number <= 0xFFFFFFFF:
            return None, (), True
        values[variable] = number
    if len(set(values.values())) > 1:
        return None, (), True
    return next(iter(values.values()), None), tuple(values), False


class RuntimeObserver:
    def __init__(
        self, *, platform: str | None = None,
        backend: ProcessBackend | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.platform = platform or sys.platform
        self.backend = backend or PsutilBackend(platform=self.platform)
        self._sleep = sleep
        self._monotonic = monotonic

    def observe(self, appid: int | None = None) -> RuntimeObservation:
        if self.platform not in {"linux", "win32"}:
            return RuntimeObservation("unknown", (), ("unsupported_platform",))
        try:
            scan = self.backend.scan()
        except ProbeError:
            return RuntimeObservation("unknown", (), ("process_probe_failed",))
        reasons = set(scan.reason_codes)
        observed: list[ObservedProcess] = []
        for process in scan.processes:
            attributed, variables, invalid = _attribution(process)
            if invalid:
                reasons.add("ambiguous_appid_environment")
            if attributed is not None and (appid is None or attributed == appid):
                observed.append(ObservedProcess(
                    attributed, process.pid, process.create_time, variables,
                ))
        observed.sort(key=lambda process: (process.appid, process.pid, process.create_time))
        state = "unknown" if reasons else "observed" if observed else "absent"
        return RuntimeObservation(state, tuple(observed), tuple(sorted(reasons)))

    def stop_app(
        self, appid: int, *, force: bool = False, timeout: float = 10.0,
        before_request: Callable[[], bool] | None = None,
    ) -> StopResult:
        """Request exit only for fresh, same-user identities attributed to this AppID.

        Force must be authorized by the caller's independent policy grant. No
        timeout or missing-window condition silently escalates to force.
        Incomplete scans permit requests only to their positively attributed
        identities; only a complete confirmation scan can establish absence.
        The caller can revalidate its authorization after a slow scan and
        before each request. Denial never undoes requests already sent.
        """
        if not 0 < appid <= 0xFFFFFFFF or timeout < 0:
            raise ValueError("invalid stop request")
        observation = self.observe(appid)
        count = len(observation.processes)
        if observation.state == "unknown" and not observation.processes:
            return StopResult("unknown", "runtime_observation_unknown", count, force)
        if observation.state == "absent":
            return StopResult("absent", "no_attributed_process", 0, force)
        requested = 0
        for target in observation.processes:
            try:
                current = self.backend.read(target.pid)
                if current is None:
                    continue
                attributed, _, invalid = _attribution(current)
                if (current.create_time != target.create_time or invalid or attributed != appid):
                    return StopResult(
                        "deferred", "process_identity_changed", count, force, requested,
                    )
                _check_stop_dispatch_gate(before_request)
                if self.backend.request_stop(
                    current, force=force, before_request=before_request,
                ):
                    requested += 1
            except StopRequestError as error:
                requested += int(error.requested)
                return StopResult(
                    "deferred" if error.reason_code == "stop_dispatch_gate_denied" else "unknown",
                    error.reason_code, count, force, requested,
                )
            except ProbeError:
                return StopResult("unknown", "stop_probe_failed", count, force, requested)
        deadline = self._monotonic() + timeout
        while True:
            after = self.observe(appid)
            if after.state == "absent":
                return StopResult(
                    "stopped", "attributed_processes_exited", count, force, requested,
                )
            if after.state == "unknown":
                return StopResult("unknown", "stop_confirmation_unknown", count, force, requested)
            if requested == 0:
                return StopResult("deferred", "close_request_unavailable", count, force, requested)
            if self._monotonic() >= deadline:
                return StopResult("deferred", "stop_timeout", count, force, requested)
            self._sleep(min(0.1, max(0, deadline - self._monotonic())))


def windows_session_id(pid: int) -> int:
    """Read the Windows session ID without exposing account identity."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.ProcessIdToSessionId
    function.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    function.restype = wintypes.BOOL
    session = wintypes.DWORD()
    if not function(pid, ctypes.byref(session)):
        raise ProbeError("session_probe_failed")
    return session.value


class WindowBackend(Protocol):
    def windows(self) -> tuple[int, ...]: ...
    def owner(self, window: int) -> int: ...
    def close(self, window: int) -> bool: ...


class NativeWindows:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._types = wintypes
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self._user.EnumWindows.argtypes = [self._callback, wintypes.LPARAM]
        self._user.EnumWindows.restype = wintypes.BOOL
        self._user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self._user.PostMessageW.restype = wintypes.BOOL

    def windows(self) -> tuple[int, ...]:
        found: list[int] = []

        @self._callback
        def collect(window: int, _: int) -> bool:
            found.append(window)
            return True

        if not self._user.EnumWindows(collect, 0):
            raise ProbeError("window_probe_failed")
        return tuple(found)

    def owner(self, window: int) -> int:
        owner = self._types.DWORD()
        if not self._user.GetWindowThreadProcessId(window, self._ctypes.byref(owner)):
            raise ProbeError("window_owner_unknown")
        return owner.value

    def close(self, window: int) -> bool:
        return bool(self._user.PostMessageW(window, 0x0010, 0, 0))


def _windows_close(
    process: ProcessRecord, *, windows: WindowBackend | None = None,
    identity_check: Callable[[], bool],
    before_request: Callable[[], bool] | None = None,
) -> bool:
    """Post WM_CLOSE only to windows still owned by the attributed identity."""
    backend = windows or NativeWindows()
    targets = tuple(window for window in backend.windows() if backend.owner(window) == process.pid)
    if not targets:
        return False
    requested = False
    for window in targets:
        try:
            if not identity_check():
                raise StopRequestError(
                    "stop_probe_failed", probe_reason="process_identity_changed",
                )
            if backend.owner(window) != process.pid:
                raise StopRequestError(
                    "stop_probe_failed", probe_reason="window_identity_changed",
                )
            # Environment and HWND ownership reads can be slow. Owner intent
            # must still hold immediately before each native WM_CLOSE.
            _check_stop_dispatch_gate(before_request)
            if not backend.close(window):
                raise StopRequestError("stop_probe_failed", probe_reason="window_close_failed")
            requested = True
        except StopRequestError as error:
            raise StopRequestError(
                error.reason_code, requested=requested or error.requested,
                probe_reason=error.probe_reason,
            ) from None
        except (ProbeError, OSError):
            raise StopRequestError("stop_probe_failed", requested=requested) from None
    return requested


class PsutilBackend:
    """Read only AppID environment fields; discard all other environment data."""

    def __init__(
        self, *, platform: str | None = None, windows: WindowBackend | None = None,
    ) -> None:
        self.platform = platform or sys.platform
        self._username: str | None = None
        self._session: int | None = None
        self._windows = windows

    def _identity(self, process: psutil.Process) -> tuple[bool, bool]:
        if self.platform == "linux":
            # Both real and effective identities must be the invoking user.
            uids = process.uids()
            return uids.real == os.getuid() and uids.effective == os.geteuid(), True
        if self.platform == "win32":
            if self._username is None:
                self._username = psutil.Process().username().casefold()
                self._session = windows_session_id(os.getpid())
            if self._session == 0:
                raise ProbeError("interactive_session_unavailable")
            if windows_session_id(process.pid) != self._session:
                return False, False
            same_user = process.username().casefold() == self._username
            if not same_user:
                return False, False
            return True, True
        raise ProbeError("unsupported_platform")

    def read(self, pid: int) -> ProcessRecord | None:
        if pid in {0, os.getpid()}:
            return None
        try:
            process = psutil.Process(pid)
            same_user, same_session = self._identity(process)
            if not same_user or not same_session:
                return None
            created = process.create_time()
            name = process.name()
            if process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
                return None
            environment = {} if name.casefold() in _INFRASTRUCTURE else _app_environment(
                process.environ(), platform=self.platform,
            )
            # A PID recycled while reading must never become an observation.
            if psutil.Process(pid).create_time() != created:
                raise ProbeError("process_identity_changed")
            return ProcessRecord(pid, created, name, environment, True, True)
        except psutil.NoSuchProcess:
            return None
        except (psutil.AccessDenied, OSError):
            raise ProbeError("process_probe_failed") from None

    def scan(self) -> ProcessScan:
        records: list[ProcessRecord] = []
        reasons: set[str] = set()
        try:
            for pid in psutil.pids():
                try:
                    process = self.read(pid)
                    if process is not None:
                        records.append(process)
                except ProbeError:
                    reasons.add("process_probe_failed")
        except (psutil.Error, OSError):
            reasons.add("process_probe_failed")
        return ProcessScan(tuple(records), tuple(sorted(reasons)))

    def request_stop(
        self, process: ProcessRecord, *, force: bool,
        before_request: Callable[[], bool] | None = None,
    ) -> bool:
        try:
            fresh = self.read(process.pid)
            if fresh is None:
                return False
            if fresh != process:
                raise ProbeError("process_identity_changed")
            target = psutil.Process(process.pid)
            if target.create_time() != process.create_time:
                raise ProbeError("process_identity_changed")
            if force:
                _check_stop_dispatch_gate(before_request)
                target.kill()
            elif self.platform == "linux":
                _check_stop_dispatch_gate(before_request)
                target.terminate()
            elif self.platform == "win32":
                return _windows_close(
                    process, windows=self._windows,
                    identity_check=lambda: self.read(process.pid) == process,
                    before_request=before_request,
                )
            else:
                return False
            return True
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, OSError):
            raise ProbeError("stop_request_failed") from None

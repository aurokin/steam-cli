"""Steam-free native stop qualification, confined to spawned fixture identities.

Run with an installed package from the same user's interactive Windows session.
Only the child process handle created here may be killed during bounded cleanup.
Hidden fixture windows are never shown, activated, or given desktop input.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import psutil

from steam_agent.execution.runtime import (
    APP_ID_VARIABLES, NativeWindows, ProbeError, ProcessRecord, ProcessScan,
    PsutilBackend, RuntimeObserver, windows_session_id,
)
from steam_agent.process_environment import child_environment

APPID = 4294967295
CASES = ("graceful", "gate_revocation", "ignored_close", "windowless")
WINDOW_CLASS = "SteamAgentOwnedStopQualification"


def verify_descendant(pid: int, created: float, spawned_pid: int, spawned_created: float, *, process_factory=psutil.Process) -> None:
    """Prove a stable, bounded parent chain to the exact spawned identity."""
    seen: set[int] = set()
    expected = created
    for _ in range(16):
        if pid in seen:
            break
        seen.add(pid)
        process = process_factory(pid)
        if process.create_time() != expected:
            break
        if pid == spawned_pid:
            if expected == spawned_created:
                return
            break
        parent = process.ppid()
        if parent <= 0:
            break
        parent_created = process_factory(parent).create_time()
        if parent_created > expected or process.create_time() != expected or process.ppid() != parent:
            break
        pid, expected = parent, parent_created
    raise ProbeError("fixture_descendant_unconfirmed")


class OwnedProcessHandle:
    """Retain the proved child's native identity across PID reuse and cleanup."""

    def __init__(self, pid: int, created: float) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        self._api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._api.OpenProcess.restype = wintypes.HANDLE
        self._api.GetProcessId.argtypes = [wintypes.HANDLE]
        self._api.GetProcessId.restype = wintypes.DWORD
        self._api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self._api.GetProcessTimes.restype = wintypes.BOOL
        self._api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._api.WaitForSingleObject.restype = wintypes.DWORD
        self._api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._api.TerminateProcess.restype = wintypes.BOOL
        self._api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self._api.GetExitCodeProcess.restype = wintypes.BOOL
        self._api.CloseHandle.argtypes = [wintypes.HANDLE]
        self._api.CloseHandle.restype = wintypes.BOOL
        self._handle = self._api.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        if not self._handle:
            raise ProbeError("fixture_handle_unavailable")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if self._api.GetProcessId(self._handle) != pid or not self._api.GetProcessTimes(self._handle, *(ctypes.byref(value) for value in times)):
                raise ProbeError("fixture_handle_unconfirmed")
            native_created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11644473600
            if not math.isclose(native_created, created, rel_tol=0, abs_tol=0.0001):
                raise ProbeError("fixture_identity_changed")
        except BaseException:
            self.close()
            raise

    def running(self) -> bool:
        result = self._api.WaitForSingleObject(self._handle, 0)
        if result not in (0, 258):
            raise ProbeError("fixture_wait_failed")
        return result == 258

    def wait(self, seconds: float) -> int:
        from ctypes import wintypes

        if self._api.WaitForSingleObject(self._handle, int(seconds * 1000)) != 0:
            raise ProbeError("fixture_cleanup_unconfirmed")
        code = wintypes.DWORD()
        if not self._api.GetExitCodeProcess(self._handle, self._ctypes.byref(code)):
            raise ProbeError("fixture_exit_unknown")
        return code.value

    def kill(self) -> None:
        if not self._api.TerminateProcess(self._handle, 1):
            raise ProbeError("fixture_cleanup_unconfirmed")

    def close(self) -> None:
        if self._handle:
            if not self._api.CloseHandle(self._handle):
                raise ProbeError("fixture_handle_close_failed")
            self._handle = None


class OwnedWindows:
    """Count real native posts while refusing any HWND outside the owned PID."""

    def __init__(self, pid: int, native: NativeWindows, *, class_name=None) -> None:
        self.pid = pid
        self.native = native
        self.close_count = 0
        if class_name is None:
            from win32gui import GetClassName
            class_name = GetClassName
        self.class_name = class_name

    def windows(self) -> tuple[int, ...]:
        return tuple(window for window in self.native.windows() if self.owner(window) == self.pid)

    def owner(self, window: int) -> int:
        owner = self.native.owner(window)
        return owner if owner == self.pid and self.class_name(window) == WINDOW_CLASS else 0

    def close(self, window: int) -> bool:
        if self.owner(window) != self.pid:
            raise ProbeError("fixture_window_not_owned")
        sent = self.native.close(window)
        self.close_count += int(sent)
        return sent


class OwnedBackend(PsutilBackend):
    """Never scan other PIDs or request force, including when a PID is recycled."""

    def __init__(self, pid: int, created: float, windows: OwnedWindows) -> None:
        super().__init__(platform="win32", windows=windows)
        self.pid = pid
        self.created = created

    def read(self, pid: int) -> ProcessRecord | None:
        if pid != self.pid:
            raise ProbeError("fixture_process_not_owned")
        record = super().read(pid)
        if record is not None and record.create_time != self.created:
            raise ProbeError("fixture_identity_changed")
        return record

    def scan(self) -> ProcessScan:
        record = self.read(self.pid)
        return ProcessScan(() if record is None else (record,))

    def request_stop(self, process: ProcessRecord, *, force: bool, before_request=None) -> bool:
        if force or process.pid != self.pid or process.create_time != self.created:
            raise ProbeError("fixture_stop_not_owned")
        return super().request_stop(process, force=False, before_request=before_request)


def fixture_child(mode: str) -> int:
    """Private child route: own hidden HWNDs, fixed protocol, 15-second lifetime."""
    if mode not in CASES or windows_session_id(os.getpid()) == 0:
        return 1
    import win32api
    import win32con
    import win32gui

    handles: list[int] = []
    closed = 0

    def procedure(window, message, wparam, lparam):
        nonlocal closed
        if message == win32con.WM_CLOSE:
            print("CLOSE", flush=True)
            if mode != "ignored_close":
                win32gui.DestroyWindow(window)
            return 0
        if message == win32con.WM_DESTROY:
            closed += 1
            return 0
        return win32gui.DefWindowProc(window, message, wparam, lparam)

    instance = win32api.GetModuleHandle(None)
    window_class = win32gui.WNDCLASS()
    window_class.hInstance = instance
    window_class.lpszClassName = WINDOW_CLASS
    window_class.lpfnWndProc = procedure
    atom = win32gui.RegisterClass(window_class)
    try:
        if mode != "windowless":
            for _ in range(2):
                window = win32gui.CreateWindowEx(
                    win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
                    atom, "Synthetic stop fixture", 0, 0, 0, 1, 1,
                    0, 0, instance, None,
                )
                if not window:
                    return 1
                handles.append(window)
        print(f"READY {os.getpid()} {psutil.Process().create_time():.9f}", flush=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            win32gui.PumpWaitingMessages()
            if handles and closed == len(handles):
                return 0
            time.sleep(0.01)
        return 1
    finally:
        for window in handles:
            if win32gui.IsWindow(window):
                win32gui.DestroyWindow(window)
        win32gui.UnregisterClass(atom, instance)


def _read_protocol(stream, messages: queue.Queue[str]) -> None:
    # This owned child emits at most READY and two CLOSE lines. Bound reads so
    # accidental changes cannot introduce unbounded output capture.
    for _ in range(4):
        raw = stream.readline(96)
        if not raw:
            break
        messages.put(raw.decode("ascii", errors="replace").strip())


def _wait_window_count(windows: OwnedWindows, count: int) -> None:
    deadline = time.monotonic() + 2
    while len(windows.windows()) != count:
        if time.monotonic() >= deadline:
            raise AssertionError("fixture window count")
        time.sleep(0.01)


def qualify_case(mode: str) -> bool:
    """Return whether fixture-only cleanup was required after a proven case."""
    environment = child_environment()
    environment = {key: value for key, value in environment.items() if key.casefold() not in {name.casefold() for name in APP_ID_VARIABLES}}
    environment["SteamAppId"] = str(APPID)
    child = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "--fixture-child", mode],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=environment, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    reader: threading.Thread | None = None
    actual: OwnedProcessHandle | None = None
    cleaned = False
    try:
        spawned_created = psutil.Process(child.pid).create_time()
        messages: queue.Queue[str] = queue.Queue(maxsize=4)
        reader = threading.Thread(target=_read_protocol, args=(child.stdout, messages), daemon=True)
        reader.start()
        ready = messages.get(timeout=5).split()
        if len(ready) != 3 or ready[0] != "READY":
            raise AssertionError("fixture startup")
        pid, created = int(ready[1]), float(ready[2])
        if pid <= 0 or not math.isfinite(created) or child.poll() is not None:
            raise AssertionError("fixture identity")
        verify_descendant(pid, created, child.pid, spawned_created)
        actual = OwnedProcessHandle(pid, created)
        verify_descendant(pid, created, child.pid, spawned_created)
        windows = OwnedWindows(pid, NativeWindows())
        backend = OwnedBackend(pid, created, windows)
        record = backend.read(pid)
        if record is None or record.app_environment != {"SteamAppId": str(APPID)}:
            raise AssertionError("fixture attribution")
        _wait_window_count(windows, 0 if mode == "windowless" else 2)
        observer = RuntimeObserver(platform="win32", backend=backend)
        observation = observer.observe(APPID)
        if observation.state != "observed" or len(observation.processes) != 1:
            raise AssertionError("fixture observation")
        result = observer.stop_app(
            APPID, force=False, timeout=0.3 if mode == "ignored_close" else 3,
            before_request=lambda: mode != "gate_revocation" or windows.close_count == 0,
        )
        if result.force:
            raise AssertionError("force escalation")
        if mode == "graceful":
            if result.state != "stopped" or result.requested_count != 1 or windows.close_count != 2:
                raise AssertionError("graceful close")
            if actual.wait(3) != 0 or child.wait(timeout=3) != 0 or [messages.get(timeout=2) for _ in range(2)] != ["CLOSE", "CLOSE"]:
                raise AssertionError("fixture close receipt")
        else:
            if result.state != "deferred" or not actual.running():
                raise AssertionError("fixture prematurely exited")
            expected = {
                "gate_revocation": ("stop_dispatch_gate_denied", 1, 1, 1),
                "ignored_close": ("stop_timeout", 1, 2, 2),
                "windowless": ("close_request_unavailable", 0, 0, 0),
            }[mode]
            reason, requested, posts, remaining = expected
            if (result.reason_code, result.requested_count, windows.close_count) != (reason, requested, posts):
                raise AssertionError("fixture stop contract")
            _wait_window_count(windows, remaining)
    finally:
        cleanup_failed = False
        if actual is not None:
            try:
                if actual.running():
                    actual.kill()
                    cleaned = True
                actual.wait(5)
            except Exception:
                cleanup_failed = True
            finally:
                try:
                    actual.close()
                except Exception:
                    cleanup_failed = True
        if child.poll() is None:
            # Popen retains the handle of this exact child; never reopen a PID
            # or invoke the runtime force path for qualification cleanup.
            try:
                child.kill()
                cleaned = True
            except Exception:
                cleanup_failed = True
        try:
            child.wait(timeout=5)
        except Exception:
            cleanup_failed = True
        if reader is not None:
            reader.join(timeout=2)
            if reader.is_alive():
                cleanup_failed = True
        if child.stdout is not None and (reader is None or not reader.is_alive()):
            child.stdout.close()
        if cleanup_failed:
            raise ProbeError("fixture_cleanup_unconfirmed")
    return cleaned


def main() -> int:
    phase = "platform"
    try:
        if sys.platform != "win32":
            raise ProbeError("unsupported_platform")
        phase = "interactive_session"
        if windows_session_id(os.getpid()) == 0:
            raise ProbeError("interactive_session_unavailable")
        cleanup_count = 0
        for phase in CASES:
            cleanup_count += int(qualify_case(phase))
        print(json.dumps({
            "schema": "windows-stop-qualification/1", "qualified": True,
            "graceful_two_hidden_windows": True, "gate_revoked_after_first_window": True,
            "ignored_close_timeout_no_force": True, "windowless_no_force": True,
            "owned_fixture_cleanup_count": cleanup_count, "steam_actions": 0,
        }, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({
            "schema": "windows-stop-qualification/1", "qualified": False,
            "phase": phase, "reason_code": "native_qualification_failed", "steam_actions": 0,
        }, sort_keys=True))
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture-child" and sys.platform == "win32":
        try:
            raise SystemExit(fixture_child(sys.argv[2]))
        except Exception:
            raise SystemExit(1) from None
    raise SystemExit(main())

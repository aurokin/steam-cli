"""Conservative Windows interactive-session launch and client probes.

Content-plane downloads remain unknown on Windows. This adapter therefore
cannot authorize a content lease, and does not infer download absence from
filesystem residue or client absence.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time

from steam_agent.execution.sessions import (
    CommandRunner, GateState, LeaseGates, run_command,
)
from steam_agent.execution.runtime import (
    ProcessBackend, ProcessScan, ProbeError, PsutilBackend, RuntimeObserver,
)
from steam_agent.process_environment import child_environment


def _spawn_client(argv: list[str]) -> bool:
    """Let the interactive client outlive the broker, reaping it if we remain alive."""
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_environment(),
            close_fds=True,
        )
    except OSError:
        return False
    # Steam is a long-lived client, not an owned SteamCMD job. A daemon waiter
    # releases its process resources on exit without delaying broker shutdown.
    def reap() -> None:
        try:
            process.wait()
        except OSError:
            pass

    try:
        Thread(target=reap, daemon=True).start()
    except RuntimeError:
        # Dropping Popen releases local Windows handles without terminating the
        # client. Refuse confirmation when we could not establish the reaper.
        return False
    return True


def _registered_steam_executable() -> Path | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, kind = winreg.QueryValueEx(key, "SteamExe")
            if kind == winreg.REG_SZ and isinstance(value, str):
                path = Path(value)
                if path.is_absolute() and path.is_file():
                    return path
    except OSError:
        pass
    return None


class WindowsSession:
    def __init__(
        self, *, library: Path,
        runner: CommandRunner = run_command,
        spawner: Callable[[list[str]], bool] = _spawn_client,
        backend: ProcessBackend | None = None,
        steam_executable: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._library = library
        self._run = runner
        self._spawn = spawner
        self._backend = backend or PsutilBackend(platform="win32")
        self._steam_executable = steam_executable or _registered_steam_executable()
        self._sleep = sleep
        self._monotonic = monotonic
        self._runtime = RuntimeObserver(platform="win32", backend=self._backend)

    def _scan(self) -> ProcessScan:
        try:
            return self._backend.scan()
        except ProbeError:
            return ProcessScan(reason_codes=("process_probe_failed",))

    @staticmethod
    def _presence(scan: ProcessScan, names: frozenset[str]) -> GateState:
        if any(
            process.same_user and process.same_session and process.name.casefold() in names
            for process in scan.processes
        ):
            return "fail"
        return "unknown" if scan.reason_codes else "pass"

    def gates(self) -> LeaseGates:
        scan = self._scan()
        observed = self._runtime.observe()
        games: GateState = (
            "fail" if observed.processes else "unknown" if observed.state == "unknown" else "pass"
        )
        return LeaseGates(
            game_running=games,
            remote_play=self._presence(scan, frozenset({"streaming_client.exe"})),
            download_in_flight="unknown",
            client_running=self._presence(scan, frozenset({"steam.exe", "steamwebhelper.exe"})),
        )

    def client_running(self) -> bool:
        return self._presence(self._scan(), frozenset({"steam.exe"})) == "fail"

    def client_possibly_running(self) -> bool:
        return self._presence(
            self._scan(), frozenset({"steam.exe", "steamwebhelper.exe"}),
        ) != "pass"

    def steamcmd_running(self) -> bool:
        return self._presence(self._scan(), frozenset({"steamcmd.exe"})) != "pass"

    def launch_app(self, appid: int) -> bool:
        if self._steam_executable is None or not self.client_running() or appid <= 0:
            return False
        return self._run([
            str(self._steam_executable), "-applaunch", str(appid),
        ]).returncode == 0

    def stop_client(self) -> bool:
        if self._steam_executable is None:
            return False
        if not self.client_possibly_running():
            return True
        # Probe errors or a client in another session never trigger shutdown.
        if not self.client_running():
            return False
        if self._run([str(self._steam_executable), "-shutdown"]).returncode != 0:
            return False
        deadline = self._monotonic() + 60
        while self._monotonic() < deadline:
            if not self.client_possibly_running():
                return True
            self._sleep(0.1)
        return False

    def start_client(self) -> bool:
        if self._steam_executable is None:
            return False
        if self.client_running():
            return True
        if self.client_possibly_running():
            return False
        if not self._spawn([str(self._steam_executable), "-silent"]):
            return False
        deadline = self._monotonic() + 90
        while self._monotonic() < deadline:
            if self.client_running():
                return True
            self._sleep(0.1)
        return False

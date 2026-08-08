"""Linux session probes and Steam client lifecycle (session-model doc).

All probes fail closed: a probe that cannot run reports ``unknown``, and the
lease logic treats anything but an explicit pass as a deferral.  The command
runner is injected so tests never touch real processes.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Literal

GateState = Literal["pass", "fail", "unknown"]

CommandRunner = Callable[[list[str]], "CommandResult"]

_SHUTDOWN_TIMEOUT_SECONDS = 60
_START_CONFIRM_TIMEOUT_SECONDS = 90


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str


def run_command(argv: list[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return CommandResult(returncode=127, stdout="")
    return CommandResult(returncode=completed.returncode, stdout=completed.stdout)


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")


@dataclass(frozen=True, slots=True)
class LeaseGates:
    game_running: GateState
    remote_play: GateState
    download_in_flight: GateState
    client_running: GateState

    def all_clear(self) -> bool:
        """True only when every activity gate is an explicit pass."""

        return (
            self.game_running == "pass"
            and self.remote_play == "pass"
            and self.download_in_flight == "pass"
        )


class LinuxSession:
    """Probes and client lifecycle for the Linux session model."""

    def __init__(
        self,
        *,
        library: Path,
        runner: CommandRunner = run_command,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._library = library
        self._run = runner
        self._sleep = sleep

    # -- probes -----------------------------------------------------------

    def _absent(self, pattern: list[str]) -> GateState:
        result = self._run(pattern)
        if result.returncode == 0:
            return "fail"  # process present -> gate fails
        if result.returncode == 1:
            return "pass"  # pgrep: no match
        return "unknown"

    def gates(self) -> LeaseGates:
        downloading = self._library / "steamapps" / "downloading"
        try:
            download_state: GateState = (
                "fail" if any(downloading.iterdir()) else "pass"
            )
        except FileNotFoundError:
            download_state = "pass"
        except OSError:
            download_state = "unknown"

        client = self._run(["pgrep", "-x", "steam"])
        client_state: GateState
        if client.returncode == 0:
            client_state = "fail"
        elif client.returncode == 1:
            client_state = "pass"
        else:
            client_state = "unknown"

        return LeaseGates(
            # 15-char comm limit: match full command lines (Phase 0 finding).
            game_running=self._absent(["pgrep", "-f", "reaper SteamLaunch"]),
            remote_play=self._absent(["pgrep", "-f", "[s]treaming_client"]),
            download_in_flight=download_state,
            client_running=client_state,
        )

    def client_running(self) -> bool:
        """Proof of presence (rc 0 only); used to confirm a started client."""

        return self._run(["pgrep", "-x", "steam"]).returncode == 0

    def client_possibly_running(self) -> bool:
        """Fail-closed presence: anything but an explicit no-match counts."""

        return self._run(["pgrep", "-x", "steam"]).returncode != 1

    def steamcmd_running(self) -> bool:
        """Fail-closed: a probe error must defer resume, not permit it."""

        return self._run(["pgrep", "-f", "steamcmd"]).returncode != 1

    # -- lifecycle --------------------------------------------------------

    def stop_client(self) -> bool:
        """Clean shutdown; True when the full process tree exited in time."""

        self._run(["steam", "-shutdown"])
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            steam = self._run(["pgrep", "-x", "steam"]).returncode
            helper = self._run(["pgrep", "-x", "steamwebhelper"]).returncode
            # pgrep: 1 means no match; anything else nonzero is a probe
            # error and must not count as proof the tree exited.
            if steam == 1 and helper == 1:
                return True
            self._sleep(1)
        return False

    def start_client(self) -> bool:
        env_runtime = f"XDG_RUNTIME_DIR={_runtime_dir()}"
        unit = f"steam-broker-client-{int(time.time())}"
        self._run(
            [
                "env",
                env_runtime,
                "systemd-run",
                "--user",
                "--collect",
                "--unit",
                unit,
                "steam",
                "-silent",
            ]
        )
        deadline = time.monotonic() + _START_CONFIRM_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.client_running():
                return True
            self._sleep(1)
        return False

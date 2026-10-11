"""Portable session interface used by the execution broker."""

from dataclasses import dataclass
import subprocess
from typing import Callable, Literal, Protocol

from steam_agent.process_environment import child_environment

GateState = Literal["pass", "fail", "unknown"]

CommandRunner = Callable[[list[str]], "CommandResult"]


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
            env=child_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return CommandResult(returncode=127, stdout="")
    return CommandResult(returncode=completed.returncode, stdout=completed.stdout)


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


class SessionProtocol(Protocol):
    def gates(self) -> LeaseGates: ...
    def client_running(self) -> bool: ...
    def client_possibly_running(self) -> bool: ...
    def steamcmd_running(self) -> bool: ...
    def stop_client(self) -> bool: ...
    def launch_app(self, appid: int) -> bool: ...
    def start_client(self) -> bool: ...

"""Linux session probes and Steam client lifecycle (session-model doc).

All probes fail closed: a probe that cannot run reports ``unknown``, and the
lease logic treats anything but an explicit pass as a deferral.  The command
runner is injected so tests never touch real processes.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Literal

GateState = Literal["pass", "fail", "unknown"]

CommandRunner = Callable[[list[str]], "CommandResult"]

_SHUTDOWN_TIMEOUT_SECONDS = 60
_START_CONFIRM_TIMEOUT_SECONDS = 90
# An empty downloading/<appid> dir older than this is finished-download
# residue; younger ones may be a download that has not written a file yet.
_DOWNLOAD_RESIDUE_AGE_SECONDS = 60 * 60


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
    """Probes and client lifecycle for the Linux session model.

    This class must execute inside the desktop user's own session: pgrep,
    ``steam -shutdown``, and ``systemd-run --user`` all target the invoking
    user.  The broker runs as that user permanently (single-identity
    model, ADR 0028).
    """

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

    @staticmethod
    def _pgrep(*arguments: str) -> list[str]:
        # Scope to the invoking user.  LinuxSession always executes inside
        # the desktop user's session (class docstring), so the invoking
        # UID IS the Steam-owning UID; another user's processes are
        # neither stoppable nor relevant here.
        return ["pgrep", "-U", str(os.getuid()), *arguments]

    def _absent(self, pattern: list[str]) -> GateState:
        result = self._run(pattern)
        if result.returncode == 0:
            return "fail"  # process present -> gate fails
        if result.returncode == 1:
            return "pass"  # pgrep: no match
        return "unknown"

    def _download_dirs(self) -> list[Path] | None:
        # Stopping the client interrupts downloads in every configured
        # library.  libraryfolders.vdf normally lives only in the primary
        # library, so also consult the standard config locations in case the
        # broker targets a secondary one.  (The broker runs in the desktop
        # user's session, so its HOME is the right root here.)
        # Returns None when a configuration file exists but is unreadable:
        # the library list is then unknown and the gate must not pass.
        dirs = [self._library / "steamapps" / "downloading"]
        vdf_candidates = [
            self._library / "steamapps" / "libraryfolders.vdf",
            Path.home() / ".steam" / "steam" / "steamapps" / "libraryfolders.vdf",
            Path.home() / ".local" / "share" / "Steam" / "steamapps" / "libraryfolders.vdf",
        ]
        for vdf in vdf_candidates:
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            except OSError:
                return None
            # A readable but truncated/malformed file could silently drop a
            # secondary library.  No path fields at all, or unbalanced
            # braces (truncation), means the library list is structurally
            # incomplete — fail closed instead of trusting what survives.
            paths = re.findall(r'"path"\s+"([^"]*)"', text)
            if not paths or text.count("{") != text.count("}"):
                return None
            for value in paths:
                candidate = Path(value) / "steamapps" / "downloading"
                if candidate not in dirs:
                    dirs.append(candidate)
        return dirs

    @staticmethod
    def _download_activity(downloading: Path) -> bool:
        # The client leaves empty per-app subdirectories behind after
        # completed downloads (observed on the target machine); stale empty
        # residue must not wedge the gate until someone cleans it by hand.
        # But a starting download also creates its per-app directory before
        # writing the first file, and only age separates the two — so an
        # empty directory younger than the residue threshold still fails.
        # Accepted residual class (do not re-flag variants): ANY download
        # that is still at zero files when its directory reads as old —
        # stalled from the start, or reusing a stale per-app directory
        # whose mtime a restart did not touch — can be misread as residue.
        # Every member of that class has transferred nothing: stopping the
        # client loses no progress and the client resumes the download on
        # its own restart, whereas failing on all empty dirs wedges
        # unattended operation behind manual residue cleanup.  The moment
        # a download has anything to lose, it has a file, and any file
        # content fails the gate.
        # Explicit walk, not rglob: rglob suppresses per-directory scan
        # errors, and an unreadable subtree must surface as OSError so the
        # gate reads unknown instead of pass.
        stack = [downloading]
        while stack:
            for entry in stack.pop().iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    age = time.time() - entry.stat().st_mtime
                    if age < _DOWNLOAD_RESIDUE_AGE_SECONDS:
                        return True
                    stack.append(entry)
                else:
                    return True  # file content: a transaction is in flight
        return False

    def gates(self) -> LeaseGates:
        download_state: GateState = "pass"
        download_dirs = self._download_dirs()
        if download_dirs is None:
            download_state = "unknown"
            download_dirs = []
        for downloading in download_dirs:
            try:
                if self._download_activity(downloading):
                    download_state = "fail"
                    break
            except FileNotFoundError:
                continue
            except OSError:
                download_state = "unknown"

        client = self._run(self._pgrep("-x", "steam")).returncode
        helper = self._run(self._pgrep("-x", "steamwebhelper")).returncode
        client_state: GateState
        if client == 0 or helper == 0:
            client_state = "fail"
        elif client == 1 and helper == 1:
            client_state = "pass"
        else:
            client_state = "unknown"

        return LeaseGates(
            # 15-char comm limit: match full command lines (Phase 0 finding).
            game_running=self._absent(self._pgrep("-f", "reaper SteamLaunch")),
            remote_play=self._absent(self._pgrep("-f", "[s]treaming_client")),
            download_in_flight=download_state,
            client_running=client_state,
        )

    def client_running(self) -> bool:
        """Proof of presence (rc 0 only); used to confirm a started client."""

        return self._run(self._pgrep("-x", "steam")).returncode == 0

    def client_possibly_running(self) -> bool:
        """Fail-closed presence: anything but an explicit no-match counts.

        A surviving steamwebhelper counts as a running client — part of the
        tree can hold library state even after the main process exits.
        """

        steam = self._run(self._pgrep("-x", "steam")).returncode
        helper = self._run(self._pgrep("-x", "steamwebhelper")).returncode
        return steam != 1 or helper != 1

    def steamcmd_running(self) -> bool:
        """Fail-closed: a probe error must defer resume, not permit it."""

        return self._run(self._pgrep("-f", "steamcmd")).returncode != 1

    # -- lifecycle --------------------------------------------------------

    def stop_client(self) -> bool:
        """Clean shutdown; True when the full process tree exited in time."""

        self._run(["steam", "-shutdown"])
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            steam = self._run(self._pgrep("-x", "steam")).returncode
            helper = self._run(self._pgrep("-x", "steamwebhelper")).returncode
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

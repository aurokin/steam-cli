"""Windows session behavior with synthetic process and command backends."""

from collections.abc import Callable
from pathlib import Path
import subprocess
from threading import Event, current_thread

import pytest

from steam_agent.execution.linux_session import CommandResult
from steam_agent.execution.runtime import ProcessRecord, ProcessScan, ProbeError
from steam_agent.execution.windows_session import WindowsSession
from steam_agent.execution import windows_session


class Processes:
    def __init__(self, *records: ProcessRecord) -> None:
        self.records = records
        self.reasons: tuple[str, ...] = ()

    def scan(self) -> ProcessScan:
        return ProcessScan(self.records, self.reasons)

    def read(self, pid: int) -> ProcessRecord | None:
        return next((record for record in self.records if record.pid == pid), None)

    def request_stop(
        self, process: ProcessRecord, *, force: bool,
        before_request: Callable[[], bool] | None = None,
    ) -> bool:
        raise AssertionError("Session probes must not stop games")


def steam(*, same_user=True, same_session=True, name="steam.exe") -> ProcessRecord:
    return ProcessRecord(100, 1.0, name, {}, same_user, same_session)


def test_windows_launch_requires_same_user_interactive_client(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    processes = Processes(steam(same_session=False), steam(same_user=False))
    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=processes,
        runner=lambda argv: calls.append(argv) or CommandResult(0, ""),
    )
    assert session.launch_app(480) is False
    assert calls == []
    processes.records = (steam(),)
    assert session.launch_app(480) is True
    assert calls == [["steam.exe", "-applaunch", "480"]]


def test_windows_download_gate_remains_unknown_even_when_client_absent(tmp_path: Path) -> None:
    session = WindowsSession(library=tmp_path, backend=Processes())
    assert session.gates().download_in_flight == "unknown"
    assert session.gates().all_clear() is False


def test_windows_runtime_gate_detects_manual_game(tmp_path: Path) -> None:
    game = ProcessRecord(101, 1.0, "game.exe", {"SteamAppId": "480"}, True)
    session = WindowsSession(library=tmp_path, backend=Processes(game))
    assert session.gates().game_running == "fail"


def test_probe_errors_are_possibly_running_and_do_not_start_client(tmp_path: Path) -> None:
    processes = Processes()
    processes.reasons = ("process_probe_failed",)
    calls: list[list[str]] = []
    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=processes,
        runner=lambda argv: calls.append(argv) or CommandResult(0, ""),
        spawner=lambda argv: calls.append(argv) or True,
    )
    assert session.client_running() is False
    assert session.client_possibly_running() is True
    assert session.steamcmd_running() is True
    assert session.start_client() is False
    assert session.stop_client() is False
    assert calls == []


def test_windows_stop_client_waits_for_full_client_tree_exit(tmp_path: Path) -> None:
    processes = Processes(steam())
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(argv)
        processes.records = (steam(name="steamwebhelper.exe"),)
        return CommandResult(0, "")

    def sleep(_: float) -> None:
        processes.records = ()

    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=processes,
        runner=runner, sleep=sleep,
    )
    assert session.stop_client() is True
    assert calls == [["steam.exe", "-shutdown"]]


def test_windows_stop_client_does_not_treat_unknown_as_exit(tmp_path: Path) -> None:
    processes = Processes(steam())

    def runner(argv: list[str]) -> CommandResult:
        processes.records = ()
        processes.reasons = ("process_probe_failed",)
        return CommandResult(0, "")

    clock = iter(range(0, 1000, 31))
    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=processes,
        runner=runner, monotonic=lambda: next(clock), sleep=lambda _: None,
    )
    assert session.stop_client() is False


def test_scan_exception_fails_closed(tmp_path: Path) -> None:
    class FailedProcesses(Processes):
        def scan(self) -> ProcessScan:
            raise ProbeError("synthetic")

    session = WindowsSession(library=tmp_path, backend=FailedProcesses())
    assert session.gates().client_running == "unknown"


def test_windows_start_client_waits_for_matching_client_presence(tmp_path: Path) -> None:
    processes = Processes()
    calls: list[list[str]] = []

    def spawn(argv: list[str]) -> bool:
        calls.append(argv)
        processes.records = (steam(same_user=False), steam(same_session=False))
        return True

    observations: list[float] = []

    def sleep(seconds: float) -> None:
        observations.append(seconds)
        processes.records = (steam(),)

    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=processes,
        spawner=spawn, sleep=sleep,
        runner=lambda _: pytest.fail("Startup must not use the blocking command runner"),
    )
    assert session.start_client() is True
    assert calls == [["steam.exe", "-silent"]]
    assert observations == [0.1]


def test_windows_start_client_does_not_respawn_running_client(tmp_path: Path) -> None:
    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=Processes(steam()),
        spawner=lambda _: pytest.fail("The existing client must not be respawned"),
    )
    assert session.start_client() is True


@pytest.mark.parametrize("spawn_accepted", [False, True])
@pytest.mark.parametrize("probe_unknown", [False, True])
def test_windows_start_client_requires_spawn_and_observed_presence(
    tmp_path: Path, spawn_accepted: bool, probe_unknown: bool,
) -> None:
    processes = Processes()
    calls: list[list[str]] = []

    def spawn(argv: list[str]) -> bool:
        calls.append(argv)
        if probe_unknown:
            processes.reasons = ("process_probe_failed",)
        return spawn_accepted

    clock = iter(range(0, 1000, 31))
    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=processes,
        spawner=spawn, monotonic=lambda: next(clock), sleep=lambda _: None,
    )
    assert session.start_client() is False
    assert calls == [["steam.exe", "-silent"]]


def test_native_start_observes_presence_before_spawned_process_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = Processes()
    waiter_started = Event()
    process_exited = Event()
    waiter_finished = Event()
    waiters: list[bool] = []
    calls: list[tuple[list[str], dict[str, object]]] = []

    class LongLivedClient:
        def wait(self) -> int:
            waiters.append(current_thread().daemon)
            waiter_started.set()
            process_exited.wait(2)
            waiter_finished.set()
            return 0

    def popen(argv: list[str], **kwargs: object) -> LongLivedClient:
        calls.append((argv, kwargs))
        processes.records = (steam(),)
        return LongLivedClient()

    monkeypatch.setattr(windows_session.subprocess, "Popen", popen)
    monkeypatch.setenv("STEAM_WEB_API_KEY", "synthetic-provider-secret")
    monkeypatch.setenv("steam_web_api_key", "synthetic-case-variant")
    monkeypatch.setenv("STEAM_CLIENT_CONTEXT", "synthetic-desktop-context")
    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=processes,
    )
    try:
        assert session.start_client() is True
        assert waiter_started.wait(1)
        assert not process_exited.is_set()
        assert not waiter_finished.is_set()
        assert waiters == [True]
        assert len(calls) == 1
        argv, options = calls[0]
        assert argv == ["steam.exe", "-silent"]
        assert options["stdin"] == subprocess.DEVNULL
        assert options["stdout"] == subprocess.DEVNULL
        assert options["stderr"] == subprocess.DEVNULL
        assert options["close_fds"] is True
        environment = options["env"]
        assert isinstance(environment, dict)
        assert "STEAM_WEB_API_KEY" not in environment
        assert "steam_web_api_key" not in environment
        assert environment["STEAM_CLIENT_CONTEXT"] == "synthetic-desktop-context"
    finally:
        process_exited.set()
        assert waiter_finished.wait(1)


def test_native_start_refuses_spawn_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def popen(argv: list[str], **kwargs: object) -> None:
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(windows_session.subprocess, "Popen", popen)
    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=Processes(),
    )
    assert session.start_client() is False


@pytest.mark.parametrize("thread_start_fails", [False, True])
def test_native_start_handles_reaper_failure_without_stopping_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thread_start_fails: bool,
) -> None:
    processes = Processes()
    waits: list[bool] = []

    class Client:
        def wait(self) -> int:
            waits.append(True)
            raise OSError("synthetic reaping failure")

    def popen(argv: list[str], **kwargs: object) -> Client:
        processes.records = (steam(),)
        return Client()

    class Reaper:
        def __init__(self, *, target: Callable[[], None], daemon: bool) -> None:
            assert daemon is True
            self.target = target

        def start(self) -> None:
            if thread_start_fails:
                raise RuntimeError("synthetic thread creation failure")
            self.target()

    monkeypatch.setattr(windows_session.subprocess, "Popen", popen)
    monkeypatch.setattr(windows_session, "Thread", Reaper)
    session = WindowsSession(
        library=tmp_path, steam_executable=Path("steam.exe"), backend=processes,
    )
    assert session.start_client() is (not thread_start_fails)
    assert waits == ([] if thread_start_fails else [True])

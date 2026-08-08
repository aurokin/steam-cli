"""Session lifecycle fail-closed behavior with an injected command runner."""

from __future__ import annotations

from pathlib import Path

from steam_agent.execution.linux_session import CommandResult, LinuxSession


def test_stop_client_treats_probe_error_as_still_running(
    tmp_path: Path, monkeypatch
) -> None:
    # pgrep rc>1 (incl. the runner's 127) is a probe failure, not proof the
    # process tree exited; stop_client must time out rather than succeed.
    clock = iter(range(0, 10_000, 31))
    monkeypatch.setattr(
        "steam_agent.execution.linux_session.time.monotonic", lambda: next(clock)
    )

    def runner(argv: list[str]) -> CommandResult:
        return CommandResult(returncode=127, stdout="")

    session = LinuxSession(library=tmp_path, runner=runner, sleep=lambda _: None)
    assert session.stop_client() is False


def test_stop_client_confirms_exit_on_explicit_no_match(tmp_path: Path) -> None:
    def runner(argv: list[str]) -> CommandResult:
        return CommandResult(returncode=1, stdout="")

    session = LinuxSession(library=tmp_path, runner=runner, sleep=lambda _: None)
    assert session.stop_client() is True

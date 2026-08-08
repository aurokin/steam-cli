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


def test_download_gate_covers_all_configured_libraries(tmp_path: Path) -> None:
    library = tmp_path / "library"
    (library / "steamapps").mkdir(parents=True)
    other = tmp_path / "other-library"
    downloading = other / "steamapps" / "downloading"
    downloading.mkdir(parents=True)
    (downloading / "999").mkdir()
    (library / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{library}"\n\t}}\n'
        f'\t"1"\n\t{{\n\t\t"path"\t\t"{other}"\n\t}}\n}}\n',
        encoding="utf-8",
    )

    def runner(argv: list[str]) -> CommandResult:
        return CommandResult(returncode=1, stdout="")

    session = LinuxSession(library=library, runner=runner, sleep=lambda _: None)
    assert session.gates().download_in_flight == "fail"


def test_truncated_library_config_is_unknown_not_pass(tmp_path: Path) -> None:
    library = tmp_path / "library"
    (library / "steamapps").mkdir(parents=True)
    # Truncated mid-file: the surviving path entry must not be trusted as
    # the complete library list.
    (library / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{library}"\n\t}}\n'
        '\t"1"\n\t{\n\t\t"pa',
        encoding="utf-8",
    )

    def runner(argv: list[str]) -> CommandResult:
        return CommandResult(returncode=1, stdout="")

    session = LinuxSession(library=library, runner=runner, sleep=lambda _: None)
    assert session.gates().download_in_flight == "unknown"


def test_unreadable_library_config_is_unknown_not_pass(tmp_path: Path) -> None:
    library = tmp_path / "library"
    (library / "steamapps").mkdir(parents=True)
    vdf = library / "steamapps" / "libraryfolders.vdf"
    vdf.write_text('"libraryfolders"\n{\n}\n', encoding="utf-8")
    vdf.chmod(0o000)

    def runner(argv: list[str]) -> CommandResult:
        return CommandResult(returncode=1, stdout="")

    session = LinuxSession(library=library, runner=runner, sleep=lambda _: None)
    try:
        assert session.gates().download_in_flight == "unknown"
    finally:
        vdf.chmod(0o644)

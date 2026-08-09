from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import steam_agent.application as application
import steam_agent.local_steam as local_steam
from steam_agent.application import installed_item, sync_installed, usable_steam_root
from steam_agent.storage import Storage


FIXTURES = Path(__file__).parent / "fixtures" / "steam"
START = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


@pytest.mark.parametrize(
    ("platform_name", "variable", "configured_value", "fallback_parts"),
    [
        ("linux", "XDG_DATA_HOME", "", (".local", "share", "steam-agent")),
        (
            "linux",
            "XDG_DATA_HOME",
            "relative/data",
            (".local", "share", "steam-agent"),
        ),
        (
            "win32",
            "LOCALAPPDATA",
            "",
            ("AppData", "Local", "steam-agent"),
        ),
        (
            "win32",
            "LOCALAPPDATA",
            "relative/data",
            ("AppData", "Local", "steam-agent"),
        ),
    ],
)
def test_invalid_platform_data_home_uses_absolute_home_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    variable: str,
    configured_value: str,
    fallback_parts: tuple[str, ...],
) -> None:
    home = tmp_path / "mock-home"
    monkeypatch.delenv("STEAM_AGENT_DATA_DIR", raising=False)
    monkeypatch.setenv(variable, configured_value)
    monkeypatch.setattr(application.sys, "platform", platform_name)
    monkeypatch.setattr(application.Path, "home", classmethod(lambda cls: home))

    result = application.default_data_dir()

    assert result == home.joinpath(*fallback_parts)
    assert result.is_absolute()


@pytest.mark.parametrize(
    ("platform_name", "variable"),
    [("linux", "XDG_DATA_HOME"), ("win32", "LOCALAPPDATA")],
)
def test_nonempty_platform_data_home_is_honored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    variable: str,
) -> None:
    configured = tmp_path / "configured-data"
    monkeypatch.delenv("STEAM_AGENT_DATA_DIR", raising=False)
    monkeypatch.setenv(variable, str(configured))
    monkeypatch.setattr(application.sys, "platform", platform_name)

    assert application.default_data_dir() == configured / "steam-agent"


def test_credential_fallback_ignores_workspace_xdg_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(application.sys, "platform", "linux")
    monkeypatch.setattr(application, "_fixed_user_home", lambda: home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(workspace))

    result = application.default_credential_dir()

    assert result == home / ".config" / "steam-agent" / "credentials"
    assert workspace not in result.parents


@pytest.mark.skipif(application.os.name != "posix", reason="POSIX account database test")
def test_fixed_user_home_ignores_home_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "spoofed-home"))

    assert application._fixed_user_home() != tmp_path / "spoofed-home"


@pytest.mark.parametrize("invalid_value", ["", "relative/program-files"])
def test_invalid_windows_program_files_values_use_platform_defaults(
    monkeypatch: pytest.MonkeyPatch, invalid_value: str
) -> None:
    observed: list[str] = []
    monkeypatch.setattr(application.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMFILES(X86)", invalid_value)
    monkeypatch.setenv("PROGRAMFILES", invalid_value)

    def record_candidate(path: str | Path) -> bool:
        observed.append(str(path))
        return False

    monkeypatch.setattr(application, "usable_steam_root", record_candidate)

    assert application.discover_steam_root() is None
    assert observed == [
        str(Path("C:/Program Files (x86)") / "Steam"),
        str(Path("C:/Program Files") / "Steam"),
    ]
    assert all(path not in {"Steam", str(Path.cwd() / "Steam")} for path in observed)


def test_absolute_windows_program_files_values_are_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    x86 = tmp_path / "Program Files (x86)"
    native = tmp_path / "Program Files"
    observed: list[Path] = []
    monkeypatch.setattr(application.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMFILES(X86)", str(x86))
    monkeypatch.setenv("PROGRAMFILES", str(native))

    def record_candidate(path: str | Path) -> bool:
        observed.append(Path(path))
        return False

    monkeypatch.setattr(application, "usable_steam_root", record_candidate)

    assert application.discover_steam_root() is None
    assert observed == [x86 / "Steam", native / "Steam"]


def test_complete_scan_promotes_and_query_hides_paths(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        result = sync_installed(
            storage,
            steam_root=FIXTURES / "valid" / "root",
            machine_id="fixture-machine",
            clock=Clock(),
        )
        games = storage.list_installed("fixture-machine")

    assert result.run.status == "complete"
    assert result.recorded_appids == (10, 20)
    assert [game.appid for game in games] == [10, 20]
    public = installed_item(games[0])
    assert "install_dir" not in public
    assert installed_item(games[0], include_paths=True)["install_dir"]


def test_partial_scan_does_not_replace_last_good_projection(tmp_path: Path) -> None:
    clock = Clock()
    with Storage(tmp_path / "db.sqlite3") as storage:
        sync_installed(
            storage,
            steam_root=FIXTURES / "valid" / "root",
            machine_id="fixture-machine",
            clock=clock,
        )
        partial = sync_installed(
            storage,
            steam_root=FIXTURES / "problems" / "root",
            machine_id="fixture-machine",
            clock=clock,
        )
        games = storage.list_installed("fixture-machine")

    assert partial.run.status == "partial"
    assert [game.appid for game in games] == [10, 20]


def test_failed_scan_is_audited_and_preserves_last_good_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    with Storage(tmp_path / "db.sqlite3") as storage:
        completed = sync_installed(
            storage,
            steam_root=FIXTURES / "valid" / "root",
            machine_id="fixture-machine",
            clock=clock,
        )

        def fail_scan(_: str | Path) -> object:
            raise RuntimeError("fixture scan failed")

        monkeypatch.setattr(application, "scan_local_steam", fail_scan)
        with pytest.raises(RuntimeError, match="fixture scan failed"):
            sync_installed(
                storage,
                steam_root=FIXTURES / "valid" / "root",
                machine_id="fixture-machine",
                clock=clock,
            )

        games = storage.list_installed("fixture-machine")
        failed = storage.get_sync_run(completed.run.id + 1)

    assert [game.appid for game in games] == [10, 20]
    assert all(game.promoted_sync_run_id == completed.run.id for game in games)
    assert failed.status == "failed"
    assert failed.error_code == "SCAN_FAILED"
    assert failed.error_detail == "RuntimeError"


def test_installed_item_hides_every_local_path_by_default(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        sync_installed(
            storage,
            steam_root=FIXTURES / "valid" / "root",
            machine_id="fixture-machine",
            clock=Clock(),
        )
        game = storage.list_installed("fixture-machine")[0]

    hidden = installed_item(game)
    visible = installed_item(game, include_paths=True)

    assert {"library_root", "install_dir", "manifest_path"}.isdisjoint(hidden)
    assert {"library_root", "install_dir", "manifest_path"} <= visible.keys()
    assert str(FIXTURES.resolve()) not in repr(hidden)


def test_manifest_disappearing_during_scan_is_partial_and_preserves_last_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    root = FIXTURES / "valid" / "root"
    disappearing = (root / "steamapps" / "appmanifest_10.acf").resolve()
    with Storage(tmp_path / "db.sqlite3") as storage:
        complete = sync_installed(
            storage,
            steam_root=root,
            machine_id="fixture-machine",
            clock=clock,
        )

        original_read_text = Path.read_text

        def sometimes_missing(path: Path, *args: object, **kwargs: object) -> str:
            if path.resolve() == disappearing:
                raise FileNotFoundError(path)
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", sometimes_missing)
        partial = sync_installed(
            storage,
            steam_root=root,
            machine_id="fixture-machine",
            clock=clock,
        )
        games = storage.list_installed("fixture-machine")

    assert complete.run.status == "complete"
    assert partial.run.status == "partial"
    assert "file_disappeared" in {warning.code for warning in partial.scan.warnings}
    assert [game.appid for game in games] == [10, 20]
    assert all(game.promoted_sync_run_id == complete.run.id for game in games)


def test_usable_root_requires_enumerable_primary_steamapps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "steam"
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    original_scandir = application.os.scandir

    def deny_primary(path: object) -> object:
        if Path(path) == steamapps:
            raise PermissionError("denied")
        return original_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(application.os, "scandir", deny_primary)

    assert usable_steam_root(root) is False


def test_unreadable_secondary_library_remains_partial_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = FIXTURES / "valid" / "root"
    secondary_steamapps = (FIXTURES / "valid" / "secondary" / "steamapps").resolve()
    original_scandir = local_steam.os.scandir

    def deny_secondary(path: object) -> object:
        if Path(path).resolve() == secondary_steamapps:
            raise PermissionError("denied")
        return original_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(local_steam.os, "scandir", deny_secondary)
    with Storage(tmp_path / "db.sqlite3") as storage:
        result = sync_installed(
            storage,
            steam_root=root,
            machine_id="fixture-machine",
            clock=Clock(),
        )

    assert result.run.status == "partial"
    assert result.recorded_appids == (10,)
    assert "inaccessible_library" in {
        warning.code for warning in result.scan.warnings
    }


def test_keyboard_interrupt_finalizes_sync_as_canceled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()

    def cancel_scan(_: str | Path) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(application, "scan_local_steam", cancel_scan)
    with Storage(tmp_path / "db.sqlite3") as storage:
        with pytest.raises(KeyboardInterrupt):
            sync_installed(
                storage,
                steam_root=FIXTURES / "valid" / "root",
                machine_id="fixture-machine",
                clock=clock,
            )
        latest = storage.latest_sync(
            capability="installed", machine_id="fixture-machine"
        )

    assert latest is not None
    assert latest.status == "failed"
    assert latest.error_code == "SCAN_CANCELED"
    assert latest.error_detail == "KeyboardInterrupt"
    assert latest.completed_at is not None


def test_keyboard_interrupt_in_observation_rolls_back_before_cancel_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        original_insert_evidence = storage._insert_evidence  # noqa: SLF001

        def cancel_after_evidence(evidence: object) -> int:
            original_insert_evidence(evidence)  # type: ignore[arg-type]
            raise KeyboardInterrupt("observation canceled")

        monkeypatch.setattr(storage, "_insert_evidence", cancel_after_evidence)
        with pytest.raises(KeyboardInterrupt, match="observation canceled"):
            sync_installed(
                storage,
                steam_root=FIXTURES / "valid" / "root",
                machine_id="fixture-machine",
                clock=Clock(),
            )

        latest = storage.latest_sync(
            capability="installed", machine_id="fixture-machine"
        )
        assert latest is not None
        assert latest.status == "failed"
        assert latest.error_code == "SCAN_CANCELED"
        assert latest.error_detail == "KeyboardInterrupt"
        assert storage._connection.in_transaction is False  # noqa: SLF001
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM evidence"
        ).fetchone()[0] == 0


def test_failed_rollback_reopens_file_database_and_finalizes_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        original_connection = storage._connection  # noqa: SLF001
        original_insert_evidence = storage._insert_evidence  # noqa: SLF001
        rollback_attempts = 0

        def cancel_after_evidence(evidence: object) -> int:
            original_insert_evidence(evidence)  # type: ignore[arg-type]
            raise KeyboardInterrupt("observation canceled")

        def fail_first_rollback() -> None:
            nonlocal rollback_attempts
            rollback_attempts += 1
            if rollback_attempts == 1:
                raise RuntimeError("rollback failed")
            original_connection.rollback()

        monkeypatch.setattr(storage, "_insert_evidence", cancel_after_evidence)
        monkeypatch.setattr(storage, "_rollback_transaction", fail_first_rollback)

        with pytest.raises(KeyboardInterrupt, match="observation canceled"):
            sync_installed(
                storage,
                steam_root=FIXTURES / "valid" / "root",
                machine_id="fixture-machine",
                clock=Clock(),
            )

        assert rollback_attempts == 1
        assert storage._connection is not original_connection  # noqa: SLF001
        latest = storage.latest_sync(
            capability="installed", machine_id="fixture-machine"
        )
        assert latest is not None
        assert latest.status == "failed"
        assert latest.error_code == "SCAN_CANCELED"
        assert latest.error_detail == "KeyboardInterrupt"
        assert latest.completed_at is not None
        assert storage._connection.in_transaction is False  # noqa: SLF001
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM sync_runs WHERE status = 'running'"
        ).fetchone()[0] == 0
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM evidence"
        ).fetchone()[0] == 0


def test_reported_rollback_failure_does_not_replace_clean_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        original_connection = storage._connection  # noqa: SLF001
        original_insert_evidence = storage._insert_evidence  # noqa: SLF001

        def cancel_after_evidence(evidence: object) -> int:
            original_insert_evidence(evidence)  # type: ignore[arg-type]
            raise KeyboardInterrupt("observation canceled")

        def rollback_then_report_failure() -> None:
            original_connection.rollback()
            raise RuntimeError("late rollback report")

        monkeypatch.setattr(storage, "_insert_evidence", cancel_after_evidence)
        monkeypatch.setattr(
            storage, "_rollback_transaction", rollback_then_report_failure
        )

        with pytest.raises(KeyboardInterrupt, match="observation canceled"):
            sync_installed(
                storage,
                steam_root=FIXTURES / "valid" / "root",
                machine_id="fixture-machine",
                clock=Clock(),
            )

        assert storage._connection is original_connection  # noqa: SLF001
        latest = storage.latest_sync(
            capability="installed", machine_id="fixture-machine"
        )
        assert latest is not None
        assert latest.status == "failed"
        assert latest.error_code == "SCAN_CANCELED"


def test_keyboard_interrupt_in_first_finish_rolls_back_for_cleanup_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "db.sqlite3") as storage:
        original_promote = storage._promote_installed  # noqa: SLF001
        first_attempt = True

        def cancel_after_promotion(machine_id: str, sync_run_id: int) -> None:
            nonlocal first_attempt
            original_promote(machine_id, sync_run_id)
            if first_attempt:
                first_attempt = False
                raise KeyboardInterrupt("finish canceled")

        monkeypatch.setattr(storage, "_promote_installed", cancel_after_promotion)
        with pytest.raises(KeyboardInterrupt, match="finish canceled"):
            sync_installed(
                storage,
                steam_root=FIXTURES / "valid" / "root",
                machine_id="fixture-machine",
                clock=Clock(),
            )

        latest = storage.latest_sync(
            capability="installed", machine_id="fixture-machine"
        )
        assert latest is not None
        assert latest.status == "failed"
        assert latest.promoted is False
        assert latest.error_code == "SCAN_CANCELED"
        assert latest.error_detail == "KeyboardInterrupt"
        assert storage.list_installed("fixture-machine") == []
        assert storage._connection.in_transaction is False  # noqa: SLF001


def test_finalization_failure_does_not_replace_original_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cancel_scan(_: str | Path) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(application, "scan_local_steam", cancel_scan)
    with Storage(tmp_path / "db.sqlite3") as storage:
        def fail_finalization(*args: object, **kwargs: object) -> object:
            raise RuntimeError("cleanup failed")

        monkeypatch.setattr(storage, "finish_installed_sync", fail_finalization)
        with pytest.raises(KeyboardInterrupt):
            sync_installed(
                storage,
                steam_root=FIXTURES / "valid" / "root",
                machine_id="fixture-machine",
                clock=Clock(),
            )


def test_residual_measurements_survive_promotion_and_stay_pathless(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    steamapps = root / "steamapps"
    (steamapps / "common" / "Eight").mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "." "apps" { "8" "1" } } }',
        encoding="utf-8",
    )
    (steamapps / "appmanifest_8.acf").write_text(
        '"AppState" { "appid" "8" "name" "Eight" "installdir" "Eight"'
        ' "StateFlags" "4" }',
        encoding="utf-8",
    )
    cache = steamapps / "shadercache" / "8"
    cache.mkdir(parents=True)
    (cache / "fozpipelinesv6").write_bytes(b"y" * 700)

    with Storage(tmp_path / "db.sqlite3") as storage:
        sync_installed(
            storage, steam_root=root, machine_id="fixture-machine", clock=Clock()
        )
        game = storage.list_installed("fixture-machine")[0]

    assert game.residual_state == "measured"
    assert game.residual_shadercache_bytes == 700
    assert game.residual_compatdata_bytes == 0
    assert game.residual_workshop_bytes == 0
    assert str(tmp_path) not in json.dumps(installed_item(game))


def _library_with(
    base: Path, manifests: dict[int, str], *, without_dirs: frozenset[int] = frozenset()
) -> Path:
    root = base / "root"
    steamapps = root / "steamapps"
    (steamapps / "common").mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "." "apps" { } } }', encoding="utf-8"
    )
    for appid, flags in manifests.items():
        if appid not in without_dirs:
            (steamapps / "common" / f"App{appid}").mkdir(exist_ok=True)
        (steamapps / f"appmanifest_{appid}.acf").write_text(
            f'"AppState" {{ "appid" "{appid}" "name" "App{appid}"'
            f' "installdir" "App{appid}" "StateFlags" "{flags}" }}',
            encoding="utf-8",
        )
    return root


def test_a_paused_download_does_not_freeze_the_installed_projection(
    tmp_path: Path,
) -> None:
    # 1538 over a directory that was never staged is what a stalled first
    # download leaves behind (observed on a real machine).  With the
    # directory present it would count as an in-place update and stay
    # installed; without one there is nothing installed to project.  Either
    # way the scan saw everything, so it is complete and still promotes.
    root = _library_with(
        tmp_path, {10: "4", 20: "1538"}, without_dirs=frozenset({20})
    )

    with Storage(tmp_path / "db.sqlite3") as storage:
        result = sync_installed(
            storage, steam_root=root, machine_id="fixture-machine", clock=Clock()
        )
        games = storage.list_installed("fixture-machine")

    assert result.run.status == "complete"
    assert [game.appid for game in games] == [10]
    # The exclusion is still reported; it just no longer blocks promotion.
    assert "not_fully_installed" in (result.run.error_detail or "")


def test_a_leftover_uninstalled_manifest_does_not_freeze_the_projection(
    tmp_path: Path,
) -> None:
    root = _library_with(
        tmp_path, {10: "4", 20: "1"}, without_dirs=frozenset({20})
    )

    with Storage(tmp_path / "db.sqlite3") as storage:
        result = sync_installed(
            storage, steam_root=root, machine_id="fixture-machine", clock=Clock()
        )

    assert result.run.status == "complete"
    assert "uninstalled_app_state" in (result.run.error_detail or "")


def test_an_unreadable_library_still_blocks_promotion(tmp_path: Path) -> None:
    # The last-good rule is unchanged for warnings that mean the scan could
    # not see everything.
    root = _library_with(tmp_path, {10: "4"})
    (root / "steamapps" / "appmanifest_30.acf").write_text(
        '"AppState" { "appid" "30" ', encoding="utf-8"
    )

    with Storage(tmp_path / "db.sqlite3") as storage:
        result = sync_installed(
            storage, steam_root=root, machine_id="fixture-machine", clock=Clock()
        )
        games = storage.list_installed("fixture-machine")

    assert result.run.status == "partial"
    assert games == []

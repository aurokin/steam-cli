"""M1 application services joining local scans to the evidence store."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import sys
from typing import Callable

from steam_agent.local_steam import (
    LocalSteamScan,
    ResidualContent,
    WarningKind,
    scan_local_steam,
)
from steam_agent.storage import (
    EvidenceInput,
    InstalledObservation,
    Machine,
    Storage,
    SyncRun,
)


Clock = Callable[[], datetime]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _absolute_environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else None


@dataclass(frozen=True, slots=True)
class InstalledSyncResult:
    run: SyncRun
    scan: LocalSteamScan
    recorded_appids: tuple[int, ...]
    skipped_appids: tuple[int, ...]


def default_data_dir() -> Path:
    override = os.environ.get("STEAM_AGENT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "steam-agent"
    if sys.platform == "win32":
        base = _absolute_environment_path("LOCALAPPDATA")
        if base is None:
            base = home / "AppData" / "Local"
        return base / "steam-agent"
    base = _absolute_environment_path("XDG_DATA_HOME")
    if base is None:
        base = home / ".local" / "share"
    return base / "steam-agent"


def default_database_path() -> Path:
    return default_data_dir() / "steam-agent.sqlite3"


def default_credential_dir() -> Path:
    """Return a non-overridable default for explicit file-secret fallback.

    Runtime ``--data-dir`` and ``STEAM_AGENT_DATA_DIR`` overrides deliberately
    do not redirect credentials into a repository or shared workspace.
    """

    home = _fixed_user_home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "steam-agent" / "credentials"
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "steam-agent" / "credentials"
    # Secret fallback placement is deliberately not environment-overridable;
    # data-directory and XDG overrides may point at a repository or shared mount.
    return home / ".config" / "steam-agent" / "credentials"


def _fixed_user_home() -> Path:
    """Resolve the OS account home without process environment overrides."""

    if os.name == "posix":
        import pwd

        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        if home.is_absolute():
            return home
        raise RuntimeError("OS account home is not absolute")
    if sys.platform == "win32":
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        # CSIDL_PROFILE resolves through the shell API rather than HOME-like env.
        result = ctypes.windll.shell32.SHGetFolderPathW(  # type: ignore[attr-defined]
            None, 40, None, 0, buffer
        )
        home = Path(buffer.value)
        if result == 0 and home.is_absolute():
            return home
        raise RuntimeError("Windows profile directory is unavailable")
    home = Path.home()
    if home.is_absolute():
        return home
    raise RuntimeError("OS account home is unavailable")


def discover_steam_root() -> Path | None:
    override = os.environ.get("STEAM_AGENT_STEAM_ROOT")
    if override:
        candidate = Path(override).expanduser()
        return candidate if usable_steam_root(candidate) else None
    home = Path.home()
    candidates: list[Path]
    if sys.platform == "darwin":
        candidates = [home / "Library" / "Application Support" / "Steam"]
    elif sys.platform == "win32":
        program_files_x86 = _absolute_environment_path("PROGRAMFILES(X86)")
        program_files = _absolute_environment_path("PROGRAMFILES")
        candidates = [
            (program_files_x86 or Path("C:/Program Files (x86)")) / "Steam",
            (program_files or Path("C:/Program Files")) / "Steam",
        ]
    else:
        candidates = [home / ".local" / "share" / "Steam", home / ".steam" / "steam"]
    return next((candidate for candidate in candidates if usable_steam_root(candidate)), None)


def usable_steam_root(path: str | Path) -> bool:
    candidate = Path(path).expanduser()
    try:
        steamapps = candidate / "steamapps"
        if not candidate.is_dir() or not steamapps.is_dir():
            return False
        with os.scandir(steamapps) as entries:
            next(entries, None)
        return True
    except OSError:
        return False


def machine_for(machine_id: str) -> Machine:
    return Machine(
        id=machine_id,
        name=machine_id,
        platform=sys.platform,
        architecture=platform.machine() or None,
    )


def sync_installed(
    storage: Storage,
    *,
    steam_root: str | Path,
    machine_id: str,
    clock: Clock = now_utc,
) -> InstalledSyncResult:
    started_at = clock()
    machine = machine_for(machine_id)
    storage.upsert_machine(machine, observed_at=started_at)
    run = storage.begin_sync(
        provider="local_steam",
        capability="installed",
        machine_id=machine_id,
        started_at=started_at,
    )

    try:
        scan = scan_local_steam(steam_root)
        recorded: list[int] = []
        skipped: list[int] = []
        for app in scan.apps:
            if app.install_dir is None:
                skipped.append(app.appid)
                continue
            try:
                manifest_mtime = datetime.fromtimestamp(
                    app.manifest_path.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                manifest_mtime = None
            residual = app.residual or ResidualContent(None, None, None, "unknown")
            payload = {
                "appid": app.appid,
                "name": app.name,
                "install_dir": str(app.install_dir),
                "library_path": str(app.library_path),
                "manifest_path": str(app.manifest_path),
                "build_id": app.build_id,
                "size_on_disk_bytes": app.size_on_disk_bytes,
                "state_flags": app.state_flags,
                "parser_version": scan.parser_version,
                "residual_state": residual.state,
                "residual_compatdata_bytes": residual.compatdata_bytes,
                "residual_shadercache_bytes": residual.shadercache_bytes,
                "residual_workshop_bytes": residual.workshop_bytes,
            }
            storage.record_installed_observation(
                run.id,
                InstalledObservation(
                    appid=app.appid,
                    name=app.name,
                    app_type="unknown",
                    library_root=str(app.library_path),
                    install_dir=str(app.install_dir),
                    state="installed",
                    build_id=None if app.build_id is None else str(app.build_id),
                    size_bytes=app.size_on_disk_bytes,
                    manifest_path=str(app.manifest_path),
                    manifest_mtime=manifest_mtime,
                    observed_at=started_at,
                    residual_state=residual.state,
                    residual_compatdata_bytes=residual.compatdata_bytes,
                    residual_shadercache_bytes=residual.shadercache_bytes,
                    residual_workshop_bytes=residual.workshop_bytes,
                ),
                EvidenceInput(
                    provider="local_steam",
                    capability="installed",
                    source_kind="local_file",
                    source_locator=str(app.manifest_path),
                    retrieved_at=started_at,
                    support_level="local_heuristic",
                    context={"machine_id": machine_id},
                    payload=payload,
                    effective_at=manifest_mtime,
                ),
            )
            recorded.append(app.appid)

        # A scan is partial when it could not see or trust everything, not
        # when it correctly excluded an app that is not installed.  Treating
        # every warning as partial meant one paused download or one leftover
        # uninstalled manifest froze the last-good projection permanently
        # (observed on a real library).  Out-of-scope codes are still
        # reported on the run; they just no longer block promotion.
        blocking = [
            warning
            for warning in scan.warnings
            if warning.kind is not WarningKind.OUT_OF_SCOPE
        ]
        status = "partial" if blocking or skipped else "complete"
        warning_codes = sorted({warning.code for warning in scan.warnings})
        if skipped:
            warning_codes.append("unrecordable_install")
        completed = storage.finish_installed_sync(
            run.id,
            status=status,
            completed_at=clock(),
            error_code="SCAN_PARTIAL" if status == "partial" else None,
            error_detail=",".join(warning_codes) if warning_codes else None,
        )
        return InstalledSyncResult(
            run=completed,
            scan=scan,
            recorded_appids=tuple(recorded),
            skipped_appids=tuple(skipped),
        )
    except BaseException as exc:
        error_code = "SCAN_FAILED" if isinstance(exc, Exception) else "SCAN_CANCELED"
        try:
            storage.finish_installed_sync(
                run.id,
                status="failed",
                completed_at=clock(),
                error_code=error_code,
                error_detail=type(exc).__name__,
            )
        except BaseException:
            # Cleanup must never replace the exception that interrupted the scan.
            pass
        raise


def installed_item(game: object, *, include_paths: bool = False) -> dict[str, object]:
    values = asdict(game)  # accepts the storage InstalledGame dataclass
    item: dict[str, object] = {
        "appid": values["appid"],
        "name": values["name"],
        "app_type": values["app_type"],
        "state": values["state"],
        "build_id": values["build_id"],
        "size_bytes": values["size_bytes"],
        "observed_at": values["observed_at"],
        "evidence_ids": [values["evidence_id"]],
    }
    if include_paths:
        item.update(
            {
                "library_root": values["library_root"],
                "install_dir": values["install_dir"],
                "manifest_path": values["manifest_path"],
            }
        )
    return item


__all__ = [
    "InstalledSyncResult",
    "default_data_dir",
    "default_database_path",
    "default_credential_dir",
    "discover_steam_root",
    "installed_item",
    "machine_for",
    "sync_installed",
    "usable_steam_root",
]

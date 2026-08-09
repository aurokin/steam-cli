"""Read-only discovery of games installed by the local Steam client.

Only the small KeyValues subset used by ``libraryfolders.vdf`` and
``appmanifest_*.acf`` is supported.  This module deliberately does not write,
repair, lock, or otherwise modify any Steam-owned path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import re
import stat
from typing import Final, Literal, Mapping


KEYVALUES_PARSER_VERSION: Final = "steam-keyvalues-minimal-v1"
_MANIFEST_RE: Final = re.compile(r"appmanifest_(\d+)\.acf\Z")
MAX_KEYVALUES_BYTES: Final = 4 * 1024 * 1024
MAX_KEYVALUES_DEPTH: Final = 64
MAX_KEYVALUES_ENTRIES: Final = 100_000
MAX_KEYVALUES_STRING: Final = 1024 * 1024

# Steam's local appmanifest StateFlags follow the EAppState bit field.  This is
# an unofficial local heuristic, not a supported Steam API contract.  M1 only
# projects manifests that assert the FullyInstalled bit. A manifest in a known
# update state also qualifies when its resolved install directory still exists;
# all raw flags remain available on the observation for future refinement.
# Source cross-check: OpenSteamClient/OpenSteamClient, opensteamworks/EAppState.h
# (the current SteamKit master does not expose this local-client enum).
_APP_STATE_FULLY_INSTALLED: Final = 1 << 2
_APP_STATE_UNINSTALLED: Final = 1 << 0
_APP_STATE_PLAUSIBLE_UPDATE: Final = (
    (1 << 1) | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 12)
    | (1 << 16) | (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)
    | (1 << 21) | (1 << 22) | (1 << 23)
)
_SQLITE_INT_MAX: Final = (1 << 63) - 1

# Residual content: the per-app directories Steam's uninstall does not
# remove.  Measuring them is the only recursive walk this scanner performs,
# so it is bounded per app — a Proton prefix is an entire Windows filesystem
# and a shader cache can hold six figures of files.  Exceeding the budget
# yields a truncated count marked ``partial``, never a wrong-looking total.
MAX_RESIDUAL_ENTRIES: Final = 60_000


class KeyValuesError(ValueError):
    """The input is not in the supported KeyValues form."""


class WarningKind(StrEnum):
    MALFORMED = "malformed"
    INACCESSIBLE = "inaccessible"
    DUPLICATE = "duplicate"
    MISSING = "missing"
    # The scan read this manifest correctly and excluded it on purpose: the
    # app is not installed by M1's definition.  Distinct from the kinds
    # above, which all mean something could not be read or trusted, because
    # only those make a scan incomplete.  A paused download and a leftover
    # uninstalled manifest are ordinary on a real machine and must not
    # freeze the projection.
    OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True, slots=True)
class LocalSteamWarning:
    kind: WarningKind
    code: str
    message: str
    path: Path | None = None
    appid: int | None = None


@dataclass(frozen=True, slots=True)
class SteamLibrary:
    path: Path
    declared_appids: tuple[int, ...]
    source_index: str | None = None


@dataclass(frozen=True, slots=True)
class ResidualContent:
    """Bytes Steam's own uninstall leaves behind for one app.

    A byte count of ``0`` means the directory was looked for and is absent;
    ``None`` means it could not be measured.  ``state`` is ``measured`` only
    when all three counts are trustworthy, ``partial`` when the walk was
    truncated, a subtree was unreadable, or a whole tree turned out to be a
    link, and ``unknown`` when measurement did not run.  Links inside a tree
    are counted at their own size and never followed, because their targets
    are not content this tree strands.

    Steam Cloud and ``userdata`` are deliberately excluded: locating them
    requires enumerating account directories, which this scanner does not do.
    """

    compatdata_bytes: int | None
    shadercache_bytes: int | None
    workshop_bytes: int | None
    state: Literal["measured", "partial", "unknown"]


@dataclass(frozen=True, slots=True)
class InstalledSteamApp:
    appid: int
    name: str | None
    install_dir: Path | None
    build_id: int | None
    size_on_disk_bytes: int | None
    state_flags: int | None
    library_path: Path
    manifest_path: Path
    residual: ResidualContent | None = None


@dataclass(frozen=True, slots=True)
class LocalSteamScan:
    parser_version: str
    steam_root: Path
    libraries: tuple[SteamLibrary, ...]
    apps: tuple[InstalledSteamApp, ...]
    warnings: tuple[LocalSteamWarning, ...]


KeyValuesObject = dict[str, "str | KeyValuesObject"]


class _KeyValuesParser:
    def __init__(self, text: str) -> None:
        self._text = text.lstrip("\ufeff")
        self._offset = 0
        self._entries = 0

    def parse(self) -> KeyValuesObject:
        result = self._parse_object(top_level=True, depth=0)
        self._skip_space_and_comments()
        if self._offset != len(self._text):
            raise self._error("unexpected trailing input")
        return result

    def _parse_object(self, *, top_level: bool, depth: int) -> KeyValuesObject:
        if depth > MAX_KEYVALUES_DEPTH:
            raise self._error("maximum nesting depth exceeded")
        result: KeyValuesObject = {}
        normalized_keys: set[str] = set()
        while True:
            self._skip_space_and_comments()
            if self._offset >= len(self._text):
                if top_level:
                    return result
                raise self._error("unterminated object")
            if self._text[self._offset] == "}":
                if top_level:
                    raise self._error("unexpected closing brace")
                self._offset += 1
                return result

            key = self._parse_string()
            normalized_key = key.casefold()
            if normalized_key in normalized_keys:
                raise self._error("duplicate key in object")
            normalized_keys.add(normalized_key)
            self._entries += 1
            if self._entries > MAX_KEYVALUES_ENTRIES:
                raise self._error("maximum entry count exceeded")
            self._skip_space_and_comments()
            if self._offset >= len(self._text):
                raise self._error("missing value for key")
            if self._text[self._offset] == "{":
                self._offset += 1
                value: str | KeyValuesObject = self._parse_object(
                    top_level=False, depth=depth + 1
                )
            else:
                value = self._parse_string()
            result[key] = value

    def _parse_string(self) -> str:
        self._skip_space_and_comments()
        if self._offset >= len(self._text) or self._text[self._offset] != '"':
            raise self._error("expected quoted string")
        self._offset += 1
        value: list[str] = []
        while self._offset < len(self._text):
            char = self._text[self._offset]
            self._offset += 1
            if char == '"':
                return "".join(value)
            if char != "\\":
                value.append(char)
                if len(value) > MAX_KEYVALUES_STRING:
                    raise self._error("maximum string length exceeded")
                continue
            if self._offset >= len(self._text):
                raise self._error("unterminated escape sequence")
            escaped = self._text[self._offset]
            self._offset += 1
            value.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
        raise self._error("unterminated quoted string")

    def _skip_space_and_comments(self) -> None:
        while True:
            while self._offset < len(self._text) and self._text[self._offset].isspace():
                self._offset += 1
            if not self._text.startswith("//", self._offset):
                return
            newline = self._text.find("\n", self._offset + 2)
            self._offset = len(self._text) if newline < 0 else newline + 1

    def _error(self, message: str) -> KeyValuesError:
        line = self._text.count("\n", 0, self._offset) + 1
        return KeyValuesError(f"{message} at line {line}")


def parse_keyvalues(text: str) -> KeyValuesObject:
    """Parse the versioned, minimal KeyValues subset used by this scanner."""

    return _KeyValuesParser(text).parse()


def scan_local_steam(steam_root: str | Path) -> LocalSteamScan:
    """Inspect ``steam_root`` and return local library and manifest evidence.

    ``steam_root`` is explicit so tests and callers never need to probe the real
    user's installation. Relative library paths in a fixture are resolved from
    this root; Steam's normal absolute paths work unchanged.
    """

    warnings: list[LocalSteamWarning] = []
    expanded_root = Path(steam_root).expanduser()
    try:
        root = expanded_root.resolve(strict=False)
    except (OSError, RuntimeError):
        root = _lexical_absolute(expanded_root)
        warnings.append(
            LocalSteamWarning(
                WarningKind.INACCESSIBLE,
                "unresolvable_steam_root",
                "Steam root could not be resolved safely",
                root,
            )
        )
        return LocalSteamScan(
            parser_version=KEYVALUES_PARSER_VERSION,
            steam_root=root,
            libraries=(),
            apps=(),
            warnings=tuple(warnings),
        )
    library_file = root / "steamapps" / "libraryfolders.vdf"
    library_presence = _probe_library_index(library_file, warnings)
    library_data = (
        _read_keyvalues(library_file, warnings) if library_presence is True else None
    )

    library_specs: list[tuple[Path, tuple[int, ...], str | None]] = [(root, (), None)]
    if library_data is None:
        if library_presence is False:
            warnings.append(
                LocalSteamWarning(
                    WarningKind.MISSING,
                    "missing_libraryfolders",
                    "Steam library index was not found; scanning the primary library only",
                    library_file,
                )
            )
    else:
        folders = _object(_case_insensitive_value(library_data, "libraryfolders"))
        if folders is None:
            warnings.append(
                LocalSteamWarning(
                    WarningKind.MALFORMED,
                    "malformed_libraryfolders",
                    "libraryfolders.vdf has no libraryfolders object",
                    library_file,
                )
            )
        else:
            library_specs = _library_specs(root, folders, library_file, warnings)

    libraries: list[SteamLibrary] = []
    seen_library_paths: set[Path] = set()
    for library_path, declared, index in library_specs:
        normalized = _safe_resolve(library_path)
        if normalized is None:
            warnings.append(
                LocalSteamWarning(
                    WarningKind.INACCESSIBLE,
                    "unresolvable_library_path",
                    "Declared Steam library path could not be resolved safely",
                    _lexical_absolute(library_path),
                )
            )
            continue
        if normalized in seen_library_paths:
            warnings.append(
                LocalSteamWarning(
                    WarningKind.DUPLICATE,
                    "duplicate_library",
                    "The same Steam library path appears more than once",
                    normalized,
                )
            )
            continue
        seen_library_paths.add(normalized)
        libraries.append(SteamLibrary(normalized, declared, index))

    apps: list[InstalledSteamApp] = []
    seen_apps: dict[int, Path] = {}
    for library in libraries:
        steamapps = library.path / "steamapps"
        if not _is_readable_directory(steamapps):
            warnings.append(
                LocalSteamWarning(
                    WarningKind.INACCESSIBLE,
                    "inaccessible_library",
                    "Steam library directory is missing or inaccessible",
                    steamapps,
                )
            )
            continue

        try:
            with os.scandir(steamapps) as entries:
                candidates = sorted(
                    (
                        Path(entry.path)
                        for entry in entries
                        if entry.name.startswith("appmanifest_")
                        and entry.name.endswith(".acf")
                    ),
                    key=lambda item: item.name,
                )
        except OSError:
            warnings.append(
                LocalSteamWarning(
                    WarningKind.INACCESSIBLE,
                    "inaccessible_library",
                    "Could not enumerate Steam manifests",
                    steamapps,
                )
            )
            continue

        manifests: list[Path] = []
        for candidate in candidates:
            if candidate.is_symlink():
                warnings.append(
                    LocalSteamWarning(
                        WarningKind.MALFORMED,
                        "unsafe_manifest_entry",
                        "Symbolic-link manifest entry was ignored",
                        candidate,
                    )
                )
                continue
            try:
                is_file = candidate.is_file()
            except OSError:
                warnings.append(
                    LocalSteamWarning(
                        WarningKind.INACCESSIBLE,
                        "inaccessible_manifest_entry",
                        "Could not inspect manifest entry",
                        candidate,
                    )
                )
                continue
            if not is_file:
                warnings.append(
                    LocalSteamWarning(
                        WarningKind.MALFORMED,
                        "nonregular_manifest_entry",
                        "Non-regular manifest entry was ignored",
                        candidate,
                    )
                )
                continue
            if _MANIFEST_RE.fullmatch(candidate.name) is None:
                warnings.append(
                    LocalSteamWarning(
                        WarningKind.MALFORMED,
                        "unsupported_manifest_name",
                        "Manifest filename does not contain a numeric AppID",
                        candidate,
                    )
                )
                continue
            manifests.append(candidate)

        manifest_ids = {
            int(match.group(1))
            for manifest in manifests
            if (match := _MANIFEST_RE.fullmatch(manifest.name)) is not None
        }
        for appid in sorted(set(library.declared_appids) - manifest_ids):
            warnings.append(
                LocalSteamWarning(
                    WarningKind.MISSING,
                    "missing_manifest",
                    "The library index declares an app whose manifest is missing",
                    steamapps / f"appmanifest_{appid}.acf",
                    appid,
                )
            )

        for manifest in manifests:
            if manifest.is_symlink() or not manifest.is_file():
                warnings.append(
                    LocalSteamWarning(
                        WarningKind.INACCESSIBLE,
                        "unsafe_manifest_file",
                        "Manifest is missing, non-regular, or a symbolic link",
                        manifest,
                        int(_MANIFEST_RE.fullmatch(manifest.name).group(1)),
                    )
                )
                continue
            app = _read_manifest(manifest, library.path, warnings)
            if app is None:
                continue
            previous = seen_apps.get(app.appid)
            if previous is not None:
                warnings.append(
                    LocalSteamWarning(
                        WarningKind.DUPLICATE,
                        "duplicate_app",
                        "App is already represented by an earlier manifest",
                        manifest,
                        app.appid,
                    )
                )
                continue
            seen_apps[app.appid] = manifest
            apps.append(app)

    return LocalSteamScan(
        parser_version=KEYVALUES_PARSER_VERSION,
        steam_root=root,
        libraries=tuple(libraries),
        apps=tuple(sorted(apps, key=lambda app: (app.appid, str(app.library_path)))),
        warnings=tuple(warnings),
    )


def _library_specs(
    root: Path,
    folders: Mapping[str, str | KeyValuesObject],
    source: Path,
    warnings: list[LocalSteamWarning],
) -> list[tuple[Path, tuple[int, ...], str | None]]:
    specs: list[tuple[Path, tuple[int, ...], str | None]] = []
    for index, raw in folders.items():
        if not index.isdecimal():
            continue
        if not isinstance(raw, dict):
            warnings.append(
                LocalSteamWarning(
                    WarningKind.MALFORMED,
                    "unsupported_library_entry",
                    f"Library entry {index!r} uses an unsupported legacy value",
                    source,
                )
            )
            continue
        raw_path = raw.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            warnings.append(
                LocalSteamWarning(
                    WarningKind.MALFORMED,
                    "malformed_library_entry",
                    f"Library entry {index!r} has no path",
                    source,
                )
            )
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = root / path
        apps_object = _object(raw.get("apps")) or {}
        declared_values: list[int] = []
        for raw_appid in apps_object:
            if not raw_appid.isdecimal():
                continue
            appid = _signed_integer(raw_appid)
            if appid is None or appid > _SQLITE_INT_MAX:
                warnings.append(
                    LocalSteamWarning(
                        WarningKind.MALFORMED,
                        "manifest_integer_out_of_range",
                        "Declared AppID exceeds the supported signed 64-bit range",
                        source,
                    )
                )
                continue
            declared_values.append(appid)
        declared = tuple(sorted(declared_values))
        specs.append((path, declared, index))
    if not any(_safe_resolve(path) == root for path, _, _ in specs):
        specs.insert(0, (root, (), None))
    return specs


def _read_manifest(
    manifest: Path,
    library_path: Path,
    warnings: list[LocalSteamWarning],
) -> InstalledSteamApp | None:
    match = _MANIFEST_RE.fullmatch(manifest.name)
    if match is None:
        return None
    filename_appid = int(match.group(1))
    if filename_appid <= 0 or filename_appid > _SQLITE_INT_MAX:
        warnings.append(
            LocalSteamWarning(
                WarningKind.MALFORMED,
                (
                    "invalid_manifest_appid"
                    if filename_appid <= 0
                    else "manifest_integer_out_of_range"
                ),
                "Manifest filename contains an unsupported AppID and was ignored",
                manifest,
                filename_appid,
            )
        )
        return None
    parsed = _read_keyvalues(manifest, warnings)
    if parsed is None:
        return None
    state = _object(parsed.get("AppState"))
    if state is None:
        warnings.append(
            LocalSteamWarning(
                WarningKind.MALFORMED,
                "malformed_manifest",
                "Manifest has no AppState object",
                manifest,
                filename_appid,
            )
        )
        return None

    raw_appid = state.get("appid")
    parsed_appid = _signed_integer(raw_appid)
    if parsed_appid is not None and (
        parsed_appid <= 0 or parsed_appid > _SQLITE_INT_MAX
    ):
        warnings.append(
            LocalSteamWarning(
                WarningKind.MALFORMED,
                (
                    "invalid_manifest_appid"
                    if parsed_appid <= 0
                    else "manifest_integer_out_of_range"
                ),
                "Manifest contains an unsupported AppID and was ignored",
                manifest,
                parsed_appid,
            )
        )
        return None
    appid = parsed_appid
    if appid is None:
        appid = filename_appid
        warnings.append(
            LocalSteamWarning(
                WarningKind.MISSING,
                "missing_manifest_appid",
                "Manifest appid is missing or invalid; using its filename",
                manifest,
                filename_appid,
            )
        )
    elif appid != filename_appid:
        warnings.append(
            LocalSteamWarning(
                WarningKind.MALFORMED,
                "manifest_appid_mismatch",
                f"Manifest appid {appid} does not match filename appid {filename_appid}",
                manifest,
                appid,
            )
        )

    raw_state_flags = state.get("StateFlags")
    state_flags = _integer(raw_state_flags)
    if state_flags is None or state_flags > _SQLITE_INT_MAX:
        warnings.append(
            LocalSteamWarning(
                (
                    WarningKind.MALFORMED
                    if state_flags is not None
                    else WarningKind.MISSING
                ),
                (
                    "manifest_integer_out_of_range"
                    if state_flags is not None
                    else "missing_or_invalid_state_flags"
                ),
                "Manifest does not provide valid StateFlags and was not classified as installed",
                manifest,
                appid,
            )
        )
        return None
    if state_flags & _APP_STATE_UNINSTALLED != 0:
        warnings.append(
            LocalSteamWarning(
                WarningKind.OUT_OF_SCOPE,
                "uninstalled_app_state",
                "Manifest asserts the Uninstalled state and was skipped",
                manifest,
                appid,
            )
        )
        return None

    install_dir: Path | None = None
    raw_install_dir = state.get("installdir")
    if isinstance(raw_install_dir, str) and raw_install_dir.strip():
        try:
            common = (library_path / "steamapps" / "common").resolve(strict=False)
            candidate = (common / raw_install_dir).resolve(strict=False)
        except (OSError, RuntimeError):
            warnings.append(
                LocalSteamWarning(
                    WarningKind.INACCESSIBLE,
                    "unresolvable_install_dir",
                    "Manifest install directory could not be resolved safely",
                    manifest,
                    appid,
                )
            )
            return None
        if _is_relative_to(candidate, common):
            install_dir = candidate
        else:
            warnings.append(
                LocalSteamWarning(
                    WarningKind.MALFORMED,
                    "unsafe_install_dir",
                    "Manifest install directory escapes steamapps/common and was ignored",
                    manifest,
                    appid,
                )
            )
    else:
        warnings.append(
            LocalSteamWarning(
                WarningKind.MISSING,
                "missing_install_dir",
                "Manifest has no install directory",
                manifest,
                appid,
            )
        )

    fully_installed = state_flags & _APP_STATE_FULLY_INSTALLED != 0
    plausible_existing_update = (
        not fully_installed
        and state_flags != 0
        and state_flags & _APP_STATE_UNINSTALLED == 0
        and state_flags & _APP_STATE_PLAUSIBLE_UPDATE != 0
        and install_dir is not None
        and _is_readable_directory(install_dir)
    )
    if not fully_installed and not plausible_existing_update:
        warnings.append(
            LocalSteamWarning(
                WarningKind.OUT_OF_SCOPE,
                "not_fully_installed",
                "Manifest lacks proof of a complete or existing in-place installation and was skipped",
                manifest,
                appid,
            )
        )
        return None

    build_id = _bounded_optional_manifest_integer(
        state.get("buildid"), "buildid", manifest, appid, warnings
    )
    size_on_disk = _bounded_optional_manifest_integer(
        state.get("SizeOnDisk"), "SizeOnDisk", manifest, appid, warnings
    )

    return InstalledSteamApp(
        appid=appid,
        name=_string(state.get("name")),
        install_dir=install_dir,
        build_id=build_id,
        size_on_disk_bytes=size_on_disk,
        state_flags=state_flags,
        library_path=library_path,
        manifest_path=manifest,
        residual=_measure_residual_content(manifest.parent, appid),
    )


def _read_keyvalues(
    path: Path, warnings: list[LocalSteamWarning]
) -> KeyValuesObject | None:
    try:
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError
        if path.stat().st_size > MAX_KEYVALUES_BYTES:
            warnings.append(
                LocalSteamWarning(
                    WarningKind.MALFORMED,
                    "keyvalues_resource_limit",
                    "Steam metadata exceeds the supported size limit",
                    path,
                )
            )
            return None
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        warnings.append(
            LocalSteamWarning(
                WarningKind.INACCESSIBLE,
                "file_disappeared",
                "Steam metadata disappeared or changed type during the scan",
                path,
            )
        )
        return None
    except (OSError, UnicodeError):
        warnings.append(
            LocalSteamWarning(
                WarningKind.INACCESSIBLE,
                "inaccessible_file",
                "Could not read Steam metadata",
                path,
            )
        )
        return None
    try:
        return parse_keyvalues(text)
    except KeyValuesError as exc:
        warnings.append(
            LocalSteamWarning(
                WarningKind.MALFORMED,
                "malformed_keyvalues",
                str(exc),
                path,
            )
        )
        return None


def _probe_library_index(
    path: Path, warnings: list[LocalSteamWarning]
) -> bool | None:
    """Return present, missing, or inaccessible without Path.exists suppression."""

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError):
        warnings.append(
            LocalSteamWarning(
                WarningKind.INACCESSIBLE,
                "inaccessible_libraryfolders",
                "Steam library index could not be inspected",
                path,
            )
        )
        return None
    return True


def _object(value: object) -> KeyValuesObject | None:
    return value if isinstance(value, dict) else None


def _case_insensitive_value(
    values: Mapping[str, str | KeyValuesObject], key: str
) -> str | KeyValuesObject | None:
    wanted = key.casefold()
    return next(
        (value for candidate, value in values.items() if candidate.casefold() == wanted),
        None,
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _integer(value: object) -> int | None:
    integer = _signed_integer(value)
    return integer if integer is not None and integer >= 0 else None


def _signed_integer(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(value, 10)
    except ValueError:
        return None


def _bounded_optional_manifest_integer(
    value: object,
    field: str,
    manifest: Path,
    appid: int,
    warnings: list[LocalSteamWarning],
) -> int | None:
    if value is None:
        return None
    integer = _signed_integer(value)
    if integer is not None and 0 <= integer <= _SQLITE_INT_MAX:
        return integer
    out_of_range = integer is not None and integer > _SQLITE_INT_MAX
    warnings.append(
        LocalSteamWarning(
            WarningKind.MALFORMED,
            (
                "manifest_integer_out_of_range"
                if out_of_range
                else "invalid_manifest_integer"
            ),
            (
                f"Manifest field {field} exceeds the supported signed 64-bit range and was ignored"
                if out_of_range
                else f"Manifest field {field} is invalid and was ignored"
            ),
            manifest,
            appid,
        )
    )
    return None


def _measure_residual_content(steamapps: Path, appid: int) -> ResidualContent:
    """Size the per-app directories a Steam uninstall leaves in place."""

    budget = [MAX_RESIDUAL_ENTRIES]
    counts: list[int | None] = []
    truncated = False
    for root in (
        steamapps / "compatdata" / str(appid),
        steamapps / "shadercache" / str(appid),
        steamapps / "workshop" / "content" / str(appid),
    ):
        total, complete = _measure_tree(root, budget)
        counts.append(total)
        truncated = truncated or not complete
    compatdata, shadercache, workshop = counts
    return ResidualContent(
        compatdata_bytes=compatdata,
        shadercache_bytes=shadercache,
        workshop_bytes=workshop,
        state="partial" if truncated else "measured",
    )


def _measure_tree(root: Path, budget: list[int]) -> tuple[int | None, bool]:
    """Sum regular-file bytes under ``root``; never follow directory links.

    Returns ``(bytes, complete)``.  An absent tree is ``(0, True)``: looked
    for and not there.  Anything else that yields no bytes — an unreadable
    tree, a truncated walk, or a symlinked root, which is a pointer to
    content living somewhere else rather than content in this library — is
    incomplete, so a zero it produces is never read as an absence.
    """

    try:
        status = root.lstat()
    except FileNotFoundError:
        return 0, True
    except OSError:
        # Denied or otherwise unreadable: not the same claim as missing.
        return 0, False
    # A linked root is different from a link inside the tree: here the whole
    # tree lives somewhere else, so nothing was measured and a zero must not
    # read as an absence.  A junction has directory mode rather than
    # S_IFLNK, so it needs its own check.
    if stat.S_ISLNK(status.st_mode) or root.is_junction():
        return 0, False
    total = 0
    complete = True
    stack = [root]
    while stack:
        if budget[0] <= 0:
            return total, False
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if budget[0] <= 0:
                        return total, False
                    budget[0] -= 1
                    try:
                        # A link inside the tree is a pointer, not bytes
                        # this tree owns: a Proton prefix holds over a
                        # thousand links into the shared Proton runtime
                        # (measured on the target machine), which no
                        # uninstall of this game strands.  Count the link
                        # itself, never its target, and do not call the
                        # measurement truncated for it.  is_junction() is
                        # os.DirEntry API from Python 3.12, which this
                        # project requires; NTFS junctions are not symlinks
                        # and would otherwise be walked into.
                        if entry.is_symlink() or entry.is_junction():
                            total += entry.stat(
                                follow_symlinks=False
                            ).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        complete = False
        except OSError:
            complete = False
    return total, complete


def _is_readable_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except (OSError, RuntimeError):
        return False


def _lexical_absolute(path: Path) -> Path:
    """Make a path absolute without resolving symlinks or requiring I/O."""

    return Path(os.path.abspath(path))


def _is_direct_self_symlink(path: Path) -> bool:
    if not path.is_symlink():
        return False
    target = path.readlink()
    if not target.is_absolute():
        target = path.parent / target
    return _lexical_absolute(target) == _lexical_absolute(path)


def _safe_resolve(path: Path) -> Path | None:
    try:
        if _is_direct_self_symlink(path):
            return None
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


__all__ = [
    "InstalledSteamApp",
    "KEYVALUES_PARSER_VERSION",
    "ResidualContent",
    "KeyValuesError",
    "LocalSteamScan",
    "LocalSteamWarning",
    "SteamLibrary",
    "WarningKind",
    "parse_keyvalues",
    "scan_local_steam",
]

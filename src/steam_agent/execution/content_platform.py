"""Choose the SteamCMD content platform for Linux install/verify from evidence.

A Linux host can hold native Linux installs and Windows-depot installs that
run through Proton. ``app_update ... validate`` run with the wrong platform
can fetch a different depot set into the same directory, and adoption would
then rewrite the client's manifest to match. The platform is therefore
derived from local evidence, never guessed:

* An existing install uses the depots in its client manifest
  (``InstalledDepots``), classified by their ``config.oslist`` in the client's
  binary ``appinfo.vdf`` cache.
* A new install is native only when the app's own ``common.oslist`` lists
  Linux, the same rule its later updates face. A new Proton install is not
  qualified.

Missing, unreadable, malformed, mixed, or ambiguous evidence is ``None`` with a
fixed reason; callers refuse or defer rather than pick a platform.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import errno
import json
import os
from pathlib import Path
import re
import stat
import struct
from typing import BinaryIO, Literal

from steam_agent.local_steam import KeyValuesError, parse_keyvalues

ContentPlatform = Literal["linux", "windows"]

_MAGIC_V28 = 0x07564428
_MAGIC_V29 = 0x07564429
# Fixed per-entry header after the size field: info state, last updated,
# PICS token, text SHA-1, change number, binary SHA-1.
_ENTRY_HEADER = struct.Struct("<IIQ20sI20s")
_MAX_DEPTH = 32
# ``appinfo`` sections the classifier reads; duplicates there fail closed.
_READ_SECTIONS = frozenset({"common", "depots", "extended"})
# One app entry is a few KiB to a few hundred KiB; refuse absurd sizes rather
# than allocate them from an untrusted length field.
_MAX_ENTRY_BYTES = 64 * 1024 * 1024
_MAX_STRINGS = 1_000_000
_MAX_STRING_TABLE_BYTES = 64 * 1024 * 1024


# Reasons for selecting no platform. They are public reason codes, so the
# vocabulary is closed and never carries paths or identifiers.
UNQUALIFIED_REASONS = frozenset({
    "app_metadata_not_cached", "app_metadata_unavailable", "app_metadata_unreadable",
    "app_platform_unknown", "app_platform_unsupported", "installed_depot_not_in_metadata",
    "installed_depots_empty_with_content", "installed_depots_mixed_platforms",
    "installed_depots_unreadable", "new_install_without_linux_depot",
    "platform_neutral_install_ambiguous", "windows_depot_architecture_unqualified",
    "source_depot_not_in_metadata", "owned_content_platform_unknown",
    "dlc_metadata_not_cached", "install_directory_changed", "owned_content_elsewhere",
})


class AppInfoError(ValueError):
    """The binary app cache could not be read as a supported format."""


@dataclass(frozen=True, slots=True)
class DepotInfo:
    # ``None`` means the depot is not restricted to an OS.
    oslist: frozenset[str] | None
    osarch: str | None = None
    # Depots that belong to a DLC, a language, or another app say nothing
    # about which platform the base game was installed for.
    incidental: bool = False
    # ``depotfromapp``: the app whose entry defines this depot's restrictions.
    source_app: int | None = None
    # ``dlcappid``: the DLC whose own entry may also describe this depot.
    dlc_app: int | None = None
    # Whether a default install receives it: not ``optional`` and released on
    # the public branch (or supplied by another app).
    default: bool = True


@dataclass(frozen=True, slots=True)
class AppPlatformInfo:
    appid: int
    app_oslist: frozenset[str] | None
    depots: dict[int, DepotInfo]
    # DLC the base app names, through ``dlcappid`` tags or ``listofdlc``.
    dlc_apps: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class PlatformDecision:
    platform: ContentPlatform | None
    reason: str
    # Apps named only by the manifest behind a Windows selection (DLC from
    # ``InstalledDepots`` and ``SharedDepots`` sources). The broker records
    # them with owned content so recovery can still gate them.
    dlc_apps: tuple[int, ...] = field(default=(), compare=False)


def _oslist(value: object) -> frozenset[str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return frozenset(part.strip().casefold() for part in value.split(",") if part.strip())


def safe_install_dir_name(name: str) -> bool:
    """True only for one path component that can be quoted into an ACF value."""

    if not name or name in {".", ".."}:
        return False
    if any(character in name for character in ("/", "\\", '"')):
        return False
    # Typical filesystem component limit; a longer name would only surface
    # as ENAMETOOLONG mid-operation, after the client was already stopped.
    # JSON can legally carry unpaired surrogates, which no real filesystem
    # name can hold — reject them instead of crashing intake.
    try:
        if len(name.encode("utf-8")) > 255:
            return False
    except UnicodeEncodeError:
        return False
    # Control characters (newlines especially) could inject VDF fields.
    return not any(ord(character) < 32 for character in name)


def keyvalues_field(mapping: object, key: str, default: object = None) -> object:
    """A KeyValues field looked up case-insensitively.

    Valve keys ignore case, so differently cased duplicates are ambiguous and
    raise rather than letting one spelling hide another.
    """

    if not isinstance(mapping, dict):
        return default
    found = [value for name, value in mapping.items() if name.lower() == key.lower()]
    if len(found) > 1:
        raise KeyValuesError(f"duplicate {key}")
    return found[0] if found else default


def manifest_install_dir(manifest: Path) -> str | None:
    """``AppState.installdir`` exactly as target selection reads it.

    Parsed as KeyValues so comments cannot shadow it, but a raw backslash is
    returned as written: KeyValues escapes would otherwise turn it into a
    different, seemingly safe name. A manifest Steam is still rewriting may
    not parse; the tolerant raw reading keeps the operation retryable.
    """
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    raw = _raw_installdir(text)
    if raw is not None and "\\" in raw:
        return raw
    try:
        name = keyvalues_field(keyvalues_field(parse_keyvalues(text), "AppState"), "installdir")
    except KeyValuesError:
        return raw
    return name if isinstance(name, str) else None


_TOKEN = re.compile(r'"((?:[^"\\\n]|\\.)*)"|//[^\n]*|[{}]')


def _raw_installdir(text: str) -> str | None:
    """The first ``installdir`` value as written, from a quote-aware scan.

    Comments are skipped only outside quotes, so a ``//`` inside a quoted
    value (a URL, say) cannot hide the field.
    """
    quoted: list[str] = []
    for match in _TOKEN.finditer(text):
        if match.group(1) is None:
            quoted.append("")  # comment or brace: breaks a key/value pair
            continue
        if len(quoted) and quoted[-1].lower() == "installdir":
            return match.group(1)
        quoted.append(match.group(1))
    return None


def _manifest_install_dir(manifest: Path) -> str | None:
    """A manifest's ``installdir`` when it is one safe path component.

    Same reading and same rule as execution, so intake never records a
    request whose directory execution would reject.
    """
    name = manifest_install_dir(manifest)
    return name if name is not None and safe_install_dir_name(name) else None


def _u32(value: str) -> bool:
    return (
        value.isascii() and value.isdecimal() and len(value) <= 10
        and 0 < int(value) <= 0xFFFFFFFF
    )


@dataclass(frozen=True, slots=True)
class _Opaque:
    """A binary value of a type no classified field accepts."""

    kind: int
    value: int


def _decode_key(raw: bytes) -> str:
    # A corrupt key must not decode into something that merely fails to match
    # ``oslist`` and so reads as "unrestricted".
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AppInfoError("malformed key") from None


class _BinaryKeyValues:
    """Minimal reader for the binary KeyValues in ``appinfo.vdf`` entries."""

    def __init__(self, data: bytes, strings: list[bytes] | None) -> None:
        self._data = data
        self._strings = strings
        self._offset = 0

    def _cstring(self, *, key: bool = False) -> str:
        end = self._data.find(b"\0", self._offset)
        if end < 0:
            raise AppInfoError("unterminated string")
        raw = self._data[self._offset:end]
        self._offset = end + 1
        return _decode_key(raw) if key else raw.decode("utf-8", errors="replace")

    def at_end(self) -> bool:
        return self._offset == len(self._data)

    def _take(self, size: int) -> bytes:
        if self._offset + size > len(self._data):
            raise AppInfoError("truncated value")
        value = self._data[self._offset:self._offset + size]
        self._offset += size
        return value

    def _key(self) -> str:
        if self._strings is None:
            return self._cstring(key=True)
        (index,) = struct.unpack("<I", self._take(4))
        if index >= len(self._strings):
            raise AppInfoError("string index out of range")
        return _decode_key(self._strings[index])

    def parse(self, depth: int = 0, strict: bool = True) -> dict[str, object]:
        """Parse one object. ``strict`` rejects duplicate keys (any casing).

        Duplicates are rejected wherever the classifier reads (the record
        root, ``appinfo`` itself, and its ``common``, ``depots`` and
        ``extended`` sections), since a repeat there could hide a
        restriction. Valve's data does repeat keys in sections the classifier
        never reads (for example install registry entries), so those keep the
        last value instead of refusing the whole record.
        """
        if depth > _MAX_DEPTH:
            raise AppInfoError("nesting too deep")
        result: dict[str, object] = {}
        while True:
            kind = self._take(1)[0]
            if kind in (0x08, 0x0B):
                return result
            # KeyValues keys are case-insensitive.
            key = self._key().lower()
            if key in result and strict:
                raise AppInfoError("duplicate key")
            if kind == 0x00:
                child_strict = strict and (depth < 1 or depth > 1 or key in _READ_SECTIONS)
                result[key] = self.parse(depth + 1, child_strict)
            elif kind == 0x01:
                result[key] = self._cstring()
            elif kind == 0x02:
                result[key] = struct.unpack("<i", self._take(4))[0]
            elif kind == 0x03:
                result[key] = struct.unpack("<f", self._take(4))[0]
            elif kind in (0x04, 0x06):
                # Pointers and colors are not integers to any field read here.
                result[key] = _Opaque(kind, struct.unpack("<i", self._take(4))[0])
            elif kind in (0x07, 0x0A):
                result[key] = struct.unpack("<Q", self._take(8))[0]
            elif kind == 0x05:
                start = self._offset
                while self._take(2) != b"\0\0":
                    pass
                result[key] = self._data[start:self._offset - 2].decode(
                    "utf-16-le", errors="replace"
                )
            else:
                raise AppInfoError("unsupported value type")


def _read_exact(handle: BinaryIO, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise AppInfoError("truncated appinfo")
    return data


def _string_table(handle: BinaryIO, offset: int) -> list[bytes]:
    # Plain reads, not mmap: the client may rewrite this cache concurrently,
    # and a shrinking mapped file raises SIGBUS instead of an exception.
    handle.seek(offset)
    (count,) = struct.unpack("<I", _read_exact(handle, 4))
    if count > _MAX_STRINGS:
        raise AppInfoError("string table too large")
    data = handle.read(_MAX_STRING_TABLE_BYTES)
    names = data.split(b"\0", count)
    if len(names) <= count:
        raise AppInfoError("unterminated or oversized string table")
    # Kept as bytes and decoded strictly when a record uses a key, so a bad
    # string elsewhere in the table cannot reject unrelated records.
    return names[:count]


def read_app_platforms(appinfo_path: Path, appids: set[int]) -> dict[int, AppPlatformInfo]:
    """Return depot evidence for each cached AppID in ``appids``.

    AppIDs absent from the cache are absent from the result. Raises
    :class:`AppInfoError` or ``OSError`` when the cache is unreadable or not a
    supported format.
    """

    found: dict[int, AppPlatformInfo] = {}
    with open(appinfo_path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        magic, _universe = struct.unpack("<II", _read_exact(handle, 8))
        if magic == _MAGIC_V29:
            (table_offset,) = struct.unpack("<q", _read_exact(handle, 8))
            if not 16 <= table_offset <= size:
                raise AppInfoError("string table offset out of range")
            end = table_offset
        elif magic == _MAGIC_V28:
            table_offset = 0
            end = size
        else:
            raise AppInfoError("unsupported appinfo version")
        entries: dict[int, bytes] = {}
        position = handle.tell()
        # Entries end with a lone 4-byte zero AppID.
        while True:
            if position + 4 > end:
                raise AppInfoError("missing terminator")
            (appid,) = struct.unpack("<I", _read_exact(handle, 4))
            if appid == 0:
                if position + 4 != end:
                    # An early terminator could hide later (duplicate) records.
                    raise AppInfoError("terminator before end of records")
                break
            (length,) = struct.unpack("<I", _read_exact(handle, 4))
            body = position + 8
            if not _ENTRY_HEADER.size <= length <= _MAX_ENTRY_BYTES or body + length > end:
                raise AppInfoError("truncated entry")
            if appid in appids:
                if appid in entries:
                    raise AppInfoError("duplicate app record")
                entries[appid] = _read_exact(handle, length)[_ENTRY_HEADER.size:]
            else:
                handle.seek(length, os.SEEK_CUR)
            position = body + length
        strings = _string_table(handle, table_offset) if magic == _MAGIC_V29 and entries else None
    for appid, data in entries.items():
        reader = _BinaryKeyValues(data, strings)
        tree = reader.parse()
        if not reader.at_end():
            # A valid prefix must not hide restrictions in unread bytes.
            raise AppInfoError("trailing record bytes")
        record = tree.get("appinfo")
        # A ``public_only`` placeholder (no access token) has no depot data;
        # treating it as cached would read unknown as empty, so it counts as
        # missing everywhere a record is required.
        if isinstance(record, dict) and _flag(record.get("public_only"), "public_only"):
            continue
        found[appid] = _platform_info(appid, tree)
    return found


def _platform_info(appid: int, tree: dict[str, object]) -> AppPlatformInfo:
    # Steam always wraps records in ``appinfo``; duplicate-key strictness is
    # placed relative to that wrapper, so an unwrapped record is malformed.
    root = tree.get("appinfo")
    if not isinstance(root, dict):
        raise AppInfoError("missing appinfo root")
    common = root.get("common", {})
    if not isinstance(common, dict) or not isinstance(common.get("oslist", ""), str):
        raise AppInfoError("malformed common section")
    app_oslist = _oslist(common.get("oslist"))
    depots: dict[int, DepotInfo] = {}
    dlc_apps: set[int] = set()
    raw_depots = root.get("depots", {})
    if not isinstance(raw_depots, dict):
        raise AppInfoError("malformed depot table")
    if raw_depots:
        for key, depot in raw_depots.items():
            if not _u32(key):
                continue
            if int(key) in depots:
                # ``11`` and ``011`` name one depot; either could hide the other.
                raise AppInfoError("duplicate depot")
            if not isinstance(depot, dict):
                # A numeric entry is a depot; an unreadable one is not "absent".
                raise AppInfoError("malformed depot entry")
            dlc_app = _app_reference(depot["dlcappid"]) if "dlcappid" in depot else None
            if dlc_app is not None:
                dlc_apps.add(dlc_app)
            config = depot.get("config", {})
            # Malformed restrictions must not read as "unrestricted".
            if not isinstance(config, dict) or not isinstance(config.get("oslist", ""), str):
                raise AppInfoError("malformed depot config")
            arch = config.get("osarch")
            if arch is not None and not isinstance(arch, (str, int)):
                raise AppInfoError("malformed depot architecture")
            source_app = (
                _app_reference(depot["depotfromapp"]) if "depotfromapp" in depot else None
            )
            depots[int(key)] = DepotInfo(
                oslist=_oslist(config.get("oslist")),
                osarch=str(arch) if arch not in (None, "") else None,
                # A ``depotfromapp`` reference can still be a base-game depot
                # (installed as a shared depot), so it is not incidental.
                incidental=bool("dlcappid" in depot or _non_default_language(config)),
                source_app=source_app,
                dlc_app=dlc_app,
                default=_received_by_default(depot, source_app),
            )
    extended = root.get("extended", {})
    if not isinstance(extended, dict):
        raise AppInfoError("malformed extended section")
    listed = extended.get("listofdlc", "")
    # A single DLC is cached as an integer, several as a comma-separated string.
    if isinstance(listed, int) and not isinstance(listed, bool):
        dlc_apps.add(_app_reference(listed))
    elif isinstance(listed, str):
        dlc_apps.update(_app_reference(item.strip()) for item in listed.split(",") if item.strip())
    else:
        raise AppInfoError("malformed listofdlc")
    return AppPlatformInfo(appid, app_oslist, depots, frozenset(dlc_apps))


def _non_default_language(config: dict[str, object]) -> bool:
    """A language depot other than English, SteamCMD's default language."""
    language = config.get("language")
    if language in (None, ""):
        return False
    if not isinstance(language, str):
        raise AppInfoError("malformed depot language")
    return language.lower() != "english"


def _flag(value: object, name: str) -> bool:
    if value not in (None, 0, "0", 1, "1"):
        raise AppInfoError(f"malformed {name} flag")
    return value in (1, "1")


def _received_by_default(depot: dict[str, object], source_app: int | None) -> bool:
    """Whether a default global desktop install receives the depot.

    Optional depots, and depots limited to another realm (Steam China), to
    Steam Deck, to low-violence builds, or to high-quality audio, are not
    part of it.
    """
    if _flag(depot.get("optional"), "optional"):
        return False
    config = depot.get("config", {})
    if isinstance(config, dict):
        realm = config.get("realm")
        if realm is not None and not isinstance(realm, str):
            raise AppInfoError("malformed realm")
        if realm not in (None, "", "steamglobal"):
            return False
        if any(_flag(config.get(name), name) for name in ("steamdeck", "lowviolence", "highqualityaudio")):
            return False
    if source_app is not None:
        return True  # the defining app releases it
    manifests = depot.get("manifests")
    if manifests is not None and not isinstance(manifests, dict):
        raise AppInfoError("malformed manifests")
    return bool(manifests) and "public" in manifests


def _app_reference(value: object) -> int:
    if isinstance(value, str) and _u32(value):
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 0xFFFFFFFF:
        return value
    raise AppInfoError("malformed app reference")


def installed_depots(manifest: Path, appid: int) -> dict[int, int | None] | None:
    """``InstalledDepots`` as depot ID -> owning DLC AppID.

    ``None`` when unreadable or when the manifest describes another AppID.
    """

    try:
        tree = parse_keyvalues(manifest.read_text(encoding="utf-8", errors="replace"))
        state = keyvalues_field(tree, "AppState")
        if not isinstance(state, dict) or keyvalues_field(state, "appid") != str(appid):
            return None
        # An omitted block means no depot is recorded yet; a wrong type is damage.
        depots = keyvalues_field(state, "InstalledDepots", {})
        if not isinstance(depots, dict):
            return None
        result: dict[int, int | None] = {}
        for key, value in depots.items():
            if not _u32(key) or not isinstance(value, dict) or int(key) in result:
                return None
            # A depot is installed only with the manifest ID it was built from.
            gid = keyvalues_field(value, "manifest")
            if not (isinstance(gid, str) and gid.isascii() and gid.isdecimal() and len(gid) <= 20
                    and 0 < int(gid) <= 0xFFFFFFFFFFFFFFFF):
                return None
            dlc = keyvalues_field(value, "dlcappid")
            if dlc is not None and not (isinstance(dlc, str) and _u32(dlc)):
                return None
            result[int(key)] = int(dlc) if isinstance(dlc, str) else None
    except (OSError, KeyValuesError):
        return None
    return result


def shared_depots(manifest: Path, appid: int) -> dict[int, int] | None:
    """``SharedDepots`` as depot ID -> source AppID; ``None`` when unreadable."""

    try:
        tree = parse_keyvalues(manifest.read_text(encoding="utf-8", errors="replace"))
        state = keyvalues_field(tree, "AppState")
        if not isinstance(state, dict) or keyvalues_field(state, "appid") != str(appid):
            return None
        shared = keyvalues_field(state, "SharedDepots", {})
        if not isinstance(shared, dict):
            return None
        if not all(_u32(key) and isinstance(value, str) and _u32(value) for key, value in shared.items()):
            return None
    except (OSError, KeyValuesError):
        return None
    result = {int(key): int(value) for key, value in shared.items()}
    # ``13`` and ``013`` name one depot; either could hide the other.
    return result if len(result) == len(shared) else None


_Restriction = ContentPlatform | Literal["neutral", "linux_shared", "windows_shared", "other"]


def _restriction_platform(oslist: frozenset[str]) -> _Restriction:
    linux, windows = "linux" in oslist, "windows" in oslist
    if linux and windows:
        # Shared content served to both platforms, such as a common data depot.
        return "neutral"
    # A depot one platform shares with macOS is evidence for that platform
    # only alongside a depot exclusive to it: a macOS install holds it too.
    if linux:
        return "linux" if oslist == {"linux"} else "linux_shared"
    if windows:
        return "windows" if oslist == {"windows"} else "windows_shared"
    return "other"


def _platform_of(depot: DepotInfo) -> _Restriction:
    return "neutral" if depot.oslist is None else _restriction_platform(depot.oslist)


def decide_platform(
    info: AppPlatformInfo | None,
    installed: Mapping[int, DepotInfo] | None,
    *,
    target_has_content: bool = False,
    shared: Iterable[int] = (),
    apps: Mapping[int, AppPlatformInfo] | None = None,
) -> PlatformDecision:
    """Classify evidence; every Windows result must still pass ``_windows_refusal``.

    ``installed`` maps each installed depot to its metadata. ``None`` or empty
    means no depot is recorded yet (a new or never-completed install).
    """

    if info is None:
        return PlatformDecision(None, "app_metadata_not_cached")
    if not installed:
        if installed is not None and target_has_content:
            # The client began an install without recording its depots; its
            # files may belong to either platform.
            return PlatformDecision(None, "installed_depots_empty_with_content")
        # The app's own platform list must list Linux, the same rule later
        # updates of this install will apply: the broker never starts an
        # install it could not maintain.
        if info.app_oslist is not None and "linux" in info.app_oslist:
            return PlatformDecision("linux", "native_linux_available")
        return PlatformDecision(None, "new_install_without_linux_depot")
    found = _restrictions(installed.values())
    if found & {"other", "linux_shared", "windows_shared"} or len(found) > 1:
        return PlatformDecision(None, "installed_depots_mixed_platforms")
    present = {*installed.keys(), *shared}
    related = apps or {info.appid: info}
    if found:
        platform: ContentPlatform = "linux" if found == {"linux"} else "windows"
        shared_ids = set(shared)
        if not _restrictions(d for k, d in installed.items() if k not in shared_ids):
            # Only depots stored in other apps' directories (``SharedDepots``)
            # name the platform; the app's own depots must still be complete
            # for it, exactly as for a shared-only install.
            refusal = _incomplete_for(info, present, related, platform)
            if refusal is not None:
                return refusal
        return PlatformDecision(platform, f"installed_{platform}_depots")
    return _neutral_install(info, present, related)


def _restrictions(depots: Iterable[DepotInfo]) -> set[str]:
    found = {_platform_of(depot) for depot in depots} - {"neutral"}
    for platform_name in ("linux", "windows"):
        if platform_name in found:
            found.discard(f"{platform_name}_shared")
    return found


def _load_missing(appinfo: Path, apps: dict[int, AppPlatformInfo], wanted: set[int]) -> None:
    missing = wanted - apps.keys()
    if missing:
        apps.update(read_app_platforms(appinfo, missing))


def _tagged_dlc(
    apps: Mapping[int, AppPlatformInfo], pairs: Iterable[tuple[int, int]],
) -> set[int]:
    """DLC tagged on the given (depot, app) descriptions that are loaded."""
    return {
        description.dlc_app
        for depot_id, app_id in pairs
        if app_id in apps
        and (description := apps[app_id].depots.get(depot_id)) is not None
        and description.dlc_app is not None
    }


def _load_related(appinfo: Path, apps: dict[int, AppPlatformInfo]) -> None:
    """Add every DLC and ``depotfromapp`` source the loaded apps name.

    Missing entries stay absent here; ``_windows_refusal`` refuses them.
    """

    # Sources can list their own DLC and DLC can reference sources; follow
    # both until nothing new is named (bounded, since IDs are finite).
    requested: set[int] = set(apps)
    while True:
        wanted = {
            related
            for app in list(apps.values())
            for related in (
                *app.dlc_apps,
                *(depot.source_app for depot in app.depots.values() if depot.source_app),
            )
        } - requested
        if not wanted:
            return
        requested |= wanted
        apps.update(read_app_platforms(appinfo, wanted))


def _merge(descriptions: Iterable[DepotInfo]) -> DepotInfo | PlatformDecision:
    """Combine every description of one depot; conflicting restrictions refuse."""

    found = list(descriptions)
    restrictions = {depot.oslist for depot in found if depot.oslist is not None}
    architectures = {depot.osarch for depot in found if depot.osarch is not None}
    if len(restrictions) > 1 or len(architectures) > 1:
        return PlatformDecision(None, "installed_depots_mixed_platforms")
    return DepotInfo(
        oslist=next(iter(restrictions), None),
        # Keep the architecture any description declares.
        osarch=next(iter(architectures), None),
        incidental=any(depot.incidental for depot in found),
    )


def _effective(
    depot_id: int, depot: DepotInfo, apps: Mapping[int, AppPlatformInfo],
) -> DepotInfo | PlatformDecision:
    """One depot entry combined with the definition its ``depotfromapp`` names.

    Only one reference is followed: an unread or chained definition could hide
    its restrictions, so it refuses. The reference's own restriction and
    architecture still count alongside the definition's.
    """

    if depot.source_app is None:
        return _merge([depot])
    source = apps.get(depot.source_app)
    defined = source.depots.get(depot_id) if source is not None else None
    if defined is None or defined.source_app is not None:
        return PlatformDecision(None, "source_depot_not_in_metadata")
    descriptions = [depot, defined]
    # The reference or its definition may tag a DLC whose own entry
    # restricts the depot.
    for tag in sorted({depot.dlc_app, defined.dlc_app} - {None}):
        dlc = apps.get(tag)
        if dlc is None:
            return PlatformDecision(None, "dlc_metadata_not_cached")
        if depot_id in dlc.depots:
            if dlc.depots[depot_id].source_app is not None:
                # Follow one reference only, here too.
                return PlatformDecision(None, "source_depot_not_in_metadata")
            descriptions.append(dlc.depots[depot_id])
    return _merge(descriptions)


def _windows_refusal(
    apps: Mapping[int, AppPlatformInfo], installed: Iterable[DepotInfo] = (),
) -> PlatformDecision | None:
    """Gate every Windows selection, however it was reached.

    SteamCMD's bitness under a forced Windows platform is unqualified, so an
    architecture-specific Windows or shared depot anywhere in the base app,
    its DLC, or a source app refuses. So does any depot whose effective
    restriction cannot be established.
    """

    # An offline client's cache can lack a DLC that SteamCMD, signed in
    # separately, is entitled to download, so a missing DLC entry refuses.
    if any(dlc not in apps for app in list(apps.values()) for dlc in app.dlc_apps):
        return PlatformDecision(None, "dlc_metadata_not_cached")
    depots = [*installed]
    described: dict[int, list[DepotInfo]] = {}
    for app in apps.values():
        for depot_id, depot in app.depots.items():
            effective = _effective(depot_id, depot, apps)
            if isinstance(effective, PlatformDecision):
                return effective
            described.setdefault(depot_id, []).append(effective)
            # Gate every description by itself too, raw reference and source
            # definition included: merging can narrow a shared description's
            # OS eligibility and hide its architecture.
            depots.append(effective)
            depots.append(depot)
            if depot.source_app is not None:
                depots.append(apps[depot.source_app].depots[depot_id])
    # Base and DLC entries can both describe one depot; merged, a conflict
    # leaves its restriction unknown and an architecture on either counts.
    for descriptions in described.values():
        merged = _merge(descriptions)
        if isinstance(merged, PlatformDecision):
            return merged
        depots.append(merged)
    if any(
        depot.osarch is not None
        and _platform_of(depot) in {"windows", "windows_shared", "neutral"}
        for depot in depots
    ):
        return PlatformDecision(None, "windows_depot_architecture_unqualified")
    return None


def _neutral_install(
    info: AppPlatformInfo, installed: Iterable[int], apps: Mapping[int, AppPlatformInfo],
) -> PlatformDecision:
    """Every installed depot is shared between Linux and Windows.

    Only the app's preferred platform is considered: native when the app
    lists Linux, otherwise Windows. It qualifies only when every depot that
    platform would receive is installed (or installed as a shared depot).
    Architecture-specific depots are left out because only one architecture's
    set is installed. A missing depot never moves the choice to the other
    platform: absence has too many causes (beta branches, architectures,
    macOS installs) to infer a platform from.
    """

    if info.app_oslist is None:
        return PlatformDecision(None, "app_platform_unknown")
    if "linux" in info.app_oslist:
        preferred: ContentPlatform = "linux"
        decision = PlatformDecision("linux", "platform_neutral_depots")
    elif "windows" in info.app_oslist:
        preferred = "windows"
        decision = PlatformDecision("windows", "platform_neutral_depots_windows_app")
    else:
        return PlatformDecision(None, "app_platform_unsupported")
    return _incomplete_for(info, set(installed), apps, preferred) or decision


def _incomplete_for(
    info: AppPlatformInfo, present: set[int], apps: Mapping[int, AppPlatformInfo],
    platform: ContentPlatform,
) -> PlatformDecision | None:
    """Refusal when a base-game depot ``platform`` would receive is missing.

    Architecture-specific depots are judged per architecture: only one
    architecture's depots are installed, so the install qualifies only if
    every depot of at least one architecture the platform receives is.
    """

    arch_groups: dict[str, set[int]] = {}
    for depot_id, depot in info.depots.items():
        # Optional and unreleased depots are not part of a default install.
        if depot.incidental or not depot.default:
            continue
        # A reference is judged by the definition it names, release status
        # included: an unreleased definition is not part of a default install.
        effective = _effective(depot_id, depot, apps)
        if isinstance(effective, PlatformDecision):
            return effective
        if depot.source_app is not None and not apps[depot.source_app].depots[depot_id].default:
            continue
        if effective.incidental:
            continue  # e.g. a language restriction stated only in the definition
        if effective.oslist is not None and platform not in effective.oslist:
            continue
        if effective.osarch is not None:
            arch_groups.setdefault(effective.osarch, set()).add(depot_id)
        elif depot_id not in present:
            return PlatformDecision(None, "platform_neutral_install_ambiguous")
    # The same effective, default-install view decides which architecture
    # depots exist and whether one architecture's set is fully installed.
    if arch_groups and not any(group <= present for group in arch_groups.values()):
        return PlatformDecision(None, "platform_neutral_install_ambiguous")
    return None


def locate_appinfo(library: Path, home: Path | None = None) -> Path | None:
    """The client's binary app cache.

    Checks the library itself (a primary library is the client root), then
    the planner's ``STEAM_AGENT_STEAM_ROOT`` override, then the client's own
    ``~/.steam/root`` link, which follows custom install locations, and the
    native and Flatpak defaults.
    """

    own = library / "appcache" / "appinfo.vdf"
    if own.is_file():
        return own
    # A client root whose own cache is missing must not borrow another's.
    if any((library / marker).exists() for marker in ("appcache", "steam.sh", "ubuntu12_32")):
        return None
    override = os.environ.get("STEAM_AGENT_STEAM_ROOT")
    if override:
        try:
            root = Path(override).expanduser()
        except RuntimeError:  # ``~user`` for an unknown user
            return None
        # Inherited from the planner, it may name another client: it is
        # evidence only when that client lists this library too.
        candidate = root / "appcache" / "appinfo.vdf"
        return candidate if _lists_library(root, library) is True and candidate.is_file() else None
    home = home or Path.home()
    roots: dict[Path, Path] = {}
    for root in (
        home / ".steam" / "root",
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    ):
        try:
            if root.is_dir():
                roots.setdefault(root.resolve(), root)
        except (OSError, RuntimeError):
            return None
    # A secondary library belongs to the client whose library list names it;
    # with several clients installed, another client's cache is not evidence.
    # Find the owner first: its missing cache refuses rather than falling
    # back to a client that happens to have one.
    listing = {root: _lists_library(root, library) for root in roots.values()}
    if None in listing.values():
        return None  # an unreadable index could hide a second owner
    owners = [root for root, lists in listing.items() if lists]
    if len(owners) != 1:
        return None
    cache = owners[0] / "appcache" / "appinfo.vdf"
    return cache if cache.is_file() else None


def _lists_library(root: Path, library: Path) -> bool | None:
    """Whether the client at ``root`` lists ``library``; ``None`` when unknown.

    A client without a library index lists nothing; an unreadable or
    malformed index leaves ownership unknown.
    """
    try:
        target = library.resolve(strict=True)
        text = (root / "steamapps" / "libraryfolders.vdf").read_text(
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError):
        return None
    try:
        folders = keyvalues_field(parse_keyvalues(text), "libraryfolders")
        if not isinstance(folders, dict):
            return None
        paths = []
        for key, entry in folders.items():
            if not key.isdecimal():
                continue  # older indexes keep non-library fields at top level
            path = keyvalues_field(entry, "path")
            if not isinstance(path, str):
                return None  # a malformed library entry hides what it names
            paths.append(path)
    except KeyValuesError:
        return None
    unknown = False
    for path in paths:
        if not isinstance(path, str):
            continue
        # Relative entries are relative to the client root, as for the
        # installed-library scanner. Strict resolution, because non-strict
        # ``resolve`` silently returns an inaccessible path unchanged.
        listed = Path(path)
        try:
            if (listed if listed.is_absolute() else root / listed).resolve(strict=True) == target:
                return True
        except (FileNotFoundError, NotADirectoryError):
            continue  # a missing library names nothing
        except RuntimeError:
            continue  # a symlink loop resolves nowhere (older Pythons)
        except OSError as error:
            if error.errno == errno.ELOOP:
                continue  # a symlink loop resolves nowhere
            # An inaccessible alias could name this library; a later entry
            # may still prove it definitely does.
            unknown = True
    return None if unknown else False


def local_nested_manifest(target: Path, appid: int) -> Path | None:
    """SteamCMD's manifest under ``target``, only when neither it nor its
    ``steamapps`` directory is a link: linked evidence describes elsewhere."""

    steamapps = target / "steamapps"
    manifest = steamapps / f"appmanifest_{appid}.acf"
    try:
        if not stat.S_ISDIR(os.lstat(steamapps).st_mode):
            return None
        return manifest if stat.S_ISREG(os.lstat(manifest).st_mode) else None
    except (FileNotFoundError, NotADirectoryError):
        return None


def _holds_installed_files(target: Path) -> bool:
    """Installed bytes outside SteamCMD's own ``steamapps`` bookkeeping and staging."""
    return _holds_bytes(target, skip="steamapps")


def _holds_bytes(
    directory: Path, *, skip: str | None = None, ignore: frozenset[Path] = frozenset(),
) -> bool:
    """Whether ``directory`` holds a non-empty regular file.

    Links are not followed: a dangling link, a link to data elsewhere, or an
    empty placeholder is not content here. ``skip`` names a top-level entry
    to ignore, and ``ignore`` specific files.
    """

    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            return False  # a linked directory is not content here
    except FileNotFoundError:
        return False
    pending = [directory]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if current == directory and entry.name == skip:
                    continue
                try:
                    found = os.lstat(entry.path)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(found.st_mode):
                    pending.append(Path(entry.path))
                elif (stat.S_ISREG(found.st_mode) and found.st_size > 0
                        and Path(entry.path) not in ignore):
                    return True
    return False


def _holds_content(directory: Path, appid: int | None) -> bool:
    """Any file, including staged downloads.

    With ``appid``, SteamCMD's own nested manifest for it does not count.
    """

    manifest = directory / "steamapps" / f"appmanifest_{appid}.acf" if appid else None

    def raise_error(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError) or Path(error.filename) != directory:
            raise error

    for root, dirs, files in os.walk(directory, onerror=raise_error):
        if any(Path(root, name) != manifest for name in files):
            return True
        # ``os.walk`` does not descend into linked directories; their content
        # is still the target's content.
        if any(Path(root, name).is_symlink() for name in dirs):
            return True
    return False


def content_platform(
    *, library: Path, appid: int, manifest: Path | None,
    target: Path | None = None, home: Path | None = None,
) -> PlatformDecision:
    """Evidence-bound platform for one Linux content operation.

    Without a client manifest, a SteamCMD manifest that an earlier run left
    under ``target`` is the install's evidence. A target with neither manifest
    can only hold content the broker itself began, and the broker starts new
    installs natively.
    """

    # Residual risk, accepted in ADR 0036: every gate reads the client's local
    # cache. Metadata Valve changed after that cache was written (for example
    # while the client stays offline) is invisible until the client refreshes.
    try:
        content_dir: Path | None = None
        if manifest is None and target is not None:
            nested = local_nested_manifest(target, appid)
            # SteamCMD's manifest describes the target it was run in, whatever
            # ``installdir`` it records, but only alongside actual files: a
            # leftover manifest alone is not an existing install.
            if nested is not None and _holds_installed_files(target):
                manifest, content_dir = nested, target
        return _content_platform(
            library=library, appid=appid, manifest=manifest, content_dir=content_dir, home=home,
        )
    except (AppInfoError, OSError, RuntimeError, ValueError, struct.error):
        return PlatformDecision(None, "app_metadata_unreadable")


def _content_platform(
    *, library: Path, appid: int, manifest: Path | None, content_dir: Path | None,
    home: Path | None,
) -> PlatformDecision:
    depots: dict[int, int | None] | None = None
    target_has_content = False
    if manifest is not None:
        depots = installed_depots(manifest, appid)
        if depots is None:
            return PlatformDecision(None, "installed_depots_unreadable")
        if not depots:
            if content_dir is None:
                name = _manifest_install_dir(manifest)
                if name is None:
                    return PlatformDecision(None, "installed_depots_unreadable")
                content_dir = library / "steamapps" / "common" / name
            # The nested manifest lives inside its own target, so ignore it.
            target_has_content = _holds_content(content_dir, appid)
    appinfo = locate_appinfo(library, home)
    if appinfo is None:
        return PlatformDecision(None, "app_metadata_unavailable")
    dlcs = {dlc for dlc in (depots or {}).values() if dlc is not None}
    apps = read_app_platforms(appinfo, {appid} | dlcs)
    info = apps.get(appid)
    if info is None:
        return PlatformDecision(None, "app_metadata_not_cached")
    if depots is None:
        return decide_platform(info, None)
    # Classification reads only the records that describe installed depots,
    # so a malformed entry for an unrelated DLC cannot block a proven native
    # title; the Windows gate below still reads every related record.
    owners_needed = {dlc for dlc in depots.values() if dlc is not None}
    owners_needed |= {
        info.depots[depot].dlc_app for depot in depots
        if depot in info.depots and info.depots[depot].dlc_app is not None
    }
    if any(depot not in info.depots for depot in depots):
        owners_needed |= info.dlc_apps
    _load_missing(appinfo, apps, owners_needed)
    _load_missing(appinfo, apps, {
        description.source_app
        for depot in depots
        for app in apps.values()
        if (description := app.depots.get(depot)) is not None and description.source_app is not None
    })
    # A defining entry can tag its own DLC, which ``_effective`` consults.
    _load_missing(appinfo, apps, {
        description.dlc_app
        for depot in depots
        for app in apps.values()
        if (description := app.depots.get(depot)) is not None and description.dlc_app is not None
    })
    installed: dict[int, DepotInfo] = {}
    for depot, dlc in depots.items():
        # Steam lists some DLC depots under the base app and others only
        # under the DLC's own entry; accept either, never neither. The DLC
        # may be named by the manifest or only by the base app's tag. Each
        # description is resolved through its own ``depotfromapp`` first.
        base = info.depots.get(depot)
        named = {
            owner for owner in (dlc, base.dlc_app if base is not None else None)
            if owner is not None
        }
        if any(owner not in apps for owner in named):
            # A named DLC's own entry may hold the restriction; without it
            # the depot's platform is unknown for either selection.
            return PlatformDecision(None, "dlc_metadata_not_cached")
        # Every DLC named by the manifest or the base tag describes it.
        # Steam's manifest records ``dlcappid`` for DLC depots, so a depot the
        # base app describes without a DLC tag is a base-game depot and its
        # entry is authoritative; listed DLC are searched only for a depot the
        # base app does not describe, and then all must be cached. (Requiring
        # every listed DLC for base depots would refuse titles such as
        # Spacewar, which list many unowned, uncached DLC.)
        # A depot the base app lacks may be described by any listed DLC, not
        # only the one the manifest names.
        owners = sorted(named | info.dlc_apps) if base is None else sorted(named)
        if base is None and any(owner not in apps for owner in owners):
            return PlatformDecision(None, "dlc_metadata_not_cached")
        descriptions = [
            found for found in (
                base,
                *(apps[name].depots.get(depot) for name in owners if name in apps),
            ) if found is not None
        ]
        if not descriptions:
            return PlatformDecision(None, "installed_depot_not_in_metadata")
        resolved: list[DepotInfo] = []
        for description in descriptions:
            effective = _effective(depot, description, apps)
            if isinstance(effective, PlatformDecision):
                return effective
            resolved.append(effective)
        merged = _merge(resolved)
        if isinstance(merged, PlatformDecision):
            return merged
        installed[depot] = merged
    # SharedDepots live in other apps' directories; they say nothing about
    # files in this one. With no InstalledDepots recorded, the empty-install
    # rule (and the marker gate behind it) governs this directory.
    shared = shared_depots(manifest, appid) if manifest is not None and depots else {}
    if shared is None:
        return PlatformDecision(None, "installed_depots_unreadable")
    # A shared depot counts as installed only when its named source is cached,
    # defines it, and agrees with the base app's own reference.
    _load_missing(appinfo, apps, set(shared.values()))
    _load_missing(appinfo, apps, _tagged_dlc(apps, [
        (depot_id, app_id) for depot_id, source_app in shared.items()
        for app_id in (appid, source_app)
    ]))
    for depot_id, source_app in shared.items():
        source = apps.get(source_app)
        base_entry = info.depots.get(depot_id)
        if (source is None or depot_id not in source.depots
                or (base_entry is not None and base_entry.source_app not in (None, source_app))):
            return PlatformDecision(None, "source_depot_not_in_metadata")
        # Resolve it like any reference, so conflicting or chained
        # descriptions refuse rather than count as installed.
        reference = DepotInfo(
            oslist=base_entry.oslist if base_entry else None,
            osarch=base_entry.osarch if base_entry else None,
            source_app=source_app,
            dlc_app=base_entry.dlc_app if base_entry else None,
        )
        effective = _effective(depot_id, reference, apps)
        if isinstance(effective, PlatformDecision):
            return effective
        # A shared depot is installed content: classify it with the rest.
        if depot_id in installed:
            merged = _merge([installed[depot_id], effective])
            if isinstance(merged, PlatformDecision):
                return merged
            effective = merged
        installed[depot_id] = effective
    # When the app's own installed depots do not name a platform, the
    # completeness check judges every base reference by the definition it
    # names, and those may tag a DLC whose own entry restricts them. Only
    # then are these records read, so a damaged record the check does not
    # need cannot block a proven native install.
    if not _restrictions(d for k, d in installed.items() if k not in shared):
        _load_missing(appinfo, apps, {
            depot.source_app for depot in info.depots.values() if depot.source_app is not None
        })
        _load_missing(appinfo, apps, _tagged_dlc(apps, [
            (depot_id, app_id) for depot_id, depot in info.depots.items()
            if depot.source_app is not None
            for app_id in (appid, depot.source_app)
        ]))
    decision = decide_platform(
        info, installed, target_has_content=target_has_content, shared=shared, apps=apps,
    )
    # Depot evidence alone is not enough: the app itself must list the
    # platform, for native updates as well as the Windows override.
    # (With no depot recorded, the new-install rule already decided.)
    if installed and decision.platform is not None and (
        info.app_oslist is None or decision.platform not in info.app_oslist
    ):
        return PlatformDecision(None, "app_platform_unknown")
    if decision.platform != "windows":
        return decision
    # A DLC named only by the manifest is as unchecked as one the app lists.
    if any(dlc is not None and dlc not in apps for dlc in depots.values()):
        return PlatformDecision(None, "dlc_metadata_not_cached")
    _load_related(appinfo, apps)
    named = tuple(sorted(
        {dlc for dlc in depots.values() if dlc is not None} | set(shared.values())
    ))
    return _windows_refusal(apps, installed.values()) or PlatformDecision(
        decision.platform, decision.reason, named,
    )


def library_content_platform(
    library: Path, appid: int, *, target: Path | None = None,
    owned_marker: Path | None = None, home: Path | None = None,
) -> PlatformDecision:
    """Platform for ``appid`` in ``library``, shared by preflight, intake and execution.

    Without a client manifest, files in ``target`` can only be reused through
    the broker's inode-bound ownership marker, which records the platform
    that wrote them. That record is the evidence for a partial download whose
    manifests record no depot. Markers survive only unfinished runs, so any
    selection for a directory a marker owns must agree with it: a resumed run
    never switches platforms over the previous run's partial content.
    """

    decision, examined = _library_content_platform(library, appid, target, owned_marker, home)
    if examined is None or decision.platform is None:
        return decision
    # The marker is kept per AppID. While it still vouches for content in
    # another surviving directory, recording this attempt would overwrite the
    # only platform record for that partial download.
    guard = (
        _marker_guards_elsewhere(owned_marker, library, examined, appid) if owned_marker else None
    )
    if guard is not None:
        return guard
    if decision.platform == "windows" and decision.reason != "owned_content_platform_recorded":
        # Depot evidence for an install whose files are gone is a leftover
        # manifest; downloading the whole title would be a new Proton install.
        try:
            if not _holds_installed_files(examined):
                # Staged bytes from a qualified Windows run the marker still
                # records are a resumable download, not a new install.
                staged = has_staged_download(examined, appid)
                recorded = _recorded_platform(owned_marker, examined) if staged else None
                if recorded != "windows":
                    return PlatformDecision(None, "new_install_without_linux_depot")
        except FileNotFoundError:
            return PlatformDecision(None, "new_install_without_linux_depot")
        except (OSError, ValueError):
            return PlatformDecision(None, "installed_depots_unreadable")
    # Without a marker there is no earlier run to reconcile; skip the walk.
    try:
        if owned_marker is None or not owned_marker.exists():
            return decision
    except OSError:
        return PlatformDecision(None, "owned_content_platform_unknown")
    # A marker binds every later run on this directory, fresh or resumed:
    # a newer client manifest does not prove Steam replaced the files. A run
    # that provably wrote nothing (``auth_required``) retires its marker.
    try:
        # With nothing written yet, no depot set could be mixed. A nested
        # SteamCMD manifest counts only when it records depots.
        # A nested manifest counts only when it was evidence: local, with
        # recorded depots, beside installed files.
        nested = local_nested_manifest(examined, appid)
        records = (
            nested is not None and bool(installed_depots(nested, appid))
            and _holds_installed_files(examined)
        )
        if not _holds_content(examined, None if records else appid):
            return decision
    except OSError:
        return PlatformDecision(None, "owned_content_platform_unknown")
    return _agrees_with_marker(decision, owned_marker, examined, library, appid, home)


def _library_content_platform(
    library: Path, appid: int, target: Path | None,
    owned_marker: Path | None, home: Path | None,
) -> tuple[PlatformDecision, Path | None]:
    """``library_content_platform`` plus the directory its evidence describes."""

    manifest = library / "steamapps" / f"appmanifest_{appid}.acf"
    try:
        present = manifest.is_file()
        if present:
            # Execution always reuses the client's directory, so check that.
            name = _manifest_install_dir(manifest)
            if name is None:
                return PlatformDecision(None, "installed_depots_unreadable"), target
            target = library / "steamapps" / "common" / name
        if target is not None:
            # SteamCMD writes its bookkeeping under ``target/steamapps``; a
            # link there would send writes elsewhere and fail adoption.
            bookkeeping = target / "steamapps"
            if os.path.lexists(bookkeeping) and not stat.S_ISDIR(os.lstat(bookkeeping).st_mode):
                return PlatformDecision(None, "installed_depots_unreadable"), target
        nested = target is not None and local_nested_manifest(target, appid) is not None
    except (OSError, KeyValuesError):
        return PlatformDecision(None, "installed_depots_unreadable"), target
    decision = content_platform(
        library=library, appid=appid, manifest=manifest if present else None,
        target=target, home=home,
    )
    if present and nested and target is not None and installed_depots(manifest, appid) == {}:
        # A queued client manifest records nothing, but SteamCMD's nested
        # manifest may record what was already written into the directory.
        nested_path = target / "steamapps" / f"appmanifest_{appid}.acf"
        nested_depots = installed_depots(nested_path, appid)
        if nested_depots is None:
            return PlatformDecision(None, "installed_depots_unreadable"), target
        # Only a SteamCMD manifest written after Steam queued the install can
        # describe this attempt; an older one is a previous run's leftover.
        try:
            newer = nested_path.stat().st_mtime_ns > manifest.stat().st_mtime_ns
        except OSError:
            return PlatformDecision(None, "installed_depots_unreadable"), target
        try:
            beside_files = _holds_installed_files(target)
        except OSError:
            return PlatformDecision(None, "installed_depots_unreadable"), target
        if nested_depots and newer and beside_files:
            nested_decision = content_platform(
                library=library, appid=appid, manifest=None, target=target, home=home,
            )
            return _with_client_sources(
                nested_decision, manifest, library, appid, home,
            ), target
    if target is None:
        return decision, target
    # A client manifest with recorded depots is direct evidence; one without
    # any (a queued install) leaves the files to the marker as well. Decided
    # before any walk of the target, so an unreadable subdirectory cannot
    # block an install the client manifest already qualifies.
    if present and decision.reason != "installed_depots_empty_with_content":
        return decision, target
    # Recorded depots in a nested manifest are direct evidence too, but only
    # beside installed files; otherwise any staging goes through the marker.
    try:
        nested_used = nested and _holds_installed_files(target)
    except OSError:
        return PlatformDecision(None, "installed_depots_unreadable"), target
    if nested_used and decision.platform is not None:
        return decision, target
    # Only these refusals concern files the marker can describe.
    if decision.reason in UNQUALIFIED_REASONS - {
        "installed_depots_empty_with_content", "new_install_without_linux_depot",
    }:
        return decision, target
    try:
        if not _holds_content(target, appid):
            return decision, target
        recorded = _recorded_platform(owned_marker, target)
        # For Windows, leftovers without payload bytes (empty placeholders,
        # links, only SteamCMD's own manifest) are not a download worth
        # resuming: that would be a new Proton install. A native retry may
        # proceed over them.
        if recorded == "windows" and not (
            _holds_installed_files(target) or has_staged_download(target, appid)
        ):
            return PlatformDecision(None, "owned_content_platform_unknown"), target
    except (OSError, ValueError):
        return PlatformDecision(None, "owned_content_platform_unknown"), target
    if recorded is None:
        return PlatformDecision(None, "owned_content_platform_unknown"), target
    # No manifest records a depot for these files, so the new-install rule
    # does not describe them. The broker's own record of the platform that
    # wrote them does, still subject to the current Windows gate.
    recorded_dlc: frozenset[int] = frozenset()
    if recorded == "windows":
        try:
            recorded_dlc = _recorded_dlc_apps(owned_marker)
        except (OSError, ValueError):
            return PlatformDecision(None, "owned_content_platform_unknown"), target
        # Sources the current client manifest names are gated too, even when
        # its InstalledDepots are empty.
        current_sources = shared_depots(manifest, appid) if present else {}
        if current_sources is None:
            return PlatformDecision(None, "installed_depots_unreadable"), target
        recorded_dlc = recorded_dlc | set(current_sources.values())
        refusal = _current_windows_refusal(library, appid, home, recorded_dlc)
        if refusal is not None:
            return refusal, target
    else:
        # A native resume still needs the app to support Linux today.
        refusal = _current_native_refusal(library, appid, home)
        if refusal is not None:
            return refusal, target
    # Carry the recorded DLC forward: the retry rewrites the marker.
    return PlatformDecision(
        recorded, "owned_content_platform_recorded", tuple(sorted(recorded_dlc)),
    ), target


def has_staged_download(target: Path, appid: int) -> bool:
    """Staged bytes for ``appid``; unreadable staging raises ``OSError``.

    Steam and SteamCMD stage per AppID under ``steamapps/downloading/<appid>``
    and ``steamapps/temp/<appid>``; another title's staging is not evidence.
    """
    steamapps = target / "steamapps"
    try:
        # Bytes reached through a linked ``steamapps`` are not local staging.
        if not stat.S_ISDIR(os.lstat(steamapps).st_mode):
            return False
    except FileNotFoundError:
        return False
    staged = False
    for name in ("downloading", "temp"):
        # Every component must be a real directory: a linked one would count
        # bytes from elsewhere as local staging.
        directory = steamapps / name / str(appid)
        try:
            if not all(
                stat.S_ISDIR(os.lstat(path).st_mode) for path in (steamapps / name, directory)
            ):
                continue
        except FileNotFoundError:
            continue
        staged = staged or _holds_bytes(directory)
    return staged


def _agrees_with_marker(
    decision: PlatformDecision, owned_marker: Path | None, target: Path,
    library: Path, appid: int, home: Path | None,
) -> PlatformDecision:
    if decision.platform is None:
        return decision
    try:
        recorded = _recorded_platform(owned_marker, target)
        recorded_dlc = _recorded_dlc_apps(owned_marker) if recorded == "windows" else frozenset()
    except FileNotFoundError:
        return decision
    except (OSError, ValueError):
        return PlatformDecision(None, "owned_content_platform_unknown")
    if recorded is not None and recorded != decision.platform:
        return PlatformDecision(None, "owned_content_platform_unknown")
    if not recorded_dlc - set(decision.dlc_apps):
        return decision
    # The interrupted run's evidence named DLC the current manifest omits;
    # they still gate this Windows write and stay recorded for the next one.
    # Gate the marker's and the manifest's DLC together, so a conflict
    # between their descriptions of one depot is seen.
    refusal = _current_windows_refusal(
        library, appid, home, recorded_dlc | set(decision.dlc_apps),
    )
    if refusal is not None:
        return refusal
    return PlatformDecision(
        decision.platform, decision.reason,
        tuple(sorted(recorded_dlc | set(decision.dlc_apps))),
    )


def _with_client_sources(
    decision: PlatformDecision, manifest: Path, library: Path, appid: int,
    home: Path | None,
) -> PlatformDecision:
    """Gate and record a queued client manifest's ``SharedDepots`` sources."""

    if decision.platform != "windows":
        return decision
    sources = shared_depots(manifest, appid)
    if sources is None:
        return PlatformDecision(None, "installed_depots_unreadable")
    if not sources:
        return decision
    related = frozenset(decision.dlc_apps) | set(sources.values())
    refusal = _current_windows_refusal(library, appid, home, related)
    if refusal is not None:
        return refusal
    return PlatformDecision(decision.platform, decision.reason, tuple(sorted(related)))


def _current_native_refusal(
    library: Path, appid: int, home: Path | None,
) -> PlatformDecision | None:
    try:
        appinfo = locate_appinfo(library, home)
        if appinfo is None:
            return PlatformDecision(None, "app_metadata_unavailable")
        info = read_app_platforms(appinfo, {appid}).get(appid)
    except (AppInfoError, OSError, ValueError, struct.error):
        return PlatformDecision(None, "app_metadata_unreadable")
    if info is None:
        return PlatformDecision(None, "app_metadata_not_cached")
    # Same rule as a new native install.
    decision = decide_platform(info, None)
    return None if decision.platform == "linux" else decision


def _current_windows_refusal(
    library: Path, appid: int, home: Path | None, recorded_dlc: frozenset[int],
) -> PlatformDecision | None:
    try:
        appinfo = locate_appinfo(library, home)
        if appinfo is None:
            return PlatformDecision(None, "app_metadata_unavailable")
        apps = read_app_platforms(appinfo, {appid} | recorded_dlc)
        if appid not in apps:
            return PlatformDecision(None, "app_metadata_not_cached")
        oslist = apps[appid].app_oslist
        if oslist is None or "windows" not in oslist:
            return PlatformDecision(None, "app_platform_unknown")
        if any(dlc not in apps for dlc in recorded_dlc):
            return PlatformDecision(None, "dlc_metadata_not_cached")
        _load_related(appinfo, apps)
    except (AppInfoError, OSError, ValueError, struct.error):
        return PlatformDecision(None, "app_metadata_unreadable")
    return _windows_refusal(apps)


def _load_marker(owned_marker: Path) -> object:
    """Marker JSON; duplicate keys in any casing are damage (``ValueError``)."""

    def strict(pairs: list[tuple[str, object]]) -> dict[str, object]:
        names = [name.lower() for name, _ in pairs]
        if len(set(names)) != len(names) or any(name != name.lower() for name, _ in pairs):
            raise ValueError("ambiguous marker keys")
        return dict(pairs)

    try:
        return json.loads(owned_marker.read_text(encoding="utf-8"), object_pairs_hook=strict)
    except RecursionError:
        raise ValueError("marker nested too deeply") from None


def _recorded_dlc_apps(owned_marker: Path | None) -> frozenset[int]:
    """DLC the marked run's evidence named; markers predating the field name none."""

    if owned_marker is None:
        return frozenset()
    record = _load_marker(owned_marker)
    values = record.get("dlc_apps", []) if isinstance(record, dict) else None
    if not isinstance(values, list):
        raise ValueError("invalid marker DLC list")
    return frozenset(_app_reference(value) for value in values)


def _marker_guards_elsewhere(
    owned_marker: Path, library: Path, target: Path, appid: int,
) -> PlatformDecision | None:
    """Refusal reason while the marker binds a different, populated directory.

    A damaged marker refuses as unknown: the earlier directory it may record
    cannot be ruled out, and recording this attempt would overwrite it.
    """
    try:
        record = _load_marker(owned_marker)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return PlatformDecision(None, "owned_content_platform_unknown")
    if not isinstance(record, dict):
        return PlatformDecision(None, "owned_content_platform_unknown")
    name = record.get("install_dir_name")
    if not isinstance(name, str) or not safe_install_dir_name(name):
        return None  # legacy markers without a name bind only by inode
    other = library / "steamapps" / "common" / name
    try:
        # A damaged identity is not a match for this target: refuse below. A
        # target not yet created is simply not the one the marker binds.
        try:
            binds_target = _recorded_platform(owned_marker, target) is not None
        except FileNotFoundError:
            binds_target = False
        if other == target or binds_target:
            return None
        # A lone SteamCMD manifest there establishes no surviving files.
        if marker_binds(owned_marker, other) and (
            _holds_installed_files(other) or has_staged_download(other, appid)
        ):
            return PlatformDecision(None, "owned_content_elsewhere")
        return None
    except (OSError, ValueError):
        return PlatformDecision(None, "owned_content_platform_unknown")


def marker_binds(owned_marker: Path, target: Path) -> bool:
    """Whether a marker binds ``target`` by inode, as the classifier reads it.

    An unreadable or damaged marker counts as binding: removing it could
    discard the only record of earlier writes.
    """
    try:
        return _recorded_platform(owned_marker, target) is not None
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def _recorded_platform(owned_marker: Path | None, target: Path) -> ContentPlatform | None:
    """The platform an ownership marker records, if it still owns ``target``."""

    if owned_marker is None:
        return None
    record = _load_marker(owned_marker)
    if not isinstance(record, dict) or not all(
        isinstance(record.get(key), int) and not isinstance(record.get(key), bool)
        for key in ("dev", "ino")
    ):
        raise ValueError("malformed marker")  # damage, not "another directory"
    current = os.lstat(target)
    if (current.st_dev, current.st_ino) != (record.get("dev"), record.get("ino")):
        return None
    # Markers predating this field come from brokers that only ever ran
    # SteamCMD natively. A damaged value on an owning marker is unknown.
    platform = record.get("platform", "linux")
    if platform not in ("linux", "windows"):
        raise ValueError("invalid marker platform")
    return platform

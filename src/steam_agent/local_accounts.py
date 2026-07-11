"""Privacy-minimizing discovery of accounts known to the local Steam client.

Steam's ``loginusers.vdf`` is an undocumented local file.  This module treats
it as a bounded, read-only heuristic and deliberately retains only SteamID64
and the ``MostRecent`` marker.  Account names, persona names, login settings,
and the raw payload never cross this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from typing import Final

from steam_agent.local_steam import KeyValuesError, parse_keyvalues


MAX_LOGINUSERS_BYTES: Final = 1024 * 1024
STEAM_ID64_MAX: Final = (1 << 64) - 1


class LocalAccountError(RuntimeError):
    """Base error for the local account discovery boundary."""


class LocalAccountRegistryUnavailable(LocalAccountError):
    """The local Steam account registry cannot be read safely."""


class MalformedLocalAccountRegistry(LocalAccountError):
    """The local Steam account registry has no trustworthy supported shape."""


class NoLocalAccount(LocalAccountError):
    """The registry contains no valid Steam account candidate."""


class AmbiguousLocalAccounts(LocalAccountError):
    """No single local account can be selected without user input."""


@dataclass(frozen=True, slots=True)
class LocalAccountCandidate:
    steam_id64: str = field(repr=False)
    most_recent: bool = False


@dataclass(frozen=True, slots=True)
class LocalAccountDiscovery:
    candidates: tuple[LocalAccountCandidate, ...]
    source_kind: str = "local_steam_login_registry"
    support_level: str = "local_heuristic"


def validate_steam_id64(value: str) -> str:
    """Return a canonical decimal uint64 Steam identifier or raise.

    Valve's Web API specifies ``steamid`` as uint64.  We intentionally avoid
    imposing undocumented universe/account-type rules at this layer.
    """

    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise ValueError("SteamID64 must be an unsigned decimal integer")
    if len(value) > 20:
        raise ValueError("SteamID64 is outside the uint64 range")
    numeric = int(value, 10)
    if numeric <= 0 or numeric > STEAM_ID64_MAX:
        raise ValueError("SteamID64 is outside the uint64 range")
    return str(numeric)


def discover_local_accounts(steam_root: str | Path) -> LocalAccountDiscovery:
    """Read candidates from ``config/loginusers.vdf`` below ``steam_root``."""

    registry = Path(steam_root).expanduser() / "config" / "loginusers.vdf"
    text = _read_regular_file_without_following_symlinks(registry)
    try:
        parsed = parse_keyvalues(text)
    except KeyValuesError as exc:
        raise MalformedLocalAccountRegistry("Steam account registry is malformed") from exc

    users = _case_insensitive_value(parsed, "users")
    if not isinstance(users, dict):
        raise MalformedLocalAccountRegistry(
            "Steam account registry has no users object"
        )

    candidates: list[LocalAccountCandidate] = []
    seen_identifiers: set[str] = set()
    for raw_steam_id, raw_record in users.items():
        try:
            steam_id64 = validate_steam_id64(raw_steam_id)
        except ValueError as exc:
            raise MalformedLocalAccountRegistry(
                "Steam account registry contains an invalid account identifier"
            ) from exc
        if steam_id64 in seen_identifiers:
            raise MalformedLocalAccountRegistry(
                "Steam account registry contains a duplicate account identifier"
            )
        seen_identifiers.add(steam_id64)
        if not isinstance(raw_record, dict):
            raise MalformedLocalAccountRegistry(
                "Steam account registry contains an invalid account record"
            )
        raw_most_recent = _case_insensitive_value(raw_record, "MostRecent")
        if raw_most_recent is None:
            most_recent = False
        elif raw_most_recent in ("0", "1"):
            most_recent = raw_most_recent == "1"
        else:
            raise MalformedLocalAccountRegistry(
                "Steam account registry contains an invalid MostRecent marker"
            )
        candidates.append(LocalAccountCandidate(steam_id64, most_recent))

    candidates.sort(key=lambda candidate: int(candidate.steam_id64))
    return LocalAccountDiscovery(tuple(candidates))


def select_primary_local_account(
    discovery: LocalAccountDiscovery,
) -> LocalAccountCandidate:
    """Select a unique recent candidate, or the sole candidate as a fallback."""

    recent = tuple(
        candidate for candidate in discovery.candidates if candidate.most_recent
    )
    if len(recent) == 1:
        return recent[0]
    if len(discovery.candidates) == 1:
        return discovery.candidates[0]
    if not discovery.candidates:
        raise NoLocalAccount("No local Steam account was found")
    raise AmbiguousLocalAccounts(
        "Multiple local Steam accounts require an explicit selection"
    )


def _read_regular_file_without_following_symlinks(path: Path) -> str:
    try:
        before_open = path.lstat()
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise LocalAccountRegistryUnavailable(
            "Steam account registry was not found"
        ) from exc
    except OSError as exc:
        raise LocalAccountRegistryUnavailable(
            "Steam account registry is inaccessible"
        ) from exc
    if stat.S_ISLNK(before_open.st_mode):
        raise LocalAccountRegistryUnavailable(
            "Steam account registry must not be a symbolic link"
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise LocalAccountRegistryUnavailable(
            "Steam account registry was not found"
        ) from exc
    except OSError as exc:
        raise LocalAccountRegistryUnavailable(
            "Steam account registry is inaccessible"
        ) from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LocalAccountRegistryUnavailable(
                "Steam account registry is not a regular file"
            )
        if (metadata.st_dev, metadata.st_ino) != (
            before_open.st_dev,
            before_open.st_ino,
        ):
            raise LocalAccountRegistryUnavailable(
                "Steam account registry changed while it was opened"
            )
        if metadata.st_size > MAX_LOGINUSERS_BYTES:
            raise MalformedLocalAccountRegistry(
                "Steam account registry exceeds the supported size"
            )
        chunks: list[bytes] = []
        remaining = MAX_LOGINUSERS_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_LOGINUSERS_BYTES:
            raise MalformedLocalAccountRegistry(
                "Steam account registry exceeds the supported size"
            )
        try:
            return payload.decode("utf-8-sig")
        except UnicodeError as exc:
            raise MalformedLocalAccountRegistry(
                "Steam account registry is not valid UTF-8"
            ) from exc
    finally:
        os.close(descriptor)


def _case_insensitive_value(value: dict[str, object], key: str) -> object | None:
    wanted = key.casefold()
    matched = False
    result: object | None = None
    for candidate, item in value.items():
        if candidate.casefold() == wanted:
            if matched:
                raise MalformedLocalAccountRegistry(
                    "Steam account registry contains duplicate fields"
                )
            matched = True
            result = item
    return result


__all__ = [
    "AmbiguousLocalAccounts",
    "LocalAccountCandidate",
    "LocalAccountDiscovery",
    "LocalAccountError",
    "LocalAccountRegistryUnavailable",
    "MalformedLocalAccountRegistry",
    "NoLocalAccount",
    "discover_local_accounts",
    "select_primary_local_account",
    "validate_steam_id64",
]

"""Immutable-source and crash-safe artifact primitives for live evaluations.

This module deliberately knows nothing about grading or App Server protocol
content.  It persists only bounded run identity and provenance fields; reports
and transcripts remain responsible for their existing privacy gates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
from types import MappingProxyType
from typing import Any, Self


MANIFEST_SCHEMA = "steam-agent-eval-run/0.1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z", re.ASCII)
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", re.ASCII)
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}\Z", re.ASCII)
_CONTROL_SET_VERSION = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}/[0-9]+\.[0-9]+\Z", re.ASCII
)
_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_CLEANLINESS_STATES = frozenset({"clean", "dirty", "unknown"})
_TRACKS = frozenset({"legacy", "answer", "discovery"})
_MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_FILES = 16_384
_IGNORED_SOURCE_NAMES = frozenset({".DS_Store", "__pycache__"})


class SnapshotIntegrityError(ValueError):
    """The source tree cannot be snapshotted or no longer matches its seal."""


class ManifestStateError(ValueError):
    """A run manifest or requested state transition is invalid."""


class RunState(StrEnum):
    INITIALIZING = "initializing"
    CONTROLS = "controls"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CONTAMINATED = "contaminated"


class TerminalReason(StrEnum):
    SOURCE_NOT_CLEAN = "source_not_clean"
    CONTROLS_FAILED = "controls_failed"
    PREFLIGHT_FAILED = "preflight_failed"
    RUNNER_ERROR = "runner_error"
    CANCELLED = "cancelled"
    SOURCE_CHANGED = "source_changed"
    SNAPSHOT_INVALID = "snapshot_invalid"
    ARTIFACT_FAILURE = "artifact_failure"


_TERMINAL_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.INTERRUPTED,
        RunState.CONTAMINATED,
    }
)
_TERMINAL_REASONS = {
    RunState.FAILED: frozenset(
        {
            TerminalReason.SOURCE_NOT_CLEAN,
            TerminalReason.CONTROLS_FAILED,
            TerminalReason.PREFLIGHT_FAILED,
            TerminalReason.RUNNER_ERROR,
            TerminalReason.ARTIFACT_FAILURE,
        }
    ),
    RunState.INTERRUPTED: frozenset({TerminalReason.CANCELLED}),
    RunState.CONTAMINATED: frozenset(
        {TerminalReason.SOURCE_CHANGED, TerminalReason.SNAPSHOT_INVALID}
    ),
}
_ALLOWED_TRANSITIONS = {
    RunState.INITIALIZING: frozenset(
        {
            RunState.CONTROLS,
            RunState.FAILED,
            RunState.INTERRUPTED,
            RunState.CONTAMINATED,
        }
    ),
    RunState.CONTROLS: frozenset(
        {
            RunState.RUNNING,
            RunState.FAILED,
            RunState.INTERRUPTED,
            RunState.CONTAMINATED,
        }
    ),
    RunState.RUNNING: frozenset({RunState.RUNNING, *_TERMINAL_STATES}),
}


def _safe_relative_name(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid relative source name")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        raise ValueError("invalid relative source name")
    if any(
        component in {"", ".", ".."}
        or _SAFE_COMPONENT.fullmatch(component) is None
        for component in path.parts
    ):
        raise ValueError("invalid relative source name")
    return value


def _safe_token(value: str, *, label: str = "manifest token") -> str:
    if not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None:
        raise ManifestStateError(f"invalid {label}")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("scenario document is not strict JSON")
        return value
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, dict | Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("scenario document is not strict JSON")
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    raise ValueError("scenario document is not strict JSON")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class FrozenScenario:
    """One parsed scenario tied to the exact original bytes selected for a run."""

    source_name: str
    original_bytes: bytes
    document: Any
    sha256: str

    @classmethod
    def create(
        cls, *, source_name: str, original_bytes: bytes, document: Any
    ) -> Self:
        source_name = _safe_relative_name(source_name)
        if not isinstance(original_bytes, bytes):
            raise TypeError("scenario source must be bytes")
        if len(original_bytes) > _MAX_SOURCE_FILE_BYTES:
            raise ValueError("scenario source exceeded safety limits")
        frozen_document = _freeze_json(document)
        return cls(
            source_name=source_name,
            original_bytes=original_bytes,
            document=frozen_document,
            sha256=_sha256_bytes(original_bytes),
        )

    def mutable_document(self) -> Any:
        """Return an isolated conventional JSON object for existing runner APIs."""

        return _thaw_json(self.document)


@dataclass(frozen=True, slots=True, order=True)
class InventoryEntry:
    relative_name: str
    kind: str
    mode: int
    size: int
    sha256: str | None

    def digest_fields(self) -> tuple[str, str, str, str, str]:
        return (
            self.relative_name,
            self.kind,
            f"{self.mode:04o}",
            str(self.size),
            self.sha256 or "",
        )


def _hash_inventory(entries: Sequence[InventoryEntry]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        for field in entry.digest_fields():
            encoded = field.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _read_regular_file_at(directory_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SnapshotIntegrityError("source snapshot requires regular files")
        if before.st_size > _MAX_SOURCE_FILE_BYTES:
            raise SnapshotIntegrityError("source file exceeded safety limits")
        chunks: list[bytes] = []
        remaining = _MAX_SOURCE_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(descriptor, 1):
            raise SnapshotIntegrityError("source file exceeded safety limits")
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise SnapshotIntegrityError("source changed while being snapshotted")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, content: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("private artifact write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_source_directory(
    source_fd: int,
    destination: Path,
    *,
    file_counter: list[int],
) -> None:
    for name in sorted(os.listdir(source_fd)):
        if name in _IGNORED_SOURCE_NAMES or name.endswith((".pyc", ".pyo")):
            continue
        if _SAFE_COMPONENT.fullmatch(name) is None:
            raise SnapshotIntegrityError("source tree contains an unsafe name")
        source_stat = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        target = destination / name
        if stat.S_ISDIR(source_stat.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            child_fd = os.open(name, flags, dir_fd=source_fd)
            try:
                opened_stat = os.fstat(child_fd)
                if not stat.S_ISDIR(opened_stat.st_mode) or (
                    opened_stat.st_dev,
                    opened_stat.st_ino,
                ) != (source_stat.st_dev, source_stat.st_ino):
                    raise SnapshotIntegrityError("source changed while being snapshotted")
                target.mkdir(mode=0o700)
                _copy_source_directory(
                    child_fd, target, file_counter=file_counter
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(source_stat.st_mode):
            raise SnapshotIntegrityError("source tree contains a nonregular file")
        content, opened_stat = _read_regular_file_at(source_fd, name)
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            source_stat.st_dev,
            source_stat.st_ino,
        ):
            raise SnapshotIntegrityError("source changed while being snapshotted")
        file_counter[0] += 1
        if file_counter[0] > _MAX_SOURCE_FILES:
            raise SnapshotIntegrityError("source tree exceeded safety limits")
        _write_exclusive(target, content, 0o600)


def _mkdir_relative(root: Path, relative_parent: PurePosixPath) -> Path:
    current = root
    for component in relative_parent.parts:
        current /= component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            current_stat = current.lstat()
            if not stat.S_ISDIR(current_stat.st_mode):
                raise SnapshotIntegrityError("snapshot destination is contaminated") from None
    return current


def _write_snapshot_input(root: Path, relative_name: str, content: bytes) -> None:
    relative_name = _safe_relative_name(relative_name)
    relative = PurePosixPath(relative_name)
    parent = _mkdir_relative(root, relative.parent)
    try:
        _write_exclusive(parent / relative.name, content, 0o600)
    except FileExistsError:
        raise SnapshotIntegrityError("snapshot input name is duplicated") from None


def _scan_inventory_unchecked(
    root: Path, *, ignore_generated: bool = False
) -> tuple[InventoryEntry, ...]:
    try:
        root_stat = root.lstat()
    except OSError:
        raise SnapshotIntegrityError("source snapshot is inaccessible") from None
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SnapshotIntegrityError("source snapshot root is not a directory")
    entries = [
        InventoryEntry(
            relative_name=".",
            kind="directory",
            mode=stat.S_IMODE(root_stat.st_mode),
            size=0,
            sha256=None,
        )
    ]
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, flags)

    def visit(directory_fd: int, relative_parent: PurePosixPath) -> None:
        for name in sorted(os.listdir(directory_fd)):
            if ignore_generated and (
                name in _IGNORED_SOURCE_NAMES or name.endswith((".pyc", ".pyo"))
            ):
                continue
            if _SAFE_COMPONENT.fullmatch(name) is None:
                raise SnapshotIntegrityError("snapshot contains an unsafe name")
            relative = relative_parent / name
            item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(item_stat.st_mode):
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if not stat.S_ISDIR(opened.st_mode) or (
                        opened.st_dev,
                        opened.st_ino,
                    ) != (item_stat.st_dev, item_stat.st_ino):
                        raise SnapshotIntegrityError("snapshot changed during verification")
                    entries.append(
                        InventoryEntry(
                            relative_name=relative.as_posix(),
                            kind="directory",
                            mode=stat.S_IMODE(opened.st_mode),
                            size=0,
                            sha256=None,
                        )
                    )
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(item_stat.st_mode):
                content, opened = _read_regular_file_at(directory_fd, name)
                if (opened.st_dev, opened.st_ino) != (
                    item_stat.st_dev,
                    item_stat.st_ino,
                ):
                    raise SnapshotIntegrityError("snapshot changed during verification")
                entries.append(
                    InventoryEntry(
                        relative_name=relative.as_posix(),
                        kind="file",
                        mode=stat.S_IMODE(opened.st_mode),
                        size=len(content),
                        sha256=_sha256_bytes(content),
                    )
                )
            else:
                raise SnapshotIntegrityError("snapshot contains a nonregular file")

    try:
        visit(root_fd, PurePosixPath())
    finally:
        os.close(root_fd)
    if sum(entry.kind == "file" for entry in entries) > _MAX_SOURCE_FILES:
        raise SnapshotIntegrityError("source tree exceeded safety limits")
    return tuple(sorted(entries))


def _scan_inventory(
    root: Path, *, ignore_generated: bool = False
) -> tuple[InventoryEntry, ...]:
    """Scan without allowing host-path-bearing filesystem errors to escape."""

    try:
        return _scan_inventory_unchecked(root, ignore_generated=ignore_generated)
    except SnapshotIntegrityError:
        raise
    except OSError:
        raise SnapshotIntegrityError("source snapshot is inaccessible") from None


def inventory_digest(root: Path) -> str:
    """Return a deterministic digest over names, kinds, modes, and file bytes."""

    return _hash_inventory(_scan_inventory(root))


def _normalized_content_inventory(
    root: Path,
) -> tuple[tuple[str, str, int, str | None], ...]:
    """Return a mode-independent source inventory with generated files omitted."""

    try:
        entries = _scan_inventory(root, ignore_generated=True)
    except SnapshotIntegrityError:
        raise
    except OSError:
        raise SnapshotIntegrityError("source changed while being snapshotted") from None
    return tuple(
        (entry.relative_name, entry.kind, entry.size, entry.sha256)
        for entry in entries
    )


def _seal_tree(root: Path) -> None:
    directories: list[Path] = []
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.append(current_path)
        for name in [*names, *files]:
            item = current_path / name
            item_stat = item.lstat()
            if stat.S_ISLNK(item_stat.st_mode):
                raise SnapshotIntegrityError("snapshot contains a symbolic link")
            if stat.S_ISREG(item_stat.st_mode):
                item.chmod(0o444)
            elif not stat.S_ISDIR(item_stat.st_mode):
                raise SnapshotIntegrityError("snapshot contains a nonregular file")
    for directory in reversed(directories):
        directory.chmod(0o555)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    root: Path
    digest: str
    inventory: tuple[InventoryEntry, ...]
    _root_device: int
    _root_inode: int

    @classmethod
    def create(
        cls,
        destination: Path,
        *,
        source_root: Path,
        harness_root: Path,
        scenarios: Sequence[FrozenScenario],
        schemas: Mapping[str, bytes],
    ) -> Self:
        """Copy and seal one deterministic execution source cohort.

        ``source_root`` becomes ``snapshot/src`` and ``harness_root`` becomes
        ``snapshot/evals/runner``. Scenario names are relative to their normal
        scenario root, and schema names are relative to the schema root; no
        host source path is retained in the inventory.
        """

        destination = Path(destination)
        try:
            source_stat = source_root.lstat()
            harness_stat = harness_root.lstat()
            parent_stat = destination.parent.lstat()
        except OSError:
            raise SnapshotIntegrityError("source snapshot input is inaccessible") from None
        if (
            not stat.S_ISDIR(source_stat.st_mode)
            or not stat.S_ISDIR(harness_stat.st_mode)
            or not stat.S_ISDIR(parent_stat.st_mode)
        ):
            raise SnapshotIntegrityError("source snapshot requires real directories")
        try:
            destination.mkdir(mode=0o700)
        except OSError:
            raise SnapshotIntegrityError("source snapshot destination is unavailable") from None

        try:
            file_counter = [0]
            source_destination = destination / "src"
            source_destination.mkdir(mode=0o700)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            source_before = _normalized_content_inventory(source_root)
            source_fd = os.open(source_root, flags)
            try:
                _copy_source_directory(
                    source_fd, source_destination, file_counter=file_counter
                )
            finally:
                os.close(source_fd)
            if not (
                source_before
                == _normalized_content_inventory(source_root)
                == _normalized_content_inventory(source_destination)
            ):
                raise SnapshotIntegrityError("source changed while being snapshotted")

            harness_destination = destination / "evals" / "runner"
            harness_destination.mkdir(mode=0o700, parents=True)
            harness_before = _normalized_content_inventory(harness_root)
            harness_fd = os.open(harness_root, flags)
            try:
                _copy_source_directory(
                    harness_fd, harness_destination, file_counter=file_counter
                )
            finally:
                os.close(harness_fd)
            if not (
                harness_before
                == _normalized_content_inventory(harness_root)
                == _normalized_content_inventory(harness_destination)
            ):
                raise SnapshotIntegrityError("source changed while being snapshotted")

            for scenario in scenarios:
                _write_snapshot_input(
                    destination,
                    f"evals/scenarios/{scenario.source_name}",
                    scenario.original_bytes,
                )
            for name, content in sorted(schemas.items()):
                if not isinstance(content, bytes):
                    raise TypeError("schema source must be bytes")
                if len(content) > _MAX_SOURCE_FILE_BYTES:
                    raise SnapshotIntegrityError("schema source exceeded safety limits")
                _write_snapshot_input(destination, f"evals/schema/{name}", content)

            _seal_tree(destination)
            inventory = _scan_inventory(destination)
            digest = _hash_inventory(inventory)
            sealed_stat = destination.lstat()
            snapshot = cls(
                root=destination,
                digest=digest,
                inventory=inventory,
                _root_device=sealed_stat.st_dev,
                _root_inode=sealed_stat.st_ino,
            )
            snapshot.verify()
            return snapshot
        except SnapshotIntegrityError:
            _remove_snapshot(destination)
            raise
        except OSError:
            _remove_snapshot(destination)
            raise SnapshotIntegrityError("source changed while being snapshotted") from None
        except BaseException:
            _remove_snapshot(destination)
            raise

    def verify(self) -> str:
        try:
            root_stat = self.root.lstat()
        except OSError:
            raise SnapshotIntegrityError("source snapshot is inaccessible") from None
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or (root_stat.st_dev, root_stat.st_ino)
            != (self._root_device, self._root_inode)
        ):
            raise SnapshotIntegrityError("source snapshot root changed")
        inventory = _scan_inventory(self.root)
        digest = _hash_inventory(inventory)
        if inventory != self.inventory or digest != self.digest:
            raise SnapshotIntegrityError("source snapshot verification failed")
        return digest

    def cleanup(self) -> None:
        """Remove exactly this snapshot without following substituted roots."""

        try:
            root_stat = self.root.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or (root_stat.st_dev, root_stat.st_ino)
            != (self._root_device, self._root_inode)
        ):
            raise SnapshotIntegrityError("source snapshot root changed")
        _remove_snapshot(self.root)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.cleanup()


def _remove_snapshot(root: Path) -> None:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SnapshotIntegrityError("source snapshot root is not a directory")
    for current, names, _files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        current_path.chmod(0o700)
        for name in list(names):
            item = current_path / name
            if stat.S_ISLNK(item.lstat().st_mode):
                item.unlink()
                names.remove(name)
    shutil.rmtree(root)


def _open_private_parent(path: Path) -> tuple[int, str]:
    if not path.name or path.name in {".", ".."}:
        raise ValueError("invalid private artifact path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.parent, flags)
    parent_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(parent_stat.st_mode):
        os.close(descriptor)
        raise ValueError("private artifact parent is not a directory")
    return descriptor, path.name


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("private artifact write failed")
        view = view[written:]


def atomic_publish_private_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Atomically publish a private regular file without replacing any target."""

    if not isinstance(content, bytes):
        raise TypeError("private artifact content must be bytes")
    if mode != 0o600:
        raise ValueError("private artifact mode must be 0600")
    parent_fd, final_name = _open_private_parent(Path(path))
    temporary_name = f".{final_name}.tmp-{secrets.token_hex(12)}"
    temporary_fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(temporary_name, flags, mode, dir_fd=parent_fd)
        os.fchmod(temporary_fd, mode)
        _write_all(temporary_fd, content)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        os.link(
            temporary_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def atomic_publish_private_text(path: Path, content: str) -> None:
    if not isinstance(content, str):
        raise TypeError("private artifact content must be text")
    atomic_publish_private_bytes(path, content.encode("utf-8"))


def _strict_json_bytes(value: Any) -> bytes:
    rendered = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (rendered + "\n").encode("utf-8")


def atomic_publish_private_json(path: Path, value: Any) -> None:
    atomic_publish_private_bytes(path, _strict_json_bytes(value))


def _canonical_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ManifestStateError("manifest time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ManifestStateError("invalid manifest time") from None
    if parsed.tzinfo is None:
        raise ManifestStateError("invalid manifest time")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True, order=True)
class RequestedRoute:
    model: str | None
    reasoning_effort: str | None

    def __post_init__(self) -> None:
        if self.model is not None:
            _safe_token(self.model, label="model route")
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in _REASONING_EFFORTS
        ):
            raise ManifestStateError("invalid reasoning effort")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True, slots=True)
class RunManifest:
    run_id: str
    state: RunState
    revision: int
    commit: str | None
    source_digest: str
    cleanliness: str
    track: str
    control_set_version: str
    controls_passed: bool | None
    terminal_reason: TerminalReason | None
    scenario_ids: tuple[str, ...]
    completed_scenario_ids: tuple[str, ...]
    fixture_hashes: tuple[tuple[str, str], ...]
    requested_routes: tuple[RequestedRoute, ...]
    tool_versions: tuple[tuple[str, str], ...]
    started_at: str
    updated_at: str
    finished_at: str | None
    schema: str = MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        _safe_token(self.run_id, label="run ID")
        if self.schema != MANIFEST_SCHEMA:
            raise ManifestStateError("invalid manifest schema")
        if not isinstance(self.state, RunState):
            raise ManifestStateError("invalid run state")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise ManifestStateError("invalid manifest revision")
        if self.state is RunState.INITIALIZING and self.revision != 0:
            raise ManifestStateError("invalid manifest revision")
        if self.state is not RunState.INITIALIZING and self.revision == 0:
            raise ManifestStateError("invalid manifest revision")
        if self.commit is not None and _COMMIT.fullmatch(self.commit) is None:
            raise ManifestStateError("invalid source commit")
        if _SHA256.fullmatch(self.source_digest) is None:
            raise ManifestStateError("invalid source digest")
        if self.cleanliness not in _CLEANLINESS_STATES:
            raise ManifestStateError("invalid source cleanliness")
        if not isinstance(self.track, str) or self.track not in _TRACKS:
            raise ManifestStateError("invalid evaluation track")
        if (
            not isinstance(self.control_set_version, str)
            or _CONTROL_SET_VERSION.fullmatch(self.control_set_version) is None
        ):
            raise ManifestStateError("invalid control set version")
        if not self.scenario_ids or len(set(self.scenario_ids)) != len(
            self.scenario_ids
        ):
            raise ManifestStateError("manifest requires unique scenarios")
        for scenario_id in self.scenario_ids:
            _safe_token(scenario_id, label="scenario ID")
        if self.completed_scenario_ids != self.scenario_ids[
            : len(self.completed_scenario_ids)
        ]:
            raise ManifestStateError("completed scenarios are not an ordered prefix")
        if self.state in {RunState.INITIALIZING, RunState.CONTROLS}:
            if self.controls_passed is not None:
                raise ManifestStateError("controls are not complete")
        elif self.state in {RunState.RUNNING, RunState.COMPLETED}:
            if self.controls_passed is not True:
                raise ManifestStateError("live execution requires passing controls")
        elif self.controls_passed is not None and not isinstance(
            self.controls_passed, bool
        ):
            raise ManifestStateError("invalid controls result")
        if self.completed_scenario_ids and self.controls_passed is not True:
            raise ManifestStateError("completed scenarios require passing controls")
        if (
            self.state is RunState.COMPLETED
            and self.completed_scenario_ids != self.scenario_ids
        ):
            raise ManifestStateError("completed run has unaccounted scenarios")
        allowed_reasons = _TERMINAL_REASONS.get(self.state)
        if allowed_reasons is None:
            if self.terminal_reason is not None:
                raise ManifestStateError("nonfailure manifest has a terminal reason")
        elif (
            not isinstance(self.terminal_reason, TerminalReason)
            or self.terminal_reason not in allowed_reasons
        ):
            raise ManifestStateError("terminal manifest lacks a valid reason")
        expected_fixture_ids = tuple(sorted(self.scenario_ids))
        if tuple(scenario for scenario, _digest in self.fixture_hashes) != (
            expected_fixture_ids
        ):
            raise ManifestStateError("fixture hashes do not match scenarios")
        if any(_SHA256.fullmatch(digest) is None for _, digest in self.fixture_hashes):
            raise ManifestStateError("invalid fixture hash")
        if not self.requested_routes or not all(
            isinstance(route, RequestedRoute) for route in self.requested_routes
        ):
            raise ManifestStateError("manifest requires requested routes")
        if tuple(sorted(self.tool_versions)) != self.tool_versions:
            raise ManifestStateError("tool versions are not deterministic")
        for name, version in self.tool_versions:
            _safe_token(name, label="tool name")
            if _SAFE_VERSION.fullmatch(version) is None:
                raise ManifestStateError("invalid tool version")
        started = _parse_time(self.started_at)
        updated = _parse_time(self.updated_at)
        if updated < started:
            raise ManifestStateError("manifest time moved backwards")
        if self.state in _TERMINAL_STATES:
            if self.finished_at != self.updated_at:
                raise ManifestStateError("terminal manifest lacks a finish time")
        elif self.finished_at is not None:
            raise ManifestStateError("nonterminal manifest has a finish time")

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        commit: str | None,
        source_digest: str,
        cleanliness: str,
        track: str,
        control_set_version: str,
        scenarios: Sequence[FrozenScenario],
        requested_routes: Sequence[RequestedRoute],
        tool_versions: Mapping[str, str],
        started_at: datetime,
    ) -> Self:
        _safe_token(run_id, label="run ID")
        scenario_ids: list[str] = []
        fixture_hashes: list[tuple[str, str]] = []
        for scenario in scenarios:
            document = scenario.document
            scenario_id = document.get("id") if isinstance(document, Mapping) else None
            scenario_ids.append(_safe_token(scenario_id, label="scenario ID"))
            fixture_hashes.append((scenario_ids[-1], scenario.sha256))
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ManifestStateError("manifest contains duplicate scenarios")
        normalized_tools: list[tuple[str, str]] = []
        for name, version in tool_versions.items():
            _safe_token(name, label="tool name")
            if not isinstance(version, str) or _SAFE_VERSION.fullmatch(version) is None:
                raise ManifestStateError("invalid tool version")
            normalized_tools.append((name, version))
        timestamp = _canonical_time(started_at)
        return cls(
            run_id=run_id,
            state=RunState.INITIALIZING,
            revision=0,
            commit=commit,
            source_digest=source_digest,
            cleanliness=cleanliness,
            track=track,
            control_set_version=control_set_version,
            controls_passed=None,
            terminal_reason=None,
            scenario_ids=tuple(scenario_ids),
            completed_scenario_ids=(),
            fixture_hashes=tuple(sorted(fixture_hashes)),
            requested_routes=tuple(requested_routes),
            tool_versions=tuple(sorted(normalized_tools)),
            started_at=timestamp,
            updated_at=timestamp,
            finished_at=None,
        )

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def transition(
        self,
        state: RunState,
        *,
        at: datetime,
        controls_passed: bool | None = None,
        completed_scenario_ids: Sequence[str] | None = None,
        terminal_reason: TerminalReason | str | None = None,
    ) -> Self:
        try:
            state = RunState(state)
        except ValueError:
            raise ManifestStateError("invalid run state") from None
        if state not in _ALLOWED_TRANSITIONS.get(self.state, frozenset()):
            raise ManifestStateError("invalid run state transition")
        if terminal_reason is not None:
            try:
                terminal_reason = TerminalReason(terminal_reason)
            except (TypeError, ValueError):
                raise ManifestStateError("invalid terminal reason") from None
        timestamp = _canonical_time(at)
        if _parse_time(timestamp) < _parse_time(self.updated_at):
            raise ManifestStateError("manifest time moved backwards")
        next_controls_passed = self.controls_passed
        if self.state is RunState.CONTROLS:
            if controls_passed is not None and not isinstance(controls_passed, bool):
                raise ManifestStateError("invalid controls result")
            next_controls_passed = controls_passed
            if state is RunState.RUNNING and next_controls_passed is not True:
                raise ManifestStateError("live execution requires passing controls")
        elif controls_passed is not None:
            raise ManifestStateError("controls result changed outside controls phase")

        next_completed = self.completed_scenario_ids
        if completed_scenario_ids is not None:
            next_completed = tuple(completed_scenario_ids)
            if self.state is not RunState.RUNNING:
                raise ManifestStateError("scenario completion changed before execution")
            if next_completed[: len(self.completed_scenario_ids)] != (
                self.completed_scenario_ids
            ):
                raise ManifestStateError("scenario completion is not append-only")
        if state is self.state is RunState.RUNNING and len(next_completed) <= len(
            self.completed_scenario_ids
        ):
            raise ManifestStateError("running checkpoint made no progress")
        return replace(
            self,
            state=state,
            revision=self.revision + 1,
            controls_passed=next_controls_passed,
            terminal_reason=terminal_reason,
            completed_scenario_ids=next_completed,
            updated_at=timestamp,
            finished_at=timestamp if state in _TERMINAL_STATES else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "state": self.state.value,
            "revision": self.revision,
            "track": self.track,
            "control_set_version": self.control_set_version,
            "controls_passed": self.controls_passed,
            "terminal_reason": (
                self.terminal_reason.value
                if self.terminal_reason is not None
                else None
            ),
            "source": {
                "commit": self.commit,
                "digest": self.source_digest,
                "cleanliness": self.cleanliness,
                "snapshot": "sealed",
            },
            "scenario_ids": list(self.scenario_ids),
            "completed_scenario_ids": list(self.completed_scenario_ids),
            "fixture_hashes": [
                {"scenario": scenario, "sha256": digest}
                for scenario, digest in self.fixture_hashes
            ],
            "requested_routes": [route.to_dict() for route in self.requested_routes],
            "tool_versions": [
                {"name": name, "version": version}
                for name, version in self.tool_versions
            ],
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }

    def persist(self, path: Path) -> None:
        """Atomically create or advance the canonical private manifest file."""

        path = Path(path)
        try:
            existing_stat = path.lstat()
        except FileNotFoundError:
            if self.revision != 0:
                raise ManifestStateError("manifest history is missing") from None
            atomic_publish_private_json(path, self.to_dict())
            return
        if not stat.S_ISREG(existing_stat.st_mode):
            raise ManifestStateError("manifest target is not a regular file")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ManifestStateError("existing manifest is invalid") from None
        if not isinstance(existing, dict):
            raise ManifestStateError("existing manifest is invalid")
        expected = self.to_dict()
        dynamic_fields = {
            "state",
            "revision",
            "controls_passed",
            "terminal_reason",
            "completed_scenario_ids",
            "updated_at",
            "finished_at",
        }
        existing_static = {
            key: value for key, value in existing.items() if key not in dynamic_fields
        }
        expected_static = {
            key: value for key, value in expected.items() if key not in dynamic_fields
        }
        try:
            existing_state = RunState(existing.get("state"))
        except (TypeError, ValueError):
            raise ManifestStateError("existing manifest is invalid") from None
        existing_controls_passed = existing.get("controls_passed")
        existing_terminal_reason = existing.get("terminal_reason")
        existing_completed = existing.get("completed_scenario_ids")
        dynamic_valid = (
            (
                existing_controls_passed is None
                or isinstance(existing_controls_passed, bool)
            )
            and isinstance(existing_completed, list)
            and existing_terminal_reason is None
            and all(isinstance(item, str) for item in existing_completed)
            and self.completed_scenario_ids[: len(existing_completed)]
            == tuple(existing_completed)
            and (
                existing_controls_passed is None
                or self.controls_passed is existing_controls_passed
            )
        )
        if (
            existing_static != expected_static
            or existing.get("revision") != self.revision - 1
            or self.state
            not in _ALLOWED_TRANSITIONS.get(existing_state, frozenset())
            or existing.get("finished_at") is not None
            or not dynamic_valid
            or _parse_time(existing.get("updated_at"))
            > _parse_time(self.updated_at)
        ):
            raise ManifestStateError("manifest history does not match")
        _atomic_replace_private_bytes(path, _strict_json_bytes(self.to_dict()))


def _atomic_replace_private_bytes(path: Path, content: bytes) -> None:
    parent_fd, final_name = _open_private_parent(path)
    temporary_name = f".{final_name}.tmp-{secrets.token_hex(12)}"
    temporary_fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        os.fchmod(temporary_fd, 0o600)
        _write_all(temporary_fd, content)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        current_stat = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(current_stat.st_mode):
            raise ManifestStateError("manifest target is not a regular file")
        os.replace(
            temporary_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)

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
MATRIX_MANIFEST_SCHEMA = "steam-agent-eval-matrix-run/0.1"
MATRIX_PREFLIGHT_SCHEMA = "steam-agent-eval-matrix-preflight/0.1"
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
_TRACKS = frozenset({"legacy", "answer", "discovery", "skill"})
_MATRIX_HARD_LAYERS = frozenset(
    {"agent_turns", "tool_policy", "oracle", "claims", "privacy"}
)
_MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_FILES = 16_384
MATRIX_MANIFEST_MAX_BYTES = 64 * 1024 * 1024
MAX_QUALITATIVE_CRITERIA = 1024
MAX_SCENARIO_TURNS = 64
PROSE_CLAIMS_ALIGNMENT_CRITERION_ID = "prose-claims-sidecar-alignment"
PROSE_CLAIMS_ALIGNMENT_SOURCE = "generated.prose_claims_sidecar_alignment"
PROSE_CLAIMS_ALIGNMENT_REQUIREMENT = (
    "Review every answer turn against its same-turn captured claims sidecar. "
    "Pass only when every factual assertion in the prose is represented by a "
    "matching sidecar claim; fail if any factual prose assertion is missing, "
    "materially broader, unsupported, or contradictory."
)
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


class MatrixState(StrEnum):
    """Lifecycle of a resumable matrix plan, separate from child cohorts."""

    OPEN = "open"
    COMPLETED = "completed"


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
        component in {"", ".", ".."} or _SAFE_COMPONENT.fullmatch(component) is None
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
    def create(cls, *, source_name: str, original_bytes: bytes, document: Any) -> Self:
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
        if any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        ):
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
                    raise SnapshotIntegrityError(
                        "source changed while being snapshotted"
                    )
                target.mkdir(mode=0o700)
                _copy_source_directory(child_fd, target, file_counter=file_counter)
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
                raise SnapshotIntegrityError(
                    "snapshot destination is contaminated"
                ) from None
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
                        raise SnapshotIntegrityError(
                            "snapshot changed during verification"
                        )
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
        (entry.relative_name, entry.kind, entry.size, entry.sha256) for entry in entries
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
        skill_root: Path | None = None,
        scenarios: Sequence[FrozenScenario],
        schemas: Mapping[str, bytes],
    ) -> Self:
        """Copy and seal one deterministic execution source cohort.

        ``source_root`` becomes ``snapshot/src``, ``harness_root`` becomes
        ``snapshot/evals/runner``, and an optional repo skill becomes
        ``snapshot/skill/steam-agent``. Scenario names are relative to their
        normal scenario root, and schema names are relative to the schema root;
        no host source path is retained in the inventory.
        """

        destination = Path(destination)
        try:
            source_stat = source_root.lstat()
            harness_stat = harness_root.lstat()
            parent_stat = destination.parent.lstat()
            skill_stat = skill_root.lstat() if skill_root is not None else None
        except OSError:
            raise SnapshotIntegrityError(
                "source snapshot input is inaccessible"
            ) from None
        if (
            not stat.S_ISDIR(source_stat.st_mode)
            or not stat.S_ISDIR(harness_stat.st_mode)
            or not stat.S_ISDIR(parent_stat.st_mode)
            or (skill_stat is not None and not stat.S_ISDIR(skill_stat.st_mode))
        ):
            raise SnapshotIntegrityError("source snapshot requires real directories")
        try:
            destination.mkdir(mode=0o700)
        except OSError:
            raise SnapshotIntegrityError(
                "source snapshot destination is unavailable"
            ) from None

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

            if skill_root is not None:
                skill_destination = destination / "skill" / "steam-agent"
                skill_destination.mkdir(mode=0o700, parents=True)
                skill_before = _normalized_content_inventory(skill_root)
                skill_fd = os.open(skill_root, flags)
                try:
                    _copy_source_directory(
                        skill_fd, skill_destination, file_counter=file_counter
                    )
                finally:
                    os.close(skill_fd)
                if not (
                    skill_before
                    == _normalized_content_inventory(skill_root)
                    == _normalized_content_inventory(skill_destination)
                ):
                    raise SnapshotIntegrityError(
                        "source changed while being snapshotted"
                    )

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
            raise SnapshotIntegrityError(
                "source changed while being snapshotted"
            ) from None
        except BaseException:
            _remove_snapshot(destination)
            raise

    def verify(self) -> str:
        try:
            root_stat = self.root.lstat()
        except OSError:
            raise SnapshotIntegrityError("source snapshot is inaccessible") from None
        if not stat.S_ISDIR(root_stat.st_mode) or (
            root_stat.st_dev,
            root_stat.st_ino,
        ) != (self._root_device, self._root_inode):
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
        if not stat.S_ISDIR(root_stat.st_mode) or (
            root_stat.st_dev,
            root_stat.st_ino,
        ) != (self._root_device, self._root_inode):
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


def atomic_publish_private_bytes(
    path: Path, content: bytes, *, mode: int = 0o600
) -> None:
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
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
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
        if (
            self.completed_scenario_ids
            != self.scenario_ids[: len(self.completed_scenario_ids)]
        ):
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
                self.terminal_reason.value if self.terminal_reason is not None else None
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

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        """Strictly load a manifest without accepting unknown provenance fields."""

        expected = {
            "schema",
            "run_id",
            "state",
            "revision",
            "track",
            "control_set_version",
            "controls_passed",
            "terminal_reason",
            "source",
            "scenario_ids",
            "completed_scenario_ids",
            "fixture_hashes",
            "requested_routes",
            "tool_versions",
            "started_at",
            "updated_at",
            "finished_at",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ManifestStateError("invalid manifest")
        source = value["source"]
        if not isinstance(source, dict) or set(source) != {
            "commit",
            "digest",
            "cleanliness",
            "snapshot",
        }:
            raise ManifestStateError("invalid manifest source")
        if source["snapshot"] != "sealed":
            raise ManifestStateError("invalid manifest snapshot")
        scenarios = value["scenario_ids"]
        completed = value["completed_scenario_ids"]
        fixture_hashes = value["fixture_hashes"]
        routes = value["requested_routes"]
        tools = value["tool_versions"]
        if not all(
            isinstance(items, list)
            for items in (scenarios, completed, fixture_hashes, routes, tools)
        ):
            raise ManifestStateError("invalid manifest collections")
        if not all(isinstance(item, str) for item in (*scenarios, *completed)):
            raise ManifestStateError("invalid manifest scenarios")
        normalized_fixtures: list[tuple[str, str]] = []
        for item in fixture_hashes:
            if not isinstance(item, dict) or set(item) != {"scenario", "sha256"}:
                raise ManifestStateError("invalid fixture hash")
            normalized_fixtures.append((item["scenario"], item["sha256"]))
        normalized_tools: list[tuple[str, str]] = []
        for item in tools:
            if not isinstance(item, dict) or set(item) != {"name", "version"}:
                raise ManifestStateError("invalid tool version")
            normalized_tools.append((item["name"], item["version"]))
        normalized_routes: list[RequestedRoute] = []
        for item in routes:
            if not isinstance(item, dict) or set(item) != {
                "model",
                "reasoning_effort",
            }:
                raise ManifestStateError("invalid requested route")
            normalized_routes.append(
                RequestedRoute(item["model"], item["reasoning_effort"])
            )
        try:
            state = RunState(value["state"])
            reason = (
                TerminalReason(value["terminal_reason"])
                if value["terminal_reason"] is not None
                else None
            )
        except (TypeError, ValueError):
            raise ManifestStateError("invalid manifest lifecycle") from None
        return cls(
            schema=value["schema"],
            run_id=value["run_id"],
            state=state,
            revision=value["revision"],
            commit=source["commit"],
            source_digest=source["digest"],
            cleanliness=source["cleanliness"],
            track=value["track"],
            control_set_version=value["control_set_version"],
            controls_passed=value["controls_passed"],
            terminal_reason=reason,
            scenario_ids=tuple(scenarios),
            completed_scenario_ids=tuple(completed),
            fixture_hashes=tuple(
                (scenario, digest) for scenario, digest in normalized_fixtures
            ),
            requested_routes=tuple(normalized_routes),
            tool_versions=tuple(normalized_tools),
            started_at=value["started_at"],
            updated_at=value["updated_at"],
            finished_at=value["finished_at"],
        )

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
            or self.state not in _ALLOWED_TRANSITIONS.get(existing_state, frozenset())
            or existing.get("finished_at") is not None
            or not dynamic_valid
            or _parse_time(existing.get("updated_at")) > _parse_time(self.updated_at)
        ):
            raise ManifestStateError("manifest history does not match")
        _atomic_replace_private_bytes(path, _strict_json_bytes(self.to_dict()))


@dataclass(frozen=True, slots=True, order=True)
class MatrixJudgeConfiguration:
    """Predeclared identity and calibrated settings for one blinded judge."""

    identifier: str
    kind: str
    model: str
    reasoning_effort: str
    settings_identity: str
    settings_sha256: str

    def __post_init__(self) -> None:
        _safe_token(self.identifier, label="matrix judge identifier")
        if self.kind not in {"human", "model"}:
            raise ManifestStateError("invalid matrix judge kind")
        _safe_token(self.model, label="matrix judge model")
        if self.reasoning_effort not in _REASONING_EFFORTS:
            raise ManifestStateError("invalid matrix judge reasoning effort")
        _safe_token(self.settings_identity, label="matrix judge settings identity")
        if _SHA256.fullmatch(self.settings_sha256) is None:
            raise ManifestStateError("invalid matrix judge settings digest")

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "identifier",
            "kind",
            "model",
            "reasoning_effort",
            "settings_identity",
            "settings_sha256",
        }:
            raise ManifestStateError("invalid matrix judge configuration")
        try:
            return cls(**value)
        except (TypeError, ManifestStateError):
            raise ManifestStateError("invalid matrix judge configuration") from None

    def to_dict(self) -> dict[str, str]:
        return {
            "identifier": self.identifier,
            "kind": self.kind,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "settings_identity": self.settings_identity,
            "settings_sha256": self.settings_sha256,
        }


CALIBRATED_JUDGE_SETTINGS_IDENTITY = "matrix-judge-settings-0.1"
CALIBRATED_JUDGE_SETTINGS_SHA256 = (
    "6cac1d14d272fb781f743ff687442db6c34278fe1ca91a2f9fe80b1a7e17d2a7"
)
CALIBRATED_JUDGE_CONFIGURATIONS = tuple(
    MatrixJudgeConfiguration(
        identifier=f"judge-{index}",
        kind="model",
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        settings_identity=CALIBRATED_JUDGE_SETTINGS_IDENTITY,
        settings_sha256=CALIBRATED_JUDGE_SETTINGS_SHA256,
    )
    for index in range(1, 4)
)
CALIBRATED_ADJUDICATION_METHOD = "agreement"
CALIBRATED_ADJUDICATOR = "configured-agreement-0.1"


@dataclass(frozen=True, slots=True)
class MatrixCampaign:
    """Normalized, manifest-bound campaign policy and source-screen lineage."""

    campaign_kind: str
    selection_version: str
    selection_mode: str
    acceptance_version: str
    hard_layers: tuple[str, ...]
    required_tracks: tuple[str, ...]
    replicates: int
    qualitative_rule: str
    judge_version: str
    judgment_schema: str
    adjudication_schema: str
    prompt_version: str
    parser_version: str
    prompt_sha256: str
    parser_sha256: str
    judges: tuple[MatrixJudgeConfiguration, ...]
    adjudication_method: str
    adjudicator: str
    source_screen_manifest_sha256: str | None = None
    source_screen_matrix_id: str | None = None
    source_screen_acceptance_sha256: str | None = None
    source_screen_qualitative_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.campaign_kind not in {"screen", "qualification", "benchmark"}:
            raise ManifestStateError("invalid matrix campaign kind")
        if (
            self.selection_version != "fixed-ordered-scenarios/0.1"
            or self.selection_mode != "fixed_ordered"
            or self.judge_version != "blinded-qualitative/0.1"
            or self.judgment_schema != "steam-agent-eval-judgment/0.1"
            or self.adjudication_schema != "steam-agent-eval-adjudication/0.1"
        ):
            raise ManifestStateError("invalid matrix campaign policy version")
        if (
            len(set(self.hard_layers)) != len(self.hard_layers)
            or set(self.hard_layers) != _MATRIX_HARD_LAYERS
        ):
            raise ManifestStateError("matrix hard layers are incomplete")
        if (
            not self.required_tracks
            or len(set(self.required_tracks)) != len(self.required_tracks)
            or any(track not in _TRACKS for track in self.required_tracks)
        ):
            raise ManifestStateError("invalid matrix required tracks")
        if (
            not isinstance(self.replicates, int)
            or isinstance(self.replicates, bool)
            or not 1 <= self.replicates <= 100
        ):
            raise ManifestStateError("invalid matrix policy replicates")
        for value in (self.prompt_version, self.parser_version):
            if _CONTROL_SET_VERSION.fullmatch(value) is None:
                raise ManifestStateError("invalid matrix judge policy version")
        for value in (self.prompt_sha256, self.parser_sha256):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ManifestStateError("invalid matrix judge policy digest")
        if (
            self.judges != CALIBRATED_JUDGE_CONFIGURATIONS
            or self.adjudication_method != CALIBRATED_ADJUDICATION_METHOD
            or self.adjudicator != CALIBRATED_ADJUDICATOR
        ):
            raise ManifestStateError("invalid matrix calibrated judge policy")
        if self.campaign_kind == "screen":
            if (
                self.acceptance_version != "fixed-corpus/0.1"
                or self.qualitative_rule != "fact_hard_safety_resolved_pass"
                or any(
                    value is not None
                    for value in (
                        self.source_screen_manifest_sha256,
                        self.source_screen_matrix_id,
                        self.source_screen_acceptance_sha256,
                        self.source_screen_qualitative_evidence_sha256,
                    )
                )
            ):
                raise ManifestStateError("invalid screen campaign policy")
        elif self.campaign_kind == "qualification" and (
            self.acceptance_version != "fixed-corpus/0.1"
            or self.qualitative_rule != "all_hard_criteria_resolved_pass"
            or not isinstance(self.source_screen_matrix_id, str)
            or _SAFE_TOKEN.fullmatch(self.source_screen_matrix_id) is None
            or not isinstance(self.source_screen_manifest_sha256, str)
            or _SHA256.fullmatch(self.source_screen_manifest_sha256) is None
            or not isinstance(self.source_screen_acceptance_sha256, str)
            or _SHA256.fullmatch(self.source_screen_acceptance_sha256) is None
            or not isinstance(self.source_screen_qualitative_evidence_sha256, str)
            or _SHA256.fullmatch(
                self.source_screen_qualitative_evidence_sha256
            )
            is None
        ):
            raise ManifestStateError("qualification lacks screen provenance")
        elif self.campaign_kind == "benchmark" and (
            self.acceptance_version != "diagnostic-corpus/0.1"
            or self.qualitative_rule != "diagnostic_criterion_vector"
            or any(
                value is not None
                for value in (
                    self.source_screen_manifest_sha256,
                    self.source_screen_matrix_id,
                    self.source_screen_acceptance_sha256,
                    self.source_screen_qualitative_evidence_sha256,
                )
            )
        ):
            raise ManifestStateError("invalid benchmark campaign policy")

    @classmethod
    def from_config(cls, value: Mapping[str, Any]) -> Self:
        try:
            selection = value["selection_policy"]
            acceptance = value["acceptance_policy"]
            judge = value["judge_policy"]
            provenance = value["screen_provenance"]
            return cls(
                campaign_kind=value["campaign_kind"],
                selection_version=selection["version"],
                selection_mode=selection["mode"],
                acceptance_version=acceptance["version"],
                hard_layers=tuple(acceptance["hard_layers"]),
                required_tracks=tuple(acceptance["required_tracks"]),
                replicates=acceptance["replicates"],
                qualitative_rule=acceptance["qualitative_rule"],
                judge_version=judge["version"],
                judgment_schema=judge["judgment_schema"],
                adjudication_schema=judge["adjudication_schema"],
                prompt_version=judge["prompt_version"],
                parser_version=judge["parser_version"],
                prompt_sha256=judge["prompt_sha256"],
                parser_sha256=judge["parser_sha256"],
                judges=tuple(
                    MatrixJudgeConfiguration.from_dict(item) for item in judge["judges"]
                ),
                adjudication_method=judge["adjudication"]["method"],
                adjudicator=judge["adjudication"]["adjudicator"],
                source_screen_manifest_sha256=(
                    provenance["source_screen_manifest_sha256"]
                    if provenance is not None
                    else None
                ),
                source_screen_matrix_id=(
                    provenance["source_screen_matrix_id"]
                    if provenance is not None
                    else None
                ),
                source_screen_acceptance_sha256=(
                    provenance["source_screen_acceptance_sha256"]
                    if provenance is not None
                    else None
                ),
                source_screen_qualitative_evidence_sha256=(
                    provenance["source_screen_qualitative_evidence_sha256"]
                    if provenance is not None
                    else None
                ),
            )
        except (KeyError, TypeError):
            raise ManifestStateError("invalid matrix campaign policy") from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_kind": self.campaign_kind,
            "selection_policy": {
                "version": self.selection_version,
                "mode": self.selection_mode,
            },
            "acceptance_policy": {
                "version": self.acceptance_version,
                "hard_layers": list(self.hard_layers),
                "required_tracks": list(self.required_tracks),
                "replicates": self.replicates,
                "qualitative_rule": self.qualitative_rule,
            },
            "judge_policy": {
                "version": self.judge_version,
                "judgment_schema": self.judgment_schema,
                "adjudication_schema": self.adjudication_schema,
                "prompt_version": self.prompt_version,
                "parser_version": self.parser_version,
                "prompt_sha256": self.prompt_sha256,
                "parser_sha256": self.parser_sha256,
                "judges": [item.to_dict() for item in self.judges],
                "adjudication": {
                    "method": self.adjudication_method,
                    "adjudicator": self.adjudicator,
                },
            },
            "screen_provenance": (
                {
                    "source_screen_matrix_id": self.source_screen_matrix_id,
                    "source_screen_manifest_sha256": self.source_screen_manifest_sha256,
                    "source_screen_acceptance_sha256": (
                        self.source_screen_acceptance_sha256
                    ),
                    "source_screen_qualitative_evidence_sha256": (
                        self.source_screen_qualitative_evidence_sha256
                    ),
                }
                if self.source_screen_manifest_sha256 is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "campaign_kind",
            "selection_policy",
            "acceptance_policy",
            "judge_policy",
            "screen_provenance",
        }:
            raise ManifestStateError("invalid matrix campaign policy")
        selection = value.get("selection_policy")
        acceptance = value.get("acceptance_policy")
        judge = value.get("judge_policy")
        provenance = value.get("screen_provenance")
        if (
            not isinstance(selection, dict)
            or set(selection) != {"version", "mode"}
            or not isinstance(acceptance, dict)
            or set(acceptance)
            != {
                "version",
                "hard_layers",
                "required_tracks",
                "replicates",
                "qualitative_rule",
            }
            or not isinstance(judge, dict)
            or set(judge)
            != {
                "version",
                "judgment_schema",
                "adjudication_schema",
                "prompt_version",
                "parser_version",
                "prompt_sha256",
                "parser_sha256",
                "judges",
                "adjudication",
            }
            or not isinstance(judge.get("judges"), list)
            or not isinstance(judge.get("adjudication"), dict)
            or set(judge["adjudication"]) != {"method", "adjudicator"}
            or (
                provenance is not None
                and (
                    not isinstance(provenance, dict)
                    or set(provenance)
                    != {
                        "source_screen_matrix_id",
                        "source_screen_manifest_sha256",
                        "source_screen_acceptance_sha256",
                        "source_screen_qualitative_evidence_sha256",
                    }
                )
            )
        ):
            raise ManifestStateError("invalid matrix campaign policy")
        return cls.from_config(value)

    @property
    def sha256(self) -> str:
        content = json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return _sha256_bytes(content)


@dataclass(frozen=True, slots=True, order=True)
class MatrixRoute:
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

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "model",
            "reasoning_effort",
        }:
            raise ManifestStateError("invalid matrix route")
        return cls(value["model"], value["reasoning_effort"])


@dataclass(frozen=True, slots=True)
class MatrixQualitativeCriterion:
    """One manifest-bound requirement shown to a blinded qualitative judge."""

    criterion_id: str
    source: str
    requirement: str
    evidence_path: str | None
    screen_safety_gate: bool = False

    def __post_init__(self) -> None:
        _safe_token(self.criterion_id, label="criterion ID")
        if self.source not in {
            "judged_answer_rubric",
            "fact_rubric.criteria.hard_fail",
            "fact_rubric.must_mention",
            "fact_rubric.support_if_claimed",
            PROSE_CLAIMS_ALIGNMENT_SOURCE,
        }:
            raise ManifestStateError("invalid qualitative criterion source")
        if (
            not isinstance(self.requirement, str)
            or not self.requirement.strip()
            or len(self.requirement) > 4096
        ):
            raise ManifestStateError("invalid qualitative criterion requirement")
        if self.source in {
            "judged_answer_rubric",
            "fact_rubric.criteria.hard_fail",
            PROSE_CLAIMS_ALIGNMENT_SOURCE,
        }:
            if self.evidence_path is not None:
                raise ManifestStateError("authored criterion has an evidence path")
        elif (
            not isinstance(self.evidence_path, str)
            or not self.evidence_path.startswith("$.")
            or len(self.evidence_path) > 1024
        ):
            raise ManifestStateError("must-mention criterion lacks an evidence path")
        if not isinstance(self.screen_safety_gate, bool) or (
            self.screen_safety_gate
            and self.source != "fact_rubric.criteria.hard_fail"
        ):
            raise ManifestStateError("invalid qualitative screen safety gate")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "id": self.criterion_id,
            "source": self.source,
            "requirement": self.requirement,
            "evidence_path": self.evidence_path,
            "screen_safety_gate": self.screen_safety_gate,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "id",
            "source",
            "requirement",
            "evidence_path",
            "screen_safety_gate",
        }:
            raise ManifestStateError("invalid qualitative criterion")
        try:
            return cls(
                criterion_id=value["id"],
                source=value["source"],
                requirement=value["requirement"],
                evidence_path=value["evidence_path"],
                screen_safety_gate=value["screen_safety_gate"],
            )
        except (TypeError, ManifestStateError):
            raise ManifestStateError("invalid qualitative criterion") from None


def matrix_qualitative_criteria(
    judged_criteria: Sequence[Mapping[str, Any]],
    must_mention: Sequence[str],
    *,
    fact_criteria: Sequence[Mapping[str, Any]] = (),
    support_if_claimed: Sequence[str] = (),
) -> tuple[MatrixQualitativeCriterion, ...]:
    """Promote every prose-review requirement into one manifest-bound rubric."""

    components = (judged_criteria, fact_criteria, must_mention, support_if_claimed)
    if any(len(items) > MAX_QUALITATIVE_CRITERIA for items in components):
        raise ManifestStateError(
            "scenario qualitative criterion component exceeds 1024 entries"
        )
    hard_fact_count = sum(
        1
        for item in fact_criteria
        if isinstance(item, Mapping) and item.get("hard_fail") is True
    )
    if (
        len(judged_criteria)
        + hard_fact_count
        + len(must_mention)
        + len(support_if_claimed)
        + 1
        > MAX_QUALITATIVE_CRITERIA
    ):
        raise ManifestStateError(
            "scenario has more than 1024 combined qualitative criteria"
        )

    result: list[MatrixQualitativeCriterion] = []
    for item in judged_criteria:
        try:
            result.append(
                MatrixQualitativeCriterion(
                    criterion_id=item["id"],
                    source="judged_answer_rubric",
                    requirement=item["requirement"],
                    evidence_path=None,
                    screen_safety_gate=item.get("screen_safety_gate", False),
                )
            )
        except (KeyError, TypeError, ManifestStateError):
            raise ManifestStateError("invalid authored qualitative criterion") from None
    for item in fact_criteria:
        if not isinstance(item, Mapping) or item.get("hard_fail") is not True:
            continue
        try:
            source_id = item["id"]
            if not isinstance(source_id, str):
                raise TypeError
            identity = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
            result.append(
                MatrixQualitativeCriterion(
                    criterion_id=f"fact-hard-{identity}",
                    source="fact_rubric.criteria.hard_fail",
                    requirement=item["requirement"],
                    evidence_path=None,
                    screen_safety_gate=item.get("screen_safety_gate", False),
                )
            )
        except (KeyError, TypeError, ManifestStateError):
            raise ManifestStateError("invalid hard fact criterion") from None
    for path in must_mention:
        if not isinstance(path, str):
            raise ManifestStateError("invalid must-mention evidence path")
        identity = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        result.append(
            MatrixQualitativeCriterion(
                criterion_id=f"must-mention-{identity}",
                source="fact_rubric.must_mention",
                requirement=(
                    "Compare the actual answer with the selected value or values "
                    f"for {path}; fail unless they are explicitly mentioned."
                ),
                evidence_path=path,
            )
        )
    for path in support_if_claimed:
        if not isinstance(path, str):
            raise ManifestStateError("invalid conditional evidence path")
        identity = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        result.append(
            MatrixQualitativeCriterion(
                criterion_id=f"support-if-claimed-{identity}",
                source="fact_rubric.support_if_claimed",
                requirement=(
                    "Compare any optional fact asserted in the actual answer with "
                    f"the selected evidence for {path}; pass when the answer omits "
                    "that fact, and fail when it asserts an unsupported or wrong value."
                ),
                evidence_path=path,
            )
        )
    result.append(
        MatrixQualitativeCriterion(
            criterion_id=PROSE_CLAIMS_ALIGNMENT_CRITERION_ID,
            source=PROSE_CLAIMS_ALIGNMENT_SOURCE,
            requirement=PROSE_CLAIMS_ALIGNMENT_REQUIREMENT,
            evidence_path=None,
        )
    )
    identifiers = tuple(item.criterion_id for item in result)
    if not identifiers:
        raise ManifestStateError("scenario has no qualitative criteria")
    if len(set(identifiers)) != len(identifiers):
        raise ManifestStateError(
            "scenario qualitative criterion IDs are not unique after promotion"
        )
    return tuple(result)


def scenario_qualitative_criteria(
    scenario: Mapping[str, Any],
) -> tuple[MatrixQualitativeCriterion, ...]:
    """Validate and promote one schema 0.3 qualitative rubric."""

    if scenario.get("schema_version") != "steam-agent-eval/0.3":
        raise ManifestStateError("scenario qualitative rubric version is invalid")
    judged_rubric = scenario.get("judged_answer_rubric")
    fact_rubric = scenario.get("fact_rubric")
    if not isinstance(judged_rubric, Mapping) or not isinstance(
        fact_rubric, Mapping
    ):
        raise ManifestStateError("scenario qualitative rubric is invalid")
    judged_criteria = judged_rubric.get("criteria")
    fact_criteria = fact_rubric.get("criteria")
    must_mention = fact_rubric.get("must_mention")
    support_if_claimed = fact_rubric.get("support_if_claimed")
    if not all(
        isinstance(items, list)
        for items in (
            judged_criteria,
            fact_criteria,
            must_mention,
            support_if_claimed,
        )
    ):
        raise ManifestStateError("scenario qualitative rubric is invalid")
    return matrix_qualitative_criteria(
        judged_criteria,
        must_mention,
        fact_criteria=fact_criteria,
        support_if_claimed=support_if_claimed,
    )


@dataclass(frozen=True, slots=True)
class MatrixScenario:
    scenario_id: str
    source_sha256: str
    child_source_digest: str
    schema_version: str
    schema_sha256: str
    execution_support: str
    turn_count: int
    rubric_sha256: str
    criterion_ids: tuple[str, ...]
    qualitative_criteria: tuple[MatrixQualitativeCriterion, ...]

    def __post_init__(self) -> None:
        _safe_token(self.scenario_id, label="scenario ID")
        for digest in (
            self.source_sha256,
            self.child_source_digest,
            self.schema_sha256,
            self.rubric_sha256,
        ):
            if _SHA256.fullmatch(digest) is None:
                raise ManifestStateError("invalid matrix scenario digest")
        if _SAFE_VERSION.fullmatch(self.schema_version) is None:
            raise ManifestStateError("invalid scenario schema version")
        if self.execution_support not in {"live", "deterministic_only"}:
            raise ManifestStateError("invalid scenario execution support")
        if (
            not isinstance(self.turn_count, int)
            or isinstance(self.turn_count, bool)
            or not 1 <= self.turn_count <= MAX_SCENARIO_TURNS
        ):
            raise ManifestStateError(
                f"invalid matrix scenario turn count (expected 1..{MAX_SCENARIO_TURNS})"
            )
        if (
            not self.criterion_ids
            or len(self.criterion_ids) > MAX_QUALITATIVE_CRITERIA
            or len(set(self.criterion_ids)) != len(self.criterion_ids)
        ):
            raise ManifestStateError("invalid judged criterion IDs")
        if self.criterion_ids != tuple(
            item.criterion_id for item in self.qualitative_criteria
        ):
            raise ManifestStateError("matrix qualitative criteria do not match IDs")
        for criterion_id in self.criterion_ids:
            _safe_token(criterion_id, label="criterion ID")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "source_sha256": self.source_sha256,
            "child_source_digest": self.child_source_digest,
            "schema_version": self.schema_version,
            "schema_sha256": self.schema_sha256,
            "execution_support": self.execution_support,
            "turn_count": self.turn_count,
            "rubric_sha256": self.rubric_sha256,
            "criterion_ids": list(self.criterion_ids),
            "qualitative_criteria": [
                item.to_dict() for item in self.qualitative_criteria
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        keys = {
            "scenario_id",
            "source_sha256",
            "child_source_digest",
            "schema_version",
            "schema_sha256",
            "execution_support",
            "turn_count",
            "rubric_sha256",
            "criterion_ids",
            "qualitative_criteria",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ManifestStateError("invalid matrix scenario")
        criterion_ids = value["criterion_ids"]
        qualitative_criteria = value["qualitative_criteria"]
        if not isinstance(criterion_ids, list) or not all(
            isinstance(item, str) for item in criterion_ids
        ) or not isinstance(qualitative_criteria, list):
            raise ManifestStateError("invalid judged criterion IDs")
        return cls(
            scenario_id=value["scenario_id"],
            source_sha256=value["source_sha256"],
            child_source_digest=value["child_source_digest"],
            schema_version=value["schema_version"],
            schema_sha256=value["schema_sha256"],
            execution_support=value["execution_support"],
            turn_count=value["turn_count"],
            rubric_sha256=value["rubric_sha256"],
            criterion_ids=tuple(criterion_ids),
            qualitative_criteria=tuple(
                MatrixQualitativeCriterion.from_dict(item)
                for item in qualitative_criteria
            ),
        )


@dataclass(frozen=True, slots=True)
class MatrixInputs:
    commit: str
    source_digest: str
    harness_digest: str
    scenarios: tuple[MatrixScenario, ...]
    tool_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if _COMMIT.fullmatch(self.commit) is None:
            raise ManifestStateError("matrix requires a source commit")
        if any(
            _SHA256.fullmatch(digest) is None
            for digest in (self.source_digest, self.harness_digest)
        ):
            raise ManifestStateError("invalid matrix input digest")
        scenario_ids = tuple(item.scenario_id for item in self.scenarios)
        if not scenario_ids or len(set(scenario_ids)) != len(scenario_ids):
            raise ManifestStateError("matrix requires unique scenarios")
        if tuple(sorted(self.tool_versions)) != self.tool_versions:
            raise ManifestStateError("matrix tool versions are not deterministic")
        for name, version in self.tool_versions:
            _safe_token(name, label="tool name")
            if _SAFE_VERSION.fullmatch(version) is None:
                raise ManifestStateError("invalid tool version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "source_digest": self.source_digest,
            "harness_digest": self.harness_digest,
            "scenarios": [item.to_dict() for item in self.scenarios],
            "tool_versions": [
                {"name": name, "version": version}
                for name, version in self.tool_versions
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "commit",
            "source_digest",
            "harness_digest",
            "scenarios",
            "tool_versions",
        }:
            raise ManifestStateError("invalid matrix inputs")
        scenarios = value["scenarios"]
        tools = value["tool_versions"]
        if not isinstance(scenarios, list) or not isinstance(tools, list):
            raise ManifestStateError("invalid matrix inputs")
        normalized_tools: list[tuple[str, str]] = []
        for item in tools:
            if not isinstance(item, dict) or set(item) != {"name", "version"}:
                raise ManifestStateError("invalid matrix tool versions")
            normalized_tools.append((item["name"], item["version"]))
        return cls(
            commit=value["commit"],
            source_digest=value["source_digest"],
            harness_digest=value["harness_digest"],
            scenarios=tuple(MatrixScenario.from_dict(item) for item in scenarios),
            tool_versions=tuple(normalized_tools),
        )


@dataclass(frozen=True, slots=True, order=True)
class MatrixPreflightScenario:
    """One exact deterministic-only scenario proven by an executed oracle."""

    scenario_id: str
    source_sha256: str
    child_source_digest: str
    schema_sha256: str
    rubric_sha256: str
    executor: str
    document_sha256: str
    grading_sha256: str
    outcome: str

    def __post_init__(self) -> None:
        _safe_token(self.scenario_id, label="preflight scenario ID")
        if any(
            _SHA256.fullmatch(digest) is None
            for digest in (
                self.source_sha256,
                self.child_source_digest,
                self.schema_sha256,
                self.rubric_sha256,
                self.document_sha256,
                self.grading_sha256,
            )
        ):
            raise ManifestStateError("invalid deterministic-only preflight digest")
        if self.executor not in {"frozen_cli", "domain_oracle"}:
            raise ManifestStateError("invalid deterministic-only preflight executor")
        if self.outcome != "passed":
            raise ManifestStateError("deterministic-only preflight did not pass")

    @classmethod
    def from_scenario(
        cls,
        scenario: MatrixScenario,
        *,
        executor: str,
        document_sha256: str,
        grading_sha256: str,
    ) -> Self:
        if scenario.execution_support != "deterministic_only":
            raise ManifestStateError("preflight scenario is not deterministic-only")
        return cls(
            scenario_id=scenario.scenario_id,
            source_sha256=scenario.source_sha256,
            child_source_digest=scenario.child_source_digest,
            schema_sha256=scenario.schema_sha256,
            rubric_sha256=scenario.rubric_sha256,
            executor=executor,
            document_sha256=document_sha256,
            grading_sha256=grading_sha256,
            outcome="passed",
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "scenario_id": self.scenario_id,
            "source_sha256": self.source_sha256,
            "child_source_digest": self.child_source_digest,
            "schema_sha256": self.schema_sha256,
            "rubric_sha256": self.rubric_sha256,
            "executor": self.executor,
            "document_sha256": self.document_sha256,
            "grading_sha256": self.grading_sha256,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        expected = {
            "scenario_id",
            "source_sha256",
            "child_source_digest",
            "schema_sha256",
            "rubric_sha256",
            "executor",
            "document_sha256",
            "grading_sha256",
            "outcome",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ManifestStateError("invalid deterministic-only preflight scenario")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class MatrixPreflightAttestation:
    """Manifest-bound proof that every frozen deterministic oracle passed."""

    scenarios: tuple[MatrixPreflightScenario, ...]
    schema: str = MATRIX_PREFLIGHT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MATRIX_PREFLIGHT_SCHEMA:
            raise ManifestStateError("invalid deterministic-only preflight schema")
        identifiers = tuple(item.scenario_id for item in self.scenarios)
        if identifiers != tuple(sorted(identifiers)) or len(set(identifiers)) != len(
            identifiers
        ):
            raise ManifestStateError("deterministic-only preflight order is invalid")

    @classmethod
    def for_inputs(
        cls,
        inputs: MatrixInputs,
        *,
        evidence: Mapping[str, tuple[str, str, str]] | None = None,
    ) -> Self:
        deterministic = tuple(
            item
            for item in inputs.scenarios
            if item.execution_support == "deterministic_only"
        )
        provided = evidence or {}
        if set(provided) != {item.scenario_id for item in deterministic}:
            raise ManifestStateError(
                "deterministic-only preflight evidence is incomplete"
            )
        return cls(
            scenarios=tuple(
                sorted(
                    (
                        MatrixPreflightScenario.from_scenario(
                            item,
                            executor=provided[item.scenario_id][0],
                            document_sha256=provided[item.scenario_id][1],
                            grading_sha256=provided[item.scenario_id][2],
                        )
                        for item in deterministic
                    ),
                    key=lambda item: item.scenario_id,
                )
            )
        )

    def require_matches(self, inputs: MatrixInputs) -> None:
        expected = {
            item.scenario_id: item
            for item in inputs.scenarios
            if item.execution_support == "deterministic_only"
        }
        if set(expected) != {item.scenario_id for item in self.scenarios} or any(
            (
                item.source_sha256,
                item.child_source_digest,
                item.schema_sha256,
                item.rubric_sha256,
            )
            != (
                expected[item.scenario_id].source_sha256,
                expected[item.scenario_id].child_source_digest,
                expected[item.scenario_id].schema_sha256,
                expected[item.scenario_id].rubric_sha256,
            )
            for item in self.scenarios
        ):
            raise ManifestStateError(
                "deterministic-only preflight does not match matrix inputs"
            )

    @property
    def sha256(self) -> str:
        return _sha256_bytes(_strict_json_bytes(self.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "scenarios": [item.to_dict() for item in self.scenarios],
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "scenarios"}
            or not isinstance(value["scenarios"], list)
        ):
            raise ManifestStateError("invalid deterministic-only preflight")
        return cls(
            schema=value["schema"],
            scenarios=tuple(
                MatrixPreflightScenario.from_dict(item)
                for item in value["scenarios"]
            ),
        )


@dataclass(frozen=True, slots=True)
class MatrixWorkItem:
    work_item_id: str
    identity_sha256: str
    ordinal: int
    scenario_id: str
    track: str
    route: MatrixRoute
    replicate: int

    def __post_init__(self) -> None:
        _safe_token(self.work_item_id, label="work item ID")
        if _SHA256.fullmatch(self.identity_sha256) is None:
            raise ManifestStateError("invalid work item identity")
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool):
            raise ManifestStateError("invalid work item ordinal")
        if self.ordinal < 0:
            raise ManifestStateError("invalid work item ordinal")
        _safe_token(self.scenario_id, label="scenario ID")
        if self.track not in _TRACKS:
            raise ManifestStateError("invalid evaluation track")
        if not isinstance(self.route, MatrixRoute):
            raise ManifestStateError("invalid matrix route")
        if (
            not isinstance(self.replicate, int)
            or isinstance(self.replicate, bool)
            or self.replicate < 1
        ):
            raise ManifestStateError("invalid replicate")

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "identity_sha256": self.identity_sha256,
            "ordinal": self.ordinal,
            "scenario_id": self.scenario_id,
            "track": self.track,
            "route": self.route.to_dict(),
            "replicate": self.replicate,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "work_item_id",
            "identity_sha256",
            "ordinal",
            "scenario_id",
            "track",
            "route",
            "replicate",
        }:
            raise ManifestStateError("invalid matrix work item")
        return cls(
            work_item_id=value["work_item_id"],
            identity_sha256=value["identity_sha256"],
            ordinal=value["ordinal"],
            scenario_id=value["scenario_id"],
            track=value["track"],
            route=MatrixRoute.from_dict(value["route"]),
            replicate=value["replicate"],
        )


@dataclass(frozen=True, slots=True)
class MatrixCompletion:
    work_item_id: str
    attempt_id: str
    started_sha256: str
    outcome: str
    unavailable_reason: str | None
    child_run_id: str | None
    child_exit_code: int | None
    artifact_hashes: tuple[tuple[str, str], ...]
    completed_at: str

    def __post_init__(self) -> None:
        for label, value in (
            ("work item ID", self.work_item_id),
            ("attempt ID", self.attempt_id),
        ):
            _safe_token(value, label=label)
        if (
            not isinstance(self.started_sha256, str)
            or _SHA256.fullmatch(self.started_sha256) is None
        ):
            raise ManifestStateError("invalid matrix attempt start digest")
        if tuple(sorted(self.artifact_hashes)) != self.artifact_hashes:
            raise ManifestStateError("artifact hashes are not deterministic")
        expected_names = {
            "controls.json",
            "manifest.json",
            "report.json",
            "summary.json",
            "transcript.jsonl",
        }
        if self.outcome == "observed":
            if (
                self.unavailable_reason is not None
                or self.child_run_id is None
                or self.child_exit_code not in {0, 1, 3}
                or {name for name, _digest in self.artifact_hashes} != expected_names
            ):
                raise ManifestStateError("invalid observed matrix completion")
            _safe_token(self.child_run_id, label="child run ID")
        elif self.outcome == "unavailable":
            if (
                self.child_run_id is not None
                or self.child_exit_code is not None
                or self.artifact_hashes
                or self.unavailable_reason is None
            ):
                raise ManifestStateError("invalid unavailable matrix completion")
            _safe_token(self.unavailable_reason, label="unavailable reason")
        else:
            raise ManifestStateError("invalid matrix completion outcome")
        for name, digest in self.artifact_hashes:
            _safe_relative_name(name)
            if _SHA256.fullmatch(digest) is None:
                raise ManifestStateError("invalid artifact hash")
        _parse_time(self.completed_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            "started_sha256": self.started_sha256,
            "outcome": self.outcome,
            "unavailable_reason": self.unavailable_reason,
            "child_run_id": self.child_run_id,
            "child_exit_code": self.child_exit_code,
            "artifact_hashes": [
                {"name": name, "sha256": digest}
                for name, digest in self.artifact_hashes
            ],
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "work_item_id",
            "attempt_id",
            "started_sha256",
            "outcome",
            "unavailable_reason",
            "child_run_id",
            "child_exit_code",
            "artifact_hashes",
            "completed_at",
        }:
            raise ManifestStateError("invalid matrix completion")
        hashes = value["artifact_hashes"]
        if not isinstance(hashes, list):
            raise ManifestStateError("invalid matrix completion")
        normalized: list[tuple[str, str]] = []
        for item in hashes:
            if not isinstance(item, dict) or set(item) != {"name", "sha256"}:
                raise ManifestStateError("invalid matrix artifact hash")
            normalized.append((item["name"], item["sha256"]))
        return cls(
            work_item_id=value["work_item_id"],
            attempt_id=value["attempt_id"],
            started_sha256=value["started_sha256"],
            outcome=value["outcome"],
            unavailable_reason=value["unavailable_reason"],
            child_run_id=value["child_run_id"],
            child_exit_code=value["child_exit_code"],
            artifact_hashes=tuple(normalized),
            completed_at=value["completed_at"],
        )


@dataclass(frozen=True, slots=True)
class MatrixManifest:
    matrix_id: str
    state: MatrixState
    revision: int
    config_sha256: str
    campaign_sha256: str
    campaign: MatrixCampaign
    plan_sha256: str
    inputs: MatrixInputs
    preflight_attestation: MatrixPreflightAttestation
    work_items: tuple[MatrixWorkItem, ...]
    excluded_scenario_ids: tuple[str, ...]
    completions: tuple[MatrixCompletion, ...]
    started_at: str
    updated_at: str
    finished_at: str | None
    acceptance_sha256: str | None = None
    acceptance_finalized_at: str | None = None
    schema: str = MATRIX_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        _safe_token(self.matrix_id, label="matrix ID")
        if self.schema != MATRIX_MANIFEST_SCHEMA:
            raise ManifestStateError("invalid matrix manifest schema")
        if not isinstance(self.state, MatrixState):
            raise ManifestStateError("invalid matrix state")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 0
        ):
            raise ManifestStateError("invalid matrix revision")
        if any(
            _SHA256.fullmatch(digest) is None
            for digest in (
                self.config_sha256,
                self.campaign_sha256,
                self.plan_sha256,
            )
        ):
            raise ManifestStateError("invalid matrix digest")
        if (
            not isinstance(self.campaign, MatrixCampaign)
            or self.campaign.sha256 != self.campaign_sha256
        ):
            raise ManifestStateError("matrix campaign digest does not match")
        if not isinstance(self.inputs, MatrixInputs):
            raise ManifestStateError("invalid matrix inputs")
        if not isinstance(self.preflight_attestation, MatrixPreflightAttestation):
            raise ManifestStateError("invalid deterministic-only preflight")
        self.preflight_attestation.require_matches(self.inputs)
        if not self.work_items:
            raise ManifestStateError("matrix requires live work items")
        if tuple(item.ordinal for item in self.work_items) != tuple(
            range(len(self.work_items))
        ):
            raise ManifestStateError("matrix work order is invalid")
        work_ids = tuple(item.work_item_id for item in self.work_items)
        if len(set(work_ids)) != len(work_ids):
            raise ManifestStateError("matrix work IDs are not unique")
        live_scenarios = {
            item.scenario_id
            for item in self.inputs.scenarios
            if item.execution_support == "live"
        }
        if any(item.scenario_id not in live_scenarios for item in self.work_items):
            raise ManifestStateError("matrix contains unsupported live work")
        if (
            {item.track for item in self.work_items}
            != set(self.campaign.required_tracks)
            or {item.replicate for item in self.work_items}
            != set(range(1, self.campaign.replicates + 1))
            or any(
                item.route.model is None or item.route.reasoning_effort is None
                for item in self.work_items
            )
        ):
            raise ManifestStateError("matrix work does not match campaign policy")
        if tuple(sorted(self.excluded_scenario_ids)) != self.excluded_scenario_ids:
            raise ManifestStateError("excluded scenarios are not deterministic")
        expected_excluded = tuple(
            sorted(
                item.scenario_id
                for item in self.inputs.scenarios
                if item.execution_support == "deterministic_only"
            )
        )
        if self.excluded_scenario_ids != expected_excluded:
            raise ManifestStateError("excluded scenarios do not match inputs")
        completed_ids = tuple(item.work_item_id for item in self.completions)
        if completed_ids != work_ids[: len(completed_ids)]:
            raise ManifestStateError("matrix completions are not an ordered prefix")
        if self.revision != len(self.completions):
            raise ManifestStateError("matrix revision does not match completions")
        if self.state is MatrixState.COMPLETED and len(self.completions) != len(
            self.work_items
        ):
            raise ManifestStateError("completed matrix has unaccounted work")
        if self.state is MatrixState.OPEN and len(self.completions) == len(
            self.work_items
        ):
            raise ManifestStateError("fully accounted matrix remains open")
        started = _parse_time(self.started_at)
        updated = _parse_time(self.updated_at)
        if updated < started:
            raise ManifestStateError("matrix time moved backwards")
        if self.state is MatrixState.COMPLETED:
            if self.finished_at != self.updated_at:
                raise ManifestStateError("completed matrix lacks a finish time")
        elif self.finished_at is not None:
            raise ManifestStateError("open matrix has a finish time")
        if (self.acceptance_sha256 is None) != (
            self.acceptance_finalized_at is None
        ):
            raise ManifestStateError("matrix acceptance finalization is incomplete")
        if self.acceptance_sha256 is not None:
            if (
                self.state is not MatrixState.COMPLETED
                or self.campaign.campaign_kind != "screen"
                or _SHA256.fullmatch(self.acceptance_sha256) is None
                or _parse_time(self.acceptance_finalized_at)
                <= _parse_time(self.finished_at)
            ):
                raise ManifestStateError("matrix acceptance finalization is invalid")

    @classmethod
    def create(
        cls,
        *,
        matrix_id: str,
        config_sha256: str,
        campaign: MatrixCampaign,
        plan_sha256: str,
        inputs: MatrixInputs,
        preflight_attestation: MatrixPreflightAttestation | None = None,
        work_items: Sequence[MatrixWorkItem],
        excluded_scenario_ids: Sequence[str],
        started_at: datetime,
    ) -> Self:
        timestamp = _canonical_time(started_at)
        if preflight_attestation is None:
            if any(
                item.execution_support == "deterministic_only"
                for item in inputs.scenarios
            ):
                raise ManifestStateError(
                    "deterministic-only preflight attestation is required"
                )
            attestation = MatrixPreflightAttestation(())
        else:
            attestation = preflight_attestation
        return cls(
            matrix_id=matrix_id,
            state=MatrixState.OPEN,
            revision=0,
            config_sha256=config_sha256,
            campaign_sha256=campaign.sha256,
            campaign=campaign,
            plan_sha256=plan_sha256,
            inputs=inputs,
            preflight_attestation=attestation,
            work_items=tuple(work_items),
            excluded_scenario_ids=tuple(sorted(excluded_scenario_ids)),
            completions=(),
            started_at=timestamp,
            updated_at=timestamp,
            finished_at=None,
        )

    def checkpoint(self, completion: MatrixCompletion, *, at: datetime) -> Self:
        if self.state is not MatrixState.OPEN:
            raise ManifestStateError("completed matrix cannot advance")
        expected = self.work_items[len(self.completions)].work_item_id
        if completion.work_item_id != expected:
            raise ManifestStateError("matrix checkpoint is out of order")
        timestamp = _canonical_time(at)
        if _parse_time(timestamp) < _parse_time(self.updated_at):
            raise ManifestStateError("matrix time moved backwards")
        next_completions = (*self.completions, completion)
        next_state = (
            MatrixState.COMPLETED
            if len(next_completions) == len(self.work_items)
            else MatrixState.OPEN
        )
        return replace(
            self,
            state=next_state,
            revision=self.revision + 1,
            completions=next_completions,
            updated_at=timestamp,
            finished_at=timestamp if next_state is MatrixState.COMPLETED else None,
        )

    def bind_acceptance(self, digest: str, *, finalized_at: str) -> Self:
        if (
            self.state is not MatrixState.COMPLETED
            or self.campaign.campaign_kind != "screen"
            or self.acceptance_sha256 is not None
            or _SHA256.fullmatch(digest) is None
        ):
            raise ManifestStateError("matrix acceptance cannot be finalized")
        _parse_time(finalized_at)
        return replace(
            self,
            acceptance_sha256=digest,
            acceptance_finalized_at=finalized_at,
        )

    @property
    def acceptance_source_sha256(self) -> str:
        source = replace(
            self,
            acceptance_sha256=None,
            acceptance_finalized_at=None,
        )
        return _sha256_bytes(_strict_json_bytes(source.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "matrix_id": self.matrix_id,
            "state": self.state.value,
            "revision": self.revision,
            "config_sha256": self.config_sha256,
            "campaign_sha256": self.campaign_sha256,
            "campaign": self.campaign.to_dict(),
            "plan_sha256": self.plan_sha256,
            "inputs": self.inputs.to_dict(),
            "preflight_attestation": self.preflight_attestation.to_dict(),
            "work_items": [item.to_dict() for item in self.work_items],
            "excluded_scenario_ids": list(self.excluded_scenario_ids),
            "completions": [item.to_dict() for item in self.completions],
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }
        if self.acceptance_sha256 is not None:
            value["acceptance_sha256"] = self.acceptance_sha256
            value["acceptance_finalized_at"] = self.acceptance_finalized_at
        return value

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        expected = {
            "schema",
            "matrix_id",
            "state",
            "revision",
            "config_sha256",
            "campaign_sha256",
            "campaign",
            "plan_sha256",
            "inputs",
            "preflight_attestation",
            "work_items",
            "excluded_scenario_ids",
            "completions",
            "started_at",
            "updated_at",
            "finished_at",
        }
        optional = {"acceptance_sha256", "acceptance_finalized_at"}
        if (
            not isinstance(value, dict)
            or frozenset(value)
            not in {frozenset(expected), frozenset(expected | optional)}
        ):
            raise ManifestStateError("invalid matrix manifest")
        work_items = value["work_items"]
        excluded = value["excluded_scenario_ids"]
        completions = value["completions"]
        if not all(
            isinstance(items, list) for items in (work_items, excluded, completions)
        ):
            raise ManifestStateError("invalid matrix manifest")
        if not all(isinstance(item, str) for item in excluded):
            raise ManifestStateError("invalid excluded scenarios")
        try:
            state = MatrixState(value["state"])
        except (TypeError, ValueError):
            raise ManifestStateError("invalid matrix state") from None
        return cls(
            schema=value["schema"],
            matrix_id=value["matrix_id"],
            state=state,
            revision=value["revision"],
            config_sha256=value["config_sha256"],
            campaign_sha256=value["campaign_sha256"],
            campaign=MatrixCampaign.from_dict(value["campaign"]),
            plan_sha256=value["plan_sha256"],
            inputs=MatrixInputs.from_dict(value["inputs"]),
            preflight_attestation=MatrixPreflightAttestation.from_dict(
                value["preflight_attestation"]
            ),
            work_items=tuple(MatrixWorkItem.from_dict(item) for item in work_items),
            excluded_scenario_ids=tuple(excluded),
            completions=tuple(MatrixCompletion.from_dict(item) for item in completions),
            started_at=value["started_at"],
            updated_at=value["updated_at"],
            finished_at=value["finished_at"],
            acceptance_sha256=value.get("acceptance_sha256"),
            acceptance_finalized_at=value.get("acceptance_finalized_at"),
        )

    def persist(self, path: Path) -> None:
        path = Path(path)
        try:
            item_stat = path.lstat()
        except FileNotFoundError:
            if self.revision != 0:
                raise ManifestStateError("matrix manifest history is missing") from None
            atomic_publish_private_json(path, self.to_dict())
            return
        if not stat.S_ISREG(item_stat.st_mode):
            raise ManifestStateError("matrix manifest target is not regular")
        try:
            if item_stat.st_size > MATRIX_MANIFEST_MAX_BYTES:
                raise ManifestStateError("existing matrix manifest is invalid")
            with path.open("rb") as manifest_file:
                source_bytes = manifest_file.read(MATRIX_MANIFEST_MAX_BYTES + 1)
            if len(source_bytes) > MATRIX_MANIFEST_MAX_BYTES:
                raise ManifestStateError("existing matrix manifest is invalid")
            existing = MatrixManifest.from_dict(
                json.loads(source_bytes.decode("utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ManifestStateError):
            raise ManifestStateError("existing matrix manifest is invalid") from None
        same_static = (
            replace(
                existing,
                state=self.state,
                revision=self.revision,
                completions=self.completions,
                updated_at=self.updated_at,
                finished_at=self.finished_at,
            )
            == self
        )
        acceptance_transition = (
            existing.acceptance_sha256 is None
            and existing.acceptance_finalized_at is None
            and self.acceptance_sha256 is not None
            and replace(
                existing,
                acceptance_sha256=self.acceptance_sha256,
                acceptance_finalized_at=self.acceptance_finalized_at,
            )
            == self
        )
        if acceptance_transition:
            _atomic_replace_private_bytes(path, _strict_json_bytes(self.to_dict()))
            return
        if (
            not same_static
            or existing.state is not MatrixState.OPEN
            or existing.revision != self.revision - 1
            or self.completions[:-1] != existing.completions
        ):
            raise ManifestStateError("matrix manifest history does not match")
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

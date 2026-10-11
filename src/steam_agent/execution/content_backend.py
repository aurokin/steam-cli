"""Candidate Windows content bindings; release authorization remains closed.

This driver composes NTFS file identities and the versioned Windows adoption
journal. It never substitutes POSIX inode or directory-fsync semantics. Content
and authentication qualification, private-cache ACL verification, directory
creation, client download observation and installed-projection validation are
still required before the broker can enable Windows content execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path, PureWindowsPath
from typing import Literal

from steam_agent.execution.content_plane import AdoptionError, WindowsContentResult
from steam_agent.execution.windows_filesystem import (
    WindowsFileIdentity, WindowsFilesystem, validate_path,
)

OWNERSHIP_SCHEMA = "windows-content-ownership/1"
EFFECT_SCHEMA = "windows-content-effect/1"
_BACKEND = "windows-ntfs-job/1"
_MARKER_LIMIT = 16 * 1024


class WindowsContentError(AdoptionError):
    """Fixed diagnostics without target paths, login names or raw output."""


@dataclass(frozen=True, slots=True)
class WindowsContentBinding:
    appid: int
    operation_id: int
    install_dir_name: str
    library_identity: WindowsFileIdentity
    target_identity: WindowsFileIdentity
    marker_directory_identity: WindowsFileIdentity


@dataclass(frozen=True, slots=True)
class WindowsEffectReceipt:
    schema: str
    backend: str
    operation: Literal["install", "verify"]
    binding: WindowsContentBinding
    state: Literal["prepared", "completed", "uncertain"]


def _identity(value: object) -> WindowsFileIdentity:
    if not isinstance(value, dict) or set(value) != {"volume_serial", "file_id"}:
        raise WindowsContentError("WINDOWS_CONTENT_MARKER_INVALID")
    serial, file_id = value["volume_serial"], value["file_id"]
    if (type(serial) is not int or not 0 <= serial <= (1 << 64) - 1
            or not isinstance(file_id, str) or len(file_id) != 32
            or any(c not in "0123456789abcdef" for c in file_id)):
        raise WindowsContentError("WINDOWS_CONTENT_MARKER_INVALID")
    return WindowsFileIdentity(serial, file_id)


def _binding(value: object) -> WindowsContentBinding:
    if not isinstance(value, dict) or set(value) != set(WindowsContentBinding.__dataclass_fields__):
        raise WindowsContentError("WINDOWS_CONTENT_MARKER_INVALID")
    appid, operation_id, name = value["appid"], value["operation_id"], value["install_dir_name"]
    if (type(appid) is not int or not 1 <= appid <= 0xFFFFFFFF
            or type(operation_id) is not int or operation_id <= 0
            or not isinstance(name, str) or not WindowsFilesystem.safe_component(name)):
        raise WindowsContentError("WINDOWS_CONTENT_MARKER_INVALID")
    return WindowsContentBinding(
        appid, operation_id, name, _identity(value["library_identity"]),
        _identity(value["target_identity"]), _identity(value["marker_directory_identity"]),
    )


class WindowsContentBackend:
    """Bind pre-provisioned directories and preserve exact recovery ownership.

    The state directory's ``owned`` and ``journal`` directories and the target
    must already exist on supported NTFS volumes. This candidate deliberately
    refuses implicit directory creation rather than following a junction while
    constructing a target. These methods require the broker execution lock.
    """

    def __init__(self, *, library: Path, state_dir: Path, filesystem: WindowsFilesystem) -> None:
        self.library = library
        self.state_dir = state_dir
        self.filesystem = filesystem
        self._library = validate_path(library)
        self._owned = validate_path(state_dir) / "owned"
        self._journal = Path(str(validate_path(state_dir) / "journal"))

    @staticmethod
    def release_denial() -> str:
        return "Windows content backend is a candidate; authentication and content recovery are unqualified"

    def _ownership_path(self, appid: int) -> PureWindowsPath:
        return self._owned / f"windows-owned-{appid}.json"

    def _effect_path(self, operation_id: int) -> PureWindowsPath:
        return self._owned / f"windows-effect-{operation_id}.json"

    def _legacy_denial(self, appid: int) -> None:
        native = self.filesystem.native
        if (native.exists(self._owned / f"{appid}.json")
                or native.exists(validate_path(self._journal) / f"adoption-{appid}.json")):
            raise WindowsContentError("WINDOWS_CONTENT_LEGACY_STATE_REFUSED")

    def bind_target(self, *, appid: int, operation_id: int, install_dir_name: str) -> WindowsContentBinding:
        if (type(appid) is not int or not 1 <= appid <= 0xFFFFFFFF
                or type(operation_id) is not int or operation_id <= 0
                or not self.filesystem.safe_component(install_dir_name)):
            raise WindowsContentError("WINDOWS_CONTENT_TARGET_INVALID")
        self._legacy_denial(appid)
        target = self._library / "steamapps" / "common" / install_dir_name
        native = self.filesystem.native
        library_id = native.identity(self._library / "steamapps", directory=True)
        target_id = native.identity(target, directory=True)
        marker_id = native.identity(self._owned, directory=True)
        if target_id.volume_serial != library_id.volume_serial:
            raise WindowsContentError("WINDOWS_CONTENT_TARGET_VOLUME_CHANGED")
        return WindowsContentBinding(appid, operation_id, install_dir_name, library_id, target_id, marker_id)

    def revalidate(self, binding: WindowsContentBinding) -> None:
        current = self.bind_target(appid=binding.appid, operation_id=binding.operation_id,
                                   install_dir_name=binding.install_dir_name)
        if current != binding:
            raise WindowsContentError("WINDOWS_CONTENT_IDENTITY_CHANGED")

    def _read(self, path: PureWindowsPath) -> dict[str, object]:
        try:
            value = json.loads(self.filesystem.native.read(path, limit=_MARKER_LIMIT))
        except Exception:
            raise WindowsContentError("WINDOWS_CONTENT_MARKER_INVALID") from None
        if not isinstance(value, dict):
            raise WindowsContentError("WINDOWS_CONTENT_MARKER_INVALID")
        return value

    def _write(self, path: PureWindowsPath, value: dict[str, object]) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if len(payload) > _MARKER_LIMIT:
            raise WindowsContentError("WINDOWS_CONTENT_MARKER_INVALID")
        native = self.filesystem.native
        pending = path.with_name(path.name + ".intent")
        if native.exists(pending):
            raise WindowsContentError("WINDOWS_CONTENT_MARKER_PENDING")
        native.write_new(pending, payload)
        if native.exists(path):
            native.replace(pending, path)
        else:
            native.move_new(pending, path)

    def record_ownership(self, binding: WindowsContentBinding) -> None:
        self.revalidate(binding)
        path = self._ownership_path(binding.appid)
        if self.filesystem.native.exists(path):
            old = self._read(path)
            if set(old) != {"schema", "backend", "binding"} or old.get("schema") != OWNERSHIP_SCHEMA or old.get("backend") != _BACKEND:
                raise WindowsContentError("WINDOWS_CONTENT_MARKER_UNSUPPORTED")
            recorded = _binding(old["binding"])
            if (recorded.appid != binding.appid or recorded.install_dir_name.casefold() != binding.install_dir_name.casefold()
                    or recorded.library_identity != binding.library_identity
                    or recorded.target_identity != binding.target_identity
                    or recorded.marker_directory_identity != binding.marker_directory_identity):
                raise WindowsContentError("WINDOWS_CONTENT_IDENTITY_CHANGED")
        self._write(path, {"schema": OWNERSHIP_SCHEMA, "backend": _BACKEND, "binding": asdict(binding)})

    def begin_effect(self, binding: WindowsContentBinding, *, operation: Literal["install", "verify"]) -> WindowsEffectReceipt:
        if operation not in {"install", "verify"}:
            raise WindowsContentError("WINDOWS_CONTENT_OPERATION_UNSUPPORTED")
        self.revalidate(binding)
        path = self._effect_path(binding.operation_id)
        if self.filesystem.native.exists(path):
            # Prepared/uncertain means a writer may have started. Never turn
            # marker reuse into permission to spawn another job.
            self.inspect_effect(binding)
            raise WindowsContentError("WINDOWS_CONTENT_EFFECT_ALREADY_RECORDED")
        receipt = WindowsEffectReceipt(EFFECT_SCHEMA, _BACKEND, operation, binding, "prepared")
        self._write(path, asdict(receipt))
        return receipt

    def inspect_effect(self, binding: WindowsContentBinding) -> WindowsEffectReceipt:
        self.revalidate(binding)
        raw = self._read(self._effect_path(binding.operation_id))
        if set(raw) != {"schema", "backend", "operation", "binding", "state"} or raw.get("schema") != EFFECT_SCHEMA or raw.get("backend") != _BACKEND:
            raise WindowsContentError("WINDOWS_CONTENT_MARKER_UNSUPPORTED")
        recorded = _binding(raw["binding"])
        operation, state = raw.get("operation"), raw.get("state")
        if recorded != binding or operation not in {"install", "verify"} or state not in {"prepared", "completed", "uncertain"}:
            raise WindowsContentError("WINDOWS_CONTENT_MARKER_INVALID")
        return WindowsEffectReceipt(EFFECT_SCHEMA, _BACKEND, operation, recorded, state)

    def finish_effect(self, binding: WindowsContentBinding, *, result: WindowsContentResult) -> WindowsEffectReceipt:
        if not isinstance(result, WindowsContentResult) or result.process_backend != "windows-job/1":
            raise WindowsContentError("WINDOWS_CONTENT_JOB_RESULT_REQUIRED")
        receipt = self.inspect_effect(binding)
        if receipt.state != "prepared":
            raise WindowsContentError("WINDOWS_CONTENT_EFFECT_TERMINAL")
        if (
            type(result.appid) is not int or result.appid != binding.appid
            or type(result.operation_id) is not int or result.operation_id != binding.operation_id
            or result.operation != receipt.operation
            or result.target_identity != binding.target_identity
        ):
            raise WindowsContentError("WINDOWS_CONTENT_JOB_RESULT_UNBOUND")
        state: Literal["completed", "uncertain"] = "completed" if result.outcome == "installed" else "uncertain"
        finished = WindowsEffectReceipt(EFFECT_SCHEMA, _BACKEND, receipt.operation, binding, state)
        self._write(self._effect_path(binding.operation_id), asdict(finished))
        return finished

    def adopt(self, binding: WindowsContentBinding, *, source: Path) -> Path:
        if self.inspect_effect(binding).state != "completed":
            raise WindowsContentError("WINDOWS_CONTENT_EFFECT_UNCONFIRMED")
        expected = (self._library / "steamapps" / "common" / binding.install_dir_name
                    / "steamapps" / f"appmanifest_{binding.appid}.acf")
        if validate_path(source) != expected:
            raise WindowsContentError("WINDOWS_CONTENT_SOURCE_UNBOUND")
        return self.filesystem.adopt_manifest(
            source=source, library=self.library, appid=binding.appid,
            install_dir_name=binding.install_dir_name, journal_dir=self._journal,
            operation_id=binding.operation_id,
        )

    def reconcile_adoption(self, binding: WindowsContentBinding) -> str:
        self.revalidate(binding)
        return self.filesystem.reconcile_adoption(
            library=self.library, appid=binding.appid, journal_dir=self._journal,
            operation_id=binding.operation_id,
        )

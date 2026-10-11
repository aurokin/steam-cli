"""Local NTFS identities and journaled Windows manifest adoption.

This backend uses flushed file writes and journal-first recovery from process
interruption. It does not claim POSIX directory-fsync equivalence or qualified
power-loss durability. Paths are ordinary absolute drive paths; reparse points,
remote volumes, named streams, device paths and multiply-linked files are out
of scope. Checks protect against accidental replacement, not a hostile owner
racing every pathname API.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, replace
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
from typing import Callable, Iterator, ParamSpec, Protocol, TypeVar

from steam_agent.execution.content_plane import AdoptionError


JOURNAL_SCHEMA = "windows-adoption/1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_JOURNAL_BYTES = 32 * 1024
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?\Z", re.IGNORECASE)
_CHECKSUM = re.compile(r"[0-9a-f]{64}\Z")
_FILE_ID = re.compile(r"[0-9a-f]{32}\Z")


class WindowsFilesystemError(AdoptionError):
    """Fixed, path-free diagnostics from the Windows filesystem boundary."""

    def __init__(self, code: str = "WINDOWS_FILESYSTEM_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WindowsFileIdentity:
    volume_serial: int
    file_id: str


def safe_component(value: str) -> bool:
    """Accept one ordinary Windows filename component, including Unicode."""

    return bool(
        isinstance(value, str)
        and value not in {"", ".", ".."}
        and not value.endswith((" ", "."))
        and not any(ord(c) < 32 or c in '<>:"/\\|?*' for c in value)
        and not _RESERVED.fullmatch(value)
        and len(value.encode("utf-16-le", errors="surrogatepass")) <= 510
        and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)
    )


def validate_path(path: str | os.PathLike[str]) -> PureWindowsPath:
    """Reject ambiguous lexical paths before calling any native API."""

    value = os.fspath(path)
    if not isinstance(value, str) or not re.match(r"^[A-Za-z]:[\\/]", value):
        raise WindowsFilesystemError("WINDOWS_PATH_UNSUPPORTED")
    raw_parts = value[3:].replace("/", "\\").split("\\")
    if raw_parts == [""]:
        raw_parts = []
    if any(not safe_component(part) for part in raw_parts) or len(value) >= 260:
        raise WindowsFilesystemError("WINDOWS_PATH_UNSUPPORTED")
    return PureWindowsPath(value)


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _sanitized(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return function(*args, **kwargs)
        except WindowsFilesystemError:
            raise
        except Exception:
            raise WindowsFilesystemError() from None

    return wrapped


class WindowsFilePrimitives(Protocol):
    """Narrow injectable boundary for this adoption protocol only."""

    def identity(self, path: PureWindowsPath, *, directory: bool) -> WindowsFileIdentity: ...
    def exists(self, path: PureWindowsPath) -> bool: ...
    def read(self, path: PureWindowsPath, *, limit: int) -> bytes: ...
    def write_new(self, path: PureWindowsPath, data: bytes) -> None: ...
    def copy_new(self, source: PureWindowsPath, destination: PureWindowsPath) -> None: ...
    def flush(self, path: PureWindowsPath) -> None: ...
    def move_new(self, source: PureWindowsPath, destination: PureWindowsPath) -> None: ...
    def replace(self, source: PureWindowsPath, destination: PureWindowsPath) -> None: ...
    def delete(self, path: PureWindowsPath) -> None: ...


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_uint32), ("ReparseTag", ctypes.c_uint32)]


class _StandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_int64), ("EndOfFile", ctypes.c_int64),
        ("NumberOfLinks", ctypes.c_uint32), ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


class NativeWindowsFiles:
    """Win32 handle inspection, bounded IO, flush, copy and same-volume swaps.

    ReplaceFileW uses flags=0 to require ACL/attribute merging; its unsupported
    WRITE_THROUGH flag is never supplied. MoveFileExW never uses COPY_ALLOWED,
    delayed reboot, or an elevation fallback. All failed APIs fail closed.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsFilesystemError("WINDOWS_FILESYSTEM_UNSUPPORTED")
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        api = self._api
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        api.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.GetFileInformationByHandleEx.restype = wintypes.BOOL
        api.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        api.GetVolumeInformationByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
        api.GetVolumeInformationByHandleW.restype = wintypes.BOOL
        api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        api.GetDriveTypeW.restype = wintypes.UINT
        api.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        api.GetFileAttributesW.restype = wintypes.DWORD
        for name in ("ReadFile", "WriteFile"):
            function = getattr(api, name)
            function.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            function.restype = wintypes.BOOL
        api.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        api.FlushFileBuffers.restype = wintypes.BOOL
        api.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        api.MoveFileExW.restype = wintypes.BOOL
        api.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        api.ReplaceFileW.restype = wintypes.BOOL
        api.CopyFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.BOOL]
        api.CopyFileW.restype = wintypes.BOOL
        api.DeleteFileW.argtypes = [wintypes.LPCWSTR]
        api.DeleteFileW.restype = wintypes.BOOL

    @staticmethod
    def _name(path: PureWindowsPath) -> str:
        return "\\\\?\\" + str(path)

    @contextmanager
    def _open(self, path: PureWindowsPath, *, write: bool = False, create: bool = False) -> Iterator[int]:
        handle = self._api.CreateFileW(
            self._name(path), 0x80000000 | (0x40000000 if write else 0),
            0 if write else 7, None, 1 if create else 3,
            0x00200000 | 0x02000000 | (0x80000000 if write else 0), None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise WindowsFilesystemError()
        try:
            yield handle
        finally:
            self._api.CloseHandle(handle)

    def _inspect(self, handle: int, path: PureWindowsPath, *, directory: bool) -> WindowsFileIdentity:
        attributes = _AttributeTagInfo()
        identity = _FileIdInfo()
        standard = _StandardInfo()
        for kind, value in ((9, attributes), (18, identity), (1, standard)):
            if not self._api.GetFileInformationByHandleEx(handle, kind, ctypes.byref(value), ctypes.sizeof(value)):
                raise WindowsFilesystemError()
        if attributes.FileAttributes & 0x400 or attributes.ReparseTag:
            raise WindowsFilesystemError("WINDOWS_REPARSE_POINT_REFUSED")
        if bool(standard.Directory) != directory or standard.DeletePending:
            raise WindowsFilesystemError("WINDOWS_FILE_TYPE_UNSUPPORTED")
        if not directory and standard.NumberOfLinks != 1:
            raise WindowsFilesystemError("WINDOWS_HARDLINK_REFUSED")
        filesystem = ctypes.create_unicode_buffer(261)
        if not self._api.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, filesystem, len(filesystem)):
            raise WindowsFilesystemError()
        if filesystem.value.upper() != "NTFS" or self._api.GetDriveTypeW(path.anchor) != 3:
            raise WindowsFilesystemError("WINDOWS_VOLUME_UNSUPPORTED")
        final = ctypes.create_unicode_buffer(32768)
        size = self._api.GetFinalPathNameByHandleW(handle, final, len(final), 0)
        if not size or size >= len(final) or final.value.casefold() != self._name(path).casefold():
            raise WindowsFilesystemError("WINDOWS_FINAL_PATH_MISMATCH")
        return WindowsFileIdentity(identity.VolumeSerialNumber, bytes(identity.FileId).hex())

    def _parents(self, path: PureWindowsPath) -> None:
        for parent in reversed(path.parents):
            with self._open(parent) as handle:
                self._inspect(handle, parent, directory=True)

    @_sanitized
    def identity(self, path: PureWindowsPath, *, directory: bool) -> WindowsFileIdentity:
        path = validate_path(path)
        self._parents(path)
        with self._open(path) as handle:
            return self._inspect(handle, path, directory=directory)

    @_sanitized
    def exists(self, path: PureWindowsPath) -> bool:
        path = validate_path(path)
        attributes = self._api.GetFileAttributesW(self._name(path))
        if attributes != 0xFFFFFFFF:
            # Existence does not certify safety; subsequent identity/read does.
            return True
        if ctypes.get_last_error() in (2, 3):
            return False
        raise WindowsFilesystemError()

    @_sanitized
    def read(self, path: PureWindowsPath, *, limit: int) -> bytes:
        self._parents(path)
        with self._open(path) as handle:
            self._inspect(handle, path, directory=False)
            data = ctypes.create_string_buffer(limit + 1)
            count = wintypes.DWORD()
            if not self._api.ReadFile(handle, data, limit + 1, ctypes.byref(count), None) or count.value > limit:
                raise WindowsFilesystemError("WINDOWS_FILE_READ_FAILED")
            return data.raw[:count.value]

    @_sanitized
    def write_new(self, path: PureWindowsPath, data: bytes) -> None:
        self._parents(path)
        with self._open(path, write=True, create=True) as handle:
            self._inspect(handle, path, directory=False)
            written = wintypes.DWORD()
            payload = ctypes.create_string_buffer(data)
            if not self._api.WriteFile(handle, payload, len(data), ctypes.byref(written), None) or written.value != len(data):
                raise WindowsFilesystemError("WINDOWS_FILE_WRITE_FAILED")
            if not self._api.FlushFileBuffers(handle):
                raise WindowsFilesystemError("WINDOWS_FILE_FLUSH_FAILED")

    @_sanitized
    def flush(self, path: PureWindowsPath) -> None:
        self._parents(path)
        with self._open(path, write=True) as handle:
            self._inspect(handle, path, directory=False)
            if not self._api.FlushFileBuffers(handle):
                raise WindowsFilesystemError("WINDOWS_FILE_FLUSH_FAILED")

    def _same_volume(self, source: PureWindowsPath, destination: PureWindowsPath) -> None:
        source_id = self.identity(source, directory=False)
        parent_id = self.identity(destination.parent, directory=True)
        if source_id.volume_serial != parent_id.volume_serial:
            raise WindowsFilesystemError("WINDOWS_CROSS_VOLUME_SWAP_REFUSED")
        if self.exists(destination):
            self.identity(destination, directory=False)

    @_sanitized
    def copy_new(self, source: PureWindowsPath, destination: PureWindowsPath) -> None:
        self._same_volume(source, destination)
        if not self._api.CopyFileW(self._name(source), self._name(destination), True):
            raise WindowsFilesystemError("WINDOWS_FILE_COPY_FAILED")
        # Windows 10/11 CopyFileW preserves security properties. Inspect and
        # flush the copied file before its checksum is journaled.
        self.flush(destination)

    @_sanitized
    def move_new(self, source: PureWindowsPath, destination: PureWindowsPath) -> None:
        self._same_volume(source, destination)
        if self.exists(destination):
            raise WindowsFilesystemError("WINDOWS_DESTINATION_EXISTS")
        if not self._api.MoveFileExW(self._name(source), self._name(destination), 8):
            raise WindowsFilesystemError("WINDOWS_FILE_MOVE_FAILED")
        self.flush(destination)

    @_sanitized
    def replace(self, source: PureWindowsPath, destination: PureWindowsPath) -> None:
        self._same_volume(source, destination)
        if not self.exists(destination):
            raise WindowsFilesystemError("WINDOWS_DESTINATION_MISSING")
        if not self._api.ReplaceFileW(self._name(destination), self._name(source), None, 0, None, None):
            # Even failed ReplaceFileW can rename/remove the destination.
            # The caller retains independent backup plus its pending journal.
            raise WindowsFilesystemError("WINDOWS_FILE_REPLACE_FAILED")
        self.flush(destination)

    @_sanitized
    def delete(self, path: PureWindowsPath) -> None:
        if not self.exists(path):
            return
        self.identity(path, directory=False)
        if not self._api.DeleteFileW(self._name(path)):
            raise WindowsFilesystemError("WINDOWS_FILE_DELETE_FAILED")


@dataclass(frozen=True, slots=True)
class _AdoptionRecord:
    schema: str
    appid: int
    operation_id: int
    checksum: str
    steamapps_identity: WindowsFileIdentity
    journal_identity: WindowsFileIdentity
    stage_identity: WindowsFileIdentity
    original_identity: WindowsFileIdentity | None
    backup_identity: WindowsFileIdentity | None
    backup_checksum: str | None
    restore_identity: WindowsFileIdentity | None


def _decode_identity(value: object) -> WindowsFileIdentity:
    if not isinstance(value, dict) or set(value) != {"volume_serial", "file_id"}:
        raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID")
    serial = value["volume_serial"]
    file_id = value["file_id"]
    if type(serial) is not int or not 0 <= serial <= (1 << 64) - 1 or not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
        raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID")
    return WindowsFileIdentity(serial, file_id)


def _decode_record(payload: bytes) -> _AdoptionRecord:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict) or raw.get("schema") != JOURNAL_SCHEMA:
            raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_UNSUPPORTED")
        if set(raw) != set(_AdoptionRecord.__dataclass_fields__):
            raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID")
        if type(raw["appid"]) is not int or not 1 <= raw["appid"] <= 0xFFFFFFFF or type(raw["operation_id"]) is not int or raw["operation_id"] <= 0:
            raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID")
        if not isinstance(raw["checksum"], str) or not _CHECKSUM.fullmatch(raw["checksum"]):
            raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID")
        for name in ("steamapps_identity", "journal_identity", "stage_identity"):
            raw[name] = _decode_identity(raw[name])
        for name in ("original_identity", "backup_identity", "restore_identity"):
            raw[name] = None if raw[name] is None else _decode_identity(raw[name])
        backup = raw["backup_checksum"]
        if backup is not None and (not isinstance(backup, str) or not _CHECKSUM.fullmatch(backup)):
            raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID")
        if (raw["original_identity"] is None) != (raw["backup_identity"] is None) or (raw["backup_identity"] is None) != (backup is None):
            raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID")
        return _AdoptionRecord(**raw)
    except WindowsFilesystemError:
        raise
    except Exception:
        raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID") from None


def _patch_manifest(payload: bytes, *, appid: int, install_dir_name: str) -> bytes:
    text = payload.decode("utf-8")
    matches = re.findall(r'"appid"\s+"(\d+)"', text)
    if matches != [str(appid)] or not safe_component(install_dir_name):
        raise WindowsFilesystemError("WINDOWS_MANIFEST_INVALID")
    patched, count = re.subn(r'("installdir"\s+")[^"]*(")', lambda m: m[1] + install_dir_name + m[2], text)
    if count != 1:
        raise WindowsFilesystemError("WINDOWS_MANIFEST_INVALID")
    return patched.encode("utf-8")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class WindowsFilesystem:
    """Dedicated manifest driver with injectable Win32 primitives.

    Callers create/provision the journal and library directories beforehand and
    serialize adoption through the existing execution lock. A journal belongs
    to one immutable operation; a foreign operation never removes its evidence.
    """

    def __init__(self, native: WindowsFilePrimitives | None = None) -> None:
        self.native = native if native is not None else NativeWindowsFiles()

    safe_component = staticmethod(safe_component)

    @_sanitized
    def identity(self, path: Path, *, directory: bool = True) -> WindowsFileIdentity:
        return self.native.identity(validate_path(path), directory=directory)

    @staticmethod
    def _paths(library: Path, appid: int, journal_dir: Path):
        if type(appid) is not int or not 1 <= appid <= 0xFFFFFFFF:
            raise WindowsFilesystemError("WINDOWS_MANIFEST_INVALID")
        steamapps = validate_path(library) / "steamapps"
        journals = validate_path(journal_dir)
        return steamapps, journals, steamapps / f"appmanifest_{appid}.acf", journals / f"windows-adoption-{appid}.json"

    def _reject_legacy(self, journals: PureWindowsPath, appid: int) -> None:
        if self.native.exists(journals / f"adoption-{appid}.json"):
            raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_UNSUPPORTED")

    @staticmethod
    def _artifacts(steamapps: PureWindowsPath, appid: int, operation_id: int):
        stem = f".steam-agent-adoption-{appid}-{operation_id}"
        return steamapps / (stem + ".stage"), steamapps / (stem + ".backup"), steamapps / (stem + ".restore")

    def _check_roots(self, steamapps: PureWindowsPath, journals: PureWindowsPath, record: _AdoptionRecord) -> None:
        if self.native.identity(steamapps, directory=True) != record.steamapps_identity or self.native.identity(journals, directory=True) != record.journal_identity:
            raise WindowsFilesystemError("WINDOWS_ADOPTION_IDENTITY_CHANGED")

    @_sanitized
    def adopt_manifest(self, *, source: Path, library: Path, appid: int, install_dir_name: str, journal_dir: Path, operation_id: int) -> Path:
        steamapps, journals, destination, journal = self._paths(library, appid, journal_dir)
        if type(operation_id) is not int or operation_id <= 0:
            raise WindowsFilesystemError("WINDOWS_MANIFEST_INVALID")
        self._reject_legacy(journals, appid)
        roots = (self.native.identity(steamapps, directory=True), self.native.identity(journals, directory=True))
        stage, backup, restore = self._artifacts(steamapps, appid, operation_id)
        intent = journals / f".windows-adoption-{appid}-{operation_id}.intent"
        if any(self.native.exists(path) for path in (journal, stage, backup, restore, intent)):
            raise WindowsFilesystemError("WINDOWS_ADOPTION_ALREADY_PENDING")
        patched = _patch_manifest(self.native.read(validate_path(source), limit=MAX_MANIFEST_BYTES), appid=appid, install_dir_name=install_dir_name)
        original_id = None
        backup_id = None
        backup_checksum = None
        if self.native.exists(destination):
            original_id = self.native.identity(destination, directory=False)
            self.native.copy_new(destination, backup)
            backup_id = self.native.identity(backup, directory=False)
            backup_checksum = _digest(self.native.read(backup, limit=MAX_MANIFEST_BYTES))
        self.native.write_new(stage, patched)
        stage_id = self.native.identity(stage, directory=False)
        if stage_id.volume_serial != roots[0].volume_serial or (backup_id is not None and backup_id.volume_serial != roots[0].volume_serial):
            raise WindowsFilesystemError("WINDOWS_CROSS_VOLUME_SWAP_REFUSED")
        record = _AdoptionRecord(JOURNAL_SCHEMA, appid, operation_id, _digest(patched), *roots, stage_id, original_id, backup_id, backup_checksum, None)
        self.native.write_new(intent, json.dumps(asdict(record), sort_keys=True, separators=(",", ":")).encode())
        self.native.move_new(intent, journal)
        self._check_roots(steamapps, journals, record)
        if original_id is not None:
            if self.native.identity(destination, directory=False) != original_id:
                raise WindowsFilesystemError("WINDOWS_ADOPTION_IDENTITY_CHANGED")
            self.native.replace(stage, destination)
        else:
            self.native.move_new(stage, destination)
        if self.native.identity(destination, directory=False) != stage_id or _digest(self.native.read(destination, limit=MAX_MANIFEST_BYTES)) != record.checksum:
            raise WindowsFilesystemError("WINDOWS_ADOPTION_VERIFY_FAILED")
        return Path(str(destination))

    @_sanitized
    def reconcile_adoption(self, *, library: Path, appid: int, journal_dir: Path, operation_id: int | None = None) -> str:
        steamapps, journals, destination, journal = self._paths(library, appid, journal_dir)
        self._reject_legacy(journals, appid)
        if not self.native.exists(journal):
            return "clean"
        record = _decode_record(self.native.read(journal, limit=_MAX_JOURNAL_BYTES))
        if record.appid != appid:
            raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID")
        self._check_roots(steamapps, journals, record)
        if operation_id is not None and record.operation_id != operation_id:
            return "stale"
        stage, backup, restore = self._artifacts(steamapps, appid, record.operation_id)
        current_id = None
        if self.native.exists(destination):
            current_id = self.native.identity(destination, directory=False)
            payload = self.native.read(destination, limit=MAX_MANIFEST_BYTES)
            if current_id == record.stage_identity and _digest(payload) == record.checksum:
                self.native.flush(destination)
                return "completed"
            if record.restore_identity is not None and current_id == record.restore_identity and _digest(payload) == record.backup_checksum:
                self.native.flush(destination)
                self.native.delete(journal)
                return "restored"
            if current_id not in (record.original_identity, record.stage_identity, record.restore_identity):
                raise WindowsFilesystemError("WINDOWS_ADOPTION_IDENTITY_CHANGED")
        if record.backup_identity is not None:
            if not self.native.exists(backup) or self.native.identity(backup, directory=False) != record.backup_identity or _digest(self.native.read(backup, limit=MAX_MANIFEST_BYTES)) != record.backup_checksum:
                raise WindowsFilesystemError("WINDOWS_ADOPTION_BACKUP_INVALID")
            if self.native.exists(restore):
                # A prior recovery may have died before its swap. Only accept
                # exact backup bytes; an unrelated artifact is never reused.
                if _digest(self.native.read(restore, limit=MAX_MANIFEST_BYTES)) != record.backup_checksum:
                    raise WindowsFilesystemError("WINDOWS_ADOPTION_BACKUP_INVALID")
            else:
                self.native.copy_new(backup, restore)
            restore_id = self.native.identity(restore, directory=False)
            record = replace(record, restore_identity=restore_id)
            recovery_intent = journals / f".windows-adoption-{appid}-{record.operation_id}.recovery"
            encoded = json.dumps(asdict(record), sort_keys=True, separators=(",", ":")).encode()
            if self.native.exists(recovery_intent):
                if self.native.read(recovery_intent, limit=_MAX_JOURNAL_BYTES) != encoded:
                    raise WindowsFilesystemError("WINDOWS_ADOPTION_JOURNAL_INVALID")
            else:
                self.native.write_new(recovery_intent, encoded)
            self.native.replace(recovery_intent, journal)
            self._check_roots(steamapps, journals, record)
            if current_id is None:
                self.native.move_new(restore, destination)
            else:
                self.native.replace(restore, destination)
            if _digest(self.native.read(destination, limit=MAX_MANIFEST_BYTES)) != record.backup_checksum:
                raise WindowsFilesystemError("WINDOWS_ADOPTION_VERIFY_FAILED")
        elif current_id is not None:
            # A fresh destination is removable only with the staged file ID.
            if current_id != record.stage_identity:
                raise WindowsFilesystemError("WINDOWS_ADOPTION_IDENTITY_CHANGED")
            self.native.delete(destination)
        self.native.delete(journal)
        # Retain backup/stage artifacts as conservative interruption evidence.
        return "restored"

    @_sanitized
    def clear_adoption_journal(self, *, appid: int, journal_dir: Path) -> None:
        journals = validate_path(journal_dir)
        self._reject_legacy(journals, appid)
        journal = journals / f"windows-adoption-{appid}.json"
        if not self.native.exists(journal):
            return
        record = _decode_record(self.native.read(journal, limit=_MAX_JOURNAL_BYTES))
        if record.appid != appid or self.native.identity(journals, directory=True) != record.journal_identity:
            raise WindowsFilesystemError("WINDOWS_ADOPTION_IDENTITY_CHANGED")
        self.native.delete(journal)


# Keyword signatures match content_plane so the executor chooses one driver.
def adopt_manifest(
    *, source: Path, library: Path, appid: int, install_dir_name: str,
    journal_dir: Path, operation_id: int,
) -> Path:
    return WindowsFilesystem().adopt_manifest(
        source=source, library=library, appid=appid, install_dir_name=install_dir_name,
        journal_dir=journal_dir, operation_id=operation_id,
    )


def reconcile_adoption(
    *, library: Path, appid: int, journal_dir: Path, operation_id: int | None = None,
) -> str:
    return WindowsFilesystem().reconcile_adoption(
        library=library, appid=appid, journal_dir=journal_dir, operation_id=operation_id,
    )


def clear_adoption_journal(*, appid: int, journal_dir: Path) -> None:
    WindowsFilesystem().clear_adoption_journal(appid=appid, journal_dir=journal_dir)

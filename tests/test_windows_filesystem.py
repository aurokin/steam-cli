from dataclasses import dataclass
import json
import os
from pathlib import Path, PureWindowsPath

import pytest

from steam_agent.execution.windows_filesystem import (
    JOURNAL_SCHEMA,
    WindowsFileIdentity,
    WindowsFilesystem,
    WindowsFilesystemError,
    safe_component,
    validate_path,
)


class Interrupted(BaseException):
    pass


@dataclass
class File:
    data: bytes
    identity: WindowsFileIdentity
    directory: bool = False


class Native:
    """In-memory, injected Win32 boundary with interruption cut points."""

    def __init__(self) -> None:
        self.files: dict[PureWindowsPath, File] = {}
        self.serial = 1
        self.events: list[tuple[str, PureWindowsPath]] = []
        self.fail_before: tuple[str, PureWindowsPath] | None = None
        self.fail_after: tuple[str, PureWindowsPath] | None = None
        self.remove_on_replace_failure = False
        self.next_id = 0

    def add(self, path: PureWindowsPath, data: bytes = b"", *, directory: bool = False) -> None:
        self.next_id += 1
        self.files[path] = File(data, WindowsFileIdentity(self.serial, f"{self.next_id:032x}"), directory)

    def _before(self, action: str, path: PureWindowsPath) -> None:
        self.events.append((action, path))
        if self.fail_before == (action, path):
            self.fail_before = None
            raise Interrupted()

    def _after(self, action: str, path: PureWindowsPath) -> None:
        if self.fail_after == (action, path):
            self.fail_after = None
            raise Interrupted()

    def identity(self, path: PureWindowsPath, *, directory: bool) -> WindowsFileIdentity:
        file = self.files[path]
        if file.directory != directory:
            raise WindowsFilesystemError("WINDOWS_FILE_TYPE_UNSUPPORTED")
        return file.identity

    def exists(self, path: PureWindowsPath) -> bool:
        return path in self.files

    def read(self, path: PureWindowsPath, *, limit: int) -> bytes:
        data = self.files[path].data
        if len(data) > limit:
            raise WindowsFilesystemError("WINDOWS_FILE_READ_FAILED")
        return data

    def write_new(self, path: PureWindowsPath, data: bytes) -> None:
        self._before("write", path)
        if self.exists(path):
            raise WindowsFilesystemError("WINDOWS_DESTINATION_EXISTS")
        self.add(path, data)
        self.flush(path)
        self._after("write", path)

    def flush(self, path: PureWindowsPath) -> None:
        self._before("flush", path)
        assert self.exists(path)
        self._after("flush", path)

    def copy_new(self, source: PureWindowsPath, destination: PureWindowsPath) -> None:
        self._before("copy", destination)
        self.write_new(destination, self.files[source].data)
        self._after("copy", destination)

    def move_new(self, source: PureWindowsPath, destination: PureWindowsPath) -> None:
        self._before("move", destination)
        if self.exists(destination):
            raise WindowsFilesystemError("WINDOWS_DESTINATION_EXISTS")
        self.files[destination] = self.files.pop(source)
        self.flush(destination)
        self._after("move", destination)

    def replace(self, source: PureWindowsPath, destination: PureWindowsPath) -> None:
        self._before("replace", destination)
        if self.remove_on_replace_failure and destination.name.startswith("appmanifest_"):
            self.remove_on_replace_failure = False
            del self.files[destination]
            raise WindowsFilesystemError("WINDOWS_FILE_REPLACE_FAILED")
        self.files[destination] = self.files.pop(source)
        self.flush(destination)
        self._after("replace", destination)

    def delete(self, path: PureWindowsPath) -> None:
        self._before("delete", path)
        self.files.pop(path, None)
        self._after("delete", path)


LIBRARY = PureWindowsPath("C:/fixture/library")
JOURNALS = PureWindowsPath("C:/fixture/journals")
SOURCE = PureWindowsPath("C:/fixture/source.acf")
STEAMAPPS = LIBRARY / "steamapps"
DESTINATION = STEAMAPPS / "appmanifest_10.acf"
JOURNAL = JOURNALS / "windows-adoption-10.json"
BACKUP = STEAMAPPS / ".steam-agent-adoption-10-7.backup"
OLD = b'"AppState" { "appid" "10" "installdir" "Old" "StateFlags" "4" }'
NEW = b'"AppState" { "appid" "10" "installdir" "Source" "StateFlags" "4" }'


def setup(*, existing: bool = False):
    native = Native()
    native.add(STEAMAPPS, directory=True)
    native.add(JOURNALS, directory=True)
    native.add(SOURCE, NEW)
    if existing:
        native.add(DESTINATION, OLD)
    return WindowsFilesystem(native), native


def adopt(driver: WindowsFilesystem):
    return driver.adopt_manifest(
        source=Path(str(SOURCE)), library=Path(str(LIBRARY)), appid=10,
        install_dir_name="Game 雪", journal_dir=Path(str(JOURNALS)), operation_id=7,
    )


def reconcile(driver: WindowsFilesystem, *, operation_id: int = 7):
    return driver.reconcile_adoption(
        library=Path(str(LIBRARY)), appid=10, journal_dir=Path(str(JOURNALS)), operation_id=operation_id,
    )


@pytest.mark.parametrize("component", ["Game", "Game 雪", "two words", "x.y", "COM10"])
def test_ordinary_windows_component_is_accepted(component: str) -> None:
    assert safe_component(component)


@pytest.mark.parametrize("component", ["", ".", "..", "CON", "con.txt", "NUL", "COM1", "LPT².txt", "a.", "a ", "a:b", "a/b", "a\\b", 'a"b', "a|b", "a?b", "a*b", "a\x00b", "\ud800", "a" * 256])
def test_ambiguous_windows_component_is_refused(component: str) -> None:
    assert not safe_component(component)


@pytest.mark.parametrize("path", ["relative", "C:relative", "\\rooted", "//server/share/file", "\\\\?\\C:\\fixture", "\\\\.\\C:", "C:/fixture/../file", "C:/fixture/./file", "C:/fixture//file", "C:/file:stream", "C:/NUL.txt", "C:/trailing.", "C:/trailing "])
def test_remote_device_ads_or_ambiguous_path_is_refused(path: str) -> None:
    with pytest.raises(WindowsFilesystemError, match="WINDOWS_PATH_UNSUPPORTED"):
        validate_path(path)


def test_valid_local_absolute_paths_are_lexically_normalized() -> None:
    assert validate_path("C:/fixture/Game 雪") == PureWindowsPath("C:/fixture/Game 雪")
    assert validate_path("C:/") == PureWindowsPath("C:/")


@pytest.mark.parametrize("existing", [False, True])
def test_adoption_is_journal_first_destination_volume_and_exact(existing: bool) -> None:
    driver, native = setup(existing=existing)
    result = adopt(driver)
    assert result == Path(str(DESTINATION))
    assert b'"installdir" "Game ' in native.files[DESTINATION].data
    record = json.loads(native.files[JOURNAL].data)
    assert record["schema"] == JOURNAL_SCHEMA
    assert record["operation_id"] == 7
    swap = "replace" if existing else "move"
    assert native.events.index(("move", JOURNAL)) < native.events.index((swap, DESTINATION))
    if existing:
        assert native.files[BACKUP].data == OLD
        assert native.events.index(("flush", BACKUP)) < native.events.index(("move", JOURNAL))
    assert reconcile(driver) == "completed"
    assert JOURNAL in native.files  # Caller retires only after ledger transition.
    driver.clear_adoption_journal(appid=10, journal_dir=Path(str(JOURNALS)))
    assert reconcile(driver) == "clean"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("after_swap", [False, True])
def test_process_interruption_around_swap_reopens_without_replay(existing: bool, after_swap: bool) -> None:
    driver, native = setup(existing=existing)
    action = "replace" if existing else "move"
    if after_swap:
        native.fail_after = (action, DESTINATION)
    else:
        native.fail_before = (action, DESTINATION)
    with pytest.raises(Interrupted):
        adopt(driver)
    reopened = WindowsFilesystem(native)
    assert reconcile(reopened) == ("completed" if after_swap else "restored")
    if not after_swap:
        assert (native.files[DESTINATION].data if DESTINATION in native.files else None) == (OLD if existing else None)
    assert reconcile(reopened) == ("completed" if after_swap else "clean")


def test_replace_failure_that_removed_original_restores_independent_backup() -> None:
    driver, native = setup(existing=True)
    native.remove_on_replace_failure = True
    with pytest.raises(WindowsFilesystemError, match="WINDOWS_FILE_REPLACE_FAILED"):
        adopt(driver)
    assert DESTINATION not in native.files
    assert native.files[BACKUP].data == OLD
    assert reconcile(WindowsFilesystem(native)) == "restored"
    assert native.files[DESTINATION].data == OLD


def test_recovery_interruption_after_restore_swap_is_idempotent() -> None:
    driver, native = setup(existing=True)
    native.fail_before = ("replace", DESTINATION)
    with pytest.raises(Interrupted):
        adopt(driver)
    native.fail_after = ("replace", DESTINATION)
    with pytest.raises(Interrupted):
        reconcile(WindowsFilesystem(native))
    assert JOURNAL in native.files and native.files[DESTINATION].data == OLD
    assert reconcile(WindowsFilesystem(native)) == "restored"
    assert reconcile(WindowsFilesystem(native)) == "clean"


def test_foreign_operation_does_not_retire_journal() -> None:
    driver, native = setup()
    adopt(driver)
    events = list(native.events)
    assert reconcile(driver, operation_id=8) == "stale"
    assert JOURNAL in native.files and native.events == events


@pytest.mark.parametrize("replace_path", [STEAMAPPS, JOURNALS, DESTINATION])
def test_changed_handle_identity_defers_without_mutation(replace_path: PureWindowsPath) -> None:
    driver, native = setup()
    adopt(driver)
    previous = native.files[replace_path]
    native.add(replace_path, previous.data, directory=previous.directory)
    events = list(native.events)
    with pytest.raises(WindowsFilesystemError, match="WINDOWS_ADOPTION_IDENTITY_CHANGED"):
        reconcile(driver)
    assert native.events == events and JOURNAL in native.files


@pytest.mark.parametrize("corrupt", ["backup_missing", "backup_changed_id", "backup_changed_bytes"])
def test_unprovable_backup_never_overwrites_destination(corrupt: str) -> None:
    driver, native = setup(existing=True)
    native.fail_before = ("replace", DESTINATION)
    with pytest.raises(Interrupted):
        adopt(driver)
    if corrupt == "backup_missing":
        del native.files[BACKUP]
    elif corrupt == "backup_changed_id":
        native.add(BACKUP, OLD)
    else:
        native.files[BACKUP].data = b"changed"
    events = list(native.events)
    with pytest.raises(WindowsFilesystemError, match="WINDOWS_ADOPTION_BACKUP_INVALID"):
        reconcile(driver)
    assert native.events == events and native.files[DESTINATION].data == OLD


@pytest.mark.parametrize("payload", [b"{}", b'{"schema":"windows-adoption/999"}', b"not json", b'{"destination":"C:/foreign"}'])
def test_unsupported_or_malformed_record_is_refused(payload: bytes) -> None:
    driver, native = setup()
    native.add(JOURNAL, payload)
    with pytest.raises(WindowsFilesystemError):
        reconcile(driver)
    assert JOURNAL in native.files


def test_posix_journal_is_never_consumed_or_deleted() -> None:
    driver, native = setup()
    legacy = JOURNALS / "adoption-10.json"
    native.add(legacy, b"{}")
    with pytest.raises(WindowsFilesystemError, match="WINDOWS_ADOPTION_JOURNAL_UNSUPPORTED"):
        reconcile(driver)
    assert legacy in native.files


def test_native_exception_text_is_sanitized() -> None:
    driver, native = setup()

    def unavailable(path: PureWindowsPath, *, directory: bool):
        raise RuntimeError("private-canary C:/private/path")

    native.identity = unavailable
    with pytest.raises(WindowsFilesystemError) as error:
        adopt(driver)
    assert str(error.value) == "WINDOWS_FILESYSTEM_UNAVAILABLE"
    assert error.value.__cause__ is None


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows local NTFS")
def test_native_ntfs_identity_adoption_and_recovery(tmp_path: Path) -> None:
    library = tmp_path / "library 雪"
    journals = tmp_path / "journals"
    (library / "steamapps").mkdir(parents=True)
    journals.mkdir()
    source = tmp_path / "source.acf"
    source.write_bytes(NEW)
    destination = library / "steamapps" / "appmanifest_10.acf"
    destination.write_bytes(OLD)
    driver = WindowsFilesystem()
    identity = driver.identity(library / "steamapps")
    assert identity == driver.identity(library / "steamapps")
    assert len(identity.file_id) == 32
    driver.adopt_manifest(source=source, library=library, appid=10, install_dir_name="Game 雪", journal_dir=journals, operation_id=7)
    assert driver.reconcile_adoption(library=library, appid=10, journal_dir=journals, operation_id=7) == "completed"
    assert destination.read_bytes() != OLD
    driver.clear_adoption_journal(appid=10, journal_dir=journals)
    assert driver.reconcile_adoption(library=library, appid=10, journal_dir=journals) == "clean"


def test_native_open_and_inspection_use_nofollow_flags_and_handle_identity() -> None:
    import ctypes

    import steam_agent.execution.windows_filesystem as module

    class Api:
        def __init__(self) -> None:
            self.opened = None
            self.closed: list[int] = []

        def CreateFileW(self, *args):
            self.opened = args
            return 42

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return 1

        def GetFileInformationByHandleEx(self, handle, kind, pointer, size):
            assert handle == 42
            if kind == 9:
                value = ctypes.cast(pointer, ctypes.POINTER(module._AttributeTagInfo)).contents
                value.FileAttributes = 0x10
            elif kind == 18:
                value = ctypes.cast(pointer, ctypes.POINTER(module._FileIdInfo)).contents
                value.VolumeSerialNumber = 123
                value.FileId[0] = 7
            else:
                value = ctypes.cast(pointer, ctypes.POINTER(module._StandardInfo)).contents
                value.Directory = 1
                value.NumberOfLinks = 1
            return 1

        def GetVolumeInformationByHandleW(self, handle, name, count, serial, component, flags, filesystem, length):
            filesystem.value = "NTFS"
            return 1

        def GetDriveTypeW(self, root):
            assert root == "C:\\"
            return 3

        def GetFinalPathNameByHandleW(self, handle, result, length, flags):
            result.value = "\\\\?\\C:\\fixture"
            return len(result.value)

    api = Api()
    native = module.NativeWindowsFiles.__new__(module.NativeWindowsFiles)
    native._api = api
    path = PureWindowsPath("C:/fixture")
    with native._open(path) as handle:
        identity = native._inspect(handle, path, directory=True)
        assert identity.volume_serial == 123
        assert identity.file_id == "07" + "00" * 15
    assert api.closed == [42]
    assert api.opened[4] == 3  # OPEN_EXISTING, never CREATE_ALWAYS.
    assert api.opened[5] & 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT.
    assert api.opened[5] & 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS.


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows local NTFS")
def test_native_multiply_linked_file_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "source.acf"
    source.write_bytes(NEW)
    os.link(source, tmp_path / "second.acf")
    with pytest.raises(WindowsFilesystemError, match="WINDOWS_HARDLINK_REFUSED"):
        WindowsFilesystem().identity(source, directory=False)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows symlink permission")
def test_native_reparse_point_and_reparse_ancestor_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    child = target / "child.acf"
    child.write_bytes(NEW)
    link = tmp_path / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("native fixture lacks symlink privilege")
    driver = WindowsFilesystem()
    for path, directory in ((link, True), (link / "child.acf", False)):
        with pytest.raises(WindowsFilesystemError, match="WINDOWS_REPARSE_POINT_REFUSED"):
            driver.identity(path, directory=directory)

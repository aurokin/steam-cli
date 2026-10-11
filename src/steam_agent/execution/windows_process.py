"""Owned Windows process trees for content-backend qualification.

Creation assigns the child to a kill-on-close job atomically. Completion means
the entire non-breakaway job exited, not merely the executable's first process.
This primitive does not enable Windows Steam content execution by itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import ctypes
from pathlib import Path
import subprocess
import sys
import time
from typing import BinaryIO

from steam_agent.process_environment import child_environment

_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_SIZE = ctypes.c_size_t


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", _DWORD), ("reserved", ctypes.c_wchar_p),
        ("desktop", ctypes.c_wchar_p), ("title", ctypes.c_wchar_p),
        ("x", _DWORD), ("y", _DWORD), ("x_size", _DWORD), ("y_size", _DWORD),
        ("x_chars", _DWORD), ("y_chars", _DWORD), ("fill", _DWORD),
        ("flags", _DWORD), ("show", ctypes.c_uint16),
        ("reserved_count", ctypes.c_uint16), ("reserved_bytes", ctypes.c_void_p),
        ("stdin", _HANDLE), ("stdout", _HANDLE), ("stderr", _HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", _StartupInfo), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", _HANDLE), ("thread", _HANDLE), ("pid", _DWORD), ("tid", _DWORD)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", _DWORD), ("min_working", _SIZE), ("max_working", _SIZE),
        ("active_limit", _DWORD), ("affinity", _SIZE),
        ("priority", _DWORD), ("scheduling", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", _IoCounters),
        ("process_memory", _SIZE), ("job_memory", _SIZE),
        ("peak_process_memory", _SIZE), ("peak_job_memory", _SIZE),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("period_user", ctypes.c_int64), ("period_kernel", ctypes.c_int64),
        ("faults", _DWORD), ("total_processes", _DWORD),
        ("active_processes", _DWORD), ("terminated_processes", _DWORD),
    ]


class WindowsJobProcess:
    """A native executable and descendants owned by one non-inherited job handle."""

    def __init__(
        self, argv: Sequence[str], *, stdout: BinaryIO, stderr: BinaryIO,
        env: Mapping[str, str] | None = None, cwd: Path | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise OSError("Windows process backend unavailable")
        if not argv or not Path(argv[0]).is_absolute() or Path(argv[0]).suffix.lower() != ".exe":
            raise ValueError("content executable must be an absolute native executable")
        if any("\x00" in value for value in argv):
            raise ValueError("invalid content arguments")
        environment = child_environment(source=env)
        if any("\x00" in key + value or "=" in key for key, value in environment.items()):
            raise ValueError("invalid child environment")
        self.args = tuple(argv)
        self.returncode: int | None = None
        self._job: int | None = None
        self._process: int | None = None
        self._kernel = self._api()
        self.pid = self._create(stdout, stderr, environment, cwd)

    @staticmethod
    def _api():
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([_HANDLE, ctypes.c_wchar_p], _HANDLE),
            "SetInformationJobObject": ([_HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD], ctypes.c_int32),
            "QueryInformationJobObject": ([_HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD, ctypes.c_void_p], ctypes.c_int32),
            "TerminateJobObject": ([_HANDLE, ctypes.c_uint], ctypes.c_int32),
            "GetCurrentProcess": ([], _HANDLE),
            "DuplicateHandle": ([_HANDLE, _HANDLE, _HANDLE, ctypes.POINTER(_HANDLE), _DWORD, ctypes.c_int32, _DWORD], ctypes.c_int32),
            "CloseHandle": ([_HANDLE], ctypes.c_int32),
            "InitializeProcThreadAttributeList": ([ctypes.c_void_p, _DWORD, _DWORD, ctypes.POINTER(_SIZE)], ctypes.c_int32),
            "UpdateProcThreadAttribute": ([ctypes.c_void_p, _DWORD, _SIZE, ctypes.c_void_p, _SIZE, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int32),
            "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
            "CreateProcessW": ([ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, _DWORD, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(_ProcessInfo)], ctypes.c_int32),
            "GetExitCodeProcess": ([_HANDLE, ctypes.POINTER(_DWORD)], ctypes.c_int32),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes = arguments
            function.restype = result
        return kernel

    def _create(
        self, stdout: BinaryIO, stderr: BinaryIO,
        environment: Mapping[str, str], cwd: Path | None,
    ) -> int:
        import msvcrt

        kernel = self._kernel
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise OSError("owned job unavailable")
        inherited: list[int] = []
        initialized = False
        created = _ProcessInfo()
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway flags.
            if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError("owned job limits unavailable")
            with open("NUL", "rb") as stdin:
                current = kernel.GetCurrentProcess()
                for stream in (stdin, stdout, stderr):
                    target = _HANDLE()
                    if not kernel.DuplicateHandle(
                        current, msvcrt.get_osfhandle(stream.fileno()), current,
                        ctypes.byref(target), 0, True, 2,
                    ) or not target.value:
                        raise OSError("owned child stream unavailable")
                    inherited.append(target.value)
                size = _SIZE()
                kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
                if not size.value:
                    raise OSError("owned process attributes unavailable")
                attributes = ctypes.create_string_buffer(size.value)
                if not kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
                    raise OSError("owned process attributes unavailable")
                initialized = True
                jobs = (_HANDLE * 1)(job)
                streams = (_HANDLE * len(inherited))(*inherited)
                for attribute, values in ((0x2000D, jobs), (0x20002, streams)):
                    if not kernel.UpdateProcThreadAttribute(
                        attributes, 0, attribute, values, ctypes.sizeof(values), None, None,
                    ):
                        raise OSError("owned process assignment unavailable")
                startup = _StartupInfoEx()
                startup.startup.cb = ctypes.sizeof(startup)
                startup.startup.flags = 0x100  # USESTDHANDLES.
                startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = inherited
                startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
                command = ctypes.create_unicode_buffer(subprocess.list2cmdline(self.args))
                block = ctypes.create_unicode_buffer("\x00".join(
                    f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].upper())
                ) + "\x00\x00")
                if not kernel.CreateProcessW(
                    self.args[0], command, None, None, True,
                    0x80000 | 0x400 | 0x08000000, block,
                    None if cwd is None else str(cwd), ctypes.byref(startup), ctypes.byref(created),
                ):
                    raise OSError("owned content process unavailable")
                self._job, self._process = job, created.process
                return created.pid
        finally:
            if initialized:
                kernel.DeleteProcThreadAttributeList(attributes)
            for handle in inherited:
                kernel.CloseHandle(handle)
            if created.thread:
                kernel.CloseHandle(created.thread)
            if self._job is None:
                kernel.CloseHandle(job)
                if created.process:
                    kernel.CloseHandle(created.process)

    def active_count(self) -> int:
        if self._job is None:
            raise OSError("owned job is closed")
        accounting = _Accounting()
        if not self._kernel.QueryInformationJobObject(
            self._job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise OSError("owned job presence unknown")
        return accounting.active_processes

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None and timeout < 0:
            raise ValueError("invalid process wait")
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.active_count():
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.01)
        code = _DWORD()
        if self._process is None or not self._kernel.GetExitCodeProcess(self._process, ctypes.byref(code)):
            raise OSError("owned process result unavailable")
        self.returncode = code.value
        return self.returncode

    def kill(self) -> None:
        if self._job is None or not self._kernel.TerminateJobObject(self._job, 1):
            raise OSError("owned job termination unconfirmed")

    def close(self) -> None:
        for name in ("_job", "_process"):
            handle = getattr(self, name, None)
            if handle is not None:
                if not self._kernel.CloseHandle(handle):
                    # Retain ownership so cleanup can be retried and callers
                    # cannot mistake a surviving job for a confirmed close.
                    raise OSError("owned process handle closure unconfirmed")
                setattr(self, name, None)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

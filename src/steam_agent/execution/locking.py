"""File locks shared by broker intake and execution on POSIX and Windows."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import time
from typing import BinaryIO


class FileLockError(RuntimeError):
    """A lock is unavailable, unsafe, or held by another process."""


def acquire_file_lock(path: Path, *, blocking: bool = False) -> BinaryIO:
    """Return an open, locked handle; closing it releases the OS lock.

    Refuse symlinks, foreign ownership on POSIX, and replaced directory
    entries. Callers choose the stable lock path and create its parent.
    """

    try:
        if path.is_symlink():
            raise FileLockError("cannot open a symlink lock file")
        descriptor = os.open(
            path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except OSError as error:
        raise FileLockError("cannot open the lock file safely") from error
    handle = os.fdopen(descriptor, "r+b")
    try:
        opened = os.fstat(descriptor)
        if os.name == "nt":
            import msvcrt

            # Byte-range locks need a byte to lock. Handle close releases
            # the lock, matching flock's lifecycle without global state.
            if opened.st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if not blocking or error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    time.sleep(0.05)
        else:
            import fcntl

            if opened.st_uid not in (os.geteuid(), 0):
                raise FileLockError("lock file owned by another user")
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(handle, flags)
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise FileLockError("lock file was replaced concurrently")
    except OSError as error:
        handle.close()
        raise FileLockError("another operation holds the lock") from error
    except FileLockError:
        handle.close()
        raise
    return handle

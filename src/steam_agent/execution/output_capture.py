"""Drain owned child output into bounded memory, never raw disk spools.

Reader threads own their raw descriptors. A surviving writer may prevent EOF;
bounded joins report uncertainty rather than closing a descriptor in another
thread or claiming complete evidence. Broker death leaves only bounded OS pipes.
"""

from __future__ import annotations

from collections import deque
import os
import threading
import time
from typing import BinaryIO


class OutputCaptureError(OSError):
    """Capture failed or a writer still holds a pipe; diagnostic text is fixed."""


class _Stream:
    def __init__(self, limit: int) -> None:
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_size = 0
        self._total = 0
        self.failed = threading.Event()
        self._ready = threading.Event()
        self._abandoned = False
        reader, writer = os.pipe()
        handle: BinaryIO | None = None
        try:
            handle = os.fdopen(writer, "wb", buffering=0)
            self.writer = handle
            self._thread = threading.Thread(target=self._drain, args=(reader,), daemon=True)
            self._thread.start()
        except BaseException:
            # The reader cannot touch its descriptor until setup commits. Even
            # interrupted thread startup can safely abandon both descriptors.
            self._abandoned = True
            try:
                if handle is None:
                    os.close(writer)
                else:
                    handle.close()
            except Exception:
                pass
            try:
                os.close(reader)
            except OSError:
                pass
            self._ready.set()
            raise
        self._ready.set()

    def _append(self, data: bytes) -> None:
        self._total += len(data)
        needed = self._head_limit - len(self._head)
        if needed:
            self._head.extend(data[:needed])
            data = data[needed:]
        if data:
            self._tail.append(data)
            self._tail_size += len(data)
        while self._tail_size > self._tail_limit:
            excess = self._tail_size - self._tail_limit
            first = self._tail.popleft()
            if len(first) > excess:
                self._tail.appendleft(first[excess:])
                self._tail_size -= excess
            else:
                self._tail_size -= len(first)

    def _drain(self, descriptor: int) -> None:
        self._ready.wait()
        if self._abandoned:
            return
        try:
            while data := os.read(descriptor, 8192):
                self._append(data)
        except Exception:
            self.failed.set()
        finally:
            try:
                os.close(descriptor)
            except OSError:
                self.failed.set()

    def join(self, timeout: float) -> None:
        self._thread.join(timeout=max(0.0, timeout))
        if self._thread.is_alive() or self.failed.is_set():
            raise OutputCaptureError("content output capture unconfirmed") from None

    def text(self) -> str:
        separator = b"\n[output truncated]\n" if self._total > self._head_limit + self._tail_limit else b""
        return (bytes(self._head) + separator + b"".join(self._tail)).decode("utf-8", errors="replace")


class BoundedOutputCapture:
    """Provide inheritable-through-spawn writers and bounded head/tail evidence."""

    def __init__(self, *, limit: int) -> None:
        if limit < 2:
            raise ValueError("capture limit must be at least two bytes")
        out: _Stream | None = None
        try:
            out = _Stream(limit)
            self._err = _Stream(limit)
        except BaseException as error:
            if out is not None:
                try:
                    out.writer.close()
                    out.join(1.0)
                except Exception:
                    pass
            if not isinstance(error, Exception):
                raise
            raise OutputCaptureError("content output capture unavailable") from None
        self._out = out
        self.stdout = self._out.writer
        self.stderr = self._err.writer

    @property
    def failed(self) -> bool:
        return self._out.failed.is_set() or self._err.failed.is_set()

    def close_writers(self) -> None:
        failed = False
        for writer in (self.stdout, self.stderr):
            try:
                writer.close()
            except Exception:
                failed = True
        if failed:
            raise OutputCaptureError("content output capture unconfirmed") from None

    def finish(self, *, timeout: float = 5.0) -> str:
        self.close_writers()
        deadline = time.monotonic() + timeout
        self._out.join(deadline - time.monotonic())
        self._err.join(deadline - time.monotonic())
        return self._out.text() + "\n" + self._err.text()

    def __enter__(self) -> BoundedOutputCapture:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.close_writers()
        except OutputCaptureError:
            if exc_type is None:
                raise
        if exc_type is not None:
            # Cleanup may remain unconfirmed after an owned-tree kill fails.
            # Do not mask that error or wait indefinitely on a surviving writer.
            for stream in (self._out, self._err):
                try:
                    stream.join(0.1)
                except OutputCaptureError:
                    pass
        else:
            self.finish()

"""Bounded capture uses owned harmless writers and never stores raw output."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import psutil
import pytest

from steam_agent.execution import output_capture
from steam_agent.execution import content_plane
from steam_agent.execution.output_capture import BoundedOutputCapture, OutputCaptureError
from steam_agent.process_environment import child_environment


TRUNCATED = "\n[output truncated]\n"


@pytest.mark.parametrize("failed_start", [1, 2])
def test_thread_start_failure_rolls_back_all_pipe_descriptors(
    monkeypatch, failed_start: int,
) -> None:
    descriptors: list[int] = []
    pipe = output_capture.os.pipe
    start = output_capture.threading.Thread.start
    attempts = 0

    def tracked_pipe():
        pair = pipe()
        descriptors.extend(pair)
        return pair

    def failing_start(thread):
        nonlocal attempts
        attempts += 1
        if attempts == failed_start:
            raise RuntimeError("synthetic-private-thread-canary")
        return start(thread)

    monkeypatch.setattr(output_capture.os, "pipe", tracked_pipe)
    monkeypatch.setattr(output_capture.threading.Thread, "start", failing_start)
    with pytest.raises(OutputCaptureError) as caught:
        BoundedOutputCapture(limit=64)
    assert isinstance(caught.value, OSError)
    assert str(caught.value) == "content output capture unavailable"
    assert "canary" not in repr(caught.value)
    assert len(descriptors) == 2 * failed_start
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_writer_close_failure_after_spawn_terminates_before_handle_release(
    tmp_path: Path, monkeypatch,
) -> None:
    events: list[str] = []

    class Writer:
        def __init__(self, handle, name):
            self.handle, self.name = handle, name

        def write(self, data):
            return self.handle.write(data)

        def close(self):
            events.append(self.name)
            self.handle.close()
            if self.name == "stdout-close":
                raise OSError("synthetic-private-close-canary")

    class CloseFailure(BoundedOutputCapture):
        def __init__(self, *, limit):
            super().__init__(limit=limit)
            self.stdout = Writer(self.stdout, "stdout-close")
            self.stderr = Writer(self.stderr, "stderr-close")

    class Process:
        returncode = None

        def wait(self, timeout):
            pytest.fail("writer-close failure must stop before process polling")

    class Adapter(content_plane.SteamcmdAdapter):
        def _spawn(self, _argv, stdout, _stderr, _environment):
            events.append("spawn")
            stdout.write(b"Success! App fully installed")
            return Process()

        def _terminate(self, process):
            events.append("terminate")
            process.returncode = -1

        def _close(self, _process):
            events.append("process-close")

    monkeypatch.setattr(content_plane, "BoundedOutputCapture", CloseFailure)
    adapter = Adapter(
        steamcmd_script=tmp_path / "unused", private_home=tmp_path / "home",
        log_dir=tmp_path / "logs", timeout_seconds=1,
    )
    with pytest.raises(content_plane.ContentProcessError) as caught:
        adapter.install(account="synthetic", appid=480, install_dir=tmp_path / "target", operation_id=1)
    assert events[:5] == ["spawn", "stdout-close", "stderr-close", "terminate", "process-close"]
    assert str(caught.value) == "content process polling failed"
    assert "canary" not in repr(caught.value)
    assert tuple((tmp_path / "logs").iterdir()) == ()


def _finish_owned(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()
    child.wait(timeout=5)


@pytest.mark.parametrize("limit", [64, 127])
def test_noisy_native_child_is_drained_with_exact_bounded_head_tail(
    tmp_path: Path, limit: int,
) -> None:
    size = 2 * 1024 * 1024
    program = (
        "import os; "
        f"out=b'OUT-BEGIN|'+b'o'*{size}+b'|OUT-END'; "
        f"err=b'ERR-BEGIN|'+b'e'*{size}+b'|ERR-END'; "
        "os.write(1,out); os.write(2,err)"
    )
    capture = BoundedOutputCapture(limit=limit)
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", program], cwd=tmp_path,
        stdin=subprocess.DEVNULL, stdout=capture.stdout, stderr=capture.stderr,
        env=child_environment(),
    )
    try:
        capture.close_writers()
        assert child.wait(timeout=5) == 0
        result = capture.finish(timeout=2)
    finally:
        _finish_owned(child)
        capture.finish(timeout=2)
    head, tail = limit // 2, limit - limit // 2
    out = "OUT-BEGIN|" + "o" * size + "|OUT-END"
    err = "ERR-BEGIN|" + "e" * size + "|ERR-END"
    assert result == out[:head] + TRUNCATED + out[-tail:] + "\n" + err[:head] + TRUNCATED + err[-tail:]
    assert len(result) == 2 * limit + 2 * len(TRUNCATED) + 1
    assert capture.failed is False
    assert tuple(tmp_path.iterdir()) == ()


def test_capture_preserves_stream_boundary_and_decodes_invalid_utf8() -> None:
    with BoundedOutputCapture(limit=128) as capture:
        capture.stdout.write(b"synthetic-unrecognized\xff")
        capture.stderr.write(b"Success! App fully installed")
        assert capture.finish(timeout=2) == "synthetic-unrecognized\ufffd\nSuccess! App fully installed"


def test_lingering_owned_writer_causes_bounded_failure_until_it_exits() -> None:
    capture = BoundedOutputCapture(limit=64)
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(15)"],
        stdin=subprocess.DEVNULL, stdout=capture.stdout, stderr=capture.stderr,
        env=child_environment(),
    )
    try:
        capture.close_writers()
        started = time.monotonic()
        with pytest.raises(OutputCaptureError, match="^content output capture unconfirmed$"):
            capture.finish(timeout=0.03)
        assert time.monotonic() - started < 1
        assert child.poll() is None
    finally:
        _finish_owned(child)
        assert capture.finish(timeout=2) == "\n"


def test_reader_error_is_reported_without_exception_text(monkeypatch) -> None:
    def broken_append(_self, _data):
        raise OSError("synthetic-private-reader-canary")

    monkeypatch.setattr(output_capture._Stream, "_append", broken_append)  # noqa: SLF001
    capture = BoundedOutputCapture(limit=64)
    try:
        capture.stdout.write(b"synthetic")
        capture.close_writers()
        with pytest.raises(OutputCaptureError) as caught:
            capture.finish(timeout=2)
        assert capture.failed is True
        assert str(caught.value) == "content output capture unconfirmed"
        assert "canary" not in repr(caught.value)
    finally:
        capture.close_writers()


def test_adapter_reader_failure_terminates_owned_writer_before_closing(
    tmp_path: Path, monkeypatch,
) -> None:
    events: list[str] = []

    def broken_append(_self, _data):
        raise OSError("synthetic-private-reader-canary")

    monkeypatch.setattr(output_capture._Stream, "_append", broken_append)  # noqa: SLF001

    class Process:
        returncode = None

        def wait(self, timeout):
            time.sleep(0.01)
            raise subprocess.TimeoutExpired("synthetic", timeout)

    class Adapter(content_plane.SteamcmdAdapter):
        def _spawn(self, _argv, stdout, _stderr, _environment):
            stdout.write(b"Success! App fully installed")
            return Process()

        def _terminate(self, process):
            events.append("terminate")
            process.returncode = -1

        def _close(self, _process):
            events.append("close")

    adapter = Adapter(
        steamcmd_script=tmp_path / "unused", private_home=tmp_path / "home",
        log_dir=tmp_path / "logs", timeout_seconds=1, abort_poll_seconds=0.01,
    )
    with pytest.raises(content_plane.ContentProcessError) as caught:
        adapter.install(account="synthetic", appid=480, install_dir=tmp_path / "target", operation_id=1)
    assert events == ["terminate", "close"]
    assert str(caught.value) == "content process polling failed"
    assert "canary" not in repr(caught.value)
    assert tuple((tmp_path / "logs").iterdir()) == ()


def test_adapter_cannot_succeed_while_an_owned_writer_still_holds_output(
    tmp_path: Path, monkeypatch,
) -> None:
    events: list[str] = []
    child: subprocess.Popen | None = None
    monkeypatch.setattr(content_plane, "_PROCESS_CLEANUP_SECONDS", 0.03)

    class CompletedLeader:
        returncode = 0

        def wait(self, timeout):
            return 0

    class Adapter(content_plane.SteamcmdAdapter):
        def _spawn(self, _argv, stdout, stderr, environment):
            nonlocal child
            child = subprocess.Popen(
                [sys.executable, "-I", "-c", "import time; time.sleep(15)"],
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=environment,
            )
            stdout.write(b"Success! App fully installed")
            return CompletedLeader()

        def _terminate(self, _process):
            events.append("terminate")
            _finish_owned(child)

        def _close(self, _process):
            events.append("close")

    adapter = Adapter(
        steamcmd_script=tmp_path / "unused", private_home=tmp_path / "home",
        log_dir=tmp_path / "logs", timeout_seconds=1,
    )
    try:
        with pytest.raises(content_plane.ContentProcessError, match="^content process polling failed$"):
            adapter.install(account="synthetic", appid=480, install_dir=tmp_path / "target", operation_id=1)
        assert events == ["terminate", "close"]
        assert child.poll() is not None
        assert tuple((tmp_path / "logs").iterdir()) == ()
    finally:
        if child is not None:
            _finish_owned(child)


_NOISY_SHIM = '''import json, os, time
from pathlib import Path
import psutil
marker = Path(os.environ["CAPTURE_FIXTURE_MARKER"])
deadline = time.monotonic() + 15
payload = b"SYNTHETIC-RAW-CAPTURE-CANARY:" + b"n" * 65536
for descriptor in (1, 2):
    for _ in range(128):
        remaining = memoryview(payload)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining):]
pending = marker.with_suffix(".pending")
pending.write_text(json.dumps({"pid": os.getpid(), "created": psutil.Process().create_time()}))
pending.replace(marker)
while time.monotonic() < deadline:
    try:
        os.write(1, payload)
    except BrokenPipeError:
        pass
    time.sleep(0.01)
'''

_ADAPTER_OWNER = '''import sys
from pathlib import Path
from steam_agent.execution.content_plane import SteamcmdAdapter
root = Path(sys.argv[1])
SteamcmdAdapter(steamcmd_script=root / "noisy-shim", private_home=root / "home",
    log_dir=root / "logs", timeout_seconds=15, abort_poll_seconds=0.1).install(
    account="synthetic", appid=480, install_dir=root / "target", operation_id=1)
'''


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL capture-owner death requires POSIX")
def test_sigkill_real_content_adapter_leaves_no_raw_spools_and_owned_writer_is_cleaned(
    tmp_path: Path,
) -> None:
    shim = tmp_path / "noisy-shim"
    shim.write_text(f"#!{sys.executable}\n" + _NOISY_SHIM)
    shim.chmod(0o700)
    marker = tmp_path / "writer.json"
    environment = child_environment(overrides={"CAPTURE_FIXTURE_MARKER": str(marker)})
    owner = subprocess.Popen(
        [sys.executable, "-I", "-c", _ADAPTER_OWNER, str(tmp_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=environment,
    )
    owned: list[tuple[psutil.Process, float]] = []
    try:
        deadline = time.monotonic() + 5
        while not marker.is_file():
            assert owner.poll() is None, "capture owner exited before fixture startup"
            assert time.monotonic() < deadline, "bounded fixture startup elapsed"
            time.sleep(0.01)
        record = json.loads(marker.read_text())
        owned = [(child, child.create_time()) for child in psutil.Process(owner.pid).children(recursive=True)]
        assert (record["pid"], record["created"]) in [(child.pid, created) for child, created in owned]
        os.kill(owner.pid, signal.SIGKILL)
        assert owner.wait(timeout=5) == -signal.SIGKILL
        writer = psutil.Process(record["pid"])
        assert writer.create_time() == record["created"]
        assert writer.is_running() and writer.status() != psutil.STATUS_ZOMBIE
        # More than eight MiB passed through each stream before startup was
        # acknowledged. Neither normal output nor broker death creates raw files.
        assert tuple((tmp_path / "logs").iterdir()) == ()
        assert not tuple(tmp_path.rglob("*.out.tmp"))
        assert not tuple(tmp_path.rglob("*.err.tmp"))
    finally:
        if owner.poll() is None:
            try:
                owned = [(child, child.create_time()) for child in psutil.Process(owner.pid).children(recursive=True)]
            except psutil.NoSuchProcess:
                pass
        _finish_owned(owner)
        for child, created in owned:
            try:
                if child.create_time() == created:
                    child.kill()
                    child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass

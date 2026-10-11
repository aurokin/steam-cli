"""Portable file-lock behavior, using only test-owned temporary paths."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from steam_agent.execution.locking import FileLockError, acquire_file_lock


_TRY_LOCK = """
from pathlib import Path
import sys
from steam_agent.execution.locking import FileLockError, acquire_file_lock
try:
    handle = acquire_file_lock(Path(sys.argv[1]))
except FileLockError:
    print('busy')
else:
    print('locked')
    handle.close()
"""


def test_nonblocking_lock_contends_across_processes_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "execution.lock"
    held = acquire_file_lock(path)
    try:
        contender = subprocess.run(
            [sys.executable, "-c", _TRY_LOCK, str(path)],
            capture_output=True, text=True, timeout=10, check=True,
        )
        assert contender.stdout.strip() == "busy"
        assert contender.stderr == ""
    finally:
        held.close()

    retry = subprocess.run(
        [sys.executable, "-c", _TRY_LOCK, str(path)],
        capture_output=True, text=True, timeout=10, check=True,
    )
    assert retry.stdout.strip() == "locked"


def test_blocking_lock_waits_for_holder_to_release(tmp_path: Path) -> None:
    path = tmp_path / "state.lock"
    held = acquire_file_lock(path)
    script = """
from pathlib import Path
import sys
from steam_agent.execution.locking import acquire_file_lock
print('waiting', flush=True)
handle = acquire_file_lock(Path(sys.argv[1]), blocking=True)
print('locked', flush=True)
handle.close()
"""
    contender = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert contender.stdout is not None
        assert contender.stdout.readline().strip() == "waiting"
        with pytest.raises(subprocess.TimeoutExpired):
            contender.communicate(timeout=0.1)
        held.close()
        output, errors = contender.communicate(timeout=10)
        assert contender.returncode == 0
        assert output.strip() == "locked"
        assert errors == ""
    finally:
        held.close()
        if contender.poll() is None:
            contender.kill()
            contender.communicate(timeout=10)


def test_lock_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("untouched", encoding="utf-8")
    path = tmp_path / "execution.lock"
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("this host does not allow test-created symlinks")

    with pytest.raises(FileLockError):
        acquire_file_lock(path)

    assert target.read_text(encoding="utf-8") == "untouched"

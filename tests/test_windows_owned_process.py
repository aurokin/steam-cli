"""Native, Steam-free qualification of the Windows content process primitive."""

from pathlib import Path
import subprocess
import sys

import pytest

from steam_agent.execution.windows_process import WindowsJobProcess

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows job objects")


def test_owned_job_waits_for_descendant_and_strips_provider_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STEAM_WEB_API_KEY", "synthetic-canary")
    with (tmp_path / "stdout").open("w+b") as stdout, (tmp_path / "stderr").open("w+b") as stderr:
        process = WindowsJobProcess([
            sys.executable, "-c",
            "import os,subprocess,sys; print('STEAM_WEB_API_KEY' in os.environ,flush=True); "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(0.5)'])",
        ], stdout=stdout, stderr=stderr)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.05)
            assert process.wait(timeout=10) == 0
            assert process.active_count() == 0
            stdout.seek(0)
            assert stdout.read() == b"False\r\n"
        finally:
            process.close()


def test_owned_job_kills_child_tree(tmp_path: Path) -> None:
    with (tmp_path / "stdout").open("w+b") as stdout, (tmp_path / "stderr").open("w+b") as stderr:
        process = WindowsJobProcess([
            sys.executable, "-c",
            "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)",
        ], stdout=stdout, stderr=stderr)
        try:
            process.kill()
            assert process.wait(timeout=10) != 0
            assert process.active_count() == 0
        finally:
            process.close()

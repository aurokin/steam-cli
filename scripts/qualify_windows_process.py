"""Run owned, Steam-free Windows process qualification and emit coarse results."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from steam_agent.execution.windows_process import WindowsJobProcess
from steam_agent.execution.output_capture import BoundedOutputCapture


def main() -> int:
    phase = "platform"
    try:
        if sys.platform != "win32":
            raise OSError("Windows required")
        with tempfile.TemporaryDirectory(prefix="steam-agent-native-") as directory:
            root = Path(directory)
            phase = "descendant_wait"
            with (root / "out").open("w+b") as out, (root / "err").open("w+b") as err:
                process = WindowsJobProcess([
                    sys.executable, "-c",
                    "import os,subprocess,sys; print('STEAM_WEB_API_KEY' in os.environ,flush=True); "
                    "subprocess.Popen([sys.executable,'-c','import time; time.sleep(0.5)'])",
                ], stdout=out, stderr=err, env=dict(os.environ, STEAM_WEB_API_KEY="synthetic-canary"))
                try:
                    try:
                        process.wait(timeout=0.05)
                        raise AssertionError("early completion")
                    except subprocess.TimeoutExpired:
                        pass
                    if process.wait(timeout=10) != 0 or process.active_count():
                        raise AssertionError("job completion failed")
                    out.seek(0)
                    if out.read().strip() != b"False":
                        raise AssertionError("provider variable inherited")
                finally:
                    process.close()
            phase = "bounded_pipe_capture"
            with BoundedOutputCapture(limit=1024) as capture:
                process = WindowsJobProcess([
                    sys.executable, "-c",
                    "import os; os.write(1,b'HEAD\\n'); "
                    "[os.write(1,b'x'*8192) for _ in range(1024)]; "
                    "os.write(1,b'\\nTAIL'); os.write(2,b'SEPARATE-STDERR')",
                ], stdout=capture.stdout, stderr=capture.stderr)
                capture.close_writers()
                try:
                    if process.wait(timeout=15) != 0 or process.active_count():
                        raise AssertionError("captured job completion failed")
                    output = capture.finish(timeout=5)
                    if (
                        not output.startswith("HEAD\n")
                        or "[output truncated]" not in output
                        or not output.endswith("\nTAIL\nSEPARATE-STDERR")
                        or len(output) > 1100
                    ):
                        raise AssertionError("bounded capture failed")
                finally:
                    process.close()
            phase = "tree_termination"
            with (root / "out2").open("w+b") as out, (root / "err2").open("w+b") as err:
                process = WindowsJobProcess([
                    sys.executable, "-c",
                    "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)",
                ], stdout=out, stderr=err)
                try:
                    deadline = time.monotonic() + 10
                    while process.active_count() < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if process.active_count() < 2:
                        raise AssertionError("descendant not observed")
                    process.kill()
                    if process.wait(timeout=10) == 0 or process.active_count():
                        raise AssertionError("job termination failed")
                finally:
                    process.close()
        print(json.dumps({
            "platform": "windows", "descendant_wait": True,
            "tree_termination": True, "secret_child_exclusion": True,
            "bounded_pipe_capture": True,
            "steam_actions": 0,
        }, sort_keys=True))
        return 0
    except Exception as error:
        # Native exception text can contain executable/private filesystem paths.
        print(json.dumps({
            "platform": "windows", "phase": phase,
            "error_type": type(error).__name__, "steam_actions": 0,
        }, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

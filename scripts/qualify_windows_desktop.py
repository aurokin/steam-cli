#!/usr/bin/env python3
"""Read-only native bridge dispatch and fresh-task recovery qualification.

Run from an installed package. No Steam operation, client shutdown, or desktop
input is issued. A failed job is retained for original-job inspection rather
than pretending that a missing response proves that nothing ran.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile

from steam_agent.execution.desktop_bridge import (
    NativeTaskScheduler, discover_desktop_jobs, dispatch_in_desktop,
    inspect_desktop_job,
)


def main() -> int:
    phase = "platform"
    state: Path | None = None
    safe_cleanup = False
    try:
        if sys.platform != "win32":
            raise OSError("Windows required")
        state = Path(tempfile.mkdtemp(prefix="steam-agent-desktop-qualification-"))
        # An elevated SSH token can create scratch directories granting its
        # logon SID rather than the owner's non-elevated interactive token.
        # Provision this owned fixture root explicitly before scheduling.
        NativeTaskScheduler().protect_directory(state)

        class RetainCompletedTask(NativeTaskScheduler):
            def remove(self, token: str) -> bool:
                # Exercise recovery through a new native backend, including
                # exact COM task inspection before eventual deletion.
                return False

        phase = "dispatch"
        result = dispatch_in_desktop(["runtime"], state, backend=RetainCompletedTask())
        if result.exit_code != 0:
            reason = result.payload.get("reason_code")
            print(json.dumps({"phase": phase, "qualified": False, "reason_code": reason, "retained_original_job": True, "steam_actions": 0}, sort_keys=True))
            return 1
        envelope = result.payload
        observed = envelope.get("result")
        if envelope.get("schema") != "broker-result/0.1" or not isinstance(observed, dict) or observed.get("schema") != "runtime-observation/0.1":
            raise AssertionError("worker contract mismatch")
        phase = "fresh_recovery"
        inventory = discover_desktop_jobs(state)
        jobs = inventory.get("jobs")
        if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
            raise AssertionError("original job unavailable")
        job_id = jobs[0].get("job_id")
        if not isinstance(job_id, str):
            raise AssertionError("original job handle unavailable")
        collected = inspect_desktop_job(state, job_id, cleanup=True)
        if collected.get("state") != "done" or collected.get("receipt") is None or collected.get("cleaned") is not True:
            raise AssertionError("original job cleanup unconfirmed")
        safe_cleanup = True
        print(json.dumps({"platform": "windows", "qualified": True, "read_only_desktop_dispatch": True, "fresh_task_recovery": True, "worker_schema": envelope["schema"], "steam_actions": 0}, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"phase": phase, "qualified": False, "failure": type(error).__name__, "retained_original_job": state is not None and not safe_cleanup, "steam_actions": 0}, sort_keys=True))
        return 1
    finally:
        if state is not None and safe_cleanup:
            shutil.rmtree(state)


if __name__ == "__main__":
    raise SystemExit(main())

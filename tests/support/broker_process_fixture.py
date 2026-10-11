"""Disposable broker driver with synthetic adapters, never Steam or host games.

Run with an installed interpreter as well as from the source checkout. The
driver imports the real broker and patches only its adapter construction and
platform qualification check. OS locks, parser, intake, executor and SQLite
transactions remain real. This is recovery evidence, not native qualification.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def append_event(root: Path, event: str, **fields: object) -> None:
    descriptor = os.open(root / "effects.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps({"event": event, **fields}) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def wait_for(path: Path, *, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("fixture barrier timed out")
        time.sleep(0.01)


def writer(root: Path, target: Path) -> int:
    """Bounded bytes, bounded lifetime, and an explicit parent-owned exit file."""
    target.mkdir(parents=True, exist_ok=True)
    with (target / "partial.bin").open("wb") as handle:
        handle.write(b"synthetic partial content\n")
        handle.flush()
        os.fsync(handle.fileno())
        write_json(root / "writer-ready.json", {"pid": os.getpid()})
        deadline = time.monotonic() + 20
        while not (root / "writer-exit").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    return 0


def drive(
    root: Path, cut: str, startup_token: str | None, command: list[str],
    execution_lock: Path | None = None,
) -> int:
    from steam_agent.execution import broker_main, client_executor, ledger as ledger_module
    from steam_agent.execution.content_plane import ContentResult
    from steam_agent.execution.ledger import ExecutionLedger
    from steam_agent.execution.linux_session import LeaseGates
    from steam_agent.execution.runtime import RuntimeObservation, StopResult

    # These fixtures deliberately exercise the Linux API on every host without
    # constructing LinuxSession or claiming support for the actual host.
    class FixtureSys:
        platform = "linux"

        def __getattr__(self, name: str):
            # Follow redirected stdio when the broker selects JSON envelopes.
            return getattr(sys, name)

    fixture_sys = FixtureSys()
    broker_main.sys = fixture_sys
    client_executor.sys = fixture_sys

    def control() -> dict[str, object]:
        path = root / "control.json"
        return json.loads(path.read_text()) if path.exists() else {}

    ledger_module._utcnow = lambda: (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=int(control().get("clock_offset", 0)))
    )

    def pause(point: str) -> None:
        if cut == point:
            write_json(root / f"{point}.json", {"pid": os.getpid()})
            wait_for(root / "release")

    class CutLedger(ExecutionLedger):
        def transition(self, operation_id: int, to_state: str, **kwargs) -> None:
            if to_state == "dispatch_pending":
                pause("before_intent")
            super().transition(operation_id, to_state, **kwargs)
            if to_state == "dispatch_pending":
                pause("after_intent")
            if to_state == "dispatched":
                pause("after_ack")

    class Session:
        def client_running(self) -> bool:
            path = root / "client.json"
            return json.loads(path.read_text())["running"] if path.exists() else True

        def client_possibly_running(self) -> bool:
            return self.client_running()

        def gates(self) -> LeaseGates:
            return LeaseGates(
                game_running=control().get("game_gate", "pass"),
                remote_play="pass", download_in_flight="pass",
                client_running="fail" if self.client_running() else "pass",
            )

        def steamcmd_running(self) -> bool:
            path = root / "writer.json"
            if not path.exists():
                return False
            identity = json.loads(path.read_text())
            try:
                process = psutil.Process(identity["pid"])
                return (
                    process.create_time() == identity["create_time"]
                    and process.is_running()
                    and process.status() != psutil.STATUS_ZOMBIE
                )
            except psutil.NoSuchProcess:
                return False

        def stop_client(self) -> bool:
            append_event(root, "client_stop")
            write_json(root / "client.json", {"running": False})
            return True

        def start_client(self) -> bool:
            with_state = ExecutionLedger(root / "state" / "ledger.sqlite3")
            try:
                active = with_state.active()
                state = active.state if active else None
            finally:
                with_state.close()
            succeeded = control().get("restore_ok", True)
            append_event(root, "client_start", ledger_state=state, succeeded=succeeded)
            if succeeded:
                write_json(root / "client.json", {"running": True})
            return succeeded

        def launch_app(self, appid: int) -> bool:
            append_event(root, "launch", appid=appid)
            pause("after_effect")
            return control().get("ack", True)

    class Runtime:
        def observe(self, appid: int) -> RuntimeObservation:
            if control().get("runtime_unknown", False):
                return RuntimeObservation("unknown", (), ("synthetic_probe_unknown",))
            return RuntimeObservation("absent", ())

        def stop_app(self, appid: int, *, force: bool, before_request=None) -> StopResult:
            if before_request is not None and not before_request():
                return StopResult("deferred", "stop_dispatch_gate_denied", 0, force)
            append_event(root, "stop", appid=appid, force=force)
            pause("after_effect")
            return StopResult("stopped", "synthetic_exit", 1, force, 1)

    class Content:
        def install(self, *, appid: int, install_dir: Path, **kwargs) -> ContentResult:
            append_event(root, "content_writer", appid=appid)
            if cut == "writer_started":
                child = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--writer", str(root), str(install_dir)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                write_json(root / "writer.json", {
                    "pid": child.pid, "create_time": psutil.Process(child.pid).create_time(),
                })
                wait_for(root / "writer-ready.json")
                pause("writer_started")
                child.wait(timeout=22)
            manifest_dir = install_dir / "steamapps"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            (manifest_dir / f"appmanifest_{appid}.acf").write_text(
                f'"AppState" {{ "appid" "{appid}" "installdir" "app_{appid}" "StateFlags" "4" }}'
            )
            return ContentResult("installed", root / "synthetic.log")

    def components(state_dir: Path):
        config = broker_main._load_config(state_dir)
        ledger = CutLedger(state_dir / "ledger.sqlite3")
        executor = client_executor.ClientExecutor(
            ledger=ledger, session=Session(), content=Content(),
            library=Path(config["library"]), state_dir=state_dir,
            lock_path=execution_lock or root / "execution.lock",
            dispatch_policy=lambda operation: broker_main._dispatch_denial(state_dir, operation),
            account_login=lambda alias: config.get("steam_login"),
        )
        return ledger, executor

    broker_main._components = components
    client_executor.RuntimeObserver = Runtime
    if startup_token:
        write_json(root / f"ready-{startup_token}.json", {"pid": os.getpid()})
        wait_for(root / "start")
    return broker_main.main(["--state-dir", str(root / "state"), *command])


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--writer":
        return writer(Path(sys.argv[2]), Path(sys.argv[3]))
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cut", default="none")
    parser.add_argument("--startup-token")
    parser.add_argument("--execution-lock", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command
    if command and command[0] == "--":
        command = command[1:]
    return drive(
        arguments.root, arguments.cut, arguments.startup_token, command,
        arguments.execution_lock,
    )


if __name__ == "__main__":
    raise SystemExit(main())

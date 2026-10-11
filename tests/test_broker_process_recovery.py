"""Composed broker recovery using real processes and effect-free adapters.

The tests prove parser/intake/executor/ledger composition and OS lock lifetime.
They do not qualify Steam, desktop routing, native game termination or real
SteamCMD descendants. Every effect and process identity belongs to the fixture.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import psutil
import pytest

from steam_agent.execution.ledger import DEFAULT_EXECUTION_WINDOW_SECONDS, ExecutionLedger


FIXTURE = Path(__file__).parent / "support" / "broker_process_fixture.py"
REQUEST_ID = "synthetic-request-001"
EXPIRED_OFFSET = DEFAULT_EXECUTION_WINDOW_SECONDS + 60


class BrokerProcesses:
    def __init__(self, root: Path, *, execution_lock: Path | None = None) -> None:
        self.root = root
        self.execution_lock = execution_lock
        self.children: list[subprocess.Popen[str]] = []

    def spawn(
        self, *command: str, cut: str = "none", startup: str | None = None,
        plan: str | None = None,
    ):
        arguments = [sys.executable, str(FIXTURE), "--root", str(self.root), "--cut", cut]
        if startup:
            arguments.extend(["--startup-token", startup])
        if self.execution_lock:
            arguments.extend(["--execution-lock", str(self.execution_lock)])
        child = subprocess.Popen(
            [*arguments, "--", *command],
            stdin=subprocess.PIPE if plan is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.children.append(child)
        if plan is not None:
            child.stdin.write(plan)
            child.stdin.close()
            child.stdin = None
        return child

    def finish(self, child: subprocess.Popen[str]) -> tuple[int, dict[str, object] | None, str]:
        stdout, stderr = child.communicate(timeout=12)
        return child.returncode, json.loads(stdout) if stdout.strip() else None, stderr

    def invoke(self, *command: str) -> tuple[int, dict[str, object] | None, str]:
        return self.finish(self.spawn(*command))

    def initialize(self, *, grant: str = "allow") -> None:
        library = self.root / "library"
        (library / "steamapps").mkdir(parents=True)
        result = self.invoke(
            "init", "--library", str(library), "--machine-id", "synthetic-machine",
            "--account", "synthetic", "--steam-login", "synthetic_login",
            "--steamcmd", str(self.root / "unused-steamcmd"),
        )
        assert result[0] == 0, result[2]
        self.policy(grant=grant)

    def policy(self, *, grant: str = "allow", allowed: int = 480) -> None:
        (self.root / "state" / "policy.toml").write_text(
            f'[grants]\nlaunch="{grant}"\nstop="{grant}"\ninstall="{grant}"\n'
            f'[limits]\nmin_free_gb=0\n[launch]\nallowed_appids=[{allowed}]\n'
            f'[stop]\nallowed_appids=[{allowed}]\n', encoding="utf-8",
        )

    def control(self, **values: object) -> None:
        (self.root / "control.json").write_text(json.dumps(values), encoding="utf-8")

    def wait(self, filename: str, child: subprocess.Popen[str] | None = None) -> None:
        deadline = time.monotonic() + 10
        while not (self.root / filename).exists():
            if child is not None and child.poll() is not None:
                pytest.fail(f"broker exited before fixture barrier: {self.finish(child)}")
            if time.monotonic() >= deadline:
                pytest.fail(f"fixture barrier timed out: {filename}")
            time.sleep(0.01)

    def snapshot(self) -> tuple[str, list[str], bool | None]:
        ledger = ExecutionLedger(self.root / "state" / "ledger.sqlite3", readonly=True)
        try:
            operation = ledger.get(1)
            return (
                operation.state, [event[2] for event in ledger.events(1)],
                operation.prior_client_running,
            )
        finally:
            ledger.close()

    def effects(self, event: str) -> list[dict[str, object]]:
        path = self.root / "effects.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if row["event"] == event]

    def stop_writer(self) -> None:
        identity_path = self.root / "writer.json"
        if not identity_path.exists():
            return
        identity = json.loads(identity_path.read_text())
        (self.root / "writer-exit").touch()
        try:
            process = psutil.Process(identity["pid"])
            if process.create_time() != identity["create_time"]:
                return
            deadline = time.monotonic() + 3
            while process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait(timeout=3)
                    break
                time.sleep(0.01)
        except psutil.NoSuchProcess:
            pass

    def cleanup(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.stop_writer()


@pytest.fixture
def broker(tmp_path: Path):
    processes = BrokerProcesses(tmp_path)
    try:
        processes.initialize()
        yield processes
    finally:
        processes.cleanup()


@contextmanager
def killed_at(broker: BrokerProcesses, operation: str, point: str):
    child = broker.spawn(operation, "480", "--request-id", REQUEST_ID, cut=point)
    try:
        broker.wait(f"{point}.json", child)
        yield child
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
        child.communicate(timeout=5)
        assert child.returncode == -signal.SIGKILL


@pytest.mark.parametrize("operation", ["launch", "stop"])
@pytest.mark.parametrize("point,state,effects", [
    ("before_intent", "authorized", 0),
    ("after_intent", "dispatch_pending", 0),
    ("after_effect", "dispatch_pending", 1),
    ("after_ack", "dispatched", 1),
])
@pytest.mark.skipif(os.name == "nt", reason="SIGKILL crash cuts require POSIX")
def test_sigkill_dispatch_boundaries_never_replay_committed_uncertainty(
    broker: BrokerProcesses, operation: str, point: str, state: str, effects: int,
) -> None:
    with killed_at(broker, operation, point):
        assert broker.snapshot()[0] == state
        assert len(broker.effects(operation)) == effects
    assert broker.snapshot()[0] == state
    for _ in range(3):
        assert broker.invoke("reconcile")[0] == 0
    expected = "authorized" if state == "authorized" else (
        "dispatched" if state == "dispatched" else "unconfirmed"
    )
    assert broker.snapshot()[0] == expected
    retry = broker.invoke(operation, "480", "--request-id", REQUEST_ID)
    assert retry[0] == (0 if expected in {"authorized", "dispatched"} else 1)
    # Explicit run is also barred after uncertainty, even though it uses a
    # different ingress route than an idempotent direct-request retry.
    broker.invoke("run", "--operation-id", "1")
    for _ in range(2):
        assert broker.invoke("reconcile")[0] == 0
    final_state = "dispatched" if expected == "authorized" else expected
    assert broker.snapshot()[0] == final_state
    assert len(broker.effects(operation)) == (1 if state == "authorized" else effects)
    events = broker.snapshot()[1]
    assert events == [
        "pending_confirmation", "authorized", "dispatch_pending", final_state,
    ]


@pytest.mark.parametrize("ingress", ["direct", "request_then_run"])
def test_simultaneous_same_request_intake_and_run_have_one_effect(
    broker: BrokerProcesses, ingress: str,
) -> None:
    args = ["launch", "480", "--request-id", REQUEST_ID]
    if ingress == "request_then_run":
        args.append("--request-only")
    children = [broker.spawn(*args, cut="before_intent", startup=str(i)) for i in range(2)]
    for i, child in enumerate(children):
        broker.wait(f"ready-{i}.json", child)
    (broker.root / "start").touch()
    if ingress == "request_then_run":
        receipts = [broker.finish(child) for child in children]
        assert all(result[0] == 0 for result in receipts)
        assert [result[1]["operation_id"] for result in receipts] == [1, 1]
        assert sorted(result[1]["reused"] for result in receipts) == [False, True]
        children = [
            broker.spawn("run", "--operation-id", "1", cut="before_intent")
            for _ in range(2)
        ]
    broker.wait("before_intent.json")
    owner = json.loads((broker.root / "before_intent.json").read_text())["pid"]
    loser = next(child for child in children if child.pid != owner)
    loser_result = broker.finish(loser)
    assert loser_result[0] == 2
    assert "holds the lock" in loser_result[2]
    assert broker.snapshot()[1] == ["pending_confirmation", "authorized"]
    assert broker.effects("launch") == []
    (broker.root / "release").touch()
    winner = next(child for child in children if child.pid == owner)
    assert broker.finish(winner)[0] == 0
    assert broker.invoke("launch", "480", "--request-id", REQUEST_ID)[0] == 0
    assert broker.invoke("reconcile")[0] == 0
    assert broker.snapshot()[1] == [
        "pending_confirmation", "authorized", "dispatch_pending", "dispatched",
    ]
    assert len(broker.effects("launch")) == 1


def test_simultaneous_stdin_plan_intake_rejects_duplicate_without_reminting(
    broker: BrokerProcesses,
) -> None:
    plan = json.dumps({
        "schema": "operation-plan/0.1", "operation": "launch",
        "idempotency_key": "synthetic-plan-key-001",
        "target": {"appid": 480, "machine_id": "synthetic-machine"},
    })
    children = [
        broker.spawn("request", "--account", "synthetic", startup=str(i), plan=plan)
        for i in range(2)
    ]
    for i, child in enumerate(children):
        broker.wait(f"ready-{i}.json", child)
    (broker.root / "start").touch()
    results = [broker.finish(child) for child in children]
    assert sorted(result[0] for result in results) == [0, 2]
    assert "idempotency key already recorded" in next(result[2] for result in results if result[0] == 2)
    assert broker.snapshot()[1] == ["pending_confirmation", "authorized"]
    assert broker.effects("launch") == []
    assert broker.invoke("run", "--operation-id", "1")[0] == 0
    rejected = broker.finish(broker.spawn("request", "--account", "synthetic", plan=plan))
    assert rejected[0] == 2
    assert "idempotency key already recorded" in rejected[2]
    assert broker.snapshot()[1] == [
        "pending_confirmation", "authorized", "dispatch_pending", "dispatched",
    ]
    assert len(broker.effects("launch")) == 1


def test_concurrent_reconcile_cannot_retire_live_dispatch_intent(broker: BrokerProcesses) -> None:
    child = broker.spawn("launch", "480", "--request-id", REQUEST_ID, cut="after_effect")
    broker.wait("after_effect.json", child)
    result = broker.invoke("reconcile")
    assert result[0] == 2
    assert "holds the lock" in result[2]
    assert broker.snapshot()[0] == "dispatch_pending"
    assert len(broker.effects("launch")) == 1
    (broker.root / "release").touch()
    assert broker.finish(child)[0] == 0
    assert broker.invoke("reconcile")[0] == 0
    assert broker.snapshot()[1] == [
        "pending_confirmation", "authorized", "dispatch_pending", "dispatched",
    ]
    assert len(broker.effects("launch")) == 1


def test_separate_broker_states_share_execution_serialization(tmp_path: Path) -> None:
    lock = tmp_path / "shared-execution.lock"
    first = BrokerProcesses(tmp_path / "first", execution_lock=lock)
    second = BrokerProcesses(tmp_path / "second", execution_lock=lock)
    try:
        first.initialize()
        second.initialize()
        assert second.invoke("launch", "480", "--request-id", REQUEST_ID, "--request-only")[0] == 0
        child = first.spawn("launch", "480", "--request-id", REQUEST_ID, cut="after_effect")
        first.wait("after_effect.json", child)
        for command in [("run", "--operation-id", "1"), ("reconcile",)]:
            result = second.invoke(*command)
            assert result[0] == 2
            assert "holds the lock" in result[2]
        assert second.snapshot()[0] == "authorized"
        assert second.effects("launch") == []
        (first.root / "release").touch()
        assert first.finish(child)[0] == 0
        assert second.invoke("run", "--operation-id", "1")[0] == 0
        assert first.snapshot()[0] == second.snapshot()[0] == "dispatched"
        assert len(first.effects("launch")) == len(second.effects("launch")) == 1
    finally:
        first.cleanup()
        second.cleanup()


@pytest.mark.parametrize("expire_at", ["content_running", "interrupted"])
@pytest.mark.skipif(os.name == "nt", reason="surviving-writer broker SIGKILL requires POSIX")
def test_surviving_owned_fake_writer_defers_restore_and_new_writer_until_exit(
    broker: BrokerProcesses, expire_at: str,
) -> None:
    with killed_at(broker, "install", "writer_started"):
        assert broker.snapshot()[0] == "content_running"
        assert broker.snapshot()[2] is True
    identity = json.loads((broker.root / "writer.json").read_text())
    process = psutil.Process(identity["pid"])
    assert process.create_time() == identity["create_time"]
    assert process.is_running()
    marker = broker.root / "state" / "owned" / "480.json"
    assert marker.exists()
    owned = json.loads(marker.read_text())
    target = broker.root / "library" / "steamapps" / "common" / "app_480"
    assert (owned["dev"], owned["ino"]) == (target.stat().st_dev, target.stat().st_ino)
    assert (target / "partial.bin").read_bytes() == b"synthetic partial content\n"
    for _ in range(2):
        assert broker.invoke("reconcile")[0] == 0
        assert broker.invoke("run", "--operation-id", "1")[0] == 1
        assert broker.snapshot()[0] == "content_running"
    assert broker.effects("client_start") == []
    assert len(broker.effects("content_writer")) == 1
    if expire_at == "content_running":
        broker.control(clock_offset=EXPIRED_OFFSET)
        assert broker.invoke("reconcile")[0] == 0
        assert broker.snapshot()[0] == "content_running"
        assert broker.effects("client_start") == []
    broker.stop_writer()
    if expire_at == "interrupted":
        assert broker.invoke("reconcile")[0] == 0
        assert broker.snapshot()[0] == "interrupted"
        assert broker.invoke("reconcile")[0] == 0
        assert broker.snapshot()[0] == "interrupted"
        assert broker.effects("client_start") == []
    broker.control(clock_offset=EXPIRED_OFFSET, restore_ok=False)
    assert broker.invoke("reconcile")[0] == 0
    assert broker.snapshot()[0] == expire_at
    assert broker.effects("client_start")[-1] == {
        "event": "client_start", "ledger_state": expire_at, "succeeded": False,
    }
    broker.control(clock_offset=EXPIRED_OFFSET, restore_ok=True)
    assert broker.invoke("reconcile")[0] == 0
    assert broker.snapshot()[0] == "failed"
    assert broker.effects("client_start")[-1] == {
        "event": "client_start", "ledger_state": expire_at, "succeeded": True,
    }
    assert json.loads((broker.root / "client.json").read_text())["running"] is True
    assert len(broker.effects("content_writer")) == 1
    before = broker.snapshot()
    for _ in range(2):
        assert broker.invoke("reconcile")[0] == 0
        assert broker.invoke("install", "480", "--request-id", REQUEST_ID)[0] == 1
    assert broker.snapshot() == before
    assert len(broker.effects("client_start")) == 2


@pytest.mark.parametrize("trajectory", [
    "allow_and_retry", "confirm_and_retry", "deny", "allowlist_denied",
    "force_denied", "queued_revocation", "unknown_then_retry", "negative_ack_retry",
])
def test_deterministic_agent_workflow_trajectories(
    broker: BrokerProcesses, trajectory: str,
) -> None:
    """Scripted caller decisions through real CLI; no language-model claims."""
    operation = "stop" if trajectory in {"force_denied", "unknown_then_retry"} else "launch"
    args = [operation, "480", "--request-id", REQUEST_ID]
    if trajectory == "deny":
        broker.policy(grant="deny")
    elif trajectory == "allowlist_denied":
        broker.policy(allowed=481)
    elif trajectory == "force_denied":
        args.append("--force")
    elif trajectory == "confirm_and_retry":
        broker.policy(grant="confirm")
    elif trajectory == "unknown_then_retry":
        broker.control(runtime_unknown=True)
    elif trajectory == "negative_ack_retry":
        broker.control(ack=False)
    elif trajectory == "queued_revocation":
        args.append("--request-only")
    first = broker.invoke(*args)
    if trajectory in {"deny", "allowlist_denied", "force_denied"}:
        assert first[0] == 2
        assert "not permitted" in first[2]
        assert broker.effects(operation) == []
        ledger = ExecutionLedger(broker.root / "state" / "ledger.sqlite3", readonly=True)
        try:
            assert ledger.recent(limit=10) == []
        finally:
            ledger.close()
        return
    if trajectory == "confirm_and_retry":
        assert first[1]["state"] == "pending_confirmation"
        assert broker.effects(operation) == []
        repeated = broker.invoke(*args)
        assert repeated[1]["nonce"] == first[1]["nonce"]
        assert broker.invoke("confirm", str(first[1]["nonce"]), "--actor", "synthetic-agent")[0] == 0
        assert broker.invoke("run", "--operation-id", "1")[0] == 0
    elif trajectory == "queued_revocation":
        assert first[1]["state"] == "authorized"
        broker.policy(grant="deny")
        assert broker.invoke("run", "--operation-id", "1")[0] == 2
        assert broker.snapshot()[0] == "aborted"
        assert broker.effects(operation) == []
        assert broker.invoke(*args)[0] == 1
        return
    elif trajectory == "unknown_then_retry":
        assert first[1]["outcome"] == "deferred"
        assert broker.snapshot()[0] == "authorized"
        assert broker.effects(operation) == []
        broker.control(runtime_unknown=False)
    elif trajectory == "negative_ack_retry":
        assert first[1]["outcome"] == "unconfirmed"
    else:
        assert first[0] == 0
    retry = broker.invoke(*args)
    expected = "unconfirmed" if trajectory == "negative_ack_retry" else "dispatched"
    assert retry[0] == (1 if expected == "unconfirmed" else 0)
    assert broker.snapshot()[0] == expected
    assert len(broker.effects(operation)) == 1
    assert broker.invoke("reconcile")[0] == 0
    assert broker.snapshot()[1] == ["pending_confirmation", "authorized", "dispatch_pending", expected]

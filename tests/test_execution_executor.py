"""Executor flow with fake session and content plane: gates, outcomes, resume."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest

from steam_agent.execution.content_plane import ContentResult
from steam_agent.execution.executor import Executor
from steam_agent.execution.ledger import ExecutionLedger
from steam_agent.execution.linux_session import LeaseGates

_MANIFEST = '''"AppState"
{
\t"appid"\t\t"480"
\t"installdir"\t\t"Spacewar"
\t"StateFlags"\t\t"4"
}
'''


class FakeSession:
    def __init__(self, *, clear: bool = True, running: bool = True) -> None:
        self.clear = clear
        self.running = running
        self.stops = 0
        self.starts = 0

    def gates(self) -> LeaseGates:
        state = "pass" if self.clear else "fail"
        return LeaseGates(
            game_running=state,
            remote_play="pass",
            download_in_flight="pass",
            client_running="fail" if self.running else "pass",
        )

    def client_running(self) -> bool:
        return self.running

    def stop_client(self) -> bool:
        self.stops += 1
        self.running = False
        return True

    def start_client(self) -> bool:
        self.starts += 1
        self.running = True
        return True


@dataclass
class FakeContent:
    outcome: str = "installed"
    log_dir: Path = field(default_factory=Path)
    write_manifest: bool = True

    def install(self, *, account: str, appid: int, install_dir: Path) -> ContentResult:
        log_path = self.log_dir / f"install-{appid}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("log", encoding="utf-8")
        if self.outcome == "installed" and self.write_manifest:
            manifest_dir = install_dir / "steamapps"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            (manifest_dir / f"appmanifest_{appid}.acf").write_text(
                _MANIFEST, encoding="utf-8"
            )
        return ContentResult(outcome=self.outcome, log_path=log_path)


@pytest.fixture()
def harness(tmp_path: Path):
    library = tmp_path / "library"
    (library / "steamapps").mkdir(parents=True)
    state_dir = tmp_path / "state"
    ledger = ExecutionLedger(state_dir / "ledger.sqlite3")
    session = FakeSession()
    content = FakeContent(log_dir=tmp_path / "logs")
    executor = Executor(
        ledger=ledger,
        session=session,
        content=content,
        library=library,
        state_dir=state_dir,
    )
    yield ledger, session, content, executor, library
    ledger.close()


def _authorized(ledger: ExecutionLedger) -> int:
    _, nonce = ledger.request(
        plan_key="k" * 8,
        plan_document=json.dumps(
            {
                "schema": "operation-plan/0.1",
                "operation": "install",
                "install_dir_name": "Spacewar",
            }
        ),
        operation="install",
        appid=480,
        account_alias="owner",
        machine_id="herb",
        policy_version="v",
    )
    return ledger.confirm(nonce=nonce, actor="discord:owner")


def test_happy_path_confirms_with_first_run_required(harness) -> None:
    ledger, session, _, executor, library = harness
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)

    assert report.outcome == "confirmed"
    assert "first run" in report.detail
    assert session.stops == 1 and session.starts == 1  # prior state restored
    adopted = library / "steamapps" / "appmanifest_480.acf"
    assert '"installdir"\t\t"Spacewar"' in adopted.read_text(encoding="utf-8")
    assert ledger.get(operation_id).state == "confirmed"


def test_gates_block_execution(harness) -> None:
    ledger, session, _, executor, _ = harness
    session.clear = False
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "gates" in report.detail
    assert ledger.get(operation_id).state == "authorized"  # untouched, retryable


def test_auth_required_fails_fast_and_restores_client(harness) -> None:
    ledger, session, content, executor, _ = harness
    content.outcome = "auth_required"
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "auth_required"
    assert ledger.get(operation_id).state == "failed"
    assert session.starts == 1


def test_client_not_running_is_not_restarted(harness) -> None:
    ledger, session, _, executor, _ = harness
    session.running = False
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "confirmed"
    assert session.stops == 0 and session.starts == 0


def test_missing_manifest_fails(harness) -> None:
    ledger, _, content, executor, _ = harness
    content.write_manifest = False
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "failed"
    assert "manifest" in report.detail


def test_reconcile_interrupted_download_resumes(harness) -> None:
    ledger, _, _, executor, _ = harness
    operation_id = _authorized(ledger)
    for state in ("lease_acquired", "client_stopping", "content_running"):
        ledger.transition(operation_id, state)

    actions = executor.reconcile()
    assert any("resume" in action for action in actions)
    assert ledger.get(operation_id).state == "interrupted"

    report = executor.execute(operation_id)
    assert report.outcome == "confirmed"


def test_reconcile_pre_content_death_aborts_cleanly(harness) -> None:
    ledger, _, _, executor, _ = harness
    operation_id = _authorized(ledger)
    ledger.transition(operation_id, "lease_acquired", prior_client_running=False)

    actions = executor.reconcile()
    assert actions and "aborted" in actions[0]
    assert ledger.get(operation_id).state == "aborted"

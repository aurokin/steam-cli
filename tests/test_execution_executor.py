"""Executor flow with fake session and content plane: gates, outcomes, resume."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest

from steam_agent.execution.content_plane import (
    ContentResult,
    adopt_manifest,
    locate_manifest,
)
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
        self.start_ok = True

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
        if not self.start_ok:
            return False
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


def _authorized(
    ledger: ExecutionLedger, *, install_dir_name: str = "Spacewar"
) -> int:
    _, nonce = ledger.request(
        plan_key="k" * 8,
        plan_document=json.dumps(
            {
                "schema": "operation-plan/0.1",
                "operation": "install",
                "install_dir_name": install_dir_name,
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


def test_unsafe_install_dir_name_aborts_before_side_effects(harness) -> None:
    ledger, session, _, executor, _ = harness
    operation_id = _authorized(ledger, install_dir_name="../../outside")
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "path component" in report.detail
    assert ledger.get(operation_id).state == "aborted"
    assert session.stops == 0  # client never touched


def test_client_restore_failure_is_reported(harness) -> None:
    ledger, session, _, executor, _ = harness
    session.start_ok = False
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "confirmed"
    assert "client restore failed" in report.detail
    assert "client restore failed" in (ledger.get(operation_id).detail or "")


def test_reconcile_interrupted_stays_resumable(harness) -> None:
    ledger, _, _, executor, _ = harness
    operation_id = _authorized(ledger)
    for state in ("lease_acquired", "client_stopping", "content_running"):
        ledger.transition(operation_id, state)

    executor.reconcile()
    actions = executor.reconcile()  # a second pass must not make it terminal
    assert ledger.get(operation_id).state == "interrupted"
    assert any("resume" in action for action in actions)


def _adopting_operation(ledger: ExecutionLedger, executor: Executor, library: Path) -> int:
    operation_id = _authorized(ledger)
    for state in ("lease_acquired", "client_stopping", "content_running", "adopting"):
        ledger.transition(operation_id, state)
    prior = library / "steamapps" / "appmanifest_480.acf"
    prior.write_text("old", encoding="utf-8")
    target = library.parent / "adoption-target" / "steamapps"
    target.mkdir(parents=True)
    (target / "appmanifest_480.acf").write_text(_MANIFEST, encoding="utf-8")
    adopt_manifest(
        source=locate_manifest(install_dir=target.parent, appid=480),
        library=library,
        appid=480,
        install_dir_name="Spacewar",
        journal_dir=executor._journal_dir,
    )
    return operation_id


def test_reconcile_adopting_completed_confirms(harness) -> None:
    ledger, _, _, executor, library = harness
    operation_id = _adopting_operation(ledger, executor, library)

    actions = executor.reconcile()
    assert ledger.get(operation_id).state == "confirmed"
    assert any("completed" in action for action in actions)


def test_reconcile_adopting_rolled_back_fails(harness) -> None:
    ledger, _, _, executor, library = harness
    operation_id = _adopting_operation(ledger, executor, library)
    adopted = library / "steamapps" / "appmanifest_480.acf"
    adopted.write_text('"AppState" { torn', encoding="utf-8")  # simulate crash

    actions = executor.reconcile()
    assert ledger.get(operation_id).state == "failed"
    assert any("restored" in action for action in actions)
    assert adopted.read_text(encoding="utf-8") == "old"  # backup reinstated

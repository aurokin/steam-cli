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
from steam_agent.execution.executor import (
    Executor,
    ExecutorLockedError,
    safe_install_dir_name,
)
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
        self.stop_ok = True
        self.steamcmd_alive = False

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

    def client_possibly_running(self) -> bool:
        return self.running

    def steamcmd_running(self) -> bool:
        return self.steamcmd_alive

    def stop_client(self) -> bool:
        self.stops += 1
        if not self.stop_ok:
            return False
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

    def install(
        self, *, account: str, appid: int, install_dir: Path, operation_id: int
    ) -> ContentResult:
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
    ledger: ExecutionLedger, *, install_dir_name: str | None = "Spacewar"
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
    assert not (executor._journal_dir / "adoption-480.json").exists()  # retired


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
    ledger, _, _, executor, library = harness
    operation_id = _authorized(ledger)
    for state in ("lease_acquired", "client_stopping", "content_running"):
        ledger.transition(operation_id, state)
    # Partial payload without a nested manifest must not block the resume.
    partial = library / "steamapps" / "common" / "Spacewar"
    partial.mkdir(parents=True)
    (partial / "partial.bin").write_text("x", encoding="utf-8")

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


def test_failure_path_stays_retryable_when_restore_fails(harness) -> None:
    ledger, session, content, executor, _ = harness
    content.outcome = "failed"
    session.start_ok = False
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "restore failed" in report.detail
    assert ledger.get(operation_id).state == "content_running"  # non-terminal


def test_client_restore_failure_stays_retryable(harness) -> None:
    ledger, session, _, executor, _ = harness
    session.start_ok = False
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "client restore failed" in report.detail
    # Non-terminal: reconciliation retries once the client can start again.
    assert ledger.get(operation_id).state == "client_restart_pending"

    session.start_ok = True
    executor.reconcile()
    assert ledger.get(operation_id).state == "confirmed"


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
        operation_id=operation_id,
    )
    return operation_id


def test_reconcile_adopting_completed_confirms(harness) -> None:
    ledger, _, _, executor, library = harness
    operation_id = _adopting_operation(ledger, executor, library)

    actions = executor.reconcile()
    assert ledger.get(operation_id).state == "confirmed"
    assert any("completed" in action for action in actions)
    assert not (executor._journal_dir / "adoption-480.json").exists()  # retired


def test_reconcile_leaves_state_while_restore_fails(harness) -> None:
    ledger, session, _, executor, _ = harness
    operation_id = _authorized(ledger)
    ledger.transition(operation_id, "lease_acquired", prior_client_running=True)
    session.running = False
    session.start_ok = False

    actions = executor.reconcile()
    assert ledger.get(operation_id).state == "lease_acquired"  # retryable
    assert any("retry" in action for action in actions)

    session.start_ok = True
    executor.reconcile()
    assert ledger.get(operation_id).state == "aborted"


def test_missing_plan_name_reuses_existing_installdir(harness) -> None:
    ledger, _, _, executor, library = harness
    (library / "steamapps" / "appmanifest_480.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"480"\n\t"installdir"\t\t"OldDir"\n'
        '\t"StateFlags"\t\t"4"\n}\n',
        encoding="utf-8",
    )
    operation_id = _authorized(ledger, install_dir_name="")
    report = executor.execute(operation_id)
    assert report.outcome == "confirmed"
    adopted = (library / "steamapps" / "appmanifest_480.acf").read_text(
        encoding="utf-8"
    )
    assert '"installdir"\t\t"OldDir"' in adopted  # not app_480


def test_null_plan_name_treated_as_unspecified(harness) -> None:
    ledger, _, _, executor, library = harness
    (library / "steamapps" / "appmanifest_480.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"480"\n\t"installdir"\t\t"OldDir"\n'
        '\t"StateFlags"\t\t"4"\n}\n',
        encoding="utf-8",
    )
    operation_id = _authorized(ledger, install_dir_name=None)
    report = executor.execute(operation_id)
    assert report.outcome == "confirmed"
    adopted = (library / "steamapps" / "appmanifest_480.acf").read_text(
        encoding="utf-8"
    )
    assert '"installdir"\t\t"OldDir"' in adopted  # not literal "None"


def test_symlinked_lock_file_is_rejected(harness) -> None:
    import hashlib

    ledger, _, _, executor, library = harness
    key = hashlib.sha256(str(library.resolve()).encode("utf-8")).hexdigest()[:16]
    lock_path = Path("/tmp") / f"steam-broker-{key}.lock"
    decoy = library.parent / "decoy"
    decoy.write_text("", encoding="utf-8")
    lock_path.symlink_to(decoy)
    try:
        with pytest.raises(ExecutorLockedError):
            executor._lock()
    finally:
        lock_path.unlink()


def test_client_reappearing_during_stop_aborts(harness) -> None:
    ledger, session, _, executor, _ = harness
    original_stop = session.stop_client

    def stop_then_relaunch() -> bool:
        result = original_stop()
        session.running = True  # user or autostart relaunched Steam
        return result

    session.stop_client = stop_then_relaunch
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "regressed during client stop" in report.detail


def test_lock_scoped_to_library_not_state_dir(harness, tmp_path: Path) -> None:
    ledger, session, content, executor, library = harness
    other = Executor(
        ledger=ledger,
        session=session,
        content=content,
        library=library,
        state_dir=tmp_path / "other-state",
    )
    lock = executor._lock()
    try:
        with pytest.raises(ExecutorLockedError):
            other._lock()
    finally:
        lock.close()


def test_install_dir_claimed_by_other_title_aborts(harness) -> None:
    ledger, _, _, executor, library = harness
    (library / "steamapps" / "appmanifest_999.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"999"\n\t"installdir"\t\t"Spacewar"\n}\n',
        encoding="utf-8",
    )
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "another installed title" in report.detail


def test_case_aliased_install_dir_collision_aborts(harness) -> None:
    ledger, _, _, executor, library = harness
    (library / "steamapps" / "appmanifest_999.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"999"\n\t"installdir"\t\t"spacewar"\n}\n',
        encoding="utf-8",
    )
    operation_id = _authorized(ledger)  # plan says "Spacewar"
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "another installed title" in report.detail


def test_unowned_existing_target_directory_aborts(harness) -> None:
    ledger, _, _, executor, library = harness
    stray = library / "steamapps" / "common" / "Spacewar"
    stray.mkdir(parents=True)
    (stray / "somefile.bin").write_text("data", encoding="utf-8")

    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "not owned" in report.detail


def test_failed_partial_install_stays_retryable(harness) -> None:
    ledger, _, content, executor, library = harness
    content.outcome = "failed"
    first = _authorized(ledger)
    original_install = content.install

    def install_with_partial(**kwargs):
        partial = kwargs["install_dir"] / "steamapps" / "downloading"
        partial.mkdir(parents=True, exist_ok=True)
        (partial / "chunk.bin").write_text("x", encoding="utf-8")
        return original_install(**kwargs)

    content.install = install_with_partial
    assert executor.execute(first).outcome == "failed"
    assert (library / "steamapps" / "common" / "Spacewar" / "steamapps").exists()

    # A fresh retry (not a resume) must reuse the partial directory.
    content.outcome = "installed"
    retry = _authorized(ledger)
    report = executor.execute(retry)
    assert report.outcome == "confirmed"
    # Marker retired once the steamcmd manifest proves ownership itself.
    assert not executor._owned_marker(480).exists()


def test_resume_stop_failure_attempts_client_restore(harness) -> None:
    ledger, session, _, executor, _ = harness
    operation_id = _authorized(ledger)
    ledger.transition(operation_id, "lease_acquired", prior_client_running=True)
    for state in ("client_stopping", "content_running", "interrupted"):
        ledger.transition(operation_id, state)
    session.stop_ok = False

    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "would not exit for resume" in report.detail
    assert session.starts == 1  # prior run-state restoration attempted
    assert ledger.get(operation_id).state == "interrupted"  # still resumable


def test_reconcile_adopting_stop_failure_attempts_restore(harness) -> None:
    ledger, session, _, executor, library = harness
    operation_id = _authorized(ledger)
    ledger.transition(operation_id, "lease_acquired", prior_client_running=True)
    for state in ("client_stopping", "content_running", "adopting"):
        ledger.transition(operation_id, state)
    session.stop_ok = False

    actions = executor.reconcile()
    assert ledger.get(operation_id).state == "adopting"  # deferred, retryable
    assert any("deferred" in action for action in actions)
    assert session.starts == 1  # prior run-state restoration attempted


def test_plan_name_conflicting_with_existing_install_aborts(harness) -> None:
    ledger, _, _, executor, library = harness
    (library / "steamapps" / "appmanifest_480.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"480"\n\t"installdir"\t\t"OldDir"\n'
        '\t"StateFlags"\t\t"4"\n}\n',
        encoding="utf-8",
    )
    operation_id = _authorized(ledger)  # plan explicitly says "Spacewar"
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "move is not an executable operation" in report.detail


def test_unreadable_own_manifest_blocks_fallback(harness) -> None:
    ledger, _, _, executor, library = harness
    (library / "steamapps" / "appmanifest_480.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"480"\n}\n', encoding="utf-8"  # no installdir
    )
    operation_id = _authorized(ledger, install_dir_name="")
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "unreadable" in report.detail


def test_stop_failure_attempts_client_restore(harness) -> None:
    ledger, session, _, executor, _ = harness
    session.stop_ok = False
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "would not exit" in report.detail
    assert session.starts == 1  # prior run-state restoration attempted
    assert ledger.get(operation_id).state == "aborted"


def test_client_relaunch_surviving_late_stop_fails(harness) -> None:
    ledger, session, content, executor, _ = harness
    original_install = content.install

    def install_and_relaunch(**kwargs):
        result = original_install(**kwargs)
        session.running = True  # client relaunched during the download

        def stop_but_respawn() -> bool:
            session.stops += 1
            return True  # exits, but respawns instantly; running stays True

        session.stop_client = stop_but_respawn
        return result

    content.install = install_and_relaunch
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "failed"
    assert "adoption skipped" in report.detail


def test_gate_regression_during_download_skips_adoption(harness) -> None:
    ledger, session, content, executor, _ = harness
    original_install = content.install

    def install_and_regress(**kwargs):
        session.clear = False  # a game/Remote Play/download appeared mid-run
        return original_install(**kwargs)

    content.install = install_and_regress
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "failed"
    assert "regressed" in report.detail
    assert ledger.get(operation_id).state == "failed"


def test_execute_defers_while_steamcmd_survives(harness) -> None:
    ledger, session, _, executor, _ = harness
    session.steamcmd_alive = True
    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "steamcmd" in report.detail
    assert ledger.get(operation_id).state == "authorized"  # retryable in place


def test_reconcile_defers_while_steamcmd_survives(harness) -> None:
    ledger, session, _, executor, _ = harness
    operation_id = _authorized(ledger)
    for state in ("lease_acquired", "client_stopping", "content_running"):
        ledger.transition(operation_id, state)
    session.steamcmd_alive = True

    actions = executor.reconcile()
    assert ledger.get(operation_id).state == "content_running"  # untouched
    assert any("deferred" in action for action in actions)


def test_safe_install_dir_name_rejects_acf_breaking_characters() -> None:
    assert not safe_install_dir_name('Bad"Name')
    assert not safe_install_dir_name("two\nlines")
    assert not safe_install_dir_name("../../outside")
    assert safe_install_dir_name("Desk Job")


def test_reconcile_detects_client_redownload_as_contradicted(harness) -> None:
    ledger, _, _, executor, library = harness
    operation_id = _adopting_operation(ledger, executor, library)
    (library / "steamapps" / "downloading" / "480").mkdir(parents=True)

    executor.reconcile()
    assert ledger.get(operation_id).state == "contradicted"


def test_symlinked_common_dir_aborts(harness) -> None:
    ledger, _, _, executor, library = harness
    outside = library.parent / "elsewhere"
    outside.mkdir()
    (library / "steamapps" / "common").symlink_to(outside)

    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "outside the library" in report.detail


def test_symlinked_install_target_aborts(harness) -> None:
    ledger, session, _, executor, library = harness
    common = library / "steamapps" / "common"
    common.mkdir(parents=True)
    outside = library.parent / "outside"
    outside.mkdir()
    (common / "Spacewar").symlink_to(outside)

    operation_id = _authorized(ledger)
    report = executor.execute(operation_id)
    assert report.outcome == "aborted"
    assert "symlink" in report.detail
    assert session.stops == 0


def test_reconcile_adopting_without_journal_fails(harness) -> None:
    ledger, _, _, executor, library = harness
    operation_id = _authorized(ledger)
    for state in ("lease_acquired", "client_stopping", "content_running", "adopting"):
        ledger.transition(operation_id, state)
    # An older fully-installed manifest exists, but this operation never
    # journaled an adoption — it must not be credited with one.
    (library / "steamapps" / "appmanifest_480.acf").write_text(
        _MANIFEST, encoding="utf-8"
    )

    actions = executor.reconcile()
    assert ledger.get(operation_id).state == "failed"
    assert any("clean" in action for action in actions)


def test_reconcile_adopting_rolled_back_fails(harness) -> None:
    ledger, _, _, executor, library = harness
    operation_id = _adopting_operation(ledger, executor, library)
    adopted = library / "steamapps" / "appmanifest_480.acf"
    adopted.write_text('"AppState" { torn', encoding="utf-8")  # simulate crash

    actions = executor.reconcile()
    assert ledger.get(operation_id).state == "failed"
    assert any("restored" in action for action in actions)
    assert adopted.read_text(encoding="utf-8") == "old"  # backup reinstated

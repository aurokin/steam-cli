"""Executor: drives one authorized operation through the ledger state machine.

One operation at a time per Steam installation (OS-level flock plus the
ledger's single-active constraint).  Gates are re-checked immediately before
every destructive step and anything but an explicit pass defers.  The client
is never killed; prior client run-state is restored afterwards.  Postcondition
verification only ever reports what manifests actually show.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
from pathlib import Path
from typing import Literal

from steam_agent.execution.content_plane import (
    SteamcmdAdapter,
    adopt_manifest,
    locate_manifest,
    manifest_state_flags,
    reconcile_adoption,
)
from steam_agent.execution.ledger import ExecutionLedger
from steam_agent.execution.linux_session import LinuxSession

ExecuteOutcome = Literal[
    "confirmed",
    "unconfirmed",
    "contradicted",
    "aborted",
    "failed",
    "auth_required",
]

_STATE_FULLY_INSTALLED = 4


class ExecutorLockedError(RuntimeError):
    """Another executor process holds the machine lock."""


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    operation_id: int
    outcome: ExecuteOutcome
    detail: str


class Executor:
    def __init__(
        self,
        *,
        ledger: ExecutionLedger,
        session: LinuxSession,
        content: SteamcmdAdapter,
        library: Path,
        state_dir: Path,
    ) -> None:
        self._ledger = ledger
        self._session = session
        self._content = content
        self._library = library
        self._state_dir = state_dir
        self._journal_dir = state_dir / "journal"

    def _lock(self) -> object:
        lock_path = self._state_dir / "executor.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise ExecutorLockedError("another execution holds the lock") from error
        return handle

    def execute(self, operation_id: int) -> ExecutionReport:
        lock = self._lock()
        try:
            return self._execute_locked(operation_id)
        finally:
            lock.close()  # type: ignore[attr-defined]

    def _execute_locked(self, operation_id: int) -> ExecutionReport:
        ledger = self._ledger
        operation = ledger.get(operation_id)
        resuming = operation.state == "interrupted"
        if operation.state not in {"authorized", "interrupted"}:
            return ExecutionReport(
                operation_id, "aborted", f"not executable: {operation.state}"
            )
        if not ledger.window_valid(operation_id):
            ledger.expire_lapsed()
            if resuming:
                ledger.transition(
                    operation_id, "failed", detail="window lapsed before resume"
                )
            return ExecutionReport(
                operation_id, "aborted", "execution window lapsed"
            )

        gates = self._session.gates()
        if not gates.all_clear():
            return ExecutionReport(
                operation_id,
                "aborted",
                "lease gates not clear: "
                f"game={gates.game_running} remote_play={gates.remote_play} "
                f"download={gates.download_in_flight}",
            )

        if resuming:
            prior_running = operation.prior_client_running or False
            if self._session.client_running() and not self._session.stop_client():
                return ExecutionReport(
                    operation_id, "aborted", "client would not exit for resume"
                )
        else:
            prior_running = self._session.client_running()
            ledger.transition(
                operation_id,
                "lease_acquired",
                prior_client_running=prior_running,
            )
            ledger.transition(operation_id, "client_stopping")
            if prior_running and not self._session.stop_client():
                ledger.transition(
                    operation_id, "aborted", detail="client would not exit cleanly"
                )
                return ExecutionReport(
                    operation_id, "aborted", "client would not exit cleanly"
                )

        plan = ledger.plan_document(operation_id)
        install_dir_name = str(plan.get("install_dir_name", ""))
        if not install_dir_name:
            install_dir_name = f"app_{operation.appid}"
        target = self._library / "steamapps" / "common" / install_dir_name

        ledger.transition(
            operation_id,
            "content_running",
            detail="resume" if resuming else None,
        )
        result = self._content.install(
            account=operation.account_alias,
            appid=operation.appid,
            install_dir=target,
        )
        if result.outcome == "auth_required":
            ledger.transition(
                operation_id, "failed", detail="auth_required: owner re-seed"
            )
            self._restore_client(prior_running)
            return ExecutionReport(
                operation_id, "auth_required", "steamcmd needs re-authentication"
            )
        if result.outcome != "installed":
            ledger.transition(
                operation_id, "failed", detail=f"steamcmd failed: {result.log_path.name}"
            )
            self._restore_client(prior_running)
            return ExecutionReport(
                operation_id, "failed", f"steamcmd failed; see {result.log_path.name}"
            )

        source = locate_manifest(install_dir=target, appid=operation.appid)
        if source is None:
            ledger.transition(
                operation_id, "failed", detail="no steamcmd-written manifest"
            )
            self._restore_client(prior_running)
            return ExecutionReport(
                operation_id, "failed", "steamcmd produced no manifest"
            )

        ledger.transition(operation_id, "adopting")
        adopted = adopt_manifest(
            source=source,
            library=self._library,
            appid=operation.appid,
            install_dir_name=install_dir_name,
            journal_dir=self._journal_dir,
        )

        ledger.transition(operation_id, "client_restart_pending")
        self._restore_client(prior_running)

        ledger.transition(operation_id, "verifying")
        flags = manifest_state_flags(adopted)
        if flags == _STATE_FULLY_INSTALLED:
            downloading = (
                self._library / "steamapps" / "downloading" / str(operation.appid)
            )
            if downloading.exists():
                ledger.transition(
                    operation_id,
                    "contradicted",
                    detail="client re-downloading after adoption",
                )
                return ExecutionReport(
                    operation_id,
                    "contradicted",
                    "client rejected the adopted manifest and is re-downloading",
                )
            ledger.transition(
                operation_id,
                "confirmed",
                detail="client_adopted; first_run_required",
            )
            return ExecutionReport(
                operation_id,
                "confirmed",
                "content present and adopted; first run still required",
            )
        ledger.transition(
            operation_id,
            "unconfirmed",
            detail=f"manifest StateFlags={flags}",
        )
        return ExecutionReport(
            operation_id,
            "unconfirmed",
            f"adoption not confirmed (StateFlags={flags})",
        )

    def _restore_client(self, prior_running: bool | None) -> None:
        if prior_running:
            self._session.start_client()

    # -- reconciliation ---------------------------------------------------

    def reconcile(self) -> list[str]:
        """Map every non-terminal operation to exactly one recovery action."""

        actions: list[str] = []
        ledger = self._ledger
        ledger.expire_lapsed()
        active = ledger.active()
        if active is None:
            return actions
        state = active.state
        operation_id = active.operation_id

        if state in {"pending_confirmation", "authorized"}:
            return actions  # no side effects yet; expiry alone governs
        if state in {"lease_acquired", "client_stopping"}:
            ledger.transition(operation_id, "aborted", detail="reconciled: no side effects")
            actions.append(f"{operation_id}: aborted (died before content ran)")
            self._restore_client(active.prior_client_running)
            return actions
        if state == "content_running":
            if ledger.window_valid(operation_id):
                actions.append(f"{operation_id}: resume via execute()")
                ledger.transition(operation_id, "interrupted", detail="resume candidate")
            else:
                ledger.transition(
                    operation_id, "failed", detail="window lapsed mid-download"
                )
                actions.append(f"{operation_id}: failed (window lapsed; partial dir invisible)")
                self._restore_client(active.prior_client_running)
            return actions
        if state == "adopting":
            verdict = reconcile_adoption(
                library=self._library,
                appid=active.appid,
                journal_dir=self._journal_dir,
            )
            ledger.transition(operation_id, "verifying", detail=f"adoption {verdict}")
            flags_path = (
                self._library / "steamapps" / f"appmanifest_{active.appid}.acf"
            )
            flags = manifest_state_flags(flags_path)
            outcome = "confirmed" if flags == _STATE_FULLY_INSTALLED else "unconfirmed"
            ledger.transition(operation_id, outcome, detail=f"reconciled: {verdict}")
            actions.append(f"{operation_id}: adoption {verdict} -> {outcome}")
            self._restore_client(active.prior_client_running)
            return actions
        if state in {"client_restart_pending", "verifying", "interrupted"}:
            self._restore_client(active.prior_client_running)
            if state != "verifying":
                ledger.transition(operation_id, "verifying", detail="reconciled")
            flags = manifest_state_flags(
                self._library / "steamapps" / f"appmanifest_{active.appid}.acf"
            )
            outcome = "confirmed" if flags == _STATE_FULLY_INSTALLED else "unconfirmed"
            ledger.transition(operation_id, outcome, detail="reconciled")
            actions.append(f"{operation_id}: {state} -> {outcome}")
            return actions
        return actions

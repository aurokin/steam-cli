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
    clear_adoption_journal,
    locate_manifest,
    manifest_install_dir,
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


def safe_install_dir_name(name: str) -> bool:
    """True only for one path component that can be quoted into an ACF value."""

    if not name or name in {".", ".."}:
        return False
    if any(character in name for character in ("/", "\\", '"')):
        return False
    # Control characters (newlines especially) could inject VDF fields.
    return not any(ord(character) < 32 for character in name)


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

        plan = ledger.plan_document(operation_id)
        install_dir_name = str(plan.get("install_dir_name", ""))
        if not install_dir_name:
            # Prefer the directory the client already uses for this AppID:
            # inventing a new one would re-download and orphan the install.
            existing = manifest_install_dir(
                self._library / "steamapps" / f"appmanifest_{operation.appid}.acf"
            )
            install_dir_name = existing or f"app_{operation.appid}"
        if not safe_install_dir_name(install_dir_name):
            ledger.transition(
                operation_id, "aborted", detail="unsafe install_dir_name"
            )
            return ExecutionReport(
                operation_id,
                "aborted",
                "install_dir_name must be a single path component",
            )
        common = self._library / "steamapps" / "common"
        target = common / install_dir_name
        try:
            # A pre-existing symlink at the target would let a safe-looking
            # name write outside the library; resolve before trusting it.
            escapes = not target.resolve().is_relative_to(common.resolve())
        except OSError:
            escapes = True
        if escapes:
            ledger.transition(
                operation_id, "aborted", detail="install target escapes library"
            )
            return ExecutionReport(
                operation_id,
                "aborted",
                "install target resolves outside the library",
            )
        for manifest in sorted((self._library / "steamapps").glob("appmanifest_*.acf")):
            if manifest.name == f"appmanifest_{operation.appid}.acf":
                continue
            if manifest_install_dir(manifest) == install_dir_name:
                ledger.transition(
                    operation_id,
                    "aborted",
                    detail="install_dir_name claimed by another title",
                )
                return ExecutionReport(
                    operation_id,
                    "aborted",
                    "install_dir_name belongs to another installed title",
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
            if self._session.client_possibly_running() and not self._session.stop_client():
                return ExecutionReport(
                    operation_id, "aborted", "client would not exit for resume"
                )
        else:
            prior_running = self._session.client_possibly_running()
            ledger.transition(
                operation_id,
                "lease_acquired",
                prior_client_running=prior_running,
            )
            ledger.transition(operation_id, "client_stopping")
            if prior_running and not self._session.stop_client():
                # The main process may already be gone with a helper (or a
                # probe error) lingering; try to restore the prior state
                # before going terminal.  A second -silent start is safe:
                # Steam is single-instance.
                note = self._restore_note(prior_running)
                ledger.transition(
                    operation_id, "aborted", detail="client would not exit cleanly"
                )
                return ExecutionReport(
                    operation_id, "aborted", f"client would not exit cleanly{note}"
                )

        ledger.transition(
            operation_id,
            "content_running",
            detail="resume" if resuming else None,
        )
        result = self._content.install(
            account=operation.account_alias,
            appid=operation.appid,
            install_dir=target,
            operation_id=operation_id,
        )
        # Restore the client before recording a terminal state: a crash in
        # between then leaves a non-terminal row that reconciliation still
        # owns, instead of a terminal row with the client silently stopped.
        if result.outcome == "auth_required":
            note = self._restore_note(prior_running)
            ledger.transition(
                operation_id, "failed", detail="auth_required: owner re-seed"
            )
            return ExecutionReport(
                operation_id,
                "auth_required",
                f"steamcmd needs re-authentication{note}",
            )
        if result.outcome != "installed":
            note = self._restore_note(prior_running)
            ledger.transition(
                operation_id, "failed", detail=f"steamcmd failed: {result.log_path.name}"
            )
            return ExecutionReport(
                operation_id,
                "failed",
                f"steamcmd failed; see {result.log_path.name}{note}",
            )

        source = locate_manifest(install_dir=target, appid=operation.appid)
        if source is None:
            note = self._restore_note(prior_running)
            ledger.transition(
                operation_id, "failed", detail="no steamcmd-written manifest"
            )
            return ExecutionReport(
                operation_id, "failed", f"steamcmd produced no manifest{note}"
            )

        # steamcmd can run for up to 30 minutes; re-check the full lease and
        # re-assert the stopped client before touching steamapps/ — never
        # adopt under a live client or a lease that regressed.
        late_gates = self._session.gates()
        if not late_gates.all_clear():
            note = self._restore_note(prior_running)
            ledger.transition(
                operation_id,
                "failed",
                detail="lease gates regressed before adoption",
            )
            return ExecutionReport(
                operation_id,
                "failed",
                f"lease gates regressed before adoption; adoption skipped{note}",
            )
        if self._session.client_possibly_running() and not self._session.stop_client():
            # The failed stop may still have killed the main process (helper
            # lingering, probe error): attempt restoration before going
            # terminal so the prior run-state is not silently lost.
            note = self._restore_note(prior_running)
            ledger.transition(
                operation_id,
                "failed",
                detail="client restarted mid-operation; adoption skipped",
            )
            return ExecutionReport(
                operation_id,
                "failed",
                f"client restarted mid-operation; adoption skipped{note}",
            )

        ledger.transition(operation_id, "adopting")
        adopted = adopt_manifest(
            source=source,
            library=self._library,
            appid=operation.appid,
            install_dir_name=install_dir_name,
            journal_dir=self._journal_dir,
            operation_id=operation_id,
        )
        # Leave the ledger's adopting state before retiring the journal: a
        # crash in between then reconciles from a state whose branch does not
        # consult the journal, never as adopting-with-no-journal (which would
        # read as an unproven adoption after the manifest was already swapped).
        ledger.transition(operation_id, "client_restart_pending")
        clear_adoption_journal(appid=operation.appid, journal_dir=self._journal_dir)
        client_restored = self._restore_client(prior_running)
        note = "" if client_restored else "; client restore failed"

        # Verification is manifest-evidence-based by design (ADR 0027 semantic
        # postconditions; Phase 0 measured 4/4 client adoption with zero
        # re-download).  The downloading/ probe is a best-effort contradiction
        # detector, not proof of client acceptance — the client validates on
        # its own schedule, so the outcome wording below distinguishes whether
        # a running client has had any chance to observe the adoption.
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
            if prior_running and client_restored:
                detail = "client_adopted; first_run_required"
                summary = "content present and adopted; first run still required"
            else:
                detail = "content_present; client validation deferred to next client start"
                summary = (
                    "content present and adopted; client validation deferred"
                    " to next client start; first run still required"
                )
            ledger.transition(operation_id, "confirmed", detail=detail + note)
            return ExecutionReport(operation_id, "confirmed", summary + note)
        ledger.transition(
            operation_id,
            "unconfirmed",
            detail=f"manifest StateFlags={flags}{note}",
        )
        return ExecutionReport(
            operation_id,
            "unconfirmed",
            f"adoption not confirmed (StateFlags={flags}){note}",
        )

    def _restore_client(self, prior_running: bool | None) -> bool:
        """Restore prior client run-state; False means a restart was needed but failed."""

        if not prior_running:
            return True
        return self._session.start_client()

    def _restore_note(self, prior_running: bool | None) -> str:
        return "" if self._restore_client(prior_running) else "; client restore failed"

    def _verified_outcome(self, appid: int) -> tuple[str, str]:
        """Manifest-evidence verdict shared by execution and reconciliation."""

        flags = manifest_state_flags(
            self._library / "steamapps" / f"appmanifest_{appid}.acf"
        )
        if flags != _STATE_FULLY_INSTALLED:
            return "unconfirmed", f"manifest StateFlags={flags}"
        if (self._library / "steamapps" / "downloading" / str(appid)).exists():
            return "contradicted", "client re-downloading after adoption"
        return "confirmed", "client_adopted"

    # -- reconciliation ---------------------------------------------------

    def reconcile(self) -> list[str]:
        """Map every non-terminal operation to exactly one recovery action."""

        lock = self._lock()
        try:
            return self._reconcile_locked()
        finally:
            lock.close()  # type: ignore[attr-defined]

    def _reconcile_locked(self) -> list[str]:
        actions: list[str] = []
        ledger = self._ledger
        ledger.expire_lapsed()
        active = ledger.active()
        if active is None:
            return actions
        state = active.state
        operation_id = active.operation_id

        def restore() -> bool:
            # Restore BEFORE terminal transitions: while restoration keeps
            # failing, the operation stays non-terminal so a later reconcile
            # retries it instead of orphaning a stopped client.
            if self._restore_client(active.prior_client_running):
                return True
            actions.append(
                f"{operation_id}: client restore failed; state left for retry"
            )
            return False

        if state in {"pending_confirmation", "authorized"}:
            return actions  # no side effects yet; expiry alone governs
        if state in {"lease_acquired", "client_stopping"}:
            if not restore():
                return actions
            ledger.transition(operation_id, "aborted", detail="reconciled: no side effects")
            actions.append(f"{operation_id}: aborted (died before content ran)")
            return actions
        if state == "content_running":
            if self._session.steamcmd_running():
                # A surviving steamcmd child would make resume a second
                # concurrent writer; defer until it is provably gone.
                actions.append(
                    f"{operation_id}: steamcmd may still be running; deferred"
                )
                return actions
            if ledger.window_valid(operation_id):
                actions.append(f"{operation_id}: resume via execute()")
                ledger.transition(operation_id, "interrupted", detail="resume candidate")
            else:
                if not restore():
                    return actions
                ledger.transition(
                    operation_id, "failed", detail="window lapsed mid-download"
                )
                actions.append(f"{operation_id}: failed (window lapsed; partial dir invisible)")
            return actions
        if state == "interrupted":
            # Already a resume candidate: leave it resumable while the window
            # holds; repeated reconciliation must never make it terminal.
            if ledger.window_valid(operation_id):
                actions.append(f"{operation_id}: resume via execute()")
            else:
                if not restore():
                    return actions
                ledger.transition(
                    operation_id, "failed", detail="window lapsed before resume"
                )
                actions.append(f"{operation_id}: failed (window lapsed before resume)")
            return actions
        if state == "adopting":
            verdict = reconcile_adoption(
                library=self._library,
                appid=active.appid,
                journal_dir=self._journal_dir,
                operation_id=operation_id,
            )
            if verdict != "completed":
                # restored: the destination is the pre-operation backup.
                # clean/stale: this operation never journaled its adoption
                # (or only a prior operation's journal survived), so a
                # StateFlags=4 manifest proves nothing about this operation.
                if not restore():
                    return actions
                ledger.transition(
                    operation_id, "failed", detail=f"reconciled: adoption {verdict}"
                )
                actions.append(f"{operation_id}: adoption {verdict} -> failed")
                return actions
            ledger.transition(
                operation_id, "client_restart_pending", detail=f"adoption {verdict}"
            )
            if not restore():
                return actions  # retried from client_restart_pending
            ledger.transition(operation_id, "verifying", detail=f"adoption {verdict}")
            outcome, why = self._verified_outcome(active.appid)
            ledger.transition(operation_id, outcome, detail=f"reconciled: {why}")
            actions.append(f"{operation_id}: adoption {verdict} -> {outcome}")
            return actions
        if state in {"client_restart_pending", "verifying"}:
            # The swap completed before these states; any journal left behind
            # is a crash remnant and must not outlive the operation.
            clear_adoption_journal(appid=active.appid, journal_dir=self._journal_dir)
            if not restore():
                return actions
            if state != "verifying":
                ledger.transition(operation_id, "verifying", detail="reconciled")
            outcome, why = self._verified_outcome(active.appid)
            ledger.transition(operation_id, outcome, detail=f"reconciled: {why}")
            actions.append(f"{operation_id}: {state} -> {outcome}")
            return actions
        return actions

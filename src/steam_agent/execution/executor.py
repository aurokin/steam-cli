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
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from steam_agent.execution.content_plane import (
    AdoptionError,
    SteamcmdAdapter,
    _durable_write,
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
    # Typical filesystem component limit; a longer name would only surface
    # as ENAMETOOLONG mid-operation, after the client was already stopped.
    if len(name.encode("utf-8")) > 255:
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
        # Keyed by the resolved library, not the state dir: two brokers with
        # different state dirs but one library must share one lock.  (The
        # library itself is never written to — ADR 0027 permits only the
        # adopted manifest under steamapps/.)
        library_key = hashlib.sha256(
            str(self._library.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        # Fixed /tmp, not tempfile.gettempdir(): TMPDIR is caller-controlled
        # and a per-process value would defeat cross-broker serialization.
        lock_path = Path("/tmp") / f"steam-broker-{library_key}.lock"
        # /tmp is shared: O_NOFOLLOW refuses a planted symlink outright,
        # ownership is checked on the opened inode, and the directory entry
        # is re-lstat'ed after locking so an unlink/replace cannot yield two
        # holders.  All failures are fail-closed (no execution), never
        # fail-open.  The sticky bit stops others unlinking our own file.
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
        except OSError as error:
            raise ExecutorLockedError("cannot open the lock file safely") from error
        handle = os.fdopen(descriptor, "r+")
        try:
            opened = os.fstat(descriptor)
            if opened.st_uid not in (os.geteuid(), 0):
                raise ExecutorLockedError("lock file owned by another user")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current = os.lstat(lock_path)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ExecutorLockedError("lock file was replaced concurrently")
        except OSError as error:
            handle.close()
            raise ExecutorLockedError("another execution holds the lock") from error
        except ExecutorLockedError:
            handle.close()
            raise
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
                # The client may still be stopped from the interrupted run;
                # restore it before this row becomes terminal (terminal rows
                # are invisible to reconciliation).
                if not self._restore_client(operation.prior_client_running):
                    return ExecutionReport(
                        operation_id,
                        "aborted",
                        "window lapsed; client restore failed; state left for retry",
                    )
                ledger.transition(
                    operation_id, "failed", detail="window lapsed before resume"
                )
            return ExecutionReport(
                operation_id, "aborted", "execution window lapsed"
            )

        plan = ledger.plan_document(operation_id)
        raw_name = plan.get("install_dir_name")
        if raw_name is not None and not isinstance(raw_name, str):
            ledger.transition(
                operation_id, "aborted", detail="unsafe install_dir_name"
            )
            return ExecutionReport(
                operation_id, "aborted", "install_dir_name must be a string"
            )
        install_dir_name = raw_name or ""
        # The directory the client already uses for this AppID always wins:
        # any other directory would re-download and orphan the install, and
        # move is not an executable operation in Phase 1.  Reusing the live
        # directory in place is the accepted recoverable-update contract
        # (ADR 0027 clause 5): steamcmd validates and repairs content on
        # retry, and failure cost is bounded at temporary unplayability.
        own_manifest = (
            self._library / "steamapps" / f"appmanifest_{operation.appid}.acf"
        )
        if own_manifest.exists():
            existing = manifest_install_dir(own_manifest)
            if existing is None:
                # An install exists but its directory is unknowable; any
                # chosen directory could orphan it.
                ledger.transition(
                    operation_id,
                    "aborted",
                    detail="existing manifest unreadable",
                )
                return ExecutionReport(
                    operation_id,
                    "aborted",
                    "existing manifest for this title is unreadable",
                )
            if (
                install_dir_name
                and install_dir_name.casefold() != existing.casefold()
            ):
                ledger.transition(
                    operation_id,
                    "aborted",
                    detail="plan name conflicts with existing install directory",
                )
                return ExecutionReport(
                    operation_id,
                    "aborted",
                    "plan install_dir_name conflicts with the existing install"
                    " directory; move is not an executable operation",
                )
            install_dir_name = existing
        elif not install_dir_name:
            install_dir_name = f"app_{operation.appid}"
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
        if target.is_symlink():
            # Even an in-library symlink can alias another title's directory.
            ledger.transition(
                operation_id, "aborted", detail="install target is a symlink"
            )
            return ExecutionReport(
                operation_id, "aborted", "install target is a symlink"
            )
        try:
            # A pre-existing symlink at the target (or at common/ itself)
            # would let a safe-looking name write outside the library;
            # anchor the check to the configured library, never to common's
            # own resolution.
            escapes = not target.resolve().is_relative_to(self._library.resolve())
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
            other_dir = manifest_install_dir(manifest)
            if other_dir is None:
                # Unreadable/malformed manifest: ownership of the target
                # directory cannot be proven, so fail closed.
                ledger.transition(
                    operation_id,
                    "aborted",
                    detail=f"cannot inspect {manifest.name}",
                )
                return ExecutionReport(
                    operation_id,
                    "aborted",
                    "another title's manifest is unreadable; collision unprovable",
                )
            # Casefolded: a case-insensitive library filesystem (exFAT)
            # aliases names that differ only by case.
            if other_dir.casefold() == install_dir_name.casefold():
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

        if target.exists():
            # An existing directory may be reused only with ownership
            # evidence: steamcmd's own manifest from a prior attempt, the
            # client manifest for this AppID naming it, the inode-bound
            # marker, or an empty directory.  Anything else (orphaned,
            # manually managed) must not be written over.  Resumes are NOT
            # exempt: the partial directory could have been deleted and the
            # path repurposed while the operation sat interrupted, which
            # only the marker's inode comparison detects.
            own_library_dir = manifest_install_dir(
                self._library / "steamapps" / f"appmanifest_{operation.appid}.acf"
            )
            try:
                owned = (
                    locate_manifest(install_dir=target, appid=operation.appid)
                    is not None
                    or (
                        own_library_dir is not None
                        and own_library_dir.casefold() == install_dir_name.casefold()
                    )
                    # A prior broker operation for this AppID recorded the
                    # directory (name and inode) before its steamcmd ran: a
                    # failed partial install stays retryable without manual
                    # cleanup.
                    or self._marker_owns_target(
                        operation.appid, install_dir_name, target
                    )
                    or not any(target.iterdir())
                )
            except OSError:
                owned = False
            if not owned:
                ledger.transition(
                    operation_id,
                    "aborted",
                    detail="existing target directory not owned by this title",
                )
                return ExecutionReport(
                    operation_id,
                    "aborted",
                    "existing target directory is not owned by this title",
                )

        # Any surviving steamcmd (an orphaned child of a crashed run — even
        # one owned by a misconfigured second broker) is a concurrent
        # writer; never start another beside it.  One state dir per library
        # is the deployment contract; the /tmp flock covers live processes
        # and this probe covers detached ones.  Checked before any side
        # effect so the operation stays retryable in place.
        if self._session.steamcmd_running():
            return ExecutionReport(
                operation_id,
                "aborted",
                "a steamcmd process is already running; deferred",
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
                # The failed stop may still have taken down the main process
                # (helper lingering, probe error); restore the recorded prior
                # state rather than leave the client half stopped.  Best
                # effort: the row stays interrupted either way, so
                # reconciliation retries.
                self._restore_client(prior_running)
                return ExecutionReport(
                    operation_id, "aborted", "client would not exit for resume"
                )
        else:
            # prior_client_running drives a later start_client(); recording
            # it from an errored probe could launch Steam the user never had
            # running.  Unlike the stop-side checks (where unknown must read
            # as "possibly running", fail closed), this value needs definite
            # evidence — defer with the state untouched otherwise.
            if gates.client_running == "unknown":
                return ExecutionReport(
                    operation_id,
                    "aborted",
                    "client presence unknown (probe error); deferred",
                )
            prior_running = gates.client_running == "fail"
            ledger.transition(
                operation_id,
                "lease_acquired",
                prior_client_running=prior_running,
            )
            ledger.transition(operation_id, "client_stopping")
            if prior_running and not self._session.stop_client():
                # The main process may already be gone with a helper (or a
                # probe error) lingering.  A second -silent start is safe:
                # Steam is single-instance.
                return self._finish_failure(
                    operation_id,
                    prior_running,
                    detail="client would not exit cleanly",
                    message="client would not exit cleanly",
                    outcome="aborted",
                    to_state="aborted",
                )

        # Shutdown can take up to a minute; re-check the lease between the
        # client stopping and steamcmd starting so freshly begun user work
        # is never mutated under.  Unlike the pre-stop check, the client
        # must now be provably absent as well.
        post_stop_gates = self._session.gates()
        if not post_stop_gates.all_clear() or post_stop_gates.client_running != "pass":
            return self._finish_failure(
                operation_id,
                prior_running,
                detail="lease gates regressed during client stop",
                message="lease gates regressed during client stop",
                outcome="aborted",
                to_state="aborted",
            )

        ledger.transition(
            operation_id,
            "content_running",
            detail="resume" if resuming else None,
        )
        try:
            # Durable ownership evidence BEFORE steamcmd can write anything:
            # a run that fails after a partial download leaves no nested
            # manifest, and without this record every fresh retry would fail
            # the not-owned check above until the directory was removed by
            # hand.
            self._record_owned_dir(operation.appid, install_dir_name, target)
            result = self._content.install(
                account=operation.account_alias,
                appid=operation.appid,
                install_dir=target,
                operation_id=operation_id,
            )
        except OSError as error:
            # The adapter can raise outside steamcmd itself (unwritable log
            # dir, full disk); the promised client run-state must still be
            # restored rather than leaving Steam stopped for manual repair.
            # Class name only: the message may carry private paths, which
            # stay out of ledger details and broker output.
            return self._finish_failure(
                operation_id,
                prior_running,
                detail=f"content adapter error: {error.__class__.__name__}",
                message="content adapter failed before completing",
            )
        # Restore the client before recording a terminal state: a crash in
        # between then leaves a non-terminal row that reconciliation still
        # owns, instead of a terminal row with the client silently stopped.
        if result.outcome == "auth_required":
            return self._finish_failure(
                operation_id,
                prior_running,
                detail="auth_required: owner re-seed",
                message="steamcmd needs re-authentication",
                outcome="auth_required",
            )
        if result.outcome != "installed":
            return self._finish_failure(
                operation_id,
                prior_running,
                detail=f"steamcmd failed: {result.log_path.name}",
                message=f"steamcmd failed; see {result.log_path.name}",
            )

        source = locate_manifest(install_dir=target, appid=operation.appid)
        if source is None:
            return self._finish_failure(
                operation_id,
                prior_running,
                detail="no steamcmd-written manifest",
                message="steamcmd produced no manifest",
            )

        # steamcmd can run for up to 30 minutes; re-check the full lease and
        # re-assert the stopped client before touching steamapps/ — never
        # adopt under a live client or a lease that regressed.
        late_gates = self._session.gates()
        if not late_gates.all_clear():
            return self._finish_failure(
                operation_id,
                prior_running,
                detail="lease gates regressed before adoption",
                message="lease gates regressed before adoption; adoption skipped",
            )
        # One final client-absent probe follows the stop: a client that
        # respawns between this probe and the millisecond-scale adoption is
        # an accepted residual race (Steam startup takes seconds).
        if self._session.client_possibly_running():
            stopped = self._session.stop_client()
            if not stopped or self._session.client_possibly_running():
                # The failed stop may still have killed the main process
                # (helper lingering, probe error).
                return self._finish_failure(
                    operation_id,
                    prior_running,
                    detail="client restarted mid-operation; adoption skipped",
                    message="client restarted mid-operation; adoption skipped",
                )

        ledger.transition(operation_id, "adopting")
        try:
            adopted = adopt_manifest(
                source=source,
                library=self._library,
                appid=operation.appid,
                install_dir_name=install_dir_name,
                journal_dir=self._journal_dir,
                operation_id=operation_id,
            )
        except (AdoptionError, OSError) as error:
            # Raw OSError covers the pre-swap steps (journal dir creation,
            # backup read/write) that adopt_manifest does not wrap itself.
            # Class name only for those: the message may carry private paths.
            reason = (
                str(error)
                if isinstance(error, AdoptionError)
                else f"filesystem error: {error.__class__.__name__}"
            )
            # Repair via the journal before any terminal transition: the
            # error may have struck after the rename landed (completed), and
            # a terminal row must never leave a live journal behind.
            verdict = reconcile_adoption(
                library=self._library,
                appid=operation.appid,
                journal_dir=self._journal_dir,
                operation_id=operation_id,
            )
            if verdict == "completed":
                adopted = (
                    self._library
                    / "steamapps"
                    / f"appmanifest_{operation.appid}.acf"
                )
            else:
                return self._finish_failure(
                    operation_id,
                    prior_running,
                    detail=f"adoption failed: {reason}; journal {verdict}",
                    message=f"adoption failed: {reason}",
                )
        # Leave the ledger's adopting state before retiring the journal: a
        # crash in between then reconciles from a state whose branch does not
        # consult the journal, never as adopting-with-no-journal (which would
        # read as an unproven adoption after the manifest was already swapped).
        ledger.transition(operation_id, "client_restart_pending")
        clear_adoption_journal(appid=operation.appid, journal_dir=self._journal_dir)
        # The steamcmd-written manifest now proves ownership on its own.
        self._owned_marker(operation.appid).unlink(missing_ok=True)
        if not self._restore_client(prior_running):
            # Never terminalize while restoration is failing: the state
            # stays at client_restart_pending so reconciliation retries.
            return ExecutionReport(
                operation_id,
                "aborted",
                "client restore failed; state left for reconcile retry",
            )

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
            if prior_running:
                detail = "client_adopted; first_run_required"
                summary = "content present and adopted; first run still required"
            else:
                detail = "content_present; client validation deferred to next client start"
                summary = (
                    "content present and adopted; client validation deferred"
                    " to next client start; first run still required"
                )
            ledger.transition(operation_id, "confirmed", detail=detail)
            return ExecutionReport(operation_id, "confirmed", summary)
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

    # -- partial-install ownership markers --------------------------------
    # Written durably before steamcmd starts; they let a later fresh
    # operation reuse a partial directory a failed run left behind (no
    # nested manifest exists yet to prove ownership).  Retired once a
    # steamcmd-written manifest provides real evidence.

    def _owned_marker(self, appid: int) -> Path:
        return self._state_dir / "owned" / f"{appid}.json"

    def _marker_owns_target(
        self, appid: int, install_dir_name: str, target: Path
    ) -> bool:
        try:
            record = json.loads(
                self._owned_marker(appid).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        if not isinstance(record, dict):
            return False
        name = record.get("install_dir_name")
        if not isinstance(name, str) or name.casefold() != install_dir_name.casefold():
            return False
        # Bound to the recorded directory inode, not just the name: a
        # partial dir the user removed and replaced with unrelated content
        # at the same path must not inherit the marker's vouching.
        try:
            current = os.lstat(target)
        except OSError:
            return False
        return (current.st_dev, current.st_ino) == (
            record.get("dev"),
            record.get("ino"),
        )

    def _record_owned_dir(
        self, appid: int, install_dir_name: str, target: Path
    ) -> None:
        # Create the directory ourselves (steamcmd accepts an existing one)
        # so the marker can bind to its inode identity from the start.
        target.mkdir(parents=True, exist_ok=True)
        identity = os.lstat(target)
        marker = self._owned_marker(appid)
        marker.parent.mkdir(parents=True, exist_ok=True)
        _durable_write(
            marker,
            json.dumps(
                {
                    "install_dir_name": install_dir_name,
                    "dev": identity.st_dev,
                    "ino": identity.st_ino,
                }
            ).encode("utf-8"),
        )

    def _restore_client(self, prior_running: bool | None) -> bool:
        """Restore prior client run-state; False means a restart was needed but failed."""

        if not prior_running:
            return True
        return self._session.start_client()

    def _finish_failure(
        self,
        operation_id: int,
        prior_running: bool | None,
        *,
        detail: str,
        message: str,
        outcome: ExecuteOutcome = "failed",
        to_state: str = "failed",
    ) -> ExecutionReport:
        """Restore the client, then terminalize; stay non-terminal if
        restoration fails so reconciliation retries it."""

        if not self._restore_client(prior_running):
            return ExecutionReport(
                operation_id,
                "aborted",
                f"{message}; client restore failed; state left for reconcile",
            )
        self._ledger.transition(operation_id, to_state, detail=detail)
        return ExecutionReport(operation_id, outcome, message)

    def release_client(self, operation_id: int) -> bool:
        """Best-effort prior-client restoration for out-of-band aborts.

        Takes the per-library lock: restarting Steam while another executor
        holds it would race that executor's stopped-client invariant.
        """

        lock = self._lock()
        try:
            operation = self._ledger.get(operation_id)
            return self._restore_client(operation.prior_client_running)
        finally:
            lock.close()  # type: ignore[attr-defined]

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
            # Steam may have been restarted since the crash; adoption repair
            # rewrites the live manifest, so re-check the full lease and
            # never race a running client or newly active user work.
            if not self._session.gates().all_clear():
                actions.append(
                    f"{operation_id}: lease gates not clear; adoption reconcile deferred"
                )
                return actions
            if self._session.client_possibly_running():
                if not self._session.stop_client():
                    # The failed stop may still have taken down the main
                    # process (helper lingering, probe error); restore the
                    # prior run-state rather than leave the client half
                    # stopped across repeated deferrals.
                    restore()
                    actions.append(
                        f"{operation_id}: client running; adoption reconcile deferred"
                    )
                    return actions
                # The stop can take up to a minute; re-check the lease and
                # client absence before repairing the live manifest.
                post_stop = self._session.gates()
                if (
                    not post_stop.all_clear()
                    or post_stop.client_running != "pass"
                ):
                    # The stop succeeded; a transient probe error or newly
                    # active gate must not leave a previously running client
                    # stopped across deferrals.
                    restore()
                    actions.append(
                        f"{operation_id}: lease regressed during stop;"
                        " adoption reconcile deferred"
                    )
                    return actions
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
            clear_adoption_journal(appid=active.appid, journal_dir=self._journal_dir)
            self._owned_marker(active.appid).unlink(missing_ok=True)
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
            self._owned_marker(active.appid).unlink(missing_ok=True)
            if not restore():
                return actions
            if state != "verifying":
                ledger.transition(operation_id, "verifying", detail="reconciled")
            outcome, why = self._verified_outcome(active.appid)
            ledger.transition(operation_id, outcome, detail=f"reconciled: {why}")
            actions.append(f"{operation_id}: {state} -> {outcome}")
            return actions
        return actions

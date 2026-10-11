"""Content plane: steamcmd downloads and journaled single-manifest adoption.

Linux steamcmd runs with the broker's private HOME (Phase 0: a shared HOME
lets the running client clobber steamcmd's credential cache) and with
``@NoPromptForPassword`` so authentication failure is a typed state, never a
prompt or a retry.  Adoption writes exactly one file into the client's
``steamapps/`` — the Valve-written manifest — journaled and backed up.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Literal, TYPE_CHECKING, BinaryIO

from steam_agent.process_environment import child_environment
from steam_agent.execution.windows_process import WindowsJobProcess
from steam_agent.execution.output_capture import BoundedOutputCapture

if TYPE_CHECKING:
    from steam_agent.execution.windows_filesystem import WindowsFileIdentity, WindowsFilesystem

OwnedContentProcess = subprocess.Popen[bytes] | WindowsJobProcess

ContentOutcome = Literal["installed", "auth_required", "failed"]

_AUTH_FAILURE = re.compile(
    r"Cached credentials not found|Invalid Password|Rate Limit|two-factor",
    re.IGNORECASE,
)
_SUCCESS = re.compile(r"fully installed", re.IGNORECASE)
_DEFAULT_TIMEOUT_SECONDS = 30 * 60
_DEFAULT_ABORT_POLL_SECONDS = 15.0
_PROCESS_CLEANUP_SECONDS = 5.0
# Per-stream bounded memory capture; retain head and tail for classification.
_OUTPUT_CAPTURE_LIMIT = 4 * 1024 * 1024

# Only recognized steamcmd diagnostic lines are persisted (after literal
# redaction); everything else is dropped with a count.  The repository
# boundary keeps raw responses and personal identifiers out of logs, and
# unrecognized output is exactly where unredactable identifiers appear.
_LOG_LINE_RECOGNIZED = re.compile(
    r"steamcmd unavailable|Steam Console Client|Loading Steam API|Logging in"
    r"|Login|Waiting for|Connecting|\bOK\b|FAILED|ERROR|Success|fully installed"
    r"|Update state|progress:|validating|preallocating|downloading"
    r"|Rate Limit|two-factor|Cached credentials|Invalid Password"
    r"|force_install_dir|app_update|licenses",
    re.IGNORECASE,
)


class AdoptionError(RuntimeError):
    """Manifest adoption could not be completed or rolled back cleanly."""


class ContentProcessError(OSError):
    """Owned content-process cleanup or polling failed; text is fixed."""


def _terminate_owned_process(process: OwnedContentProcess) -> None:
    """Stop this adapter's session group and bound the wait for its leader.

    A leader-only fallback cannot prove that wrapped writers also exited, so
    failed group termination remains an error even if the leader was reaped.
    """

    unconfirmed = False
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except Exception as error:
        unconfirmed = not isinstance(error, ProcessLookupError)
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except Exception:
            unconfirmed = True
    try:
        process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
    except Exception:
        unconfirmed = True
    if unconfirmed:
        raise ContentProcessError("content process cleanup unconfirmed") from None


def _fsync_dir(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _durable_write(path: Path, data: bytes) -> None:
    """Atomic and power-loss durable: fsync the file, rename, fsync the dir."""

    temporary = path.with_name(path.name + ".tmp")
    # O_NOFOLLOW: steamapps/ is shared, and a planted .tmp symlink must not
    # redirect this write outside the intended file.
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o644
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    _fsync_dir(path.parent)


@dataclass(frozen=True, slots=True)
class ContentResult:
    outcome: ContentOutcome
    log_path: Path


@dataclass(frozen=True, slots=True)
class WindowsContentResult(ContentResult):
    """A completed native job bound to one operation and confirmed NTFS target."""

    appid: int
    operation_id: int
    operation: Literal["install", "verify"]
    target_identity: WindowsFileIdentity
    process_backend: Literal["windows-job/1"] = "windows-job/1"


class SteamcmdAdapter:
    """Runs Valve's steamcmd under the broker's private HOME."""

    def __init__(
        self,
        *,
        steamcmd_script: Path,
        private_home: Path,
        log_dir: Path,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        abort_poll_seconds: float = _DEFAULT_ABORT_POLL_SECONDS,
    ) -> None:
        self._script = steamcmd_script
        self._home = private_home
        self._log_dir = log_dir
        self._timeout = timeout_seconds
        self._abort_poll = abort_poll_seconds

    def _prepare_directories(self) -> None:
        self._home.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def _environment(self) -> dict[str, str]:
        return child_environment(overrides={"HOME": str(self._home)})

    def _spawn(
        self, argv: list[str], stdout: BinaryIO, stderr: BinaryIO,
        environment: dict[str, str],
    ) -> OwnedContentProcess:
        if os.name == "nt":
            raise ContentProcessError("Windows content requires the owned job backend")
        return subprocess.Popen(
            argv, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
            env=environment, start_new_session=True,
        )

    def _terminate(self, process: OwnedContentProcess) -> None:
        _terminate_owned_process(process)

    def _close(self, process: OwnedContentProcess) -> None:
        pass  # Popen owns its leader resources; the Linux group has no handle.

    def _result(self, outcome: ContentOutcome, log_path: Path) -> ContentResult:
        return ContentResult(outcome=outcome, log_path=log_path)

    def install(
        self,
        *,
        account: str,
        appid: int,
        install_dir: Path,
        operation_id: int,
        validate: bool = False,
        abort_when: Callable[[], bool] | None = None,
    ) -> ContentResult:
        """Run ``app_update``, optionally with Valve's ``validate`` pass.

        ``validate`` is the same Valve verb the client's "verify integrity"
        button uses: it re-checks every file against the depot manifest and
        replaces whatever differs, which is why it is a separately granted
        capability rather than a flag on install.
        """

        self._prepare_directories()
        # Per-attempt filename (an operation can retry after interruption):
        # every attempt keeps its own evidence instead of the last one's.
        # The pid disambiguates a quick crash-restart within one second.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        kind = "verify" if validate else "install"
        log_path = (
            self._log_dir
            / f"{kind}-{appid}-op{operation_id}-{stamp}-p{os.getpid()}.log"
        )
        argv = [
            str(self._script),
            "+@NoPromptForPassword",
            "1",
            "+force_install_dir",
            str(install_dir),
            "+login",
            account,
            "+app_update",
            str(appid),
            *(["validate"] if validate else []),
            "+quit",
        ]
        environment = self._environment()
        timed_out = False
        aborted = False
        poll_failed = False
        returncode: int | None = None
        output: str | None = None
        # Readers drain immediately into bounded head/tail memory. Broker
        # death cannot leave raw output files or unbounded disk growth.
        with BoundedOutputCapture(limit=_OUTPUT_CAPTURE_LIMIT) as capture:
            try:
                process = self._spawn(argv, capture.stdout, capture.stderr, environment)
            except OSError:
                output = "steamcmd unavailable"
                capture.close_writers()
            else:
                # Poll while steamcmd runs: a Steam client relaunched
                # during a 30-minute download is a concurrent writer in
                # the same install directory, and waiting for the
                # post-download probe would leave that overlap window
                # open the whole time.
                cleanup_attempted = False
                try:
                    capture.close_writers()
                    deadline = time.monotonic() + self._timeout
                    while True:
                        if capture.failed:
                            raise ContentProcessError("content output capture failed")
                        remaining = deadline - time.monotonic()
                        try:
                            process.wait(
                                timeout=min(self._abort_poll, max(remaining, 0.0))
                            )
                            break
                        except subprocess.TimeoutExpired:
                            if remaining <= 0:
                                timed_out = True
                            elif abort_when is not None:
                                try:
                                    aborted = bool(abort_when())
                                except Exception:
                                    # Unknown authorization or session
                                    # state ends writes just like denial.
                                    poll_failed = aborted = True
                            if not timed_out and not aborted:
                                continue
                            cleanup_attempted = True
                            self._terminate(process)
                            break
                    returncode = process.returncode
                    output = capture.finish(timeout=_PROCESS_CLEANUP_SECONDS)
                except BaseException as error:
                    # A failed wait/probe must not orphan the owned writer.
                    # Cancellation also cleans up before propagating.
                    if not cleanup_attempted:
                        self._terminate(process)
                    if not isinstance(error, Exception):
                        raise
                    raise ContentProcessError("content process polling failed") from None
                finally:
                    self._close(process)
        # Classify on the raw output first: redaction could mangle a marker
        # (an account alias like "all" is a substring of "fully installed").
        # Success additionally requires a clean exit: output text alone must
        # never outrank a nonzero exit or a killed/timed-out process.
        if poll_failed:
            outcome: ContentOutcome = "failed"
        elif _AUTH_FAILURE.search(output):
            outcome = "auth_required"
        elif (
            not timed_out
            and not aborted
            and returncode == 0
            and _SUCCESS.search(output)
        ):
            outcome = "installed"
        else:
            outcome = "failed"

        # Persist recognized diagnostic lines only, with configured
        # identifiers and SteamIDs redacted; unrecognized output (where
        # unredactable identifiers would hide) is dropped with a count.
        kept: list[str] = []
        dropped = 0
        for line in output.splitlines():
            if not _LOG_LINE_RECOGNIZED.search(line):
                dropped += 1
                continue
            # Paths before the account alias: replacing the alias first
            # could mangle a path that contains it and leave the remainder
            # of that private path in the log.
            for value, label in (
                (str(self._home), "<steamcmd-home>"),
                (str(install_dir), "<install-dir>"),
                (str(self._script), "<steamcmd>"),
            ):
                if value:
                    line = line.replace(value, label)
            if account:
                # Case-insensitive: steamcmd may echo the login with its
                # case normalized, and the identifier must not survive that.
                line = re.sub(
                    re.escape(account), "<account>", line, flags=re.IGNORECASE
                )
            # Any absolute path still present is by definition NOT one of
            # the configured ones — a diagnostic like "ERROR opening
            # /home/user/file" on an allowlisted line must not persist a
            # private path verbatim.
            line = re.sub(r"(?:/[^/\s\"']+){2,}/?", "<path>", line)
            line = re.sub(r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\"']+", "<path>", line)
            # Every SteamID rendering: SteamID64 (17 digits from 7656…,
            # spanning the whole 32-bit account-ID space), bracketed
            # SteamID3 ("[U:1:…]" — steamcmd's actual login-line format),
            # and legacy SteamID2.
            line = re.sub(r"\b7656\d{13}\b", "<steamid>", line)
            line = re.sub(r"\[U:\d+:\d+\]", "<steamid>", line)
            line = re.sub(r"\bSTEAM_\d+:\d+:\d+\b", "<steamid>", line)
            kept.append(line)
        kept.append(f"[{dropped} unrecognized line(s) omitted]")
        log_path.write_text("\n".join(kept) + "\n", encoding="utf-8", errors="replace")
        return self._result(outcome, log_path)


class WindowsSteamcmdAdapter(SteamcmdAdapter):
    """Candidate native adapter from a separately provisioned private install.

    Windows SteamCMD caches authentication beside its installation; HOME is
    not an isolation mechanism here. Provisioning and auth/title qualification
    remain owner work. This adapter does not lift the broker's Windows gate.
    """

    def __init__(
        self, *, steamcmd_executable: Path, private_installation: Path,
        log_dir: Path, filesystem: WindowsFilesystem | None = None,
        client_installation: Path,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        abort_poll_seconds: float = _DEFAULT_ABORT_POLL_SECONDS,
    ) -> None:
        from steam_agent.execution.windows_filesystem import WindowsFilesystem, validate_path

        executable = validate_path(steamcmd_executable)
        installation = validate_path(private_installation)
        if executable.parent != installation or executable.name.casefold() != "steamcmd.exe":
            raise ContentProcessError("Windows SteamCMD private installation is invalid")
        client = validate_path(client_installation)
        if installation == client or installation.is_relative_to(client) or client.is_relative_to(installation):
            raise ContentProcessError("Windows SteamCMD installation overlaps the client")
        self._filesystem = filesystem if filesystem is not None else WindowsFilesystem()
        self._installation_identity = self._filesystem.identity(private_installation)
        if self._filesystem.identity(client_installation) == self._installation_identity:
            raise ContentProcessError("Windows SteamCMD installation overlaps the client")
        self._executable_identity = self._filesystem.identity(steamcmd_executable, directory=False)
        self._log_identity = self._filesystem.identity(log_dir)
        super().__init__(
            steamcmd_script=steamcmd_executable, private_home=private_installation,
            log_dir=log_dir, timeout_seconds=timeout_seconds,
            abort_poll_seconds=abort_poll_seconds,
        )

    def _prepare_directories(self) -> None:
        if (self._filesystem.identity(self._home) != self._installation_identity
                or self._filesystem.identity(self._script, directory=False) != self._executable_identity
                or self._filesystem.identity(self._log_dir) != self._log_identity):
            raise ContentProcessError("Windows SteamCMD provisioning identity changed")

    def _environment(self) -> dict[str, str]:
        return child_environment()

    def _spawn(
        self, argv: list[str], stdout: BinaryIO, stderr: BinaryIO,
        environment: dict[str, str],
    ) -> WindowsJobProcess:
        self._prepare_directories()
        return WindowsJobProcess(argv, stdout=stdout, stderr=stderr,
                                 env=environment, cwd=self._home)

    def _terminate(self, process: OwnedContentProcess) -> None:
        try:
            process.kill()
            process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
        except Exception:
            raise ContentProcessError("Windows content job cleanup unconfirmed") from None

    def _close(self, process: OwnedContentProcess) -> None:
        if not isinstance(process, WindowsJobProcess):
            raise ContentProcessError("Windows content job backend required")
        try:
            process.close()
        except Exception:
            raise ContentProcessError("Windows content job close unconfirmed") from None

    def install(
        self, *, account: str, appid: int, install_dir: Path, operation_id: int,
        validate: bool = False, abort_when: Callable[[], bool] | None = None,
    ) -> WindowsContentResult:
        # A private executable alone does not make its write target safe.
        # The candidate coordinator additionally binds this NTFS identity to
        # a library/AppID before ever treating it as game ownership evidence.
        before = self._filesystem.identity(install_dir)
        result = super().install(
            account=account, appid=appid, install_dir=install_dir,
            operation_id=operation_id, validate=validate, abort_when=abort_when,
        )
        if self._filesystem.identity(install_dir) != before:
            raise ContentProcessError("Windows content target identity changed")
        return WindowsContentResult(
            outcome=result.outcome, log_path=result.log_path, appid=appid,
            operation_id=operation_id, operation="verify" if validate else "install",
            target_identity=before,
        )


def locate_manifest(*, install_dir: Path, appid: int) -> Path | None:
    """Find the Valve-written manifest under the force_install_dir target."""

    candidate = install_dir / "steamapps" / f"appmanifest_{appid}.acf"
    return candidate if candidate.is_file() else None


def manifest_appid(manifest: Path) -> int | None:
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"appid"\s+"(\d+)"', text)
    return None if match is None else _manifest_u32(match.group(1))


def _manifest_u32(value: str) -> int | None:
    """Malformed manifest numbers are unknown, including oversized decimals."""
    if len(value) > 10 or not value.isascii() or not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed <= 0xFFFFFFFF else None


def manifest_install_dir(manifest: Path) -> str | None:
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"installdir"\s+"([^"]*)"', text)
    return None if match is None else match.group(1)


def manifest_state_flags(manifest: Path) -> int | None:
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"StateFlags"\s+"(\d+)"', text)
    return None if match is None else _manifest_u32(match.group(1))


def _patched_manifest(source: Path, install_dir_name: str) -> str:
    text = source.read_text(encoding="utf-8", errors="replace")
    patched, count = re.subn(
        r'("installdir"\s+")[^"]*(")',
        lambda match: f"{match.group(1)}{install_dir_name}{match.group(2)}",
        text,
    )
    if count != 1:
        # A manifest whose installdir cannot be pointed at the target must
        # never be adopted — the client would look in the wrong directory.
        raise AdoptionError(
            f"manifest installdir field missing or ambiguous ({count} matches)"
        )
    return patched


def adopt_manifest(
    *,
    source: Path,
    library: Path,
    appid: int,
    install_dir_name: str,
    journal_dir: Path,
    operation_id: int,
) -> Path:
    """Place one manifest into the client library, journaled and backed up.

    Journal-first: intent (with the patched content's checksum) is durable
    before the destination is touched, so reconciliation can always decide
    complete-the-swap vs restore-the-backup.
    """

    destination = library / "steamapps" / f"appmanifest_{appid}.acf"
    # Capture the library's mount identity for the journal: recovery must
    # never restore/unlink against a different volume exposed at the same
    # path after an unmount while the broker was down.
    try:
        library_stat = os.lstat(library / "steamapps")
    except OSError as error:
        raise AdoptionError("library steamapps unavailable") from error
    journal_dir.mkdir(parents=True, exist_ok=True)
    # The journal directory's own entry must be durable before anything is
    # journaled into it: a power loss could otherwise persist the swapped
    # manifest while losing the journal (and backup) entirely, and
    # reconciliation would read the mutation as unprovable ("clean").
    _fsync_dir(journal_dir.parent)
    patched = _patched_manifest(source, install_dir_name)
    checksum = hashlib.sha256(patched.encode("utf-8")).hexdigest()

    backup: Path | None = None
    if destination.exists():
        backup = journal_dir / f"appmanifest_{appid}.acf.backup"
        _durable_write(backup, destination.read_bytes())

    journal = journal_dir / f"adoption-{appid}.json"
    record = json.dumps(
        {
            "appid": appid,
            "operation_id": operation_id,
            "destination": str(destination),
            "checksum": checksum,
            "backup": None if backup is None else str(backup),
            "library_dev": library_stat.st_dev,
            "library_ino": library_stat.st_ino,
            "at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
    )
    # Durability order is the contract: backup, then journal, then manifest.
    # A power loss can never persist the swapped manifest without the
    # journal (and backup) that reconciliation needs to judge it.
    _durable_write(journal, record.encode("utf-8"))

    try:
        _durable_write(destination, patched.encode("utf-8"))
    except OSError as error:
        raise AdoptionError("manifest adoption failed mid-write") from error
    return destination


def clear_adoption_journal(*, appid: int, journal_dir: Path) -> None:
    """Retire the journal once its swap is durably complete."""

    (journal_dir / f"adoption-{appid}.json").unlink(missing_ok=True)
    # The dir may not exist (an operation that never adopted anything);
    # reconciliation must not crash clearing a journal that was never born.
    if journal_dir.is_dir():
        _fsync_dir(journal_dir)


def reconcile_adoption(
    *, library: Path, appid: int, journal_dir: Path, operation_id: int | None = None
) -> str:
    """Deterministic adopting-crash recovery: complete or restore.

    Returns ``completed``, ``restored``, ``clean`` (no pending journal), or
    ``stale`` (a journal from a different operation; proves nothing here).
    """

    journal_path = journal_dir / f"adoption-{appid}.json"
    if not journal_path.is_file():
        return "clean"
    try:
        record = json.loads(journal_path.read_text(encoding="utf-8"))
        destination = Path(str(record["destination"]))
        checksum = str(record["checksum"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        # An unreadable journal proves nothing either way; the caller must
        # defer (restoring the client), never crash or destroy evidence.
        raise AdoptionError("adoption journal unreadable") from error
    # The journal's paths must match this configuration exactly: a
    # semantically corrupted (or foreign) journal must never aim the
    # restore/unlink below at an arbitrary recorded path.
    if destination != library / "steamapps" / f"appmanifest_{appid}.acf":
        raise AdoptionError("adoption journal names an unexpected destination")
    if record.get("backup") is not None and Path(
        str(record["backup"])
    ) != journal_dir / f"appmanifest_{appid}.acf.backup":
        raise AdoptionError("adoption journal names an unexpected backup")
    # The journaled mount identity must still be present: recovery against
    # a different volume exposed at the library path (unmount/replacement
    # while the broker was down) would mutate the wrong filesystem.
    try:
        current = os.lstat(library / "steamapps")
    except OSError as error:
        raise AdoptionError("library steamapps unavailable for recovery") from error
    if (current.st_dev, current.st_ino) != (
        record.get("library_dev"),
        record.get("library_ino"),
    ):
        raise AdoptionError("library mount changed; adoption recovery deferred")
    if operation_id is not None and record.get("operation_id") != operation_id:
        # A prior operation's journal that survived only because its swap
        # already completed; discard it rather than let it vouch for this one.
        journal_path.unlink()
        return "stale"
    backup_value = record.get("backup")

    if destination.is_file():
        try:
            current = hashlib.sha256(destination.read_bytes()).hexdigest()
        except OSError as error:
            raise AdoptionError("adoption destination unreadable") from error
        if current == checksum:
            # Leave the journal for the caller to retire AFTER the ledger
            # leaves adopting; deleting it here would let a crash read a
            # completed swap as adopting-with-no-journal (unprovable).
            return "completed"
        # A byte mismatch is not proof of a torn swap: a client started
        # between the crash and this recovery rewrites the adopted manifest
        # in its own formatting (recovery's own stop_client triggers that
        # shutdown rewrite too).  For a fresh install (no backup) a
        # parseable manifest for this appid at the destination can only be
        # the landed swap — unlinking it would orphan the downloaded
        # content.  With a backup the old manifest is indistinguishable
        # from a rewritten new one, so the conservative restore stands.
        if backup_value is None and manifest_appid(destination) == appid:
            return "completed"
    # The restored manifest must be durable before the journal disappears:
    # losing the journal while the restore is still in the page cache would
    # leave no recovery record for the next pass.
    if backup_value is not None:
        backup_path = Path(str(backup_value))
        if not backup_path.is_file():
            # The journal promises a backup that is gone: evidence is
            # incomplete, and the fresh-install deletion path below would
            # orphan an existing installation.  Defer, never guess.
            raise AdoptionError("adoption backup missing; recovery deferred")
        _durable_write(destination, backup_path.read_bytes())
        journal_path.unlink()
        _fsync_dir(journal_dir)
        return "restored"
    destination.unlink(missing_ok=True)
    _fsync_dir(destination.parent)
    journal_path.unlink()
    _fsync_dir(journal_dir)
    return "restored"

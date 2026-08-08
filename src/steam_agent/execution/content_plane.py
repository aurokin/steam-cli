"""Content plane: steamcmd downloads and journaled single-manifest adoption.

steamcmd always runs with the broker's private HOME (Phase 0: a shared HOME
lets the running client clobber steamcmd's credential cache) and with
``@NoPromptForPassword`` so authentication failure is a typed state, never a
prompt or a retry.  Adoption writes exactly one file into the client's
``steamapps/`` — the Valve-written manifest — journaled and backed up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Literal

ContentOutcome = Literal["installed", "auth_required", "failed"]

_AUTH_FAILURE = re.compile(
    r"Cached credentials not found|Invalid Password|Rate Limit|two-factor",
    re.IGNORECASE,
)
_SUCCESS = re.compile(r"fully installed", re.IGNORECASE)
_DEFAULT_TIMEOUT_SECONDS = 30 * 60

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


def _captured_text(captured: str | bytes | None) -> str:
    if captured is None:
        return ""
    if isinstance(captured, bytes):
        return captured.decode("utf-8", errors="replace")
    return captured


@dataclass(frozen=True, slots=True)
class ContentResult:
    outcome: ContentOutcome
    log_path: Path


class SteamcmdAdapter:
    """Runs Valve's steamcmd under the broker's private HOME."""

    def __init__(
        self,
        *,
        steamcmd_script: Path,
        private_home: Path,
        log_dir: Path,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._script = steamcmd_script
        self._home = private_home
        self._log_dir = log_dir
        self._timeout = timeout_seconds

    def install(
        self, *, account: str, appid: int, install_dir: Path, operation_id: int
    ) -> ContentResult:
        self._home.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        # Per-attempt filename (an operation can retry after interruption):
        # every attempt keeps its own evidence instead of the last one's.
        # The pid disambiguates a quick crash-restart within one second.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = (
            self._log_dir
            / f"install-{appid}-op{operation_id}-{stamp}-p{os.getpid()}.log"
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
            "+quit",
        ]
        environment = dict(os.environ, HOME=str(self._home))
        timed_out = False
        returncode: int | None = None
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                # Bytes, decoded leniently below: text mode would raise
                # UnicodeDecodeError on locale-invalid steamcmd output,
                # escaping classification with the client still stopped.
                env=environment,
                start_new_session=True,
            )
        except OSError as error:
            output = f"steamcmd unavailable: {error}"
        else:
            try:
                out, err = process.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                # Kill the whole session group: the configured script wraps
                # the real steamcmd binary, and killing only the leader
                # would leave a live writer in the install directory.
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
                out, err = process.communicate()
            returncode = process.returncode
            # Explicit separator: without one, a stdout tail lacking its
            # newline would merge with stderr's first line, and a recognized
            # marker could smuggle an unrecognized (unredactable) line past
            # the log filter below.
            output = _captured_text(out) + "\n" + _captured_text(err)
        # Classify on the raw output first: redaction could mangle a marker
        # (an account alias like "all" is a substring of "fully installed").
        # Success additionally requires a clean exit: output text alone must
        # never outrank a nonzero exit or a killed/timed-out process.
        if _AUTH_FAILURE.search(output):
            outcome: ContentOutcome = "auth_required"
        elif not timed_out and returncode == 0 and _SUCCESS.search(output):
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
                (account, "<account>"),
            ):
                if value:
                    line = line.replace(value, label)
            # 17 digits from 7656…: SteamID64 individual accounts span
            # 76561197…–76561202… (32-bit account-ID space), not just the
            # 7656119 prefix.
            kept.append(re.sub(r"\b7656\d{13}\b", "<steamid>", line))
        kept.append(f"[{dropped} unrecognized line(s) omitted]")
        log_path.write_text("\n".join(kept) + "\n", encoding="utf-8", errors="replace")
        return ContentResult(outcome=outcome, log_path=log_path)


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
    return None if match is None else int(match.group(1))


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
    return None if match is None else int(match.group(1))


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
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    if operation_id is not None and record.get("operation_id") != operation_id:
        # A prior operation's journal that survived only because its swap
        # already completed; discard it rather than let it vouch for this one.
        journal_path.unlink()
        return "stale"
    destination = Path(str(record["destination"]))
    checksum = str(record["checksum"])

    if destination.is_file():
        current = hashlib.sha256(destination.read_bytes()).hexdigest()
        if current == checksum:
            # Leave the journal for the caller to retire AFTER the ledger
            # leaves adopting; deleting it here would let a crash read a
            # completed swap as adopting-with-no-journal (unprovable).
            return "completed"
    backup_value = record.get("backup")
    # The restored manifest must be durable before the journal disappears:
    # losing the journal while the restore is still in the page cache would
    # leave no recovery record for the next pass.
    if backup_value is not None and Path(str(backup_value)).is_file():
        _durable_write(destination, Path(str(backup_value)).read_bytes())
        journal_path.unlink()
        _fsync_dir(journal_dir)
        return "restored"
    destination.unlink(missing_ok=True)
    _fsync_dir(destination.parent)
    journal_path.unlink()
    _fsync_dir(journal_dir)
    return "restored"

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
import subprocess
from typing import Literal

ContentOutcome = Literal["installed", "auth_required", "failed"]

_AUTH_FAILURE = re.compile(
    r"Cached credentials not found|Invalid Password|Rate Limit|two-factor",
    re.IGNORECASE,
)
_SUCCESS = re.compile(r"fully installed", re.IGNORECASE)
_DEFAULT_TIMEOUT_SECONDS = 30 * 60


class AdoptionError(RuntimeError):
    """Manifest adoption could not be completed or rolled back cleanly."""


def _captured_text(captured: str | bytes | None) -> str:
    # TimeoutExpired carries bytes even when the run used text=True.
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
        self, *, account: str, appid: int, install_dir: Path
    ) -> ContentResult:
        self._home.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"install-{appid}.log"
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
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                stdin=subprocess.DEVNULL,
                env=environment,
            )
            output = completed.stdout + completed.stderr
        except subprocess.TimeoutExpired as error:
            output = _captured_text(error.stdout) + _captured_text(error.stderr)
        except OSError as error:
            output = f"steamcmd unavailable: {error}"
        # Classify on the raw output first: redaction could mangle a marker
        # (an account alias like "all" is a substring of "fully installed").
        if _AUTH_FAILURE.search(output):
            outcome: ContentOutcome = "auth_required"
        elif _SUCCESS.search(output):
            outcome = "installed"
        else:
            outcome = "failed"

        # Raw steamcmd output carries the account name and private absolute
        # paths; the repository boundary keeps both out of persisted logs.
        # Redaction is bounded to the identifiers the broker was configured
        # with — values steamcmd invents (persona names, SteamIDs) cannot be
        # matched textually, and these logs stay inside the broker-owned
        # state directory, never in fixtures or committed files.
        for value, label in (
            (account, "<account>"),
            (str(self._home), "<steamcmd-home>"),
            (str(install_dir), "<install-dir>"),
            (str(self._script), "<steamcmd>"),
        ):
            if value:
                output = output.replace(value, label)
        log_path.write_text(output, encoding="utf-8", errors="replace")
        return ContentResult(outcome=outcome, log_path=log_path)


def locate_manifest(*, install_dir: Path, appid: int) -> Path | None:
    """Find the Valve-written manifest under the force_install_dir target."""

    candidate = install_dir / "steamapps" / f"appmanifest_{appid}.acf"
    return candidate if candidate.is_file() else None


def manifest_state_flags(manifest: Path) -> int | None:
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"StateFlags"\s+"(\d+)"', text)
    return None if match is None else int(match.group(1))


def _patched_manifest(source: Path, install_dir_name: str) -> str:
    text = source.read_text(encoding="utf-8", errors="replace")
    return re.sub(
        r'("installdir"\s+")[^"]*(")',
        lambda match: f"{match.group(1)}{install_dir_name}{match.group(2)}",
        text,
    )


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
    patched = _patched_manifest(source, install_dir_name)
    checksum = hashlib.sha256(patched.encode("utf-8")).hexdigest()

    backup: Path | None = None
    if destination.exists():
        backup = journal_dir / f"appmanifest_{appid}.acf.backup"
        backup.write_bytes(destination.read_bytes())

    journal = journal_dir / f"adoption-{appid}.json"
    journal_temporary = journal_dir / f"adoption-{appid}.json.tmp"
    journal_temporary.write_text(
        json.dumps(
            {
                "appid": appid,
                "operation_id": operation_id,
                "destination": str(destination),
                "checksum": checksum,
                "backup": None if backup is None else str(backup),
                "at": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    journal_temporary.replace(journal)  # never leave partial journal JSON

    temporary = destination.with_suffix(".acf.adopting")
    try:
        temporary.write_text(patched, encoding="utf-8")
        temporary.replace(destination)
    except OSError as error:
        raise AdoptionError("manifest adoption failed mid-write") from error
    return destination


def clear_adoption_journal(*, appid: int, journal_dir: Path) -> None:
    """Retire the journal once its swap is durably complete."""

    (journal_dir / f"adoption-{appid}.json").unlink(missing_ok=True)


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
            journal_path.unlink()
            return "completed"
    backup_value = record.get("backup")
    if backup_value is not None and Path(str(backup_value)).is_file():
        destination.write_bytes(Path(str(backup_value)).read_bytes())
        journal_path.unlink()
        return "restored"
    destination.unlink(missing_ok=True)
    journal_path.unlink()
    return "restored"

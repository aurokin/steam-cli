"""SQLite persistence for scanner observations and current projections.

The scanner writes immutable observations into a sync run. A successful,
complete run is promoted atomically to ``installed_current``. Partial and failed
runs remain available for diagnostics but never replace last-known-good state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Literal, Mapping


SyncStatus = Literal["running", "complete", "partial", "failed"]
TerminalSyncStatus = Literal["complete", "partial", "failed"]


class StorageError(RuntimeError):
    """Base error raised by the storage boundary."""


class UnknownSyncRun(StorageError):
    """The requested sync run does not exist."""


class InvalidSyncTransition(StorageError):
    """A sync run was used for the wrong capability or after completion."""


@dataclass(frozen=True)
class Machine:
    id: str
    name: str
    platform: str
    architecture: str | None = None


@dataclass(frozen=True)
class SteamApp:
    appid: int
    name: str | None
    app_type: str
    updated_at: str


@dataclass(frozen=True)
class SyncRun:
    id: int
    provider: str
    capability: str
    machine_id: str | None
    started_at: str
    completed_at: str | None
    status: SyncStatus
    promoted: bool
    records_seen: int
    error_code: str | None
    error_detail: str | None


@dataclass(frozen=True)
class EvidenceInput:
    provider: str
    capability: str
    source_kind: str
    source_locator: str
    retrieved_at: str | datetime
    support_level: str
    payload: Mapping[str, Any]
    context: Mapping[str, Any] | None = None
    effective_at: str | datetime | None = None


@dataclass(frozen=True)
class InstalledObservation:
    appid: int
    library_root: str
    install_dir: str
    observed_at: str | datetime
    name: str | None = None
    app_type: str = "unknown"
    state: str = "installed"
    build_id: str | None = None
    size_bytes: int | None = None
    manifest_path: str | None = None
    manifest_mtime: str | datetime | None = None


@dataclass(frozen=True)
class InstalledGame:
    machine_id: str
    appid: int
    name: str | None
    app_type: str
    library_root: str
    install_dir: str
    state: str
    build_id: str | None
    size_bytes: int | None
    manifest_path: str | None
    manifest_mtime: str | None
    observed_at: str
    evidence_id: int
    promoted_sync_run_id: int


@dataclass(frozen=True)
class InstalledSnapshot:
    """Installed projection and sync metadata from one SQLite read snapshot."""

    games: tuple[InstalledGame, ...]
    latest: SyncRun | None
    latest_complete: SyncRun | None


def _timestamp(value: str | datetime) -> str:
    if isinstance(value, str):
        candidate = value
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        parsed = datetime.fromisoformat(candidate)
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: str | datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _canonical_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Storage:
    """Owns a SQLite connection and exposes scanner-oriented transactions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            if self.path.is_symlink():
                raise StorageError("database path must not be a symbolic link")
            created_parent = False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
                created_parent = True
            except FileExistsError:
                if not self.path.parent.is_dir():
                    raise StorageError("database parent must be a directory") from None
            if created_parent and os.name != "nt":
                self.path.parent.chmod(0o700)
        self._connection = self._open_connection()
        if self.path != Path(":memory:") and os.name != "nt":
            self.path.chmod(0o600)
        self.migrate()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _rollback_transaction(self) -> None:
        """Rollback hook kept separate so failure recovery can be fault tested."""

        self._connection.rollback()

    def _rollback_or_reopen(self) -> None:
        """Leave the storage usable after an interrupted explicit transaction.

        Replacing a connection is intentionally limited to a file-backed database
        whose rollback failed and left a transaction active. Reopening ``:memory:``
        would silently discard the entire database, so an in-memory connection
        cannot recover this way; callers still retain their original exception.
        """

        try:
            self._rollback_transaction()
        except BaseException:
            # Some drivers can report rollback failure after actually ending the
            # transaction. In that case the caller-owned connection is healthy.
            pass
        if not self._connection.in_transaction or self.path == Path(":memory:"):
            return

        old_connection = self._connection
        try:
            old_connection.close()
        except BaseException:
            # A fresh connection may still be able to recover the durable run.
            pass
        self._connection = self._open_connection()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def migrate(self) -> None:
        migrations_dir = Path(__file__).with_name("migrations")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row["version"]
                for row in self._connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
                version = int(migration.name.split("_", 1)[0])
                if version in applied:
                    continue
                sql = migration.read_text(encoding="utf-8")
                for statement in _sql_statements(sql):
                    self._connection.execute(statement)
                applied_at = datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, applied_at),
                )
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise

    def upsert_machine(
        self, machine: Machine, *, observed_at: str | datetime
    ) -> Machine:
        timestamp = _timestamp(observed_at)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO machines(id, name, platform, architecture, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    platform = excluded.platform,
                    architecture = excluded.architecture,
                    updated_at = excluded.updated_at
                """,
                (machine.id, machine.name, machine.platform, machine.architecture, timestamp, timestamp),
            )
        return machine

    def begin_sync(
        self,
        *,
        provider: str,
        capability: str,
        started_at: str | datetime,
        machine_id: str | None = None,
    ) -> SyncRun:
        timestamp = _timestamp(started_at)
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO sync_runs(provider, capability, machine_id, started_at, status)
                VALUES (?, ?, ?, ?, 'running')
                """,
                (provider, capability, machine_id, timestamp),
            )
        return self.get_sync_run(int(cursor.lastrowid))

    def record_installed_observation(
        self,
        sync_run_id: int,
        observation: InstalledObservation,
        evidence: EvidenceInput,
    ) -> int:
        observed_at = _timestamp(observation.observed_at)
        manifest_mtime = _optional_timestamp(observation.manifest_mtime)
        if observation.appid <= 0:
            raise ValueError("appid must be positive")
        if observation.size_bytes is not None and observation.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            run = self._require_running_installed_sync(sync_run_id)
            if run.machine_id is None:
                raise InvalidSyncTransition("installed sync requires a machine_id")
            if evidence.capability != "installed":
                raise ValueError("installed observations require installed evidence")

            evidence_id = self._insert_evidence(evidence)
            self._connection.execute(
                """
                INSERT INTO steam_apps(appid, name, app_type, updated_at)
                VALUES (?, NULL, 'unknown', ?)
                ON CONFLICT(appid) DO NOTHING
                """,
                (observation.appid, observed_at),
            )
            self._connection.execute(
                """
                INSERT INTO installed_observations(
                    sync_run_id, evidence_id, machine_id, appid, name, app_type,
                    library_root, install_dir, state, build_id, size_bytes,
                    manifest_path, manifest_mtime, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sync_run_id, appid) DO UPDATE SET
                    evidence_id = excluded.evidence_id,
                    name = excluded.name,
                    app_type = excluded.app_type,
                    library_root = excluded.library_root,
                    install_dir = excluded.install_dir,
                    state = excluded.state,
                    build_id = excluded.build_id,
                    size_bytes = excluded.size_bytes,
                    manifest_path = excluded.manifest_path,
                    manifest_mtime = excluded.manifest_mtime,
                    observed_at = excluded.observed_at
                """,
                (
                    sync_run_id,
                    evidence_id,
                    run.machine_id,
                    observation.appid,
                    observation.name,
                    observation.app_type,
                    observation.library_root,
                    observation.install_dir,
                    observation.state,
                    observation.build_id,
                    observation.size_bytes,
                    observation.manifest_path,
                    manifest_mtime,
                    observed_at,
                ),
            )
            self._connection.execute(
                """
                UPDATE sync_runs
                SET records_seen = (
                    SELECT COUNT(*) FROM installed_observations WHERE sync_run_id = ?
                )
                WHERE id = ?
                """,
                (sync_run_id, sync_run_id),
            )
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise
        return evidence_id

    def finish_installed_sync(
        self,
        sync_run_id: int,
        *,
        status: TerminalSyncStatus,
        completed_at: str | datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> SyncRun:
        if status not in ("complete", "partial", "failed"):
            raise ValueError("status must be complete, partial, or failed")
        timestamp = _timestamp(completed_at)

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_sync_run(sync_run_id)
            if existing.capability != "installed":
                raise InvalidSyncTransition("sync run is not an installed scan")
            if existing.status != "running":
                if (
                    existing.status == status
                    and existing.completed_at == timestamp
                    and existing.error_code == error_code
                    and existing.error_detail == error_detail
                ):
                    self._connection.commit()
                    return existing
                raise InvalidSyncTransition(
                    f"sync run {sync_run_id} is already {existing.status}"
                )
            if existing.machine_id is None:
                raise InvalidSyncTransition("installed sync requires a machine_id")

            promoted = 0
            if status == "complete":
                newer_complete = self._connection.execute(
                    """
                    SELECT 1 FROM sync_runs
                    WHERE capability = 'installed' AND machine_id = ?
                      AND id > ? AND status = 'complete'
                    LIMIT 1
                    """,
                    (existing.machine_id, sync_run_id),
                ).fetchone()
                if newer_complete is None:
                    self._promote_installed(existing.machine_id, sync_run_id)
                    promoted = 1

            self._connection.execute(
                """
                UPDATE sync_runs SET status = ?, completed_at = ?, promoted = ?,
                    error_code = ?, error_detail = ?
                WHERE id = ?
                """,
                (status, timestamp, promoted, error_code, error_detail, sync_run_id),
            )
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise
        return self.get_sync_run(sync_run_id)

    def get_sync_run(self, sync_run_id: int) -> SyncRun:
        row = self._connection.execute(
            "SELECT * FROM sync_runs WHERE id = ?", (sync_run_id,)
        ).fetchone()
        if row is None:
            raise UnknownSyncRun(f"unknown sync run {sync_run_id}")
        return _sync_run(row)

    def latest_sync(
        self,
        *,
        capability: str,
        machine_id: str,
        status: SyncStatus | None = None,
    ) -> SyncRun | None:
        clauses = ["capability = ?", "machine_id = ?"]
        parameters: list[object] = [capability, machine_id]
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        row = self._connection.execute(
            f"SELECT * FROM sync_runs WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 1",
            parameters,
        ).fetchone()
        return None if row is None else _sync_run(row)

    def list_installed(self, machine_id: str) -> list[InstalledGame]:
        rows = self._connection.execute(
            """
            SELECT
                current.machine_id,
                current.appid,
                promoted.name,
                promoted.app_type,
                current.library_root,
                current.install_dir,
                current.state,
                current.build_id,
                current.size_bytes,
                current.manifest_path,
                current.manifest_mtime,
                current.observed_at,
                current.evidence_id,
                current.promoted_sync_run_id
            FROM installed_current AS current
            JOIN installed_observations AS promoted
              ON promoted.sync_run_id = current.promoted_sync_run_id
             AND promoted.machine_id = current.machine_id
             AND promoted.appid = current.appid
            WHERE current.machine_id = ?
            ORDER BY current.appid
            """,
            (machine_id,),
        )
        return [InstalledGame(**dict(row)) for row in rows]

    def read_installed_snapshot(self, machine_id: str) -> InstalledSnapshot:
        """Read projection and freshness metadata without a concurrent split view."""

        if self._connection.in_transaction:
            raise StorageError("cannot start a read snapshot inside a transaction")
        self._connection.execute("BEGIN")
        try:
            games = tuple(self.list_installed(machine_id))
            latest = self.latest_sync(
                capability="installed", machine_id=machine_id
            )
            latest_complete = self.latest_sync(
                capability="installed", machine_id=machine_id, status="complete"
            )
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise
        return InstalledSnapshot(
            games=games,
            latest=latest,
            latest_complete=latest_complete,
        )

    def get_app(self, appid: int) -> SteamApp | None:
        row = self._connection.execute(
            "SELECT appid, name, app_type, updated_at FROM steam_apps WHERE appid = ?",
            (appid,),
        ).fetchone()
        return None if row is None else SteamApp(**dict(row))

    def _require_running_installed_sync(self, sync_run_id: int) -> SyncRun:
        run = self.get_sync_run(sync_run_id)
        if run.capability != "installed":
            raise InvalidSyncTransition("sync run is not an installed scan")
        if run.status != "running":
            raise InvalidSyncTransition(f"sync run {sync_run_id} is already {run.status}")
        return run

    def _insert_evidence(self, evidence: EvidenceInput) -> int:
        retrieved_at = _timestamp(evidence.retrieved_at)
        effective_at = _optional_timestamp(evidence.effective_at)
        context_json = _canonical_json(evidence.context)
        payload_json = _canonical_json(evidence.payload)
        content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        self._connection.execute(
            """
            INSERT INTO evidence(
                provider, capability, source_kind, source_locator, retrieved_at,
                effective_at, support_level, context_json, payload_json, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                evidence.provider,
                evidence.capability,
                evidence.source_kind,
                evidence.source_locator,
                retrieved_at,
                effective_at,
                evidence.support_level,
                context_json,
                payload_json,
                content_hash,
            ),
        )
        row = self._connection.execute(
            """
            SELECT id FROM evidence
            WHERE provider = ? AND capability = ? AND source_kind = ?
              AND source_locator = ? AND retrieved_at = ? AND context_json = ?
              AND content_hash = ?
            """,
            (
                evidence.provider,
                evidence.capability,
                evidence.source_kind,
                evidence.source_locator,
                retrieved_at,
                context_json,
                content_hash,
            ),
        ).fetchone()
        assert row is not None
        return int(row["id"])

    def _promote_installed(self, machine_id: str, sync_run_id: int) -> None:
        observed_at = self._connection.execute(
            "SELECT started_at FROM sync_runs WHERE id = ?", (sync_run_id,)
        ).fetchone()["started_at"]
        self._connection.execute(
            """
            UPDATE steam_apps
            SET
                name = COALESCE((
                    SELECT obs.name FROM installed_observations AS obs
                    WHERE obs.sync_run_id = ? AND obs.appid = steam_apps.appid
                ), name),
                app_type = COALESCE((
                    SELECT obs.app_type FROM installed_observations AS obs
                    WHERE obs.sync_run_id = ? AND obs.appid = steam_apps.appid
                ), app_type),
                updated_at = ?
            WHERE appid IN (
                SELECT appid FROM installed_observations WHERE sync_run_id = ?
            )
            """,
            (sync_run_id, sync_run_id, observed_at, sync_run_id),
        )
        self._connection.execute(
            """
            INSERT INTO installed_current(
                machine_id, appid, evidence_id, promoted_sync_run_id, library_root,
                install_dir, state, build_id, size_bytes, manifest_path,
                manifest_mtime, observed_at
            )
            SELECT
                machine_id, appid, evidence_id, sync_run_id, library_root,
                install_dir, state, build_id, size_bytes, manifest_path,
                manifest_mtime, observed_at
            FROM installed_observations
            WHERE sync_run_id = ?
            ON CONFLICT(machine_id, appid) DO UPDATE SET
                evidence_id = excluded.evidence_id,
                promoted_sync_run_id = excluded.promoted_sync_run_id,
                library_root = excluded.library_root,
                install_dir = excluded.install_dir,
                state = excluded.state,
                build_id = excluded.build_id,
                size_bytes = excluded.size_bytes,
                manifest_path = excluded.manifest_path,
                manifest_mtime = excluded.manifest_mtime,
                observed_at = excluded.observed_at
            """,
            (sync_run_id,),
        )
        self._connection.execute(
            """
            DELETE FROM installed_current
            WHERE machine_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM installed_observations AS obs
                  WHERE obs.sync_run_id = ?
                    AND obs.appid = installed_current.appid
              )
            """,
            (machine_id, sync_run_id),
        )


__all__ = [
    "EvidenceInput",
    "InstalledGame",
    "InstalledObservation",
    "InstalledSnapshot",
    "InvalidSyncTransition",
    "Machine",
    "SteamApp",
    "Storage",
    "StorageError",
    "SyncRun",
    "UnknownSyncRun",
]


def _sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise StorageError("migration contains an incomplete SQL statement")
    return statements


def _sync_run(row: sqlite3.Row) -> SyncRun:
    values = dict(row)
    values["promoted"] = bool(values["promoted"])
    return SyncRun(**values)

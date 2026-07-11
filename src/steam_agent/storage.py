"""SQLite persistence for scanner observations and current projections.

The scanner writes immutable observations into a sync run. A successful,
complete run is promoted atomically to ``installed_current``. Partial and failed
runs remain available for diagnostics but never replace last-known-good state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Literal, Mapping
import uuid

from steam_agent.local_accounts import validate_steam_id64


SyncStatus = Literal["running", "complete", "partial", "failed"]
TerminalSyncStatus = Literal["complete", "partial", "failed"]
STEAM_APPLICATION_IDENTITY_NAMESPACE = uuid.UUID(
    "d95b6568-2886-5d15-aa84-1986e4ac511e"
)


def steam_application_stable_id(appid: int | str) -> str:
    """Return the stable UUIDv5 for one typed Steam application identity."""

    text = str(appid)
    if not text.isdecimal() or not 1 <= int(text) <= (1 << 32) - 1:
        raise ValueError("Steam application AppID is invalid")
    return str(
        uuid.uuid5(
            STEAM_APPLICATION_IDENTITY_NAMESPACE,
            f"steam:application_appid:{int(text)}",
        )
    )


class StorageError(RuntimeError):
    """Base error raised by the storage boundary."""


class UnknownSyncRun(StorageError):
    """The requested sync run does not exist."""


class InvalidSyncTransition(StorageError):
    """A sync run was used for the wrong capability or after completion."""


class AccountConflict(StorageError):
    """An alias or provider identity is already configured differently."""


@dataclass(frozen=True)
class Machine:
    id: str
    name: str
    platform: str
    architecture: str | None = None


@dataclass(frozen=True)
class Account:
    id: int
    alias: str
    provider: str
    provider_account_id: str = field(repr=False)
    source_kind: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CredentialReferenceRecord:
    provider: str
    kind: str
    profile_id: str
    backend: str
    configured_at: str
    updated_at: str
    backend_locator: str | None


@dataclass(frozen=True)
class ProviderProbeRecord:
    capability: str
    account_alias: str
    probe_state: str
    checked_at: str
    retryable: bool


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
    account_id: int | None
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
    account_id: int | None = None


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


OwnedInclusionBasis = Literal["visible_owned", "played_free"]


@dataclass(frozen=True)
class OwnedObservation:
    appid: int
    playtime_forever_minutes: int | None
    inclusion_basis: OwnedInclusionBasis
    observed_at: str | datetime
    name: str | None = None


@dataclass(frozen=True)
class OwnedGame:
    account_id: int
    appid: int
    name: str | None
    playtime_forever_minutes: int | None
    inclusion_basis: OwnedInclusionBasis
    observed_at: str
    evidence_id: int
    promoted_sync_run_id: int


@dataclass(frozen=True)
class OwnedSnapshot:
    games: tuple[OwnedGame, ...]
    latest: SyncRun | None
    latest_complete: SyncRun | None
    latest_complete_provenance: OwnedSnapshotProvenance | None
    stable_game_ids_by_appid: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class OwnedSnapshotProvenance:
    sync_run_id: int
    provider: str
    support_level: str
    include_appinfo: bool | None
    base_include_played_free_games: bool | None
    base_retrieved_at: str | None
    base_reported_count: int | None
    expanded_include_played_free_games: bool | None
    expanded_retrieved_at: str
    expanded_reported_count: int | None
    classification_method: str


@dataclass(frozen=True)
class AccountDataConsent:
    account_id: int
    consent_kind: str
    disclosure_version: str
    backups_acknowledged: bool
    accepted_at: str


@dataclass(frozen=True)
class AccountDataDeletion:
    account_removed: bool
    owned_observations_removed: int
    owned_current_removed: int
    sync_runs_removed: int
    probes_removed: int
    consents_removed: int
    evidence_removed: int
    orphan_apps_removed: int
    shared_credential_preserved: bool = True


@dataclass(frozen=True)
class AllSteamAccountDataDeletion:
    accounts_removed: int
    owned_observations_removed: int
    owned_current_removed: int
    sync_runs_removed: int
    probes_removed: int
    consents_removed: int
    evidence_removed: int
    orphan_apps_removed: int
    credential_refs_removed: int
    shared_credential_preserved: bool = True


@dataclass(frozen=True)
class LibrarySnapshot:
    """Account and machine projections captured in one SQLite read transaction."""

    owned: OwnedSnapshot
    installed: InstalledSnapshot
    stable_game_ids_by_appid: tuple[tuple[int, str], ...]


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
    return json.dumps(
        value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


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
        connection.create_function(
            "steam_application_uuid_v5",
            1,
            steam_application_stable_id,
            deterministic=True,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
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
                for row in self._connection.execute(
                    "SELECT version FROM schema_migrations"
                )
            }
            for migration in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
                version = int(migration.name.split("_", 1)[0])
                if version in applied:
                    continue
                sql = migration.read_text(encoding="utf-8")
                for statement in _sql_statements(sql):
                    self._connection.execute(statement)
                applied_at = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
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
                (
                    machine.id,
                    machine.name,
                    machine.platform,
                    machine.architecture,
                    timestamp,
                    timestamp,
                ),
            )
        return machine

    def configure_steam_account(
        self,
        *,
        alias: str,
        steam_id64: str,
        configured_at: str | datetime,
        source_kind: str = "local_steam_login_registry",
    ) -> Account:
        """Persist an explicitly selected Steam account without profile names."""

        normalized_alias = _account_alias(alias)
        normalized_steam_id = validate_steam_id64(steam_id64)
        timestamp = _timestamp(configured_at)
        if not source_kind or len(source_kind) > 128:
            raise ValueError("source_kind must be between 1 and 128 characters")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            alias_row = self._connection.execute(
                "SELECT * FROM accounts WHERE alias = ? COLLATE NOCASE",
                (normalized_alias,),
            ).fetchone()
            identity_row = self._connection.execute(
                """
                SELECT * FROM accounts
                WHERE provider = 'steam' AND provider_account_id = ?
                """,
                (normalized_steam_id,),
            ).fetchone()
            if (
                alias_row is not None
                and alias_row["provider_account_id"] != normalized_steam_id
            ):
                raise AccountConflict("account alias is already configured")
            if (
                identity_row is not None
                and identity_row["alias"].casefold() != normalized_alias.casefold()
            ):
                raise AccountConflict(
                    "Steam account is already configured under another alias"
                )

            if alias_row is None:
                cursor = self._connection.execute(
                    """
                    INSERT INTO accounts(
                        alias, provider, provider_account_id, source_kind,
                        created_at, updated_at
                    ) VALUES (?, 'steam', ?, ?, ?, ?)
                    """,
                    (
                        normalized_alias,
                        normalized_steam_id,
                        source_kind,
                        timestamp,
                        timestamp,
                    ),
                )
                account_id = int(cursor.lastrowid)
            else:
                account_id = int(alias_row["id"])
                self._connection.execute(
                    """
                    UPDATE accounts
                    SET source_kind = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (source_kind, timestamp, account_id),
                )
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise
        account = self.get_account(normalized_alias)
        assert account is not None
        return account

    def get_account(self, alias: str) -> Account | None:
        normalized_alias = _account_alias(alias)
        row = self._connection.execute(
            "SELECT * FROM accounts WHERE alias = ? COLLATE NOCASE",
            (normalized_alias,),
        ).fetchone()
        return None if row is None else Account(**dict(row))

    def list_accounts(self) -> list[Account]:
        rows = self._connection.execute(
            "SELECT * FROM accounts ORDER BY alias COLLATE NOCASE, id"
        )
        return [Account(**dict(row)) for row in rows]

    def remove_account(self, alias: str) -> bool:
        normalized_alias = _account_alias(alias)
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM accounts WHERE alias = ? COLLATE NOCASE",
                (normalized_alias,),
            )
        return cursor.rowcount > 0

    def upsert_credential_reference(
        self,
        *,
        provider: str,
        kind: str,
        profile_id: str,
        backend: str,
        configured_at: str | datetime,
        backend_locator: str | None = None,
    ) -> CredentialReferenceRecord:
        if backend not in ("os", "file"):
            raise ValueError("backend must be os or file")
        for value in (provider, kind, profile_id):
            if not value or len(value) > 128:
                raise ValueError(
                    "credential reference parts must be between 1 and 128 characters"
                )
        timestamp = _timestamp(configured_at)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO credential_refs(
                    provider, kind, profile_id, backend, configured_at, updated_at,
                    backend_locator
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, kind, profile_id) DO UPDATE SET
                    backend = excluded.backend,
                    updated_at = excluded.updated_at,
                    backend_locator = excluded.backend_locator
                """,
                (
                    provider,
                    kind,
                    profile_id,
                    backend,
                    timestamp,
                    timestamp,
                    backend_locator,
                ),
            )
        record = self.get_credential_reference(
            provider=provider, kind=kind, profile_id=profile_id
        )
        assert record is not None
        return record

    def upsert_credential_and_clear_probes(
        self,
        *,
        provider: str,
        kind: str,
        profile_id: str,
        backend: str,
        backend_locator: str | None,
        configured_at: str | datetime,
        capability: str | None,
    ) -> None:
        """Commit credential metadata and dependent-probe invalidation together."""

        if backend not in ("os", "file"):
            raise ValueError("backend must be os or file")
        for value in (provider, kind, profile_id):
            if not value or len(value) > 128:
                raise ValueError("credential metadata inputs are invalid")
        if capability is not None and (not capability or len(capability) > 128):
            raise ValueError("credential capability input is invalid")
        timestamp = _timestamp(configured_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """
                INSERT INTO credential_refs(
                    provider, kind, profile_id, backend, configured_at, updated_at,
                    backend_locator
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, kind, profile_id) DO UPDATE SET
                    backend = excluded.backend,
                    updated_at = excluded.updated_at,
                    backend_locator = excluded.backend_locator
                """,
                (
                    provider,
                    kind,
                    profile_id,
                    backend,
                    timestamp,
                    timestamp,
                    backend_locator,
                ),
            )
            if capability is not None:
                self._connection.execute(
                    "DELETE FROM provider_probes WHERE capability = ?", (capability,)
                )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def remove_credential_and_clear_probes(
        self,
        *,
        provider: str,
        kind: str,
        profile_id: str,
        capability: str | None,
    ) -> bool:
        """Remove credential metadata and dependent probes atomically."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if capability is not None:
                self._connection.execute(
                    "DELETE FROM provider_probes WHERE capability = ?", (capability,)
                )
            cursor = self._connection.execute(
                """
                DELETE FROM credential_refs
                WHERE provider = ? AND kind = ? AND profile_id = ?
                """,
                (provider, kind, profile_id),
            )
            self._connection.commit()
            return cursor.rowcount > 0
        except BaseException:
            self._connection.rollback()
            raise

    def get_credential_reference(
        self, *, provider: str, kind: str, profile_id: str
    ) -> CredentialReferenceRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM credential_refs
            WHERE provider = ? AND kind = ? AND profile_id = ?
            """,
            (provider, kind, profile_id),
        ).fetchone()
        return None if row is None else CredentialReferenceRecord(**dict(row))

    def remove_credential_reference(
        self, *, provider: str, kind: str, profile_id: str
    ) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM credential_refs
                WHERE provider = ? AND kind = ? AND profile_id = ?
                """,
                (provider, kind, profile_id),
            )
        return cursor.rowcount > 0

    def clear_provider_probes(self, *, capability: str | None = None) -> int:
        """Invalidate coarse capability evidence after an auth-policy change."""

        with self._connection:
            if capability is None:
                cursor = self._connection.execute("DELETE FROM provider_probes")
            else:
                cursor = self._connection.execute(
                    "DELETE FROM provider_probes WHERE capability = ?", (capability,)
                )
        return cursor.rowcount

    def reserve_provider_request(
        self,
        *,
        provider: str,
        budget_scope: str,
        requested_at: str | datetime,
        minimum_interval_seconds: float,
    ) -> bool:
        """Atomically reserve a cross-process provider request interval."""

        if not provider or not budget_scope or minimum_interval_seconds <= 0:
            raise ValueError("provider request budget inputs are invalid")
        timestamp = _timestamp(requested_at)
        requested = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        next_allowed = _timestamp(
            requested + timedelta(seconds=minimum_interval_seconds)
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                """
                SELECT next_allowed_at FROM provider_request_limits
                WHERE provider = ? AND budget_scope = ?
                """,
                (provider, budget_scope),
            ).fetchone()
            if row is not None:
                current_limit = datetime.fromisoformat(
                    row["next_allowed_at"].replace("Z", "+00:00")
                )
                remaining_seconds = (current_limit - requested).total_seconds()
                # A valid reservation is only one configured interval ahead.
                # Treat a much larger future deadline as stale clock-jump
                # residue so wall-clock correction cannot wedge all restarts.
                recovery_window = max(5.0, minimum_interval_seconds * 2)
                if 0 < remaining_seconds <= recovery_window:
                    self._connection.rollback()
                    return False
                if remaining_seconds > recovery_window:
                    self._connection.execute(
                        """
                        UPDATE provider_request_limits
                        SET next_allowed_at = ?
                        WHERE provider = ? AND budget_scope = ?
                        """,
                        (next_allowed, provider, budget_scope),
                    )
                    self._connection.commit()
                    return False
            self._connection.execute(
                """
                INSERT INTO provider_request_limits(
                    provider, budget_scope, next_allowed_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(provider, budget_scope) DO UPDATE SET
                    next_allowed_at = excluded.next_allowed_at
                """,
                (provider, budget_scope, next_allowed),
            )
            self._connection.commit()
            return True
        except BaseException:
            self._connection.rollback()
            raise

    def save_provider_probe(
        self,
        *,
        capability: str,
        account_alias: str,
        probe_state: str,
        checked_at: str | datetime,
        retryable: bool,
    ) -> ProviderProbeRecord:
        allowed_states = {
            "ready",
            "authentication_failed",
            "data_inaccessible",
            "provider_unavailable",
            "rate_limited",
            "contract_changed",
            "invalid_request",
        }
        if probe_state not in allowed_states:
            raise ValueError("unsupported provider probe state")
        normalized_alias = _account_alias(account_alias)
        timestamp = _timestamp(checked_at)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO provider_probes(
                    capability, account_alias, probe_state, checked_at,
                    retryable
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(capability, account_alias) DO UPDATE SET
                    probe_state = excluded.probe_state,
                    checked_at = excluded.checked_at,
                    retryable = excluded.retryable
                """,
                (
                    capability,
                    normalized_alias,
                    probe_state,
                    timestamp,
                    int(retryable),
                ),
            )
        record = self.get_provider_probe(
            capability=capability, account_alias=normalized_alias
        )
        assert record is not None
        return record

    def get_provider_probe(
        self, *, capability: str, account_alias: str
    ) -> ProviderProbeRecord | None:
        normalized_alias = _account_alias(account_alias)
        row = self._connection.execute(
            """
            SELECT * FROM provider_probes
            WHERE capability = ? AND account_alias = ? COLLATE NOCASE
            """,
            (capability, normalized_alias),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["retryable"] = bool(values["retryable"])
        return ProviderProbeRecord(**values)

    def begin_sync(
        self,
        *,
        provider: str,
        capability: str,
        started_at: str | datetime,
        machine_id: str | None = None,
        account_id: int | None = None,
    ) -> SyncRun:
        timestamp = _timestamp(started_at)
        if machine_id is not None and account_id is not None:
            raise ValueError("a sync cannot target both a machine and an account")
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO sync_runs(
                    provider, capability, machine_id, account_id, started_at, status
                ) VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (provider, capability, machine_id, account_id, timestamp),
            )
        return self.get_sync_run(int(cursor.lastrowid))

    def record_owned_data_consent(
        self,
        *,
        account_id: int,
        disclosure_version: str,
        accepted_at: str | datetime,
        backups_acknowledged: bool,
    ) -> AccountDataConsent:
        if not disclosure_version or len(disclosure_version) > 128:
            raise ValueError("disclosure_version must be between 1 and 128 characters")
        if backups_acknowledged is not True:
            raise ValueError("backup implications must be acknowledged")
        timestamp = _timestamp(accepted_at)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO account_data_consents(
                    account_id, consent_kind, disclosure_version,
                    backups_acknowledged, accepted_at
                ) VALUES (?, 'owned_persistence', ?, 1, ?)
                ON CONFLICT(account_id, consent_kind) DO UPDATE SET
                    disclosure_version = excluded.disclosure_version,
                    backups_acknowledged = excluded.backups_acknowledged,
                    accepted_at = excluded.accepted_at
                """,
                (account_id, disclosure_version, timestamp),
            )
        consent = self.get_owned_data_consent(account_id)
        assert consent is not None
        return consent

    def get_owned_data_consent(self, account_id: int) -> AccountDataConsent | None:
        row = self._connection.execute(
            """
            SELECT * FROM account_data_consents
            WHERE account_id = ? AND consent_kind = 'owned_persistence'
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["backups_acknowledged"] = bool(values["backups_acknowledged"])
        return AccountDataConsent(**values)

    def record_owned_snapshot(
        self,
        sync_run_id: int,
        observations: list[OwnedObservation] | tuple[OwnedObservation, ...],
        *,
        base_retrieved_at: str | datetime,
        expanded_retrieved_at: str | datetime,
        base_reported_count: int,
        expanded_reported_count: int,
        support_level: str = "official_documented",
        _manage_transaction: bool = True,
    ) -> tuple[int, ...]:
        """Atomically record one normalized GetOwnedGames response.

        Evidence payloads are constructed here from the allowlisted normalized
        fields. Callers cannot pass a raw provider object through this method.
        """

        if not _manage_transaction and not self._connection.in_transaction:
            raise StorageError("managed owned recording requires a transaction")

        base_retrieved = _timestamp(base_retrieved_at)
        expanded_retrieved = _timestamp(expanded_retrieved_at)
        normalized: list[tuple[OwnedObservation, str]] = []
        appids: set[int] = set()
        if not support_level or len(support_level) > 128:
            raise ValueError("support_level must be between 1 and 128 characters")
        if (
            not isinstance(base_reported_count, int)
            or isinstance(base_reported_count, bool)
            or base_reported_count < 0
            or not isinstance(expanded_reported_count, int)
            or isinstance(expanded_reported_count, bool)
            or expanded_reported_count < 0
        ):
            raise ValueError("owned reported counts must be nonnegative integers")
        for observation in observations:
            if observation.appid <= 0 or observation.appid in appids:
                raise ValueError("owned AppIDs must be positive and unique")
            if (
                observation.playtime_forever_minutes is not None
                and observation.playtime_forever_minutes < 0
            ):
                raise ValueError("owned playtime cannot be negative")
            if observation.inclusion_basis not in ("visible_owned", "played_free"):
                raise ValueError("unsupported owned inclusion basis")
            if observation.name is not None and (
                not isinstance(observation.name, str)
                or len(observation.name) > 512
                or any(ord(character) < 32 for character in observation.name)
            ):
                raise ValueError("owned name is invalid")
            observed = _timestamp(observation.observed_at)
            normalized.append((observation, observed))
            appids.add(observation.appid)
        visible_owned_count = sum(
            observation.inclusion_basis == "visible_owned"
            for observation, _ in normalized
        )
        if (
            visible_owned_count != base_reported_count
            or len(normalized) != expanded_reported_count
        ):
            raise ValueError("owned reported counts do not match normalized records")

        if _manage_transaction:
            self._connection.execute("BEGIN IMMEDIATE")
        try:
            run = self._require_running_owned_sync(sync_run_id)
            already_recorded = self._connection.execute(
                "SELECT 1 FROM owned_sync_metadata WHERE sync_run_id = ?",
                (sync_run_id,),
            ).fetchone()
            if already_recorded is not None:
                raise InvalidSyncTransition("owned snapshot is already recorded")
            consent = self._connection.execute(
                """
                SELECT 1 FROM account_data_consents
                WHERE account_id = ? AND consent_kind = 'owned_persistence'
                """,
                (run.account_id,),
            ).fetchone()
            if consent is None:
                raise InvalidSyncTransition("owned persistence consent is required")

            self._connection.execute(
                """
                INSERT INTO owned_sync_metadata(
                    sync_run_id, account_id, provider, support_level,
                    include_appinfo, base_include_played_free_games,
                    base_retrieved_at, base_reported_count,
                    expanded_include_played_free_games, expanded_retrieved_at,
                    expanded_reported_count, classification_method
                ) VALUES (?, ?, ?, ?, 1, 0, ?, ?, 1, ?, ?,
                    'sequential_set_difference')
                """,
                (
                    sync_run_id,
                    run.account_id,
                    run.provider,
                    support_level,
                    base_retrieved,
                    base_reported_count,
                    expanded_retrieved,
                    expanded_reported_count,
                ),
            )

            evidence_ids: list[int] = []
            for observation, observed in normalized:
                payload = {
                    "appid": observation.appid,
                    "inclusion_basis": observation.inclusion_basis,
                    "name": observation.name,
                    "playtime_forever_minutes": observation.playtime_forever_minutes,
                }
                evidence_id = self._insert_evidence(
                    EvidenceInput(
                        provider=run.provider,
                        capability=run.capability,
                        source_kind="steam_web_api",
                        source_locator=f"GetOwnedGames:app:{observation.appid}",
                        retrieved_at=expanded_retrieved,
                        support_level=support_level,
                        context={
                            "account_id": run.account_id,
                            "classification_method": "sequential_set_difference",
                            "request_pair": {
                                "base": {
                                    "include_appinfo": True,
                                    "include_played_free_games": False,
                                    "reported_count": base_reported_count,
                                    "retrieved_at": base_retrieved,
                                },
                                "expanded": {
                                    "include_appinfo": True,
                                    "include_played_free_games": True,
                                    "reported_count": expanded_reported_count,
                                    "retrieved_at": expanded_retrieved,
                                },
                            },
                        },
                        payload=payload,
                        account_id=run.account_id,
                    )
                )
                self._ensure_steam_application_identity(
                    observation.appid, observed_at=observed
                )
                self._connection.execute(
                    """
                    INSERT INTO owned_observations(
                        sync_run_id, evidence_id, account_id, appid, name,
                        playtime_forever_minutes, inclusion_basis, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sync_run_id,
                        evidence_id,
                        run.account_id,
                        observation.appid,
                        observation.name,
                        observation.playtime_forever_minutes,
                        observation.inclusion_basis,
                        observed,
                    ),
                )
                evidence_ids.append(evidence_id)
            self._connection.execute(
                "UPDATE sync_runs SET records_seen = ? WHERE id = ?",
                (len(normalized), sync_run_id),
            )
            if _manage_transaction:
                self._connection.commit()
            return tuple(evidence_ids)
        except BaseException:
            if _manage_transaction:
                try:
                    self._rollback_or_reopen()
                except BaseException:
                    pass
            raise

    def complete_owned_snapshot(
        self,
        sync_run_id: int,
        observations: list[OwnedObservation] | tuple[OwnedObservation, ...],
        *,
        base_retrieved_at: str | datetime,
        expanded_retrieved_at: str | datetime,
        base_reported_count: int,
        expanded_reported_count: int,
        completed_at: str | datetime,
        support_level: str = "official_documented",
    ) -> SyncRun:
        """Record, promote, prune, and finish one owned snapshot atomically."""

        completed = _timestamp(completed_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._require_running_owned_sync(sync_run_id)
            self.record_owned_snapshot(
                sync_run_id,
                observations,
                base_retrieved_at=base_retrieved_at,
                expanded_retrieved_at=expanded_retrieved_at,
                base_reported_count=base_reported_count,
                expanded_reported_count=expanded_reported_count,
                support_level=support_level,
                _manage_transaction=False,
            )
            newer_complete = self._connection.execute(
                """
                SELECT 1 FROM sync_runs
                WHERE capability = 'owned.visible.read' AND account_id = ?
                  AND id > ? AND status = 'complete'
                LIMIT 1
                """,
                (existing.account_id, sync_run_id),
            ).fetchone()
            promoted = 0
            if newer_complete is None:
                self._promote_owned(existing.account_id, sync_run_id)
                promoted = 1
                self._prune_owned_payloads(
                    existing.account_id, keep_sync_run_id=sync_run_id
                )
            else:
                self._prune_owned_payloads(
                    existing.account_id, only_sync_run_id=sync_run_id
                )
            self._connection.execute(
                """
                UPDATE sync_runs
                SET status = 'complete', completed_at = ?, promoted = ?,
                    error_code = NULL, error_detail = NULL
                WHERE id = ?
                """,
                (completed, promoted, sync_run_id),
            )
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise
        return self.get_sync_run(sync_run_id)

    def finish_owned_sync(
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
            existing = self._require_owned_sync(sync_run_id)
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
            promoted = 0
            if status == "complete":
                recorded = self._connection.execute(
                    "SELECT 1 FROM owned_sync_metadata WHERE sync_run_id = ?",
                    (sync_run_id,),
                ).fetchone()
                if recorded is None:
                    raise InvalidSyncTransition(
                        "a complete owned sync requires a recorded snapshot"
                    )
                newer_complete = self._connection.execute(
                    """
                    SELECT 1 FROM sync_runs
                    WHERE capability = 'owned.visible.read' AND account_id = ?
                      AND id > ? AND status = 'complete'
                    LIMIT 1
                    """,
                    (existing.account_id, sync_run_id),
                ).fetchone()
                if newer_complete is None:
                    self._promote_owned(existing.account_id, sync_run_id)
                    promoted = 1
                    self._prune_owned_payloads(
                        existing.account_id, keep_sync_run_id=sync_run_id
                    )
                else:
                    self._prune_owned_payloads(
                        existing.account_id, only_sync_run_id=sync_run_id
                    )
            else:
                self._prune_owned_payloads(
                    existing.account_id, only_sync_run_id=sync_run_id
                )
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
            self._ensure_steam_application_identity(
                observation.appid, observed_at=observed_at
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

    def latest_account_sync(
        self,
        *,
        capability: str,
        account_id: int,
        status: SyncStatus | None = None,
    ) -> SyncRun | None:
        clauses = ["capability = ?", "account_id = ?"]
        parameters: list[object] = [capability, account_id]
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        row = self._connection.execute(
            f"SELECT * FROM sync_runs WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT 1",
            parameters,
        ).fetchone()
        return None if row is None else _sync_run(row)

    def list_owned(self, account_id: int) -> list[OwnedGame]:
        rows = self._connection.execute(
            """
            SELECT
                account_id, appid, name, playtime_forever_minutes,
                inclusion_basis, observed_at, evidence_id, promoted_sync_run_id
            FROM owned_current
            WHERE account_id = ?
            ORDER BY appid
            """,
            (account_id,),
        )
        return [OwnedGame(**dict(row)) for row in rows]

    def read_owned_snapshot(self, account_id: int) -> OwnedSnapshot:
        if self._connection.in_transaction:
            raise StorageError("cannot start a read snapshot inside a transaction")
        self._connection.execute("BEGIN")
        try:
            snapshot = self._read_owned_snapshot(account_id)
            self._connection.commit()
            return snapshot
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise

    def read_library_snapshot(
        self, account_id: int, machine_id: str
    ) -> LibrarySnapshot:
        if self._connection.in_transaction:
            raise StorageError("cannot start a read snapshot inside a transaction")
        self._connection.execute("BEGIN")
        try:
            owned = self._read_owned_snapshot(account_id)
            installed_games = tuple(self.list_installed(machine_id))
            installed = InstalledSnapshot(
                games=installed_games,
                latest=self.latest_sync(capability="installed", machine_id=machine_id),
                latest_complete=self.latest_sync(
                    capability="installed",
                    machine_id=machine_id,
                    status="complete",
                ),
            )
            stable_game_ids_by_appid = self._stable_game_ids_for_appids(
                {
                    *(game.appid for game in owned.games),
                    *(game.appid for game in installed.games),
                }
            )
            self._connection.commit()
            return LibrarySnapshot(
                owned=owned,
                installed=installed,
                stable_game_ids_by_appid=stable_game_ids_by_appid,
            )
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise

    def delete_steam_account_data(self, account_id: int) -> AccountDataDeletion:
        """Delete one target account while preserving data-profile credentials."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            account = self._connection.execute(
                "SELECT alias FROM accounts WHERE id = ? AND provider = 'steam'",
                (account_id,),
            ).fetchone()
            if account is None:
                self._connection.commit()
                return AccountDataDeletion(False, 0, 0, 0, 0, 0, 0, 0)
            alias = account["alias"]
            counts = {
                "owned_observations": self._count_where(
                    "owned_observations", "account_id", account_id
                ),
                "owned_current": self._count_where(
                    "owned_current", "account_id", account_id
                ),
                "sync_runs": self._count_where("sync_runs", "account_id", account_id),
                "consents": self._count_where(
                    "account_data_consents", "account_id", account_id
                ),
            }
            counts["probes"] = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM provider_probes "
                    "WHERE account_alias = ? COLLATE NOCASE",
                    (alias,),
                ).fetchone()[0]
            )
            evidence_ids = tuple(
                int(row[0])
                for row in self._connection.execute(
                    "SELECT DISTINCT evidence_id FROM owned_observations "
                    "WHERE account_id = ?",
                    (account_id,),
                )
            )
            appids = tuple(
                int(row[0])
                for row in self._connection.execute(
                    "SELECT DISTINCT appid FROM owned_observations "
                    "WHERE account_id = ?",
                    (account_id,),
                )
            )
            cursor = self._connection.execute(
                "DELETE FROM accounts WHERE id = ? AND provider = 'steam'",
                (account_id,),
            )
            evidence_removed = len(evidence_ids)
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                evidence_cursor = self._connection.execute(
                    f"""
                    DELETE FROM evidence
                    WHERE id IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM installed_observations
                          WHERE installed_observations.evidence_id = evidence.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM installed_current
                          WHERE installed_current.evidence_id = evidence.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM owned_observations
                          WHERE owned_observations.evidence_id = evidence.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM owned_current
                          WHERE owned_current.evidence_id = evidence.id
                      )
                    """,
                    evidence_ids,
                )
                evidence_removed = max(evidence_removed, evidence_cursor.rowcount)
            orphan_apps_removed = 0
            if appids:
                placeholders = ",".join("?" for _ in appids)
                app_cursor = self._connection.execute(
                    f"""
                    DELETE FROM steam_apps
                    WHERE appid IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM installed_observations
                          WHERE installed_observations.appid = steam_apps.appid
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM installed_current
                          WHERE installed_current.appid = steam_apps.appid
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM owned_observations
                          WHERE owned_observations.appid = steam_apps.appid
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM owned_current
                          WHERE owned_current.appid = steam_apps.appid
                      )
                    """,
                    appids,
                )
                orphan_apps_removed = app_cursor.rowcount
            self._connection.commit()
            return AccountDataDeletion(
                account_removed=cursor.rowcount > 0,
                owned_observations_removed=counts["owned_observations"],
                owned_current_removed=counts["owned_current"],
                sync_runs_removed=counts["sync_runs"],
                probes_removed=counts["probes"],
                consents_removed=counts["consents"],
                evidence_removed=evidence_removed,
                orphan_apps_removed=orphan_apps_removed,
            )
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise

    def delete_all_steam_account_data(
        self,
        *,
        credential_provider: str | None = None,
        credential_kind: str | None = None,
        credential_profile_id: str | None = None,
    ) -> AllSteamAccountDataDeletion:
        """Delete every Steam account subject in one database transaction.

        When a complete credential identity is supplied, its metadata is
        removed in the same transaction. The caller deletes the external
        secret first and restores it if this database transaction fails.
        """

        credential_parts = (
            credential_provider,
            credential_kind,
            credential_profile_id,
        )
        if any(part is not None for part in credential_parts) and not all(
            isinstance(part, str) and part for part in credential_parts
        ):
            raise ValueError("credential identity must be complete or omitted")
        credential_identity_supplied = credential_provider is not None

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            accounts = tuple(
                (int(row["id"]), str(row["alias"]))
                for row in self._connection.execute(
                    "SELECT id, alias FROM accounts WHERE provider = 'steam'"
                )
            )
            if not accounts:
                credential_refs_removed = self._remove_credential_identity(
                    credential_provider, credential_kind, credential_profile_id
                )
                self._connection.commit()
                return AllSteamAccountDataDeletion(
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    credential_refs_removed,
                    not credential_identity_supplied,
                )
            account_ids = tuple(account_id for account_id, _ in accounts)
            aliases = tuple(alias for _, alias in accounts)
            id_placeholders = ",".join("?" for _ in account_ids)
            alias_placeholders = ",".join("?" for _ in aliases)
            counts = {
                "owned_observations": int(
                    self._connection.execute(
                        f"SELECT COUNT(*) FROM owned_observations "
                        f"WHERE account_id IN ({id_placeholders})",
                        account_ids,
                    ).fetchone()[0]
                ),
                "owned_current": int(
                    self._connection.execute(
                        f"SELECT COUNT(*) FROM owned_current "
                        f"WHERE account_id IN ({id_placeholders})",
                        account_ids,
                    ).fetchone()[0]
                ),
                "sync_runs": int(
                    self._connection.execute(
                        f"SELECT COUNT(*) FROM sync_runs "
                        f"WHERE account_id IN ({id_placeholders})",
                        account_ids,
                    ).fetchone()[0]
                ),
                "consents": int(
                    self._connection.execute(
                        f"SELECT COUNT(*) FROM account_data_consents "
                        f"WHERE account_id IN ({id_placeholders})",
                        account_ids,
                    ).fetchone()[0]
                ),
                "probes": int(
                    self._connection.execute(
                        f"SELECT COUNT(*) FROM provider_probes "
                        f"WHERE account_alias COLLATE NOCASE IN ({alias_placeholders})",
                        aliases,
                    ).fetchone()[0]
                ),
            }
            evidence_ids = tuple(
                int(row[0])
                for row in self._connection.execute(
                    f"SELECT DISTINCT evidence_id FROM owned_observations "
                    f"WHERE account_id IN ({id_placeholders})",
                    account_ids,
                )
            )
            appids = tuple(
                int(row[0])
                for row in self._connection.execute(
                    f"SELECT DISTINCT appid FROM owned_observations "
                    f"WHERE account_id IN ({id_placeholders})",
                    account_ids,
                )
            )
            account_cursor = self._connection.execute(
                "DELETE FROM accounts WHERE provider = 'steam'"
            )
            evidence_removed = max(
                len(evidence_ids), self._delete_orphan_owned_evidence(evidence_ids)
            )
            orphan_apps_removed = self._delete_orphan_apps(appids)
            credential_refs_removed = self._remove_credential_identity(
                credential_provider, credential_kind, credential_profile_id
            )
            self._connection.commit()
            return AllSteamAccountDataDeletion(
                accounts_removed=account_cursor.rowcount,
                owned_observations_removed=counts["owned_observations"],
                owned_current_removed=counts["owned_current"],
                sync_runs_removed=counts["sync_runs"],
                probes_removed=counts["probes"],
                consents_removed=counts["consents"],
                evidence_removed=evidence_removed,
                orphan_apps_removed=orphan_apps_removed,
                credential_refs_removed=credential_refs_removed,
                shared_credential_preserved=not credential_identity_supplied,
            )
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise

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
            latest = self.latest_sync(capability="installed", machine_id=machine_id)
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

    def _read_owned_snapshot(self, account_id: int) -> OwnedSnapshot:
        games = tuple(self.list_owned(account_id))
        latest = self.latest_account_sync(
            capability="owned.visible.read", account_id=account_id
        )
        latest_complete = self.latest_account_sync(
            capability="owned.visible.read",
            account_id=account_id,
            status="complete",
        )
        return OwnedSnapshot(
            games=games,
            latest=latest,
            latest_complete=latest_complete,
            latest_complete_provenance=(
                None
                if latest_complete is None
                else self._owned_snapshot_provenance(latest_complete.id)
            ),
            stable_game_ids_by_appid=self._stable_game_ids_for_appids(
                {game.appid for game in games}
            ),
        )

    def _owned_snapshot_provenance(
        self, sync_run_id: int
    ) -> OwnedSnapshotProvenance | None:
        row = self._connection.execute(
            """
            SELECT
                sync_run_id, provider, support_level, include_appinfo,
                base_include_played_free_games, base_retrieved_at,
                base_reported_count, expanded_include_played_free_games,
                expanded_retrieved_at, expanded_reported_count,
                classification_method
            FROM owned_sync_metadata
            WHERE sync_run_id = ?
            """,
            (sync_run_id,),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        for key in (
            "include_appinfo",
            "base_include_played_free_games",
            "expanded_include_played_free_games",
        ):
            if values[key] is not None:
                values[key] = bool(values[key])
        return OwnedSnapshotProvenance(**values)

    def _ensure_steam_application_identity(
        self, appid: int, *, observed_at: str
    ) -> int:
        """Create the bounded Steam-application mapping without type merging."""

        self._connection.execute(
            """
            INSERT INTO steam_apps(appid, name, app_type, updated_at)
            VALUES (?, NULL, 'unknown', ?)
            ON CONFLICT(appid) DO NOTHING
            """,
            (appid, observed_at),
        )
        row = self._connection.execute(
            """
            SELECT game_entity_id FROM external_game_identities
            WHERE provider = 'steam' AND identity_kind = 'application_appid'
              AND external_id = ?
            """,
            (str(appid),),
        ).fetchone()
        if row is not None:
            return int(row[0])
        cursor = self._connection.execute(
            """
            INSERT INTO game_entities(entity_kind, created_at, updated_at, stable_id)
            VALUES ('application', ?, ?, ?)
            """,
            (observed_at, observed_at, steam_application_stable_id(appid)),
        )
        entity_id = int(cursor.lastrowid)
        self._connection.execute(
            """
            INSERT INTO external_game_identities(
                provider, identity_kind, external_id, game_entity_id, created_at
            ) VALUES ('steam', 'application_appid', ?, ?, ?)
            """,
            (str(appid), entity_id, observed_at),
        )
        return entity_id

    def _stable_game_ids_for_appids(
        self, appids: set[int]
    ) -> tuple[tuple[int, int], ...]:
        if not appids:
            return ()
        external_ids = tuple(str(appid) for appid in sorted(appids))
        placeholders = ",".join("?" for _ in external_ids)
        rows = self._connection.execute(
            f"""
            SELECT CAST(external_id AS INTEGER) AS appid, stable_id
            FROM external_game_identities
            JOIN game_entities ON game_entities.id = game_entity_id
            WHERE provider = 'steam'
              AND identity_kind = 'application_appid'
              AND external_id IN ({placeholders})
            ORDER BY CAST(external_id AS INTEGER)
            """,
            external_ids,
        )
        result = tuple((int(row[0]), str(row[1])) for row in rows)
        if len(result) != len(appids):
            raise StorageError("Steam application identity mapping is incomplete")
        return result

    def _count_where(self, table: str, column: str, value: object) -> int:
        allowed = {
            ("owned_observations", "account_id"),
            ("owned_current", "account_id"),
            ("sync_runs", "account_id"),
            ("account_data_consents", "account_id"),
        }
        if (table, column) not in allowed:
            raise ValueError("unsupported count target")
        return int(
            self._connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)
            ).fetchone()[0]
        )

    def _delete_orphan_owned_evidence(self, evidence_ids: tuple[int, ...]) -> int:
        if not evidence_ids:
            return 0
        placeholders = ",".join("?" for _ in evidence_ids)
        cursor = self._connection.execute(
            f"""
            DELETE FROM evidence
            WHERE id IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM installed_observations
                  WHERE installed_observations.evidence_id = evidence.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM installed_current
                  WHERE installed_current.evidence_id = evidence.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM owned_observations
                  WHERE owned_observations.evidence_id = evidence.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM owned_current
                  WHERE owned_current.evidence_id = evidence.id
              )
            """,
            evidence_ids,
        )
        return cursor.rowcount

    def _prune_owned_payloads(
        self,
        account_id: int | None,
        *,
        keep_sync_run_id: int | None = None,
        only_sync_run_id: int | None = None,
    ) -> None:
        """Remove non-current account payload while retaining coarse run rows."""

        if account_id is None or (keep_sync_run_id is None) == (
            only_sync_run_id is None
        ):
            raise ValueError("owned payload pruning requires exactly one run selector")
        if keep_sync_run_id is not None:
            operator = "<>"
            selected_run_id = keep_sync_run_id
            terminal_clause = (
                "AND sync_run_id IN "
                "(SELECT id FROM sync_runs WHERE status <> 'running')"
            )
        else:
            operator = "="
            assert only_sync_run_id is not None
            selected_run_id = only_sync_run_id
            terminal_clause = ""
        evidence_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                f"""
                SELECT DISTINCT evidence_id FROM owned_observations
                WHERE account_id = ? AND sync_run_id {operator} ?
                  {terminal_clause}
                """,
                (account_id, selected_run_id),
            )
        )
        appids = tuple(
            int(row[0])
            for row in self._connection.execute(
                f"""
                SELECT DISTINCT appid FROM owned_observations
                WHERE account_id = ? AND sync_run_id {operator} ?
                  {terminal_clause}
                """,
                (account_id, selected_run_id),
            )
        )
        self._connection.execute(
            f"""
            DELETE FROM owned_observations
            WHERE account_id = ? AND sync_run_id {operator} ?
              {terminal_clause}
            """,
            (account_id, selected_run_id),
        )
        self._connection.execute(
            f"""
            DELETE FROM owned_sync_metadata
            WHERE account_id = ? AND sync_run_id {operator} ?
              {terminal_clause}
            """,
            (account_id, selected_run_id),
        )
        self._delete_orphan_owned_evidence(evidence_ids)
        self._delete_orphan_apps(appids)

    def _remove_credential_identity(
        self,
        provider: str | None,
        kind: str | None,
        profile_id: str | None,
    ) -> int:
        if provider is None or kind is None or profile_id is None:
            return 0
        cursor = self._connection.execute(
            """
            DELETE FROM credential_refs
            WHERE provider = ? AND kind = ? AND profile_id = ?
            """,
            (provider, kind, profile_id),
        )
        return cursor.rowcount

    def _delete_orphan_apps(self, appids: tuple[int, ...]) -> int:
        if not appids:
            return 0
        placeholders = ",".join("?" for _ in appids)
        cursor = self._connection.execute(
            f"""
            DELETE FROM steam_apps
            WHERE appid IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM installed_observations
                  WHERE installed_observations.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM installed_current
                  WHERE installed_current.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM owned_observations
                  WHERE owned_observations.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM owned_current
                  WHERE owned_current.appid = steam_apps.appid
              )
            """,
            appids,
        )
        external_ids = tuple(str(appid) for appid in appids)
        entity_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                f"""
                SELECT game_entity_id FROM external_game_identities
                WHERE provider = 'steam'
                  AND identity_kind = 'application_appid'
                  AND external_id IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM steam_apps
                      WHERE CAST(steam_apps.appid AS TEXT) = external_id
                  )
                """,
                external_ids,
            )
        )
        if entity_ids:
            self._connection.execute(
                f"""
                DELETE FROM external_game_identities
                WHERE provider = 'steam'
                  AND identity_kind = 'application_appid'
                  AND external_id IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM steam_apps
                      WHERE CAST(steam_apps.appid AS TEXT) = external_id
                  )
                """,
                external_ids,
            )
            entity_placeholders = ",".join("?" for _ in entity_ids)
            self._connection.execute(
                f"""
                DELETE FROM game_entities
                WHERE id IN ({entity_placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM external_game_identities
                      WHERE external_game_identities.game_entity_id = game_entities.id
                  )
                """,
                entity_ids,
            )
        return cursor.rowcount

    def _require_owned_sync(self, sync_run_id: int) -> SyncRun:
        run = self.get_sync_run(sync_run_id)
        if run.capability != "owned.visible.read":
            raise InvalidSyncTransition("sync run is not an owned-library sync")
        if run.account_id is None or run.machine_id is not None:
            raise InvalidSyncTransition("owned sync requires only an account_id")
        return run

    def _require_running_owned_sync(self, sync_run_id: int) -> SyncRun:
        run = self._require_owned_sync(sync_run_id)
        if run.status != "running":
            raise InvalidSyncTransition(
                f"sync run {sync_run_id} is already {run.status}"
            )
        return run

    def _require_running_installed_sync(self, sync_run_id: int) -> SyncRun:
        run = self.get_sync_run(sync_run_id)
        if run.capability != "installed":
            raise InvalidSyncTransition("sync run is not an installed scan")
        if run.status != "running":
            raise InvalidSyncTransition(
                f"sync run {sync_run_id} is already {run.status}"
            )
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
                effective_at, support_level, context_json, payload_json, content_hash,
                account_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                evidence.account_id,
            ),
        )
        row = self._connection.execute(
            """
            SELECT id FROM evidence
            WHERE provider = ? AND capability = ? AND source_kind = ?
              AND source_locator = ? AND retrieved_at = ? AND context_json = ?
              AND content_hash = ?
              AND account_id IS ?
            """,
            (
                evidence.provider,
                evidence.capability,
                evidence.source_kind,
                evidence.source_locator,
                retrieved_at,
                context_json,
                content_hash,
                evidence.account_id,
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

    def _promote_owned(self, account_id: int | None, sync_run_id: int) -> None:
        if account_id is None:
            raise InvalidSyncTransition("owned sync requires an account_id")
        observed_at = self._connection.execute(
            "SELECT started_at FROM sync_runs WHERE id = ?", (sync_run_id,)
        ).fetchone()["started_at"]
        # Account-sourced names remain observation-local. The shared steam_apps
        # catalog must not inherit a private account's display metadata.
        self._connection.execute(
            """
            INSERT INTO steam_apps(appid, name, app_type, updated_at)
            SELECT DISTINCT appid, NULL, 'unknown', ?
            FROM owned_observations WHERE sync_run_id = ?
            ON CONFLICT(appid) DO NOTHING
            """,
            (observed_at, sync_run_id),
        )
        self._connection.execute(
            "DELETE FROM owned_current WHERE account_id = ?", (account_id,)
        )
        self._connection.execute(
            """
            INSERT INTO owned_current(
                account_id, appid, evidence_id, promoted_sync_run_id, name,
                playtime_forever_minutes, inclusion_basis, observed_at
            )
            SELECT
                account_id, appid, evidence_id, sync_run_id, name,
                playtime_forever_minutes, inclusion_basis, observed_at
            FROM owned_observations
            WHERE sync_run_id = ? AND account_id = ?
            """,
            (sync_run_id, account_id),
        )


__all__ = [
    "Account",
    "AccountConflict",
    "AccountDataConsent",
    "AccountDataDeletion",
    "AllSteamAccountDataDeletion",
    "CredentialReferenceRecord",
    "EvidenceInput",
    "InstalledGame",
    "InstalledObservation",
    "InstalledSnapshot",
    "InvalidSyncTransition",
    "LibrarySnapshot",
    "Machine",
    "OwnedGame",
    "OwnedInclusionBasis",
    "OwnedObservation",
    "OwnedSnapshot",
    "OwnedSnapshotProvenance",
    "ProviderProbeRecord",
    "SteamApp",
    "Storage",
    "StorageError",
    "SyncRun",
    "UnknownSyncRun",
    "steam_application_stable_id",
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


def _account_alias(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("account alias must be text")
    if not 1 <= len(value) <= 64:
        raise ValueError("account alias must be between 1 and 64 characters")
    if not value[0].isascii() or not value[0].isalpha():
        raise ValueError("account alias must begin with an ASCII letter")
    if any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in value
    ):
        raise ValueError(
            "account alias may contain only ASCII letters, digits, _ and -"
        )
    return value

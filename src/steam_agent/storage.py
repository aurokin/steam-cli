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
from urllib.parse import parse_qs, urlsplit

from steam_agent.local_accounts import validate_steam_id64


SyncStatus = Literal["running", "complete", "partial", "failed"]
TerminalSyncStatus = Literal["complete", "partial", "failed"]
STEAM_APPLICATION_IDENTITY_NAMESPACE = uuid.UUID("d95b6568-2886-5d15-aa84-1986e4ac511e")


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
class WishlistObservation:
    appid: int
    priority: int
    date_added: int
    observed_at: str | datetime


@dataclass(frozen=True)
class WishlistGame:
    account_id: int
    appid: int
    priority: int
    date_added: int
    observed_at: str
    evidence_id: int
    promoted_sync_run_id: int


@dataclass(frozen=True)
class WishlistSnapshotProvenance:
    sync_run_id: int
    provider: str
    support_level: str
    item_list_retrieved_at: str
    item_list_reported_count: int
    item_count_retrieved_at: str
    item_count_reported_count: int
    validation_method: str


@dataclass(frozen=True)
class WishlistSnapshot:
    games: tuple[WishlistGame, ...]
    latest: SyncRun | None
    latest_complete: SyncRun | None
    latest_complete_provenance: WishlistSnapshotProvenance | None
    stable_game_ids_by_appid: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class PriceDemandSubject:
    appid: int
    demand_order: int
    wishlist_priority: int
    wishlist_date_added: int


@dataclass(frozen=True)
class PriceFactObservation:
    appid: int
    ordinal: int
    fact_kind: Literal["offer", "historical_low"]
    provider_product_id: str
    amount_minor: int
    currency: str
    regular_amount_minor: int | None
    discount_percent: int | None
    store_class: Literal["official", "keyshop", "unknown"]
    comparability: Literal["exact_product", "normalized_game", "unknown"]
    low_scope: str | None
    effective_at: str | datetime | None
    observed_at: str | datetime
    provider_url: str


@dataclass(frozen=True)
class StoredPriceFact:
    account_id: int
    country: str
    provider: str
    appid: int
    ordinal: int
    fact_kind: str
    provider_product_id: str
    product_mapping: str
    amount_minor: int
    currency: str
    regular_amount_minor: int | None
    discount_percent: int | None
    store_class: str
    comparability: str
    low_scope: str | None
    effective_at: str | None
    observed_at: str
    fresh_until: str
    hard_expires_at: str
    provider_url: str
    access_mode: str
    automation_supported: int
    evidence_id: int
    promoted_sync_run_id: int


@dataclass(frozen=True)
class StoredPriceSubject:
    account_id: int
    country: str
    provider: str
    appid: int
    outcome: str
    observed_at: str
    fresh_until: str
    hard_expires_at: str
    promoted_sync_run_id: int


@dataclass(frozen=True)
class PriceSnapshot:
    facts: tuple[StoredPriceFact, ...]
    subjects: tuple[StoredPriceSubject, ...]
    attempts: tuple[SyncRun, ...]
    stale_offer_count: int
    stale_historical_low_count: int
    stale_subject_count: int
    running: bool
    abandoned_running: bool


@dataclass(frozen=True)
class PriceDataDeletion:
    provider: str
    observations_removed: int
    current_removed: int
    subjects_removed: int
    sync_runs_removed: int
    evidence_removed: int
    credential_refs_removed: int = 0


CatalogClassification = Literal["game", "non_game", "not_observed"]
CatalogStreamName = Literal["games", "non_games"]


@dataclass(frozen=True)
class CatalogObservation:
    appid: int
    classification: CatalogClassification
    last_modified: int | None = None
    price_change_number: int | None = None


@dataclass(frozen=True)
class CatalogPageInput:
    page_number: int
    requested_last_appid: int
    first_appid: int | None
    last_appid: int
    item_count: int
    have_more_results: bool
    retrieved_at: str | datetime


@dataclass(frozen=True)
class CatalogStreamInput:
    stream: CatalogStreamName
    termination: str
    scanned_through_appid: int
    filter_context: Mapping[str, Any]
    pages: tuple[CatalogPageInput, ...]


@dataclass(frozen=True)
class CatalogFact:
    appid: int
    stable_game_id: str
    classification: CatalogClassification
    last_modified: int | None
    price_change_number: int | None
    observed_at: str
    evidence_id: int
    promoted_sync_run_id: int


@dataclass(frozen=True)
class CatalogStreamProvenance:
    stream: CatalogStreamName
    termination: str
    scanned_through_appid: int
    filter_context: Mapping[str, Any]
    pages: tuple[CatalogPageInput, ...]


@dataclass(frozen=True)
class CatalogSourceProvenance:
    sync_run_id: int
    provider: str
    support_level: str
    streams: tuple[CatalogStreamProvenance, ...]


@dataclass(frozen=True)
class CatalogRelevantAttempt:
    run: SyncRun
    appids: tuple[int, ...]


@dataclass(frozen=True)
class CatalogSnapshot:
    facts: tuple[CatalogFact, ...]
    sources: tuple[CatalogSourceProvenance, ...]
    # Compatibility shortcut only when one unique relevant attempt represents
    # the scoped demand. `attempts` is authoritative for aggregate truth.
    latest: SyncRun | None
    attempts: tuple[CatalogRelevantAttempt, ...] = ()


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
    wishlist_observations_removed: int = 0
    wishlist_current_removed: int = 0
    price_observations_removed: int = 0
    price_current_removed: int = 0
    price_subjects_removed: int = 0


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
    catalog_observations_removed: int = 0
    catalog_current_removed: int = 0
    catalog_sync_runs_removed: int = 0
    catalog_metadata_removed: int = 0
    catalog_streams_removed: int = 0
    catalog_pages_removed: int = 0
    catalog_evidence_removed: int = 0
    shared_credential_preserved: bool = True
    wishlist_observations_removed: int = 0
    wishlist_current_removed: int = 0
    price_observations_removed: int = 0
    price_current_removed: int = 0
    price_subjects_removed: int = 0


@dataclass(frozen=True)
class LibrarySnapshot:
    """Account and machine projections captured in one SQLite read transaction."""

    owned: OwnedSnapshot
    installed: InstalledSnapshot
    stable_game_ids_by_appid: tuple[tuple[int, str], ...]
    catalog: CatalogSnapshot


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


def _validate_price_fact(
    fact: PriceFactObservation, *, provider: str, country: str
) -> tuple[str, str | None]:
    if (
        not isinstance(fact.appid, int)
        or isinstance(fact.appid, bool)
        or not 1 <= fact.appid <= (1 << 32) - 1
        or not isinstance(fact.ordinal, int)
        or isinstance(fact.ordinal, bool)
        or not 0 <= fact.ordinal <= 10_000
        or fact.fact_kind not in {"offer", "historical_low"}
        or not isinstance(fact.provider_product_id, str)
        or not 1 <= len(fact.provider_product_id) <= 512
        or any(ord(character) < 32 for character in fact.provider_product_id)
        or not isinstance(fact.amount_minor, int)
        or isinstance(fact.amount_minor, bool)
        or not 0 <= fact.amount_minor <= (1 << 63) - 1
        or fact.currency != "USD"
        or country != "US"
        or fact.store_class not in {"official", "keyshop", "unknown"}
        or fact.comparability not in {"exact_product", "normalized_game", "unknown"}
    ):
        raise ValueError("price fact is invalid")
    if fact.regular_amount_minor is not None and (
        not isinstance(fact.regular_amount_minor, int)
        or isinstance(fact.regular_amount_minor, bool)
        or not 0 <= fact.regular_amount_minor <= (1 << 63) - 1
    ):
        raise ValueError("price regular amount is invalid")
    if fact.discount_percent is not None and (
        not isinstance(fact.discount_percent, int)
        or isinstance(fact.discount_percent, bool)
        or not 0 <= fact.discount_percent <= 100
    ):
        raise ValueError("price discount is invalid")
    allowed_scopes = {
        "all_time_official_stores",
        "all_time_keyshops",
        "all_time_any_store",
    }
    if fact.fact_kind == "offer":
        if fact.low_scope is not None or fact.effective_at is not None:
            raise ValueError("offer cannot carry historical-low fields")
    elif (
        fact.low_scope not in allowed_scopes
        or fact.regular_amount_minor is not None
        or fact.discount_percent is not None
    ):
        raise ValueError("historical-low fact is invalid")
    observed = _timestamp(fact.observed_at)
    effective = _optional_timestamp(fact.effective_at)
    if effective is not None:
        observed_dt = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        effective_dt = datetime.fromisoformat(effective.replace("Z", "+00:00"))
        if effective_dt > observed_dt:
            raise ValueError("price effective time cannot follow observation")
    try:
        parsed = urlsplit(fact.provider_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("price provider URL is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ValueError("price provider URL is invalid")
    if provider == "gg-deals":
        valid_url = (
            parsed.hostname == "gg.deals"
            and not parsed.query
            and parsed.path.startswith(
                ("/game/", "/dlc/", "/pack/", "/steam/app/")
            )
        )
    elif provider == "cheapshark":
        query = parse_qs(parsed.query, keep_blank_values=True)
        valid_url = (
            parsed.hostname == "www.cheapshark.com"
            and (
                (
                    parsed.path == "/redirect"
                    and set(query) == {"dealID"}
                    and len(query["dealID"]) == 1
                    and bool(query["dealID"][0])
                )
                or (
                    parsed.path == "/search"
                    and set(query) == {"steamAppID"}
                    and len(query["steamAppID"]) == 1
                    and query["steamAppID"][0].isdecimal()
                    and 1 <= int(query["steamAppID"][0]) <= (1 << 32) - 1
                )
            )
        )
    else:
        valid_url = False
    if not valid_url:
        raise ValueError("price provider URL is invalid")
    return observed, effective


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

    def begin_catalog_sync(
        self,
        *,
        provider: str,
        account_id: int,
        machine_id: str,
        demanded_appids: list[int] | tuple[int, ...],
        started_at: str | datetime,
    ) -> SyncRun:
        """Atomically record a catalog attempt and its complete demand subject."""

        timestamp = _timestamp(started_at)
        demanded = _catalog_appids(demanded_appids)
        if (
            not isinstance(machine_id, str)
            or not 1 <= len(machine_id) <= 256
            or any(ord(character) < 32 for character in machine_id)
        ):
            raise ValueError("catalog machine ID is invalid")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            account = self._connection.execute(
                "SELECT 1 FROM accounts WHERE id = ? AND provider = 'steam'",
                (account_id,),
            ).fetchone()
            if account is None:
                raise ValueError("catalog account is not configured")
            cursor = self._connection.execute(
                """
                INSERT INTO sync_runs(
                    provider, capability, started_at, status
                ) VALUES (?, 'catalog.application.read', ?, 'running')
                """,
                (provider, timestamp),
            )
            sync_run_id = int(cursor.lastrowid)
            self._connection.execute(
                """
                INSERT INTO catalog_sync_subjects(
                    sync_run_id, account_id, machine_id
                ) VALUES (?, ?, ?)
                """,
                (sync_run_id, account_id, machine_id),
            )
            self._connection.executemany(
                "INSERT INTO catalog_sync_demand(sync_run_id, appid) VALUES (?, ?)",
                ((sync_run_id, appid) for appid in demanded),
            )
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise
        return self.get_sync_run(sync_run_id)

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

    def record_wishlist_data_consent(
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
                ) VALUES (?, 'wishlist_persistence', ?, 1, ?)
                ON CONFLICT(account_id, consent_kind) DO UPDATE SET
                    disclosure_version = excluded.disclosure_version,
                    backups_acknowledged = excluded.backups_acknowledged,
                    accepted_at = excluded.accepted_at
                """,
                (account_id, disclosure_version, timestamp),
            )
        consent = self.get_wishlist_data_consent(account_id)
        assert consent is not None
        return consent

    def get_wishlist_data_consent(self, account_id: int) -> AccountDataConsent | None:
        row = self._connection.execute(
            """
            SELECT * FROM account_data_consents
            WHERE account_id = ? AND consent_kind = 'wishlist_persistence'
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["backups_acknowledged"] = bool(values["backups_acknowledged"])
        return AccountDataConsent(**values)

    def complete_wishlist_snapshot(
        self,
        sync_run_id: int,
        observations: list[WishlistObservation] | tuple[WishlistObservation, ...],
        *,
        item_list_retrieved_at: str | datetime,
        item_count_retrieved_at: str | datetime,
        item_list_reported_count: int,
        item_count_reported_count: int,
        completed_at: str | datetime,
        support_level: str = "official_undocumented_provisional",
    ) -> SyncRun:
        list_at = _timestamp(item_list_retrieved_at)
        count_at = _timestamp(item_count_retrieved_at)
        completed = _timestamp(completed_at)
        if not support_level or len(support_level) > 128:
            raise ValueError("support_level must be between 1 and 128 characters")
        if (
            not isinstance(item_list_reported_count, int)
            or isinstance(item_list_reported_count, bool)
            or not isinstance(item_count_reported_count, int)
            or isinstance(item_count_reported_count, bool)
            or item_list_reported_count < 0
            or item_count_reported_count < 0
            or item_list_reported_count != item_count_reported_count
            or item_list_reported_count != len(observations)
        ):
            raise ValueError("wishlist list and count must match")
        normalized: list[tuple[WishlistObservation, str]] = []
        seen: set[int] = set()
        for observation in observations:
            if (
                not isinstance(observation.appid, int)
                or isinstance(observation.appid, bool)
                or not 1 <= observation.appid <= (1 << 32) - 1
                or observation.appid in seen
                or not isinstance(observation.priority, int)
                or isinstance(observation.priority, bool)
                or not 0 <= observation.priority <= (1 << 32) - 1
                or not isinstance(observation.date_added, int)
                or isinstance(observation.date_added, bool)
                or not 0 <= observation.date_added <= (1 << 32) - 1
            ):
                raise ValueError("wishlist observations are invalid")
            seen.add(observation.appid)
            normalized.append((observation, _timestamp(observation.observed_at)))

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            run = self._require_wishlist_sync(sync_run_id)
            if run.status != "running":
                raise InvalidSyncTransition("wishlist sync is already terminal")
            consent = self._connection.execute(
                """
                SELECT 1 FROM account_data_consents
                WHERE account_id = ? AND consent_kind = 'wishlist_persistence'
                """,
                (run.account_id,),
            ).fetchone()
            if consent is None:
                raise InvalidSyncTransition("wishlist persistence consent is required")
            self._connection.execute(
                """
                INSERT INTO wishlist_sync_metadata(
                    sync_run_id, account_id, provider, support_level,
                    item_list_retrieved_at, item_list_reported_count,
                    item_count_retrieved_at, item_count_reported_count,
                    validation_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sequential_count_match')
                """,
                (
                    sync_run_id,
                    run.account_id,
                    run.provider,
                    support_level,
                    list_at,
                    item_list_reported_count,
                    count_at,
                    item_count_reported_count,
                ),
            )
            for observation, observed_at in normalized:
                self._ensure_steam_application_identity(
                    observation.appid, observed_at=observed_at
                )
                evidence_id = self._insert_evidence(
                    EvidenceInput(
                        provider=run.provider,
                        capability=run.capability,
                        source_kind="steam_web_api",
                        source_locator=f"GetWishlist:app:{observation.appid}",
                        retrieved_at=list_at,
                        support_level=support_level,
                        context={
                            "account_id": run.account_id,
                            "validation_method": "sequential_count_match",
                            "item_list_reported_count": item_list_reported_count,
                            "item_count_reported_count": item_count_reported_count,
                        },
                        payload={
                            "appid": observation.appid,
                            "priority": observation.priority,
                            "date_added": observation.date_added,
                        },
                        account_id=run.account_id,
                    )
                )
                self._connection.execute(
                    """
                    INSERT INTO wishlist_observations(
                        sync_run_id, evidence_id, account_id, appid,
                        priority, date_added, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sync_run_id,
                        evidence_id,
                        run.account_id,
                        observation.appid,
                        observation.priority,
                        observation.date_added,
                        observed_at,
                    ),
                )
            newer = self._connection.execute(
                """
                SELECT 1 FROM sync_runs
                WHERE capability = 'wishlist.read' AND account_id = ?
                  AND id > ? AND status = 'complete' LIMIT 1
                """,
                (run.account_id, sync_run_id),
            ).fetchone()
            promoted = 0
            if newer is None:
                self._connection.execute(
                    "DELETE FROM wishlist_current WHERE account_id = ?",
                    (run.account_id,),
                )
                self._connection.execute(
                    """
                    INSERT INTO wishlist_current(
                        account_id, appid, evidence_id, promoted_sync_run_id,
                        priority, date_added, observed_at
                    )
                    SELECT account_id, appid, evidence_id, sync_run_id,
                           priority, date_added, observed_at
                    FROM wishlist_observations WHERE sync_run_id = ?
                    """,
                    (sync_run_id,),
                )
                promoted = 1
            self._connection.execute(
                """
                UPDATE sync_runs SET status = 'complete', completed_at = ?,
                    promoted = ?, records_seen = ?, error_code = NULL,
                    error_detail = NULL WHERE id = ?
                """,
                (completed, promoted, len(normalized), sync_run_id),
            )
            self._prune_wishlist_payloads(
                run.account_id, sync_run_id, keep_selected=bool(promoted)
            )
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise
        return self.get_sync_run(sync_run_id)

    def finish_wishlist_sync(
        self,
        sync_run_id: int,
        *,
        completed_at: str | datetime,
        error_code: str,
    ) -> SyncRun:
        completed = _timestamp(completed_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            run = self._require_wishlist_sync(sync_run_id)
            if run.status != "running":
                raise InvalidSyncTransition("wishlist sync is already terminal")
            self._connection.execute(
                """
                UPDATE sync_runs SET status = 'failed', completed_at = ?,
                    promoted = 0, error_code = ?, error_detail = NULL WHERE id = ?
                """,
                (completed, error_code, sync_run_id),
            )
            self._prune_wishlist_payloads(
                run.account_id, sync_run_id, keep_selected=False
            )
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise
        return self.get_sync_run(sync_run_id)

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

    def complete_catalog_snapshot(
        self,
        sync_run_id: int,
        demanded_appids: list[int] | tuple[int, ...],
        observations: list[CatalogObservation] | tuple[CatalogObservation, ...],
        *,
        games: CatalogStreamInput,
        non_games: CatalogStreamInput,
        completed_at: str | datetime,
        support_level: str = "official_documented",
    ) -> SyncRun:
        """Atomically promote demanded application facts from two complete scans."""

        completed = _timestamp(completed_at)
        demanded = _catalog_appids(demanded_appids)
        normalized = _catalog_observations(observations, demanded=demanded)
        streams = _catalog_streams(games, non_games)
        for stream, _ in streams:
            if demanded and stream.termination == "no_demand":
                raise ValueError(
                    "nonempty catalog demand cannot use no-demand provenance"
                )
            if (
                demanded
                and stream.termination == "demand_boundary"
                and stream.scanned_through_appid < demanded[-1]
            ):
                raise ValueError("catalog demand boundary does not cover demand")
        if not support_level or len(support_level) > 128:
            raise ValueError("support_level must be between 1 and 128 characters")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            run = self._require_running_catalog_sync(sync_run_id)
            if self._catalog_demand_for_run(sync_run_id) != demanded:
                raise InvalidSyncTransition(
                    "catalog completion demand differs from the recorded attempt"
                )
            subject = self._connection.execute(
                "SELECT account_id, machine_id FROM catalog_sync_subjects "
                "WHERE sync_run_id = ?",
                (sync_run_id,),
            ).fetchone()
            if subject is None:
                raise InvalidSyncTransition("catalog sync subject is not recorded")
            self._connection.execute(
                """
                INSERT INTO catalog_sync_metadata(
                    sync_run_id, provider, support_level, demanded_count
                ) VALUES (?, ?, ?, ?)
                """,
                (sync_run_id, run.provider, support_level, len(demanded)),
            )
            for stream, pages in streams:
                self._connection.execute(
                    """
                    INSERT INTO catalog_stream_provenance(
                        sync_run_id, stream, termination, scanned_through_appid,
                        filter_context_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        sync_run_id,
                        stream.stream,
                        stream.termination,
                        stream.scanned_through_appid,
                        _canonical_json(stream.filter_context),
                    ),
                )
                for page, retrieved_at in pages:
                    self._connection.execute(
                        """
                        INSERT INTO catalog_page_provenance(
                            sync_run_id, stream, page_number,
                            requested_last_appid, first_appid, last_appid,
                            item_count, have_more_results, retrieved_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sync_run_id,
                            stream.stream,
                            page.page_number,
                            page.requested_last_appid,
                            page.first_appid,
                            page.last_appid,
                            page.item_count,
                            int(page.have_more_results),
                            retrieved_at,
                        ),
                    )

            for observation in normalized:
                self._ensure_steam_application_identity(
                    observation.appid, observed_at=completed
                )
                evidence_id = self._insert_evidence(
                    EvidenceInput(
                        provider=run.provider,
                        capability=run.capability,
                        source_kind="steam_web_api",
                        source_locator=f"GetAppList:app:{observation.appid}",
                        retrieved_at=completed,
                        support_level=support_level,
                        context={
                            "demanded": True,
                            "games_filter": games.filter_context,
                            "non_games_filter": non_games.filter_context,
                        },
                        payload={
                            "appid": observation.appid,
                            "classification": observation.classification,
                            "last_modified": observation.last_modified,
                            "price_change_number": observation.price_change_number,
                        },
                    )
                )
                self._connection.execute(
                    """
                    INSERT INTO catalog_observations(
                        sync_run_id, evidence_id, appid, classification,
                        last_modified, price_change_number, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sync_run_id,
                        evidence_id,
                        observation.appid,
                        observation.classification,
                        observation.last_modified,
                        observation.price_change_number,
                        completed,
                    ),
                )

            promotable = tuple(
                observation.appid
                for observation in normalized
                if not self._connection.execute(
                    """
                    SELECT 1 FROM catalog_current
                    WHERE appid = ? AND promoted_sync_run_id > ?
                    """,
                    (observation.appid, sync_run_id),
                ).fetchone()
            )
            if promotable:
                placeholders = ",".join("?" for _ in promotable)
                self._connection.execute(
                    f"DELETE FROM catalog_current WHERE appid IN ({placeholders})",
                    promotable,
                )
                self._connection.execute(
                    f"""
                    INSERT INTO catalog_current(
                        appid, evidence_id, promoted_sync_run_id, classification,
                        last_modified, price_change_number, observed_at
                    )
                    SELECT
                        appid, evidence_id, sync_run_id, classification,
                        last_modified, price_change_number, observed_at
                    FROM catalog_observations
                    WHERE sync_run_id = ? AND appid IN ({placeholders})
                    """,
                    (sync_run_id, *promotable),
                )
            subject_promotable = tuple(
                observation.appid
                for observation in normalized
                if not self._connection.execute(
                    """
                    SELECT 1 FROM catalog_subject_current
                    WHERE account_id = ? AND machine_id = ? AND appid = ?
                      AND promoted_sync_run_id > ?
                    """,
                    (
                        subject["account_id"],
                        subject["machine_id"],
                        observation.appid,
                        sync_run_id,
                    ),
                ).fetchone()
            )
            if subject_promotable:
                placeholders = ",".join("?" for _ in subject_promotable)
                self._connection.execute(
                    f"""
                    DELETE FROM catalog_subject_current
                    WHERE account_id = ? AND machine_id = ?
                      AND appid IN ({placeholders})
                    """,
                    (
                        subject["account_id"],
                        subject["machine_id"],
                        *subject_promotable,
                    ),
                )
                self._connection.execute(
                    f"""
                    INSERT INTO catalog_subject_current(
                        account_id, machine_id, appid, evidence_id,
                        promoted_sync_run_id, classification, last_modified,
                        price_change_number, observed_at
                    )
                    SELECT ?, ?, appid, evidence_id, sync_run_id,
                           classification, last_modified, price_change_number,
                           observed_at
                    FROM catalog_observations
                    WHERE sync_run_id = ? AND appid IN ({placeholders})
                    """,
                    (
                        subject["account_id"],
                        subject["machine_id"],
                        sync_run_id,
                        *subject_promotable,
                    ),
                )
            self._connection.execute(
                """
                UPDATE sync_runs
                SET status = 'complete', completed_at = ?, promoted = ?,
                    records_seen = ?, error_code = NULL, error_detail = NULL
                WHERE id = ?
                """,
                (
                    completed,
                    int(bool(promotable) or bool(subject_promotable)),
                    len(normalized),
                    sync_run_id,
                ),
            )
            self._prune_catalog_payloads()
            self._connection.commit()
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise
        return self.get_sync_run(sync_run_id)

    def finish_catalog_sync(
        self,
        sync_run_id: int,
        *,
        status: Literal["partial", "failed"],
        completed_at: str | datetime,
        error_code: str,
    ) -> SyncRun:
        if status not in ("partial", "failed") or not error_code:
            raise ValueError("catalog failure requires partial/failed status and code")
        completed = _timestamp(completed_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._require_running_catalog_sync(sync_run_id)
            if self._catalog_demand_for_run(sync_run_id) is None:
                raise InvalidSyncTransition("catalog sync demand is not recorded")
            self._connection.execute(
                """
                UPDATE sync_runs
                SET status = ?, completed_at = ?, promoted = 0,
                    error_code = ?, error_detail = NULL
                WHERE id = ?
                """,
                (status, completed, error_code, sync_run_id),
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

    def begin_price_sync(
        self,
        *,
        provider: str,
        account_id: int,
        country: str,
        wishlist_sync_run_id: int,
        demand: tuple[PriceDemandSubject, ...],
        requested_limit: int | None,
        started_at: str | datetime,
    ) -> SyncRun:
        if provider not in {"gg-deals", "cheapshark"}:
            raise ValueError("unsupported price provider")
        if len(country) != 2 or not country.isascii() or not country.isalpha():
            raise ValueError("country must be a two-letter code")
        country = country.upper()
        if requested_limit is not None and (
            not isinstance(requested_limit, int)
            or isinstance(requested_limit, bool)
            or not 1 <= requested_limit <= 10_000
        ):
            raise ValueError("requested_limit must be positive")
        ordered = sorted(demand, key=lambda item: item.demand_order)
        if any(
            not isinstance(item.appid, int)
            or isinstance(item.appid, bool)
            or not 1 <= item.appid <= (1 << 32) - 1
            or not isinstance(item.demand_order, int)
            or isinstance(item.demand_order, bool)
            or not isinstance(item.wishlist_priority, int)
            or isinstance(item.wishlist_priority, bool)
            or not 0 <= item.wishlist_priority <= (1 << 32) - 1
            or not isinstance(item.wishlist_date_added, int)
            or isinstance(item.wishlist_date_added, bool)
            or not 0 <= item.wishlist_date_added <= (1 << 32) - 1
            for item in ordered
        ):
            raise ValueError("price demand subject is invalid")
        if [item.demand_order for item in ordered] != list(range(len(ordered))):
            raise ValueError("price demand order must be contiguous")
        if len({item.appid for item in ordered}) != len(ordered):
            raise ValueError("price demand AppIDs must be unique")
        timestamp = _timestamp(started_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            wishlist = self.get_sync_run(wishlist_sync_run_id)
            if (
                wishlist.capability != "wishlist.read"
                or wishlist.account_id != account_id
                or wishlist.status != "complete"
                or not wishlist.promoted
            ):
                raise ValueError("price demand requires a promoted wishlist snapshot")
            cursor = self._connection.execute(
                """
                INSERT INTO sync_runs(
                    provider, capability, account_id, started_at, status
                ) VALUES (?, 'prices.wishlist.read', ?, ?, 'running')
                """,
                (provider, account_id, timestamp),
            )
            run_id = int(cursor.lastrowid)
            self._connection.execute(
                """
                INSERT INTO price_sync_metadata(
                    sync_run_id, account_id, country, provider, scope,
                    wishlist_sync_run_id, demand_count, requested_limit
                ) VALUES (?, ?, ?, ?, 'wishlist', ?, ?, ?)
                """,
                (
                    run_id,
                    account_id,
                    country,
                    provider,
                    wishlist_sync_run_id,
                    len(ordered),
                    requested_limit,
                ),
            )
            for subject in ordered:
                self._ensure_steam_application_identity(
                    subject.appid, observed_at=timestamp
                )
                self._connection.execute(
                    """
                    INSERT INTO price_sync_demand(
                        sync_run_id, account_id, country, appid, demand_order,
                        wishlist_priority, wishlist_date_added
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        account_id,
                        country,
                        subject.appid,
                        subject.demand_order,
                        subject.wishlist_priority,
                        subject.wishlist_date_added,
                    ),
                )
            self._connection.commit()
            return self.get_sync_run(run_id)
        except BaseException:
            self._rollback_transaction()
            raise

    def complete_price_sync(
        self,
        sync_run_id: int,
        *,
        outcomes: Mapping[int, Literal["observed", "not_found"]],
        facts: tuple[PriceFactObservation, ...],
        completed_at: str | datetime,
        status: Literal["complete", "partial"],
        rate_limit: int | None = None,
        rate_remaining: int | None = None,
        rate_reset_value: int | None = None,
        error_code: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> SyncRun:
        completed = _timestamp(completed_at)
        if status not in {"complete", "partial"}:
            raise ValueError("price sync status must be complete or partial")
        if error_code is not None and (
            not isinstance(error_code, str)
            or not 1 <= len(error_code) <= 128
            or any(ord(character) < 32 for character in error_code)
        ):
            raise ValueError("price sync error code is invalid")
        if retry_after_seconds is not None and (
            not isinstance(retry_after_seconds, int)
            or isinstance(retry_after_seconds, bool)
            or not 0 <= retry_after_seconds <= 86_400
        ):
            raise ValueError("price retry delay is invalid")
        rate_values = (rate_limit, rate_remaining, rate_reset_value)
        if any(
            value is not None
            and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= (1 << 63) - 1
            )
            for value in rate_values
        ) or (
            rate_limit is not None
            and rate_remaining is not None
            and rate_remaining > rate_limit
        ):
            raise ValueError("price rate metadata is invalid")
        if any(
            not isinstance(appid, int)
            or isinstance(appid, bool)
            or not 1 <= appid <= (1 << 32) - 1
            or outcome not in {"observed", "not_found"}
            for appid, outcome in outcomes.items()
        ):
            raise ValueError("price outcomes are invalid")
        by_appid: dict[int, list[PriceFactObservation]] = {}
        for fact in facts:
            by_appid.setdefault(fact.appid, []).append(fact)
        if set(by_appid) - set(outcomes):
            raise ValueError("price facts require an evaluated outcome")
        if any(outcomes[appid] != "observed" for appid in by_appid):
            raise ValueError("price facts require an observed outcome")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.get_sync_run(sync_run_id)
            if run.capability != "prices.wishlist.read" or run.status != "running":
                raise InvalidSyncTransition("price sync is not running")
            metadata = self._connection.execute(
                "SELECT * FROM price_sync_metadata WHERE sync_run_id = ?",
                (sync_run_id,),
            ).fetchone()
            if metadata is None:
                raise InvalidSyncTransition("price sync metadata is missing")
            demanded = {
                int(row[0])
                for row in self._connection.execute(
                    "SELECT appid FROM price_sync_demand WHERE sync_run_id = ?",
                    (sync_run_id,),
                )
            }
            if set(outcomes) - demanded:
                raise ValueError("evaluated price subject was not demanded")
            if status == "complete" and (
                set(outcomes) != demanded or error_code is not None
            ):
                raise ValueError("complete price sync must evaluate all demand")
            inserted: dict[int, list[int]] = {}
            for appid in sorted(by_appid):
                seen_keys: set[tuple[str, int]] = set()
                for fact in by_appid[appid]:
                    key = (fact.fact_kind, fact.ordinal)
                    if key in seen_keys:
                        raise ValueError("duplicate price fact ordinal")
                    seen_keys.add(key)
                    observed, effective = _validate_price_fact(
                        fact, provider=run.provider, country=metadata["country"]
                    )
                    observed_dt = datetime.fromisoformat(observed.replace("Z", "+00:00"))
                    started_dt = datetime.fromisoformat(
                        run.started_at.replace("Z", "+00:00")
                    )
                    completed_dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                    if not started_dt <= observed_dt <= completed_dt:
                        raise ValueError("price observation is outside its sync run")
                    fresh_seconds = 6 * 60 * 60 if fact.fact_kind == "offer" else 24 * 60 * 60
                    fresh_until = _timestamp(
                        observed_dt + timedelta(seconds=fresh_seconds)
                    )
                    hard_expires = _timestamp(observed_dt + timedelta(days=7))
                    evidence_id = self._insert_evidence(
                        EvidenceInput(
                            provider=run.provider,
                            capability=run.capability,
                            source_kind="third_party_api",
                            source_locator=(
                                f"{run.provider}:app:{appid}:{fact.fact_kind}:{fact.ordinal}"
                            ),
                            retrieved_at=observed,
                            effective_at=effective,
                            support_level="contractual_third_party",
                            account_id=run.account_id,
                            context={
                                "country": metadata["country"],
                                "currency": fact.currency,
                                "comparability": fact.comparability,
                            },
                            payload={
                                "appid": appid,
                                "fact_kind": fact.fact_kind,
                                "amount_minor": fact.amount_minor,
                                "currency": fact.currency,
                                "store_class": fact.store_class,
                                "provider_product_id": fact.provider_product_id,
                            },
                        )
                    )
                    cursor = self._connection.execute(
                        """
                        INSERT INTO price_observations(
                            sync_run_id, evidence_id, account_id, country, provider,
                            appid, ordinal, fact_kind, provider_product_id,
                            product_mapping, amount_minor, currency,
                            regular_amount_minor, discount_percent, store_class,
                            comparability, low_scope, effective_at, observed_at,
                            fresh_until, hard_expires_at, provider_url,
                            access_mode, automation_supported
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'exact', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_only', 0)
                        """,
                        (
                            sync_run_id,
                            evidence_id,
                            run.account_id,
                            metadata["country"],
                            run.provider,
                            appid,
                            fact.ordinal,
                            fact.fact_kind,
                            fact.provider_product_id,
                            fact.amount_minor,
                            fact.currency,
                            fact.regular_amount_minor,
                            fact.discount_percent,
                            fact.store_class,
                            fact.comparability,
                            fact.low_scope,
                            effective,
                            observed,
                            fresh_until,
                            hard_expires,
                            fact.provider_url,
                        ),
                    )
                    inserted.setdefault(appid, []).append(int(cursor.lastrowid))
            promoted_any = False
            for appid, outcome in sorted(outcomes.items()):
                row = self._connection.execute(
                    """
                    SELECT promoted_sync_run_id FROM price_subject_current
                    WHERE account_id = ? AND country = ? AND provider = ? AND appid = ?
                    """,
                    (run.account_id, metadata["country"], run.provider, appid),
                ).fetchone()
                if row is not None and int(row[0]) > sync_run_id:
                    continue
                promoted_any = True
                fact_rows = inserted.get(appid, [])
                subject_observed = completed
                if fact_rows:
                    subject_observed = str(
                        self._connection.execute(
                            "SELECT MAX(observed_at) FROM price_observations WHERE id IN ("
                            + ",".join("?" for _ in fact_rows)
                            + ")",
                            fact_rows,
                        ).fetchone()[0]
                    )
                expires = _timestamp(
                    datetime.fromisoformat(subject_observed.replace("Z", "+00:00"))
                    + timedelta(days=7)
                )
                subject_fresh_until = _timestamp(
                    datetime.fromisoformat(subject_observed.replace("Z", "+00:00"))
                    + timedelta(hours=6)
                )
                self._connection.execute(
                    """
                    DELETE FROM price_current
                    WHERE account_id = ? AND country = ? AND provider = ? AND appid = ?
                    """,
                    (run.account_id, metadata["country"], run.provider, appid),
                )
                self._connection.execute(
                    """
                    INSERT INTO price_subject_current(
                        account_id, country, provider, appid, outcome, observed_at,
                        fresh_until, hard_expires_at, promoted_sync_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, country, provider, appid) DO UPDATE SET
                        outcome=excluded.outcome, observed_at=excluded.observed_at,
                        fresh_until=excluded.fresh_until,
                        hard_expires_at=excluded.hard_expires_at,
                        promoted_sync_run_id=excluded.promoted_sync_run_id
                    """,
                    (
                        run.account_id,
                        metadata["country"],
                        run.provider,
                        appid,
                        outcome,
                        subject_observed,
                        subject_fresh_until,
                        expires,
                        sync_run_id,
                    ),
                )
                if fact_rows:
                    placeholders = ",".join("?" for _ in fact_rows)
                    self._connection.execute(
                        f"""
                        INSERT INTO price_current(
                            account_id, country, provider, appid, fact_kind, ordinal,
                            evidence_id, provider_product_id, product_mapping,
                            amount_minor, currency, regular_amount_minor,
                            discount_percent, store_class, comparability, low_scope,
                            effective_at, observed_at, fresh_until, hard_expires_at,
                            provider_url, access_mode, automation_supported,
                            promoted_sync_run_id
                        )
                        SELECT account_id, country, provider, appid, fact_kind, ordinal,
                               evidence_id, provider_product_id, product_mapping,
                               amount_minor, currency, regular_amount_minor,
                               discount_percent, store_class, comparability, low_scope,
                               effective_at, observed_at, fresh_until, hard_expires_at,
                               provider_url, access_mode, automation_supported, ?
                        FROM price_observations WHERE id IN ({placeholders})
                        """,
                        (sync_run_id, *fact_rows),
                    )
            for appid, outcome in outcomes.items():
                self._connection.execute(
                    "UPDATE price_sync_demand SET evaluated=1, outcome=? WHERE sync_run_id=? AND appid=?",
                    (outcome, sync_run_id, appid),
                )
            self._connection.execute(
                """
                UPDATE price_sync_metadata SET evaluated_count = ?, rate_limit = ?,
                    rate_remaining = ?, rate_reset_value = ?, retry_after_seconds = ?
                WHERE sync_run_id = ?
                """,
                (
                    len(outcomes), rate_limit, rate_remaining, rate_reset_value,
                    retry_after_seconds, sync_run_id,
                ),
            )
            self._connection.execute(
                """
                UPDATE sync_runs SET completed_at=?, status=?, promoted=?,
                    records_seen=?, error_code=?, error_detail=NULL WHERE id=?
                """,
                (
                    completed, status, int(promoted_any), len(facts), error_code,
                    sync_run_id,
                ),
            )
            self._expire_price_data(completed)
            self._connection.commit()
            return self.get_sync_run(sync_run_id)
        except BaseException:
            self._rollback_transaction()
            raise

    def finish_price_sync(
        self, sync_run_id: int, *, completed_at: str | datetime, error_code: str
    ) -> SyncRun:
        completed = _timestamp(completed_at)
        with self._connection:
            run = self.get_sync_run(sync_run_id)
            if run.capability != "prices.wishlist.read" or run.status != "running":
                raise InvalidSyncTransition("price sync is not running")
            self._connection.execute(
                """
                UPDATE sync_runs SET completed_at=?, status='failed', promoted=0,
                    records_seen=0, error_code=?, error_detail=NULL WHERE id=?
                """,
                (completed, error_code, sync_run_id),
            )
        return self.get_sync_run(sync_run_id)

    def read_price_snapshot(
        self, *, account_id: int, country: str, provider: str | None = None,
        now: str | datetime | None = None,
    ) -> PriceSnapshot:
        country = country.upper()
        evaluated_now = _timestamp(now or datetime.now(timezone.utc))
        if now is not None:
            with self._connection:
                self._expire_price_data(evaluated_now)
        clauses = ["account_id = ?", "country = ?"]
        parameters: list[object] = [account_id, country]
        if provider is not None:
            clauses.append("provider = ?")
            parameters.append(provider)
        where = " AND ".join(clauses)
        facts = tuple(
            StoredPriceFact(**dict(row))
            for row in self._connection.execute(
                f"SELECT * FROM price_current WHERE {where} ORDER BY appid, provider, fact_kind, ordinal",
                parameters,
            )
        )
        subjects = tuple(
            StoredPriceSubject(**dict(row))
            for row in self._connection.execute(
                f"SELECT * FROM price_subject_current WHERE {where} ORDER BY appid, provider",
                parameters,
            )
        )
        attempts = tuple(
            _sync_run(row)
            for row in self._connection.execute(
                """
                SELECT runs.* FROM sync_runs AS runs
                JOIN price_sync_metadata AS metadata ON metadata.sync_run_id = runs.id
                WHERE metadata.account_id = ? AND metadata.country = ?
                  AND (? IS NULL OR metadata.provider = ?)
                ORDER BY runs.id
                """,
                (account_id, country, provider, provider),
            )
        )
        running_attempts = tuple(run for run in attempts if run.status == "running")
        now_dt = datetime.fromisoformat(evaluated_now.replace("Z", "+00:00"))
        return PriceSnapshot(
            facts=facts,
            subjects=subjects,
            attempts=attempts,
            stale_offer_count=sum(
                fact.fact_kind == "offer" and fact.fresh_until <= evaluated_now
                for fact in facts
            ),
            stale_historical_low_count=sum(
                fact.fact_kind == "historical_low"
                and fact.fresh_until <= evaluated_now
                for fact in facts
            ),
            stale_subject_count=sum(
                subject.fresh_until <= evaluated_now for subject in subjects
            ),
            running=bool(running_attempts),
            abandoned_running=any(
                (now_dt - datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))).total_seconds()
                > 15 * 60
                for run in running_attempts
            ),
        )

    def delete_price_data(
        self, *, provider: str, account_id: int | None = None,
        credential_kind: str | None = None,
        credential_profile_id: str | None = None,
    ) -> PriceDataDeletion:
        if provider not in {"gg-deals", "cheapshark"}:
            raise ValueError("unsupported price provider")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            clause = "provider = ?" + (" AND account_id = ?" if account_id is not None else "")
            params: tuple[object, ...] = (provider,) if account_id is None else (provider, account_id)
            observations = int(self._connection.execute(
                f"SELECT COUNT(*) FROM price_observations WHERE {clause}", params
            ).fetchone()[0])
            current = int(self._connection.execute(
                f"SELECT COUNT(*) FROM price_current WHERE {clause}", params
            ).fetchone()[0])
            subjects = int(self._connection.execute(
                f"SELECT COUNT(*) FROM price_subject_current WHERE {clause}", params
            ).fetchone()[0])
            evidence_ids = tuple(int(row[0]) for row in self._connection.execute(
                f"SELECT evidence_id FROM price_observations WHERE {clause}", params
            ))
            run_rows = tuple(int(row[0]) for row in self._connection.execute(
                "SELECT sync_run_id FROM price_sync_metadata WHERE provider = ?"
                + (" AND account_id = ?" if account_id is not None else ""), params
            ))
            if run_rows:
                placeholders = ",".join("?" for _ in run_rows)
                self._connection.execute(
                    f"DELETE FROM sync_runs WHERE id IN ({placeholders})", run_rows
                )
            evidence_removed = self._delete_orphan_owned_evidence(evidence_ids)
            credential_refs_removed = 0
            if credential_kind is not None or credential_profile_id is not None:
                if account_id is not None or not credential_kind or not credential_profile_id:
                    raise ValueError("credential deletion requires provider-all scope")
                credential_refs_removed = self._remove_credential_identity(
                    provider, credential_kind, credential_profile_id
                )
            self._connection.commit()
            return PriceDataDeletion(
                provider, observations, current, subjects, len(run_rows),
                evidence_removed, credential_refs_removed
            )
        except BaseException:
            self._rollback_transaction()
            raise

    def _expire_price_data(self, now: str) -> None:
        evidence_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT evidence_id FROM price_observations WHERE hard_expires_at <= ?",
                (now,),
            )
        )
        self._connection.execute(
            "DELETE FROM price_current WHERE hard_expires_at <= ?", (now,)
        )
        self._connection.execute(
            "DELETE FROM price_subject_current WHERE hard_expires_at <= ?", (now,)
        )
        self._connection.execute(
            "DELETE FROM price_observations WHERE hard_expires_at <= ?", (now,)
        )
        self._delete_orphan_owned_evidence(evidence_ids)

    def read_wishlist_snapshot(self, account_id: int) -> WishlistSnapshot:
        if self._connection.in_transaction:
            raise StorageError("cannot start a read snapshot inside a transaction")
        self._connection.execute("BEGIN")
        try:
            rows = self._connection.execute(
                """
                SELECT account_id, appid, priority, date_added, observed_at,
                       evidence_id, promoted_sync_run_id
                FROM wishlist_current WHERE account_id = ? ORDER BY appid
                """,
                (account_id,),
            )
            games = tuple(WishlistGame(**dict(row)) for row in rows)
            latest = self.latest_account_sync(
                capability="wishlist.read", account_id=account_id
            )
            latest_complete = self.latest_account_sync(
                capability="wishlist.read", account_id=account_id, status="complete"
            )
            provenance = None
            if latest_complete is not None:
                row = self._connection.execute(
                    """
                    SELECT sync_run_id, provider, support_level,
                           item_list_retrieved_at, item_list_reported_count,
                           item_count_retrieved_at, item_count_reported_count,
                           validation_method
                    FROM wishlist_sync_metadata WHERE sync_run_id = ?
                    """,
                    (latest_complete.id,),
                ).fetchone()
                if row is not None:
                    provenance = WishlistSnapshotProvenance(**dict(row))
            snapshot = WishlistSnapshot(
                games=games,
                latest=latest,
                latest_complete=latest_complete,
                latest_complete_provenance=provenance,
                stable_game_ids_by_appid=self._stable_game_ids_for_appids(
                    {game.appid for game in games}
                ),
            )
            self._connection.commit()
            return snapshot
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise

    def read_catalog_snapshot(
        self,
        appids: list[int] | tuple[int, ...],
        *,
        account_id: int | None = None,
        machine_id: str | None = None,
    ) -> CatalogSnapshot:
        demanded = set(_catalog_appids(appids))
        if self._connection.in_transaction:
            raise StorageError("cannot start a read snapshot inside a transaction")
        self._connection.execute("BEGIN")
        try:
            snapshot = self._read_catalog_snapshot(
                demanded, account_id=account_id, machine_id=machine_id
            )
            self._connection.commit()
            return snapshot
        except BaseException:
            try:
                self._rollback_or_reopen()
            except BaseException:
                pass
            raise

    def read_catalog_demand(self, account_id: int, machine_id: str) -> tuple[int, ...]:
        """Read only owned/installed demand, without interpreting catalog state."""

        if self._connection.in_transaction:
            raise StorageError("cannot start a read snapshot inside a transaction")
        self._connection.execute("BEGIN")
        try:
            if (
                self._connection.execute(
                    "SELECT 1 FROM accounts WHERE id = ? AND provider = 'steam'",
                    (account_id,),
                ).fetchone()
                is None
            ):
                raise ValueError("catalog account is not configured")
            rows = self._connection.execute(
                """
                SELECT appid FROM owned_current WHERE account_id = ?
                UNION
                SELECT appid FROM installed_current WHERE machine_id = ?
                ORDER BY appid
                """,
                (account_id, machine_id),
            )
            demanded = tuple(int(row[0]) for row in rows)
            self._connection.commit()
            return demanded
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
            catalog = self._read_catalog_snapshot(
                {appid for appid, _ in stable_game_ids_by_appid},
                account_id=account_id,
                machine_id=machine_id,
            )
            self._connection.commit()
            return LibrarySnapshot(
                owned=owned,
                installed=installed,
                stable_game_ids_by_appid=stable_game_ids_by_appid,
                catalog=catalog,
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
                "wishlist_observations": self._count_where(
                    "wishlist_observations", "account_id", account_id
                ),
                "wishlist_current": self._count_where(
                    "wishlist_current", "account_id", account_id
                ),
                "price_observations": self._count_where(
                    "price_observations", "account_id", account_id
                ),
                "price_current": self._count_where(
                    "price_current", "account_id", account_id
                ),
                "price_subjects": self._count_where(
                    "price_subject_current", "account_id", account_id
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
                    """
                    SELECT evidence_id FROM owned_observations WHERE account_id = ?
                    UNION
                    SELECT evidence_id FROM wishlist_observations WHERE account_id = ?
                    UNION
                    SELECT evidence_id FROM price_observations WHERE account_id = ?
                    """,
                    (account_id, account_id, account_id),
                )
            )
            appids = tuple(
                int(row[0])
                for row in self._connection.execute(
                    """
                    SELECT appid FROM owned_observations WHERE account_id = ?
                    UNION
                    SELECT appid FROM wishlist_observations WHERE account_id = ?
                    UNION
                    SELECT appid FROM price_sync_demand WHERE account_id = ?
                    """,
                    (account_id, account_id, account_id),
                )
            )
            (
                catalog_runs_removed,
                catalog_evidence_removed,
                catalog_appids,
            ) = self._delete_account_catalog_scope(account_id)
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
            evidence_removed += catalog_evidence_removed
            orphan_apps_removed = self._delete_orphan_apps(
                tuple(sorted({*appids, *catalog_appids}))
            )
            self._connection.commit()
            return AccountDataDeletion(
                account_removed=cursor.rowcount > 0,
                owned_observations_removed=counts["owned_observations"],
                owned_current_removed=counts["owned_current"],
                sync_runs_removed=counts["sync_runs"] + catalog_runs_removed,
                probes_removed=counts["probes"],
                consents_removed=counts["consents"],
                evidence_removed=evidence_removed,
                orphan_apps_removed=orphan_apps_removed,
                wishlist_observations_removed=counts["wishlist_observations"],
                wishlist_current_removed=counts["wishlist_current"],
                price_observations_removed=counts["price_observations"],
                price_current_removed=counts["price_current"],
                price_subjects_removed=counts["price_subjects"],
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
            account_ids = tuple(account_id for account_id, _ in accounts)
            aliases = tuple(alias for _, alias in accounts)
            counts = {
                "owned_observations": 0,
                "owned_current": 0,
                "wishlist_observations": 0,
                "wishlist_current": 0,
                "price_observations": 0,
                "price_current": 0,
                "price_subjects": 0,
                "sync_runs": 0,
                "consents": 0,
                "probes": 0,
            }
            evidence_ids: tuple[int, ...] = ()
            account_appids: tuple[int, ...] = ()
            if account_ids:
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
                    "wishlist_observations": int(
                        self._connection.execute(
                            f"SELECT COUNT(*) FROM wishlist_observations "
                            f"WHERE account_id IN ({id_placeholders})",
                            account_ids,
                        ).fetchone()[0]
                    ),
                    "wishlist_current": int(
                        self._connection.execute(
                            f"SELECT COUNT(*) FROM wishlist_current "
                            f"WHERE account_id IN ({id_placeholders})",
                            account_ids,
                        ).fetchone()[0]
                    ),
                    "price_observations": int(
                        self._connection.execute(
                            f"SELECT COUNT(*) FROM price_observations "
                            f"WHERE account_id IN ({id_placeholders})",
                            account_ids,
                        ).fetchone()[0]
                    ),
                    "price_current": int(
                        self._connection.execute(
                            f"SELECT COUNT(*) FROM price_current "
                            f"WHERE account_id IN ({id_placeholders})",
                            account_ids,
                        ).fetchone()[0]
                    ),
                    "price_subjects": int(
                        self._connection.execute(
                            f"SELECT COUNT(*) FROM price_subject_current "
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
                            f"WHERE account_alias COLLATE NOCASE IN "
                            f"({alias_placeholders})",
                            aliases,
                        ).fetchone()[0]
                    ),
                }
                evidence_ids = tuple(
                    int(row[0])
                    for row in self._connection.execute(
                        f"""
                        SELECT evidence_id FROM owned_observations
                        WHERE account_id IN ({id_placeholders})
                        UNION
                        SELECT evidence_id FROM wishlist_observations
                        WHERE account_id IN ({id_placeholders})
                        UNION
                        SELECT evidence_id FROM price_observations
                        WHERE account_id IN ({id_placeholders})
                        """,
                        (*account_ids, *account_ids, *account_ids),
                    )
                )
                account_appids = tuple(
                    int(row[0])
                    for row in self._connection.execute(
                        f"""
                        SELECT appid FROM owned_observations
                        WHERE account_id IN ({id_placeholders})
                        UNION
                        SELECT appid FROM wishlist_observations
                        WHERE account_id IN ({id_placeholders})
                        UNION
                        SELECT appid FROM price_sync_demand
                        WHERE account_id IN ({id_placeholders})
                        """,
                        (*account_ids, *account_ids, *account_ids),
                    )
                )

            catalog_counts = {
                "observations": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM catalog_observations"
                    ).fetchone()[0]
                ),
                "current": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM catalog_current"
                    ).fetchone()[0]
                ),
                "sync_runs": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM sync_runs "
                        "WHERE capability = 'catalog.application.read'"
                    ).fetchone()[0]
                ),
                "metadata": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM catalog_sync_metadata"
                    ).fetchone()[0]
                ),
                "streams": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM catalog_stream_provenance"
                    ).fetchone()[0]
                ),
                "pages": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM catalog_page_provenance"
                    ).fetchone()[0]
                ),
            }
            catalog_evidence_ids = tuple(
                int(row[0])
                for row in self._connection.execute(
                    """
                    SELECT evidence_id FROM catalog_observations
                    UNION
                    SELECT evidence_id FROM catalog_current
                    """
                )
            )
            catalog_appids = tuple(
                int(row[0])
                for row in self._connection.execute(
                    """
                    SELECT appid FROM catalog_observations
                    UNION
                    SELECT appid FROM catalog_current
                    """
                )
            )
            account_cursor = self._connection.execute(
                "DELETE FROM accounts WHERE provider = 'steam'"
            )
            catalog_runs_cursor = self._connection.execute(
                "DELETE FROM sync_runs WHERE capability = 'catalog.application.read'"
            )
            catalog_evidence_removed = self._delete_orphan_owned_evidence(
                catalog_evidence_ids
            )
            account_evidence_removed = max(
                len(evidence_ids), self._delete_orphan_owned_evidence(evidence_ids)
            )
            orphan_apps_removed = self._delete_orphan_apps(
                tuple(sorted({*account_appids, *catalog_appids}))
            )
            credential_refs_removed = self._remove_credential_identity(
                credential_provider, credential_kind, credential_profile_id
            )
            self._connection.commit()
            return AllSteamAccountDataDeletion(
                accounts_removed=account_cursor.rowcount,
                owned_observations_removed=counts["owned_observations"],
                owned_current_removed=counts["owned_current"],
                sync_runs_removed=(counts["sync_runs"] + catalog_runs_cursor.rowcount),
                probes_removed=counts["probes"],
                consents_removed=counts["consents"],
                evidence_removed=(account_evidence_removed + catalog_evidence_removed),
                orphan_apps_removed=orphan_apps_removed,
                credential_refs_removed=credential_refs_removed,
                catalog_observations_removed=catalog_counts["observations"],
                catalog_current_removed=catalog_counts["current"],
                catalog_sync_runs_removed=catalog_runs_cursor.rowcount,
                catalog_metadata_removed=catalog_counts["metadata"],
                catalog_streams_removed=catalog_counts["streams"],
                catalog_pages_removed=catalog_counts["pages"],
                catalog_evidence_removed=catalog_evidence_removed,
                shared_credential_preserved=not credential_identity_supplied,
                wishlist_observations_removed=counts["wishlist_observations"],
                wishlist_current_removed=counts["wishlist_current"],
                price_observations_removed=counts["price_observations"],
                price_current_removed=counts["price_current"],
                price_subjects_removed=counts["price_subjects"],
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

    def _read_catalog_snapshot(
        self,
        appids: set[int],
        *,
        account_id: int | None = None,
        machine_id: str | None = None,
    ) -> CatalogSnapshot:
        if (account_id is None) != (machine_id is None):
            raise ValueError("catalog attempt scope must be complete or omitted")
        if account_id is None:
            latest_row = self._connection.execute(
                """
                SELECT * FROM sync_runs
                WHERE capability = 'catalog.application.read'
                  AND machine_id IS NULL AND account_id IS NULL
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            latest = None if latest_row is None else _sync_run(latest_row)
            attempts = (
                ()
                if latest is None or not appids
                else (CatalogRelevantAttempt(latest, tuple(sorted(appids))),)
            )
        else:
            assert machine_id is not None
            attempts = self._latest_catalog_attempts(
                account_id=account_id,
                machine_id=machine_id,
                demanded=appids,
            )
            latest = (
                attempts[0].run
                if len(attempts) == 1 and set(attempts[0].appids) == appids
                else None
            )
        if not appids:
            return CatalogSnapshot(facts=(), sources=(), latest=None, attempts=())
        parameters = tuple(sorted(appids))
        placeholders = ",".join("?" for _ in parameters)
        if account_id is None:
            current_table = "catalog_current"
            subject_clause = ""
            fact_parameters: tuple[object, ...] = parameters
        else:
            assert machine_id is not None
            current_table = "catalog_subject_current"
            subject_clause = "AND current.account_id = ? AND current.machine_id = ?"
            fact_parameters = (*parameters, account_id, machine_id)
        fact_rows = tuple(
            self._connection.execute(
                f"""
                SELECT
                    current.appid, entities.stable_id AS stable_game_id,
                    current.classification, current.last_modified,
                    current.price_change_number, current.observed_at,
                    current.evidence_id, current.promoted_sync_run_id
                FROM {current_table} AS current
                JOIN external_game_identities AS identities
                  ON identities.provider = 'steam'
                 AND identities.identity_kind = 'application_appid'
                 AND identities.external_id = CAST(current.appid AS TEXT)
                JOIN game_entities AS entities
                  ON entities.id = identities.game_entity_id
                WHERE current.appid IN ({placeholders})
                  {subject_clause}
                ORDER BY current.appid
                """,
                fact_parameters,
            )
        )
        facts = tuple(CatalogFact(**dict(row)) for row in fact_rows)
        run_ids = tuple(sorted({fact.promoted_sync_run_id for fact in facts}))
        sources: list[CatalogSourceProvenance] = []
        for run_id in run_ids:
            metadata = self._connection.execute(
                """
                SELECT provider, support_level FROM catalog_sync_metadata
                WHERE sync_run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if metadata is None:
                raise StorageError("catalog source provenance is missing")
            stream_values: list[CatalogStreamProvenance] = []
            for stream_row in self._connection.execute(
                """
                SELECT stream, termination, scanned_through_appid,
                       filter_context_json
                FROM catalog_stream_provenance
                WHERE sync_run_id = ? ORDER BY stream
                """,
                (run_id,),
            ):
                page_values = tuple(
                    CatalogPageInput(
                        page_number=int(page["page_number"]),
                        requested_last_appid=int(page["requested_last_appid"]),
                        first_appid=page["first_appid"],
                        last_appid=int(page["last_appid"]),
                        item_count=int(page["item_count"]),
                        have_more_results=bool(page["have_more_results"]),
                        retrieved_at=str(page["retrieved_at"]),
                    )
                    for page in self._connection.execute(
                        """
                        SELECT page_number, requested_last_appid, first_appid,
                               last_appid, item_count, have_more_results,
                               retrieved_at
                        FROM catalog_page_provenance
                        WHERE sync_run_id = ? AND stream = ?
                        ORDER BY page_number
                        """,
                        (run_id, stream_row["stream"]),
                    )
                )
                stream_values.append(
                    CatalogStreamProvenance(
                        stream=stream_row["stream"],
                        termination=stream_row["termination"],
                        scanned_through_appid=int(stream_row["scanned_through_appid"]),
                        filter_context=json.loads(stream_row["filter_context_json"]),
                        pages=page_values,
                    )
                )
            if {stream.stream for stream in stream_values} != {"games", "non_games"}:
                raise StorageError("catalog stream provenance is incomplete")
            sources.append(
                CatalogSourceProvenance(
                    sync_run_id=run_id,
                    provider=metadata["provider"],
                    support_level=metadata["support_level"],
                    streams=tuple(stream_values),
                )
            )
        return CatalogSnapshot(
            facts=facts,
            sources=tuple(sources),
            latest=latest,
            attempts=attempts,
        )

    def _latest_catalog_attempts(
        self, *, account_id: int, machine_id: str, demanded: set[int]
    ) -> tuple[CatalogRelevantAttempt, ...]:
        if not demanded:
            return ()
        by_run: dict[int, tuple[SyncRun, list[int]]] = {}
        for appid in sorted(demanded):
            row = self._connection.execute(
                """
                SELECT runs.*
                FROM sync_runs AS runs
                JOIN catalog_sync_subjects AS subjects
                  ON subjects.sync_run_id = runs.id
                JOIN catalog_sync_demand AS demand
                  ON demand.sync_run_id = runs.id
                WHERE subjects.account_id = ? AND subjects.machine_id = ?
                  AND demand.appid = ?
                  AND runs.capability = 'catalog.application.read'
                ORDER BY runs.id DESC LIMIT 1
                """,
                (account_id, machine_id, appid),
            ).fetchone()
            if row is None:
                continue
            run = _sync_run(row)
            entry = by_run.setdefault(run.id, (run, []))
            entry[1].append(appid)
        return tuple(
            CatalogRelevantAttempt(run=run, appids=tuple(appids))
            for run, appids in (by_run[run_id] for run_id in sorted(by_run))
        )

    def _catalog_demand_for_run(self, sync_run_id: int) -> tuple[int, ...] | None:
        if (
            self._connection.execute(
                "SELECT 1 FROM catalog_sync_subjects WHERE sync_run_id = ?",
                (sync_run_id,),
            ).fetchone()
            is None
        ):
            return None
        return tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT appid FROM catalog_sync_demand "
                "WHERE sync_run_id = ? ORDER BY appid",
                (sync_run_id,),
            )
        )

    def _count_where(self, table: str, column: str, value: object) -> int:
        allowed = {
            ("owned_observations", "account_id"),
            ("owned_current", "account_id"),
            ("wishlist_observations", "account_id"),
            ("wishlist_current", "account_id"),
            ("price_observations", "account_id"),
            ("price_current", "account_id"),
            ("price_subject_current", "account_id"),
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
              AND NOT EXISTS (
                  SELECT 1 FROM wishlist_observations
                  WHERE wishlist_observations.evidence_id = evidence.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM wishlist_current
                  WHERE wishlist_current.evidence_id = evidence.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_observations
                  WHERE catalog_observations.evidence_id = evidence.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_current
                  WHERE catalog_current.evidence_id = evidence.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_subject_current
                  WHERE catalog_subject_current.evidence_id = evidence.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM price_observations
                  WHERE price_observations.evidence_id = evidence.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM price_current
                  WHERE price_current.evidence_id = evidence.id
              )
            """,
            evidence_ids,
        )
        return cursor.rowcount

    def _prune_catalog_payloads(self) -> None:
        evidence_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                """
                SELECT DISTINCT observations.evidence_id
                FROM catalog_observations AS observations
                WHERE NOT EXISTS (
                    SELECT 1 FROM catalog_current AS current
                    WHERE current.appid = observations.appid
                      AND current.promoted_sync_run_id = observations.sync_run_id
                      AND current.evidence_id = observations.evidence_id
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM catalog_subject_current AS subject_current
                    WHERE subject_current.appid = observations.appid
                      AND subject_current.promoted_sync_run_id =
                          observations.sync_run_id
                      AND subject_current.evidence_id = observations.evidence_id
                  )
                """
            )
        )
        self._connection.execute(
            """
            DELETE FROM catalog_observations
            WHERE NOT EXISTS (
                SELECT 1 FROM catalog_current AS current
                WHERE current.appid = catalog_observations.appid
                  AND current.promoted_sync_run_id = catalog_observations.sync_run_id
                  AND current.evidence_id = catalog_observations.evidence_id
            )
              AND NOT EXISTS (
                SELECT 1 FROM catalog_subject_current AS subject_current
                WHERE subject_current.appid = catalog_observations.appid
                  AND subject_current.promoted_sync_run_id =
                      catalog_observations.sync_run_id
                  AND subject_current.evidence_id =
                      catalog_observations.evidence_id
              )
            """
        )
        self._connection.execute(
            """
            DELETE FROM catalog_sync_metadata
            WHERE NOT EXISTS (
                SELECT 1 FROM catalog_current
                WHERE catalog_current.promoted_sync_run_id =
                      catalog_sync_metadata.sync_run_id
            )
              AND NOT EXISTS (
                SELECT 1 FROM catalog_subject_current
                WHERE catalog_subject_current.promoted_sync_run_id =
                      catalog_sync_metadata.sync_run_id
              )
            """
        )
        self._delete_orphan_owned_evidence(evidence_ids)

    def _delete_account_catalog_scope(
        self, account_id: int
    ) -> tuple[int, int, tuple[int, ...]]:
        """Remove one account's demand lineage while retaining shared facts.

        Catalog facts are public and shared, but the demand set that selected
        them is account data. A current fact whose provenance belongs to the
        deleted account is detached to an unscoped copy only when another
        account or installed projection still needs that AppID.
        """

        run_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT sync_run_id FROM catalog_sync_subjects "
                "WHERE account_id = ? ORDER BY sync_run_id",
                (account_id,),
            )
        )
        catalog_appids = tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT appid FROM catalog_current ORDER BY appid"
            )
        )
        evidence_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                """
                SELECT evidence_id FROM catalog_observations
                UNION
                SELECT evidence_id FROM catalog_current
                """
            )
        )
        needed = {
            int(row[0])
            for row in self._connection.execute(
                """
                SELECT demand.appid
                FROM catalog_sync_demand AS demand
                JOIN catalog_sync_subjects AS subjects
                  ON subjects.sync_run_id = demand.sync_run_id
                WHERE subjects.account_id <> ?
                UNION
                SELECT appid FROM owned_current WHERE account_id <> ?
                UNION
                SELECT appid FROM installed_current
                """,
                (account_id, account_id),
            )
        }

        for source_run_id in run_ids:
            retained_appids = tuple(
                int(row[0])
                for row in self._connection.execute(
                    "SELECT appid FROM catalog_current "
                    "WHERE promoted_sync_run_id = ? ORDER BY appid",
                    (source_run_id,),
                )
                if int(row[0]) in needed
            )
            if not retained_appids:
                continue
            source = self._connection.execute(
                "SELECT * FROM sync_runs WHERE id = ? AND status = 'complete'",
                (source_run_id,),
            ).fetchone()
            metadata = self._connection.execute(
                "SELECT provider, support_level FROM catalog_sync_metadata "
                "WHERE sync_run_id = ?",
                (source_run_id,),
            ).fetchone()
            if source is None or metadata is None:
                raise StorageError("shared catalog provenance is incomplete")
            cursor = self._connection.execute(
                """
                INSERT INTO sync_runs(
                    provider, capability, started_at, completed_at, status,
                    promoted, records_seen, error_code, error_detail
                ) VALUES (?, 'catalog.application.read', ?, ?, 'complete',
                          1, ?, NULL, NULL)
                """,
                (
                    source["provider"],
                    source["started_at"],
                    source["completed_at"],
                    len(retained_appids),
                ),
            )
            detached_run_id = int(cursor.lastrowid)
            self._connection.execute(
                """
                INSERT INTO catalog_sync_metadata(
                    sync_run_id, provider, support_level, demanded_count
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    detached_run_id,
                    metadata["provider"],
                    metadata["support_level"],
                    len(retained_appids),
                ),
            )
            self._connection.execute(
                """
                INSERT INTO catalog_stream_provenance(
                    sync_run_id, stream, termination, scanned_through_appid,
                    filter_context_json
                )
                SELECT ?, stream, termination, scanned_through_appid,
                       filter_context_json
                FROM catalog_stream_provenance WHERE sync_run_id = ?
                """,
                (detached_run_id, source_run_id),
            )
            self._connection.execute(
                """
                INSERT INTO catalog_page_provenance(
                    sync_run_id, stream, page_number, requested_last_appid,
                    first_appid, last_appid, item_count, have_more_results,
                    retrieved_at
                )
                SELECT ?, stream, page_number, requested_last_appid,
                       first_appid, last_appid, item_count, have_more_results,
                       retrieved_at
                FROM catalog_page_provenance WHERE sync_run_id = ?
                """,
                (detached_run_id, source_run_id),
            )
            placeholders = ",".join("?" for _ in retained_appids)
            self._connection.execute(
                f"""
                INSERT INTO catalog_observations(
                    sync_run_id, evidence_id, appid, classification,
                    last_modified, price_change_number, observed_at
                )
                SELECT ?, evidence_id, appid, classification,
                       last_modified, price_change_number, observed_at
                FROM catalog_current WHERE appid IN ({placeholders})
                """,
                (detached_run_id, *retained_appids),
            )
            self._connection.execute(
                f"""
                UPDATE catalog_current SET promoted_sync_run_id = ?
                WHERE appid IN ({placeholders})
                """,
                (detached_run_id, *retained_appids),
            )

        if needed:
            placeholders = ",".join("?" for _ in needed)
            self._connection.execute(
                f"DELETE FROM catalog_current WHERE appid NOT IN ({placeholders})",
                tuple(sorted(needed)),
            )
        else:
            self._connection.execute("DELETE FROM catalog_current")
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            self._connection.execute(
                f"DELETE FROM sync_runs WHERE id IN ({placeholders})", run_ids
            )
        self._prune_catalog_payloads()
        orphan_runs = self._connection.execute(
            """
            DELETE FROM sync_runs
            WHERE capability = 'catalog.application.read'
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_sync_subjects
                  WHERE catalog_sync_subjects.sync_run_id = sync_runs.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_sync_metadata
                  WHERE catalog_sync_metadata.sync_run_id = sync_runs.id
              )
            """
        ).rowcount
        self._delete_orphan_owned_evidence(evidence_ids)
        evidence_removed = sum(
            self._connection.execute(
                "SELECT 1 FROM evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
            is None
            for evidence_id in evidence_ids
        )
        return len(run_ids) + orphan_runs, evidence_removed, catalog_appids

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

    def _prune_wishlist_payloads(
        self,
        account_id: int | None,
        selected_sync_run_id: int,
        *,
        keep_selected: bool,
    ) -> None:
        if account_id is None:
            raise ValueError("wishlist payload pruning requires an account")
        operator = "<>" if keep_selected else "="
        evidence_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                f"""
                SELECT DISTINCT evidence_id FROM wishlist_observations
                WHERE account_id = ? AND sync_run_id {operator} ?
                  AND sync_run_id IN (
                      SELECT id FROM sync_runs WHERE status <> 'running'
                  )
                """,
                (account_id, selected_sync_run_id),
            )
        )
        appids = tuple(
            int(row[0])
            for row in self._connection.execute(
                f"""
                SELECT DISTINCT appid FROM wishlist_observations
                WHERE account_id = ? AND sync_run_id {operator} ?
                  AND sync_run_id IN (
                      SELECT id FROM sync_runs WHERE status <> 'running'
                  )
                """,
                (account_id, selected_sync_run_id),
            )
        )
        self._connection.execute(
            f"""
            DELETE FROM wishlist_observations
            WHERE account_id = ? AND sync_run_id {operator} ?
              AND sync_run_id IN (SELECT id FROM sync_runs WHERE status <> 'running')
            """,
            (account_id, selected_sync_run_id),
        )
        self._connection.execute(
            f"""
            DELETE FROM wishlist_sync_metadata
            WHERE account_id = ? AND sync_run_id {operator} ?
              AND sync_run_id IN (SELECT id FROM sync_runs WHERE status <> 'running')
            """,
            (account_id, selected_sync_run_id),
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
              AND NOT EXISTS (
                  SELECT 1 FROM wishlist_observations
                  WHERE wishlist_observations.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM wishlist_current
                  WHERE wishlist_current.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_observations
                  WHERE catalog_observations.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_current
                  WHERE catalog_current.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_subject_current
                  WHERE catalog_subject_current.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM price_sync_demand
                  WHERE price_sync_demand.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM price_observations
                  WHERE price_observations.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM price_current
                  WHERE price_current.appid = steam_apps.appid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM price_subject_current
                  WHERE price_subject_current.appid = steam_apps.appid
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

    def _require_wishlist_sync(self, sync_run_id: int) -> SyncRun:
        run = self.get_sync_run(sync_run_id)
        if run.capability != "wishlist.read":
            raise InvalidSyncTransition("sync run is not a wishlist sync")
        if run.account_id is None or run.machine_id is not None:
            raise InvalidSyncTransition("wishlist sync requires only an account_id")
        return run

    def _require_running_catalog_sync(self, sync_run_id: int) -> SyncRun:
        run = self.get_sync_run(sync_run_id)
        if run.capability != "catalog.application.read":
            raise InvalidSyncTransition("sync run is not an application-catalog sync")
        if run.account_id is not None or run.machine_id is not None:
            raise InvalidSyncTransition(
                "catalog sync cannot target an account or machine"
            )
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
    "CatalogFact",
    "CatalogObservation",
    "CatalogPageInput",
    "CatalogSnapshot",
    "CatalogSourceProvenance",
    "CatalogStreamInput",
    "CatalogStreamProvenance",
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
    "WishlistGame",
    "WishlistObservation",
    "WishlistSnapshot",
    "WishlistSnapshotProvenance",
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


def _catalog_appids(values: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= (1 << 32) - 1
        for value in values
    ):
        raise ValueError("catalog AppIDs must be positive unsigned 32-bit integers")
    normalized = tuple(sorted(set(values)))
    if len(normalized) != len(values):
        raise ValueError("catalog demanded AppIDs must be unique")
    return normalized


def _catalog_observations(
    values: list[CatalogObservation] | tuple[CatalogObservation, ...],
    *,
    demanded: tuple[int, ...],
) -> tuple[CatalogObservation, ...]:
    by_appid: dict[int, CatalogObservation] = {}
    for observation in values:
        if observation.appid in by_appid:
            raise ValueError("catalog observations must have unique AppIDs")
        if observation.classification not in ("game", "non_game", "not_observed"):
            raise ValueError("unsupported catalog classification")
        for counter in (
            observation.last_modified,
            observation.price_change_number,
        ):
            if counter is not None and (
                not isinstance(counter, int)
                or isinstance(counter, bool)
                or not 0 <= counter <= (1 << 64) - 1
            ):
                raise ValueError("catalog counters must be unsigned 64-bit integers")
        if observation.classification == "not_observed" and (
            observation.last_modified is not None
            or observation.price_change_number is not None
        ):
            raise ValueError("not-observed catalog facts cannot have provider counters")
        by_appid[observation.appid] = observation
    if tuple(sorted(by_appid)) != demanded:
        raise ValueError("catalog observations must exactly cover demanded AppIDs")
    return tuple(by_appid[appid] for appid in demanded)


def _catalog_streams(
    games: CatalogStreamInput,
    non_games: CatalogStreamInput,
) -> tuple[tuple[CatalogStreamInput, tuple[tuple[CatalogPageInput, str], ...]], ...]:
    if games.stream != "games" or non_games.stream != "non_games":
        raise ValueError("catalog provenance requires games and non-games streams")
    result: list[
        tuple[CatalogStreamInput, tuple[tuple[CatalogPageInput, str], ...]]
    ] = []
    for stream in (games, non_games):
        if stream.termination not in ("no_demand", "demand_boundary", "end_of_stream"):
            raise ValueError("catalog stream must be complete before persistence")
        if (
            not isinstance(stream.scanned_through_appid, int)
            or isinstance(stream.scanned_through_appid, bool)
            or not 0 <= stream.scanned_through_appid <= (1 << 32) - 1
        ):
            raise ValueError("catalog scan boundary is invalid")
        if not isinstance(stream.filter_context, Mapping):
            raise ValueError("catalog filter context must be a mapping")
        _validate_catalog_filter_context(stream.stream, stream.filter_context)
        normalized_pages: list[tuple[CatalogPageInput, str]] = []
        previous_last = 0
        for expected_number, page in enumerate(stream.pages, start=1):
            if page.page_number != expected_number:
                raise ValueError("catalog page numbers must be contiguous")
            if page.requested_last_appid != previous_last:
                raise ValueError("catalog page cursor chain is inconsistent")
            if (
                page.first_appid is not None
                and not 1 <= page.first_appid <= page.last_appid
            ):
                raise ValueError("catalog first AppID is invalid")
            if not 0 <= page.last_appid <= (1 << 32) - 1 or page.item_count < 0:
                raise ValueError("catalog page bounds are invalid")
            if not isinstance(page.have_more_results, bool):
                raise ValueError("catalog page continuation must be boolean")
            normalized_pages.append((page, _timestamp(page.retrieved_at)))
            previous_last = page.last_appid
        if previous_last != stream.scanned_through_appid:
            raise ValueError("catalog scan boundary does not match its final page")
        result.append((stream, tuple(normalized_pages)))
    return tuple(result)


def _validate_catalog_filter_context(
    stream: CatalogStreamName, context: Mapping[str, Any]
) -> None:
    boolean_keys = {
        "include_games",
        "include_dlc",
        "include_software",
        "include_videos",
        "include_hardware",
    }
    if not boolean_keys <= set(context) or set(context) - boolean_keys - {
        "max_results"
    }:
        raise ValueError("catalog filter context contains unsupported fields")
    if any(not isinstance(context[key], bool) for key in boolean_keys):
        raise ValueError("catalog include filters must be boolean")
    expected_games = stream == "games"
    expected = {
        "include_games": expected_games,
        "include_dlc": not expected_games,
        "include_software": not expected_games,
        "include_videos": not expected_games,
        "include_hardware": not expected_games,
    }
    if any(context[key] is not value for key, value in expected.items()):
        raise ValueError("catalog filter context does not match its stream")
    if "max_results" in context and (
        not isinstance(context["max_results"], int)
        or isinstance(context["max_results"], bool)
        or not 1 <= context["max_results"] <= 50_000
    ):
        raise ValueError("catalog max_results filter is invalid")

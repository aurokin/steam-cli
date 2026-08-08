"""Durable operation ledger: state machine, nonces, and audit in one SQLite.

The ledger is the system of record for every execution attempt.  State
transitions and confirmation-nonce consumption are atomic SQL updates, so a
crash can never leave an operation authorized twice or a nonce reusable.
Startup reconciliation reads non-terminal rows and maps filesystem/process
evidence to exactly one recovery action (executor.reconcile).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Literal

SCHEMA = "operation-execution/0.1"

OperationState = Literal[
    "pending_confirmation",
    "authorized",
    "lease_acquired",
    "client_stopping",
    "content_running",
    "adopting",
    "client_restart_pending",
    "verifying",
    "interrupted",
    "confirmed",
    "unconfirmed",
    "contradicted",
    "aborted",
    "failed",
    "expired",
]

TERMINAL_STATES: frozenset[str] = frozenset(
    {"confirmed", "unconfirmed", "contradicted", "aborted", "failed", "expired"}
)

_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending_confirmation": frozenset({"authorized", "expired", "aborted"}),
    "authorized": frozenset({"lease_acquired", "expired", "aborted"}),
    "lease_acquired": frozenset({"client_stopping", "aborted", "interrupted"}),
    "client_stopping": frozenset({"content_running", "aborted", "interrupted"}),
    "content_running": frozenset(
        {"adopting", "failed", "interrupted", "aborted"}
    ),
    "adopting": frozenset({"client_restart_pending", "interrupted", "failed"}),
    "client_restart_pending": frozenset({"verifying", "interrupted"}),
    "verifying": frozenset({"confirmed", "unconfirmed", "contradicted"}),
    "interrupted": frozenset(
        {"content_running", "adopting", "verifying", "failed", "aborted"}
    ),
}

DEFAULT_NONCE_TTL_SECONDS = 15 * 60
DEFAULT_EXECUTION_WINDOW_SECONDS = 4 * 60 * 60


class LedgerError(RuntimeError):
    """Base error for ledger failures."""


class InvalidTransition(LedgerError):
    """A state change not present in the transition table was requested."""


class ConfirmationRejected(LedgerError):
    """Nonce missing, consumed, expired, or bound to different content."""


@dataclass(frozen=True, slots=True)
class LedgerOperation:
    operation_id: int
    plan_key: str
    operation: str
    appid: int
    account_alias: str
    machine_id: str
    state: str
    policy_version: str
    confirmation_actor: str | None
    prior_client_running: bool | None
    execution_window_expires_at: str | None
    detail: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class ExecutionLedger:
    """SQLite-backed operation ledger owned by the broker identity."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self):
        # isolation_level=None leaves sqlite3 in autocommit; a state change
        # and its audit event must land in one explicit transaction so a
        # crash can never advance durable state without its event.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _migrate(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id INTEGER PRIMARY KEY,
                    schema TEXT NOT NULL,
                    plan_key TEXT NOT NULL,
                    plan_document TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    appid INTEGER NOT NULL,
                    account_alias TEXT NOT NULL,
                    machine_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    nonce TEXT,
                    nonce_expires_at TEXT,
                    confirmation_actor TEXT,
                    execution_window_expires_at TEXT,
                    prior_client_running INTEGER,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY,
                    operation_id INTEGER NOT NULL REFERENCES operations(operation_id),
                    at TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    detail TEXT
                )
                """
            )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_operation
                ON operations(machine_id)
                WHERE state NOT IN (
                    'confirmed','unconfirmed','contradicted',
                    'aborted','failed','expired'
                )
                """
            )
            # One idempotency key, one attempt that could ever execute —
            # durably, so a transport retry can never mint a second nonce
            # for a plan that already ran (or is running).  'expired' rows
            # are excluded: a nonce that lapsed unconfirmed never executed,
            # and re-requesting that plan is legitimate.
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_operation_per_plan_key
                ON operations(plan_key)
                WHERE state != 'expired'
                """
            )

    # -- intake -----------------------------------------------------------

    def request(
        self,
        *,
        plan_key: str,
        plan_document: str,
        operation: str,
        appid: int,
        account_alias: str,
        machine_id: str,
        policy_version: str,
        nonce_ttl_seconds: int = DEFAULT_NONCE_TTL_SECONDS,
    ) -> tuple[int, str]:
        """Record a requested operation and mint its single-use nonce.

        Returns ``(operation_id, nonce)``.  The nonce is the human-facing
        confirmation secret; the OS identity boundary, not nonce secrecy,
        is what keeps the requesting agent from confirming its own plans.
        """

        # token_hex, not token_urlsafe: a nonce is retyped as a CLI argument
        # and must never begin with "-" or argparse consumes it as a flag.
        nonce = secrets.token_hex(16)
        # A pending row whose nonce lapsed would otherwise hold the
        # single-active slot forever and block every new request.
        self.expire_lapsed()
        now = _utcnow()
        expires = now + timedelta(seconds=nonce_ttl_seconds)
        try:
            with self._transaction():
                # Idempotency: a retry of an already-recorded plan (lost
                # response, transport replay) must surface as an error, not
                # a fresh nonce that could execute the plan a second time.
                duplicate = self._connection.execute(
                    "SELECT state FROM operations"
                    " WHERE plan_key = ? AND state != 'expired' LIMIT 1",
                    (plan_key,),
                ).fetchone()
                if duplicate is not None:
                    raise LedgerError(
                        "idempotency key already recorded"
                        f" (state: {duplicate[0]})"
                    )
                cursor = self._connection.execute(
                    """
                    INSERT INTO operations (
                        schema, plan_key, plan_document, operation, appid,
                        account_alias, machine_id, state, policy_version,
                        nonce, nonce_expires_at, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        SCHEMA,
                        plan_key,
                        plan_document,
                        operation,
                        appid,
                        account_alias,
                        machine_id,
                        "pending_confirmation",
                        policy_version,
                        nonce,
                        _stamp(expires),
                        _stamp(now),
                        _stamp(now),
                    ),
                )
                operation_id = int(cursor.lastrowid or 0)
                self._event(operation_id, "none", "pending_confirmation", None)
        except sqlite3.IntegrityError as error:
            raise LedgerError(
                "another operation is already active for this machine"
            ) from error
        return operation_id, nonce

    def confirm(
        self,
        *,
        nonce: str,
        actor: str,
        execution_window_seconds: int = DEFAULT_EXECUTION_WINDOW_SECONDS,
    ) -> int:
        """Atomically consume a nonce; returns the authorized operation id."""

        now = _utcnow()
        window = now + timedelta(seconds=execution_window_seconds)
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE operations
                SET state='authorized', nonce=NULL, nonce_expires_at=NULL,
                    confirmation_actor=?, execution_window_expires_at=?,
                    updated_at=?
                WHERE nonce=? AND state='pending_confirmation'
                  AND nonce_expires_at > ?
                RETURNING operation_id
                """,
                (actor, _stamp(window), _stamp(now), nonce, _stamp(now)),
            )
            row = cursor.fetchone()
            if row is None:
                raise ConfirmationRejected(
                    "nonce missing, already consumed, or expired"
                )
            operation_id = int(row[0])
            self._event(operation_id, "pending_confirmation", "authorized", actor)
        return operation_id

    # -- state machine ----------------------------------------------------

    def transition(
        self,
        operation_id: int,
        to_state: str,
        *,
        detail: str | None = None,
        prior_client_running: bool | None = None,
    ) -> None:
        current = self.get(operation_id)
        allowed = _TRANSITIONS.get(current.state, frozenset())
        if to_state not in allowed:
            raise InvalidTransition(
                f"{current.state!r} cannot transition to {to_state!r}"
            )
        now = _stamp(_utcnow())
        assignments = "state=?, detail=?, updated_at=?"
        parameters: list[object] = [to_state, detail, now]
        if prior_client_running is not None:
            assignments += ", prior_client_running=?"
            parameters.append(1 if prior_client_running else 0)
        parameters.extend([operation_id, current.state])
        with self._transaction():
            cursor = self._connection.execute(
                f"UPDATE operations SET {assignments}"
                " WHERE operation_id=? AND state=?",
                parameters,
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("operation state changed concurrently")
            self._event(operation_id, current.state, to_state, detail)

    def expire_lapsed(self) -> list[int]:
        """Expire pending confirmations and authorized-but-lapsed windows."""

        now = _stamp(_utcnow())
        expired: list[tuple[int, str]] = []
        with self._transaction():
            for state, column in (
                ("pending_confirmation", "nonce_expires_at"),
                ("authorized", "execution_window_expires_at"),
            ):
                cursor = self._connection.execute(
                    f"""
                    UPDATE operations SET state='expired', updated_at=?
                    WHERE state=? AND {column} <= ?
                    RETURNING operation_id
                    """,
                    (now, state, now),
                )
                for row in cursor.fetchall():
                    expired.append((int(row[0]), state))
            for operation_id, from_state in expired:
                self._event(operation_id, from_state, "expired", None)
        return [operation_id for operation_id, _ in expired]

    # -- reads ------------------------------------------------------------

    def get(self, operation_id: int) -> LedgerOperation:
        row = self._connection.execute(
            """
            SELECT operation_id, plan_key, operation, appid, account_alias,
                   machine_id, state, policy_version, confirmation_actor,
                   prior_client_running, execution_window_expires_at, detail
            FROM operations WHERE operation_id=?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"operation {operation_id} does not exist")
        return LedgerOperation(
            operation_id=row[0],
            plan_key=row[1],
            operation=row[2],
            appid=row[3],
            account_alias=row[4],
            machine_id=row[5],
            state=row[6],
            policy_version=row[7],
            confirmation_actor=row[8],
            prior_client_running=None if row[9] is None else bool(row[9]),
            execution_window_expires_at=row[10],
            detail=row[11],
        )

    def plan_document(self, operation_id: int) -> dict[str, object]:
        row = self._connection.execute(
            "SELECT plan_document FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"operation {operation_id} does not exist")
        return json.loads(row[0])

    def active(self) -> LedgerOperation | None:
        row = self._connection.execute(
            "SELECT operation_id FROM operations WHERE state NOT IN"
            " ('confirmed','unconfirmed','contradicted','aborted','failed',"
            "'expired') LIMIT 1"
        ).fetchone()
        return None if row is None else self.get(int(row[0]))

    def window_valid(self, operation_id: int) -> bool:
        operation = self.get(operation_id)
        if operation.execution_window_expires_at is None:
            return False
        return operation.execution_window_expires_at > _stamp(_utcnow())

    def events(self, operation_id: int) -> list[tuple[str, str, str]]:
        rows = self._connection.execute(
            "SELECT at, from_state, to_state FROM events"
            " WHERE operation_id=? ORDER BY event_id",
            (operation_id,),
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def _event(
        self,
        operation_id: int,
        from_state: str,
        to_state: str,
        detail: str | None,
    ) -> None:
        # Always called inside an open _transaction(); never commits itself.
        self._connection.execute(
            "INSERT INTO events (operation_id, at, from_state, to_state,"
            " detail) VALUES (?,?,?,?,?)",
            (operation_id, _stamp(_utcnow()), from_state, to_state, detail),
        )

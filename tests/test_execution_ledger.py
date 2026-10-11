"""Ledger state machine, nonce consumption, and single-writer constraint."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from steam_agent.execution.ledger import (
    TERMINAL_STATES,
    ConfirmationRejected,
    ExecutionLedger,
    InvalidTransition,
    LedgerError,
    UnsupportedLedger,
)


@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    instance = ExecutionLedger(tmp_path / "ledger.sqlite3")
    yield instance
    instance.close()


def _request(
    ledger: ExecutionLedger,
    machine: str = "machine-a",
    ttl: int = 900,
    key: str = "k" * 8,
) -> tuple[int, str]:
    return ledger.request(
        plan_key=key,
        plan_document='{"schema": "operation-plan/0.1"}',
        operation="install",
        appid=1902490,
        account_alias="owner",
        machine_id=machine,
        policy_version="deadbeef",
        nonce_ttl_seconds=ttl,
    )


def test_expired_pending_does_not_block_new_request(
    ledger: ExecutionLedger,
) -> None:
    stale_id, _ = _request(ledger, ttl=0)  # lapses immediately
    fresh_id, _ = _request(ledger)  # intake expires the stale row first
    assert fresh_id != stale_id
    assert ledger.get(stale_id).state == "expired"
    # The audit event records the real prior state, not a pseudo-state.
    events = ledger.events(stale_id)
    assert events[-1][1] == "pending_confirmation"
    assert events[-1][2] == "expired"


def test_request_confirm_flow(ledger: ExecutionLedger) -> None:
    operation_id, nonce = _request(ledger)
    assert ledger.get(operation_id).state == "pending_confirmation"

    confirmed = ledger.confirm(nonce=nonce, actor="discord:owner")
    assert confirmed == operation_id
    operation = ledger.get(operation_id)
    assert operation.state == "authorized"
    assert operation.confirmation_actor == "discord:owner"
    assert ledger.window_valid(operation_id)


def test_nonce_is_single_use(ledger: ExecutionLedger) -> None:
    _, nonce = _request(ledger)
    ledger.confirm(nonce=nonce, actor="a")
    with pytest.raises(ConfirmationRejected):
        ledger.confirm(nonce=nonce, actor="a")


def test_wrong_nonce_rejected(ledger: ExecutionLedger) -> None:
    _request(ledger)
    with pytest.raises(ConfirmationRejected):
        ledger.confirm(nonce="not-a-nonce", actor="a")


def test_expired_nonce_rejected(ledger: ExecutionLedger) -> None:
    _, nonce = ledger.request(
        plan_key="k" * 8,
        plan_document="{}",
        operation="install",
        appid=480,
        account_alias="owner",
        machine_id="machine-a",
        policy_version="v",
        nonce_ttl_seconds=0,
    )
    with pytest.raises(ConfirmationRejected):
        ledger.confirm(nonce=nonce, actor="a")
    assert ledger.expire_lapsed() != []


def test_single_active_operation_per_machine(ledger: ExecutionLedger) -> None:
    _request(ledger)
    with pytest.raises(LedgerError):
        _request(ledger, key="different-key")  # same machine, distinct plan


def test_terminal_frees_the_machine_slot(ledger: ExecutionLedger) -> None:
    operation_id, nonce = _request(ledger)
    ledger.confirm(nonce=nonce, actor="a")
    for state in ("lease_acquired", "client_stopping", "content_running"):
        ledger.transition(operation_id, state)
    ledger.transition(operation_id, "failed", detail="test")
    _request(ledger, key="next-plan")  # does not raise


def test_idempotency_key_replay_rejected(ledger: ExecutionLedger) -> None:
    operation_id, nonce = _request(ledger)
    ledger.confirm(nonce=nonce, actor="a")
    for state in ("lease_acquired", "client_stopping", "content_running"):
        ledger.transition(operation_id, state)
    ledger.transition(operation_id, "failed", detail="test")
    # Terminal or not, a recorded key must never mint a second nonce.
    with pytest.raises(LedgerError):
        _request(ledger)
    _request(ledger, key="fresh-key")  # a new plan proceeds normally


def test_transition_table_enforced(ledger: ExecutionLedger) -> None:
    operation_id, nonce = _request(ledger)
    ledger.confirm(nonce=nonce, actor="a")
    with pytest.raises(InvalidTransition):
        ledger.transition(operation_id, "verifying")


def test_transition_expected_from_guards_concurrent_advance(
    ledger: ExecutionLedger,
) -> None:
    operation_id, nonce = _request(ledger)
    ledger.confirm(nonce=nonce, actor="a")
    ledger.transition(operation_id, "lease_acquired")  # a run advanced it
    # A revocation abort pinned to authorized must not terminalize the
    # in-flight execution.
    with pytest.raises(InvalidTransition):
        ledger.transition(operation_id, "aborted", expected_from="authorized")
    assert ledger.get(operation_id).state == "lease_acquired"


def test_events_are_recorded_in_order(ledger: ExecutionLedger) -> None:
    operation_id, nonce = _request(ledger)
    ledger.confirm(nonce=nonce, actor="a")
    ledger.transition(operation_id, "lease_acquired", prior_client_running=True)
    states = [event[2] for event in ledger.events(operation_id)]
    assert states == ["pending_confirmation", "authorized", "lease_acquired"]
    assert ledger.get(operation_id).prior_client_running is True


def test_interrupted_supports_resume_transition(ledger: ExecutionLedger) -> None:
    operation_id, nonce = _request(ledger)
    ledger.confirm(nonce=nonce, actor="a")
    for state in ("lease_acquired", "client_stopping", "content_running"):
        ledger.transition(operation_id, state)
    ledger.transition(operation_id, "interrupted")
    ledger.transition(operation_id, "content_running")
    assert ledger.get(operation_id).state == "content_running"


def test_every_terminal_state_is_a_declared_operation_state() -> None:
    from typing import get_args

    from steam_agent.execution.ledger import OperationState

    assert TERMINAL_STATES <= set(get_args(OperationState))


def test_dispatched_is_reachable_only_after_durable_intent() -> None:
    from steam_agent.execution.ledger import _TRANSITIONS

    sources = {
        state
        for state, targets in _TRANSITIONS.items()
        if "dispatched" in targets
    }

    assert sources == {"dispatch_pending"}
    # Terminal: nothing leads out of it.
    assert "dispatched" not in _TRANSITIONS


def test_a_dispatched_row_frees_the_single_active_slot(tmp_path: Path) -> None:
    # A terminal state the single-active index does not know about would
    # block every later operation on that machine, forever.
    ledger = ExecutionLedger(tmp_path / "ledger.sqlite3")
    try:
        _, nonce = ledger.request(
            plan_key="k" * 16,
            plan_document="{}",
            operation="launch",
            appid=480,
            account_alias="owner",
            machine_id="machine-a",
            policy_version="v",
        )
        operation_id = ledger.confirm(nonce=nonce, actor="owner")
        ledger.transition(operation_id, "dispatch_pending")
        ledger.transition(operation_id, "dispatched")

        assert ledger.active() is None
        assert [row[0] for row in ledger.recent(5)] == [operation_id]
        # The slot is genuinely free: a second operation can be requested.
        ledger.request(
            plan_key="j" * 16,
            plan_document="{}",
            operation="install",
            appid=220,
            account_alias="owner",
            machine_id="machine-a",
            policy_version="v",
        )
    finally:
        ledger.close()


def test_dispatch_intent_survives_reopen_and_keeps_the_active_slot(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = ExecutionLedger(path)
    operation_id, nonce = _request(ledger)
    ledger.confirm(nonce=nonce, actor="owner")
    ledger.transition(operation_id, "dispatch_pending")
    ledger.close()

    reopened = ExecutionLedger(path)
    try:
        assert reopened.get(operation_id).state == "dispatch_pending"
        with pytest.raises(LedgerError, match="already active"):
            _request(reopened, key="new-plan-key")
        reopened.transition(operation_id, "unconfirmed")
        assert reopened.active() is None
        with pytest.raises(LedgerError, match="idempotency key already recorded"):
            _request(reopened)
    finally:
        reopened.close()


def test_find_request_returns_the_existing_pending_authorization(ledger: ExecutionLedger) -> None:
    operation_id, nonce = _request(ledger)

    found = ledger.find_request(
        "k" * 8, plan_document='{"schema": "operation-plan/0.1"}'
    )

    assert found is not None
    assert found[0].operation_id == operation_id
    assert found[1] == nonce
    ledger.confirm(nonce=nonce, actor="owner")
    ledger.transition(operation_id, "dispatch_pending")
    ledger.transition(operation_id, "dispatched")
    receipt = ledger.find_request(
        "k" * 8, plan_document='{"schema": "operation-plan/0.1"}'
    )
    assert receipt is not None
    assert receipt[0].state == "dispatched"
    assert receipt[1] is None


def test_find_request_rejects_changed_plan_content(ledger: ExecutionLedger) -> None:
    _request(ledger)

    with pytest.raises(LedgerError, match="different plan content"):
        ledger.find_request("k" * 8, plan_document='{"appid": 480}')


def test_find_request_ignores_expired_attempts(ledger: ExecutionLedger) -> None:
    _request(ledger, ttl=0)
    ledger.expire_lapsed()

    assert ledger.find_request(
        "k" * 8, plan_document='{"schema": "operation-plan/0.1"}'
    ) is None


def test_a_ledger_predating_a_terminal_state_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = ExecutionLedger(path)
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX one_active_operation")
        connection.execute(
            "CREATE UNIQUE INDEX one_active_operation ON operations(machine_id)"
            " WHERE state NOT IN ('confirmed','aborted')"
        )

    reopened = ExecutionLedger(path)
    try:
        stored = reopened._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='one_active_operation'"
        ).fetchone()[0]
        for state in TERMINAL_STATES:
            assert f"'{state}'" in stored
    finally:
        reopened.close()


def test_concurrent_openers_rebuild_the_active_index_once(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ExecutionLedger(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX one_active_operation")
        connection.execute(
            "CREATE UNIQUE INDEX one_active_operation ON operations(machine_id)"
            " WHERE state NOT IN ('confirmed')"
        )

    opened = [ExecutionLedger(path) for _ in range(4)]
    try:
        definitions = {
            ledger._connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='one_active_operation'"
            ).fetchone()[0]
            for ledger in opened
        }
        assert len(definitions) == 1
        assert "'dispatched'" in definitions.pop()
    finally:
        for ledger in opened:
            ledger.close()


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE operations SET schema='operation-execution/9.9'",
        "UPDATE operations SET state='future_terminal'",
        "UPDATE operations SET operation='future_content_operation'",
        "PRAGMA user_version=1",
    ],
)
def test_unknown_ledger_refused_before_journal_or_index_changes(
    tmp_path: Path, readonly: bool, mutation: str,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    seeded = ExecutionLedger(path)
    _request(seeded)
    seeded.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(mutation)
        connection.execute("DROP INDEX one_active_operation")
        connection.execute(
            "CREATE UNIQUE INDEX one_active_operation ON operations(machine_id)"
            " WHERE state NOT IN ('future_terminal')"
        )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        before = tuple(connection.iterdump())
    original = path.read_bytes()

    with pytest.raises(UnsupportedLedger, match="unsupported ledger"):
        ExecutionLedger(path, readonly=readonly)

    assert path.read_bytes() == original
    with closing(sqlite3.connect(path)) as connection, connection:
        assert tuple(connection.iterdump()) == before
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_future_row_written_after_open_is_not_confirmed_or_expired(
    ledger: ExecutionLedger,
) -> None:
    operation_id, nonce = _request(ledger)
    with closing(sqlite3.connect(ledger._path)) as connection, connection:  # noqa: SLF001
        connection.execute("UPDATE operations SET schema='operation-execution/9.9'")
        before = tuple(connection.iterdump())
    with pytest.raises(UnsupportedLedger, match="unsupported ledger"):
        ledger.confirm(nonce=nonce, actor="synthetic")
    with pytest.raises(UnsupportedLedger, match="unsupported ledger"):
        ledger.expire_lapsed()
    with pytest.raises(UnsupportedLedger, match="unsupported ledger"):
        ledger.get(operation_id)
    with closing(sqlite3.connect(ledger._path)) as connection, connection:  # noqa: SLF001
        assert tuple(connection.iterdump()) == before


def test_unsupported_intake_does_not_expire_existing_request(
    ledger: ExecutionLedger,
) -> None:
    operation_id, _ = _request(ledger, ttl=0)
    with pytest.raises(UnsupportedLedger, match="unsupported ledger operation"):
        ledger.request(
            plan_key="future", plan_document="{}", operation="future_content_operation",
            appid=480, account_alias="synthetic", machine_id="other", policy_version="synthetic",
        )
    assert ledger.get(operation_id).state == "pending_confirmation"


@pytest.mark.parametrize("column", ["schema", "state", "operation"])
def test_nullable_future_layout_cannot_hide_unknown_contract(tmp_path: Path, column: str) -> None:
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE operations(schema TEXT, state TEXT, operation TEXT)")
        values = {"schema": "operation-execution/0.1", "state": "authorized", "operation": "launch"}
        values[column] = None
        connection.execute("INSERT INTO operations VALUES (:schema, :state, :operation)", values)
    before = path.read_bytes()
    with pytest.raises(UnsupportedLedger, match="unsupported ledger"):
        ExecutionLedger(path)
    assert path.read_bytes() == before

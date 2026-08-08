"""Ledger state machine, nonce consumption, and single-writer constraint."""

from __future__ import annotations

from pathlib import Path

import pytest

from steam_agent.execution.ledger import (
    ConfirmationRejected,
    ExecutionLedger,
    InvalidTransition,
    LedgerError,
)


@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    instance = ExecutionLedger(tmp_path / "ledger.sqlite3")
    yield instance
    instance.close()


def _request(
    ledger: ExecutionLedger,
    machine: str = "herb",
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
        machine_id="herb",
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

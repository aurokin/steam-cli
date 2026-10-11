"""Explicit broker result serialization; durable state is never an attempt outcome."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
import sqlite3
import sys

from steam_agent import __version__
from steam_agent.execution.ledger import ExecutionLedger, LedgerError, TERMINAL_STATES

_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_PRIVATE_KEYS = frozenset({
    "path", "paths", "account", "account_alias", "steam_login", "username", "user_sid",
})


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items() if key not in _PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?:[A-Za-z]:[\\/]|/)[^\s\"']+[\\/][^\s\"']*", "<path>", value)
    return value


def reason_code(message: str | None) -> str | None:
    """A bounded vocabulary; exception text never becomes public error detail."""
    if message is None:
        return None
    lowered = message.lower()
    if "not initialized" in lowered:
        return "broker_not_initialized"
    if "broker.json is corrupt" in lowered:
        return "broker_config_invalid"
    if "different machine" in lowered or "expected machine" in lowered:
        return "machine_mismatch"
    if "not found" in lowered or "does not exist" in lowered:
        return "request_not_found"
    if "different plan content" in lowered or "bound to different" in lowered:
        return "request_conflict"
    if "policy" in lowered and ("unavailable" in lowered or "unreadable" in lowered):
        return "policy_unavailable"
    if "policy" in lowered or "allowlist" in lowered or "permitted" in lowered:
        return "policy_denied"
    if "provision" in lowered or "steam login" in lowered:
        return "not_provisioned"
    if "qualified" in lowered or "unsupported" in lowered:
        return "unsupported"
    if "locked" in lowered or "state unavailable" in lowered:
        return "state_unavailable"
    if "nonce" in lowered:
        return "confirmation_rejected"
    if "filesystem" in lowered:
        return "filesystem_unavailable"
    if "no authorized" in lowered:
        return "no_authorized_operation"
    return "invalid_request"


def serialize_result(
    payload: Mapping[str, object] | None, *, exit_code: int, command: str,
    state_dir: Path, machine_id: str | None, failure: str | None,
    request_id: str | None = None, operation_id: int | None = None,
) -> dict[str, object]:
    receipt: dict[str, object] | None = None
    if payload is not None:
        identifier = payload.get("operation_id")
        if isinstance(identifier, int) and not isinstance(identifier, bool):
            operation_id = identifier
    try:
        if operation_id is None and request_id is None:
            raise LedgerError("no receipt identifier")
        ledger = ExecutionLedger(state_dir / "ledger.sqlite3", readonly=True)
        try:
            operation = (
                ledger.get(operation_id) if operation_id is not None
                else ledger.lookup_request_id(request_id) if request_id is not None
                else None
            )
            if operation is not None:
                receipt = {
                    "operation_id": operation.operation_id,
                    "operation": operation.operation,
                    "appid": operation.appid,
                    "state": operation.state,
                    "terminal": operation.state in TERMINAL_STATES,
                    "dispatch_certainty": (
                        "acknowledged" if operation.state == "dispatched"
                        else "uncertain" if operation.state in {"dispatch_pending", "unconfirmed"}
                        else "not_established"
                    ),
                    "execution_window_expires_at": operation.execution_window_expires_at,
                }
        finally:
            ledger.close()
    except (OSError, sqlite3.Error, LedgerError):
        pass  # A missing receipt is information, never fabricated durable state.
    outcome: object = "refused" if failure is not None else "observed"
    code = reason_code(failure)
    if failure is None and payload is not None:
        if payload.get("schema") == "desktop-dispatch/0.1" and payload.get("state") == "unknown":
            outcome = "transport_unknown"
            code = payload.get("reason_code")
        elif payload.get("reused"):
            outcome = "receipt_reused"
        elif "outcome" in payload:
            outcome = payload["outcome"]
            if outcome == "deferred":
                code = "execution_deferred"
        elif command in {"request", "install", "verify", "launch", "stop", "confirm"}:
            outcome = "request_recorded"
        elif exit_code != 0:
            outcome = "unsuccessful"
    state = receipt.get("state") if receipt is not None else None
    category = "none"
    if state in TERMINAL_STATES:
        category = "new_intent_required"
    elif state == "pending_confirmation":
        category = "confirmation_required"
    elif state == "dispatch_pending":
        category = "reconcile_original"
    elif state == "interrupted":
        category = "reconcile_original"
    elif state == "authorized":
        category = "resume_original"
    elif state is not None:
        category = "inspect_original"
    if outcome == "transport_unknown":
        category = "collect_original_job" if payload is not None and payload.get("job_id") else "inspect_original"
    return {
        "schema": "broker-result/0.1",
        "context": {
            "broker_version": __version__, "platform": sys.platform,
            "machine_id": machine_id if machine_id and _ALIAS.fullmatch(machine_id) else "unknown",
        },
        "receipt": receipt,
        "attempt": {"outcome": outcome, "reason_code": code},
        "retry": {"category": category, "automatic": False},
        "result": redact(dict(payload)) if payload is not None else None,
    }

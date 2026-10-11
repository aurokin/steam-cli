"""Client-plane operations share the broker's ledger and machine execution lock."""

from __future__ import annotations

import sys
import sqlite3

from steam_agent.execution.executor import ExecutionReport, Executor
from steam_agent.execution.ledger import LedgerError
from steam_agent.execution.policy import PolicyError, load_policy
from steam_agent.execution.runtime import RuntimeObserver


class ClientExecutor(Executor):
    """Add attributed game stopping without granting content-plane authority."""

    def _execute_locked(self, operation_id: int) -> ExecutionReport:
        operation = self._ledger.get(operation_id)
        if operation.operation == "launch" and sys.platform not in {"linux", "win32"}:
            return ExecutionReport(
                operation_id, "deferred", "client execution requires Windows or Linux"
            )
        if operation.operation != "stop":
            if sys.platform != "linux" and operation.operation in {"install", "verify"}:
                return ExecutionReport(
                    operation_id,
                    "deferred",
                    "content execution requires a qualified Linux host",
                )
            return super()._execute_locked(operation_id)
        if operation.state != "authorized":
            return ExecutionReport(
                operation_id, "aborted", f"not executable: {operation.state}"
            )
        if not self._ledger.window_valid(operation_id):
            self._ledger.expire_lapsed()
            return ExecutionReport(operation_id, "aborted", "execution window lapsed")
        plan = self._ledger.plan_document(operation_id)
        force = plan.get("force", False)
        if not isinstance(force, bool):
            return ExecutionReport(operation_id, "deferred", "malformed force request")
        observer = RuntimeObserver()
        observed = observer.observe(operation.appid)
        if observed.state == "unknown" and not observed.processes:
            return ExecutionReport(
                operation_id, "deferred", "runtime observation unknown"
            )
        # Re-read immediately before the durable side-effect intent. The broker
        # also checks at intake, confirmation and run; edits revoke queued work.
        try:
            policy = load_policy(self._state_dir / "policy.toml")
        except PolicyError:
            return ExecutionReport(
                operation_id, "deferred", "policy unreadable before stop"
            )
        if policy.grant_for("stop") == "deny" or not policy.stop_permitted(
            operation.appid, force=force
        ):
            self._ledger.transition(
                operation_id, "aborted", detail="stop policy revoked"
            )
            return ExecutionReport(operation_id, "aborted", "stop policy revoked")
        if not self._ledger.window_valid(operation_id):
            self._ledger.expire_lapsed()
            return ExecutionReport(operation_id, "aborted", "execution window lapsed")
        self._ledger.transition(
            operation_id, "dispatch_pending", expected_from="authorized"
        )

        def before_request() -> bool:
            # The runtime's fresh scan can be slow, and a multi-process game
            # needs the gate checked before every individual exit request.
            # Keep dispatch_pending durable even when the window has lapsed.
            try:
                current = load_policy(self._state_dir / "policy.toml")
                return (
                    current.grant_for("stop") != "deny"
                    and current.stop_permitted(operation.appid, force=force)
                    and self._ledger.window_valid(operation_id)
                )
            except (PolicyError, LedgerError, sqlite3.Error, OSError):
                return False

        result = observer.stop_app(
            operation.appid, force=force, before_request=before_request,
        )
        # Once a signal may have been sent, never replay this request. A new
        # request ID represents a deliberate new attempt, including escalation.
        outcome = (
            "dispatched" if result.state in {"stopped", "absent"} else "unconfirmed"
        )
        detail = f"stop {result.state}: {result.reason_code}; requested {result.requested_count} attributed processes"
        self._ledger.transition(operation_id, outcome, detail=detail)
        return ExecutionReport(operation_id, outcome, detail)

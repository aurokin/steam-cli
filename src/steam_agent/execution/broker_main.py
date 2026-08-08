"""``steam-agent-broker``: the provisioned execution entry point (ADR 0027).

Installing and invoking this command under a dedicated OS identity IS the
provisioning act.  The inert planner (``steam-agent``) never imports this
module.  Subcommands:

- ``init``       scaffold the state directory and a deny-all policy template
- ``request``    accept one operation-plan JSON on stdin, mint a nonce
- ``confirm``    consume a nonce with an actor identity
- ``run``        execute the single authorized (or resumable) operation
- ``reconcile``  map any non-terminal operation to its one recovery action
- ``status``     print the active operation and recent events

Diagnostics go to stderr; stdout is deterministic JSON.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from steam_agent.execution.content_plane import SteamcmdAdapter
from steam_agent.execution.executor import Executor, ExecutorLockedError
from steam_agent.execution.ledger import (
    ConfirmationRejected,
    ExecutionLedger,
    LedgerError,
)
from steam_agent.execution.linux_session import LinuxSession
from steam_agent.execution.policy import (
    PolicyError,
    load_policy,
    write_policy_template,
)

_SUPPORTED_OPERATIONS = frozenset({"install"})


def _default_state_dir() -> Path:
    override = os.environ.get("STEAM_BROKER_STATE")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "steam-broker"


def _emit(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


def _fail(message: str) -> int:
    print(f"steam-agent-broker: {message}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steam-agent-broker",
        description="Provisioned execution broker (ADR 0027, Phase 1).",
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Scaffold state dir and policy.")
    init.add_argument("--library", type=Path, required=True)
    init.add_argument("--steamcmd", type=Path, required=True)

    request = commands.add_parser(
        "request", help="Submit one operation-plan JSON on stdin."
    )
    request.add_argument("--account", required=True)

    confirm = commands.add_parser("confirm", help="Consume a nonce.")
    confirm.add_argument("nonce")
    confirm.add_argument("--actor", required=True)

    run = commands.add_parser("run", help="Execute the authorized operation.")
    run.add_argument("--operation-id", type=int, default=None)

    commands.add_parser("reconcile", help="Recover non-terminal operations.")
    commands.add_parser("status", help="Show active operation and events.")
    return parser


def _load_config(state_dir: Path) -> dict[str, str]:
    config_path = state_dir / "broker.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise PolicyError("broker not initialized; run init first") from error


def _components(state_dir: Path) -> tuple[ExecutionLedger, Executor]:
    config = _load_config(state_dir)
    library = Path(config["library"])
    ledger = ExecutionLedger(state_dir / "ledger.sqlite3")
    session = LinuxSession(library=library)
    content = SteamcmdAdapter(
        steamcmd_script=Path(config["steamcmd"]),
        private_home=state_dir / "steamcmd-home",
        log_dir=state_dir / "logs",
    )
    executor = Executor(
        ledger=ledger,
        session=session,
        content=content,
        library=library,
        state_dir=state_dir,
    )
    return ledger, executor


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    state_dir = arguments.state_dir or _default_state_dir()

    if arguments.command == "init":
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            write_policy_template(state_dir / "policy.toml")
        except PolicyError as error:
            return _fail(str(error))
        (state_dir / "broker.json").write_text(
            json.dumps(
                {
                    "library": str(arguments.library),
                    "steamcmd": str(arguments.steamcmd),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        _emit({"initialized": True, "policy": "deny-all template written"})
        return 0

    try:
        policy = load_policy(state_dir / "policy.toml")
    except PolicyError as error:
        return _fail(str(error))

    if arguments.command == "request":
        try:
            plan = json.load(sys.stdin)
        except json.JSONDecodeError:
            return _fail("stdin is not a JSON operation plan")
        if plan.get("schema") != "operation-plan/0.1":
            return _fail("unsupported plan schema")
        operation = str(plan.get("operation", ""))
        if operation not in _SUPPORTED_OPERATIONS:
            return _fail(f"operation {operation!r} is not executable in Phase 1")
        if policy.grant_for(operation) != "confirm":
            return _fail(f"policy denies {operation!r}")
        target = plan.get("target", {})
        if not isinstance(target, dict):
            return _fail("plan target is malformed")
        ledger, _ = _components(state_dir)
        try:
            operation_id, nonce = ledger.request(
                plan_key=str(plan.get("idempotency_key", "")),
                plan_document=json.dumps(plan, sort_keys=True),
                operation=operation,
                appid=int(target.get("appid", 0)),
                account_alias=str(arguments.account),
                machine_id=str(target.get("machine_id", "")),
                policy_version=policy.version,
            )
        except LedgerError as error:
            return _fail(str(error))
        _emit(
            {
                "operation_id": operation_id,
                "nonce": nonce,
                "state": "pending_confirmation",
            }
        )
        return 0

    ledger, executor = _components(state_dir)

    if arguments.command == "confirm":
        try:
            operation_id = ledger.confirm(
                nonce=arguments.nonce, actor=arguments.actor
            )
        except ConfirmationRejected as error:
            return _fail(str(error))
        _emit({"operation_id": operation_id, "state": "authorized"})
        return 0

    if arguments.command == "run":
        operation_id = arguments.operation_id
        if operation_id is None:
            active = ledger.active()
            if active is None or active.state not in {"authorized", "interrupted"}:
                return _fail("no authorized operation to run")
            operation_id = active.operation_id
        try:
            report = executor.execute(operation_id)
        except ExecutorLockedError as error:
            return _fail(str(error))
        _emit(
            {
                "operation_id": report.operation_id,
                "outcome": report.outcome,
                "detail": report.detail,
            }
        )
        return 0 if report.outcome == "confirmed" else 1

    if arguments.command == "reconcile":
        _emit({"actions": executor.reconcile()})
        return 0

    if arguments.command == "status":
        active = ledger.active()
        if active is None:
            _emit({"active": None})
            return 0
        _emit(
            {
                "active": {
                    "operation_id": active.operation_id,
                    "operation": active.operation,
                    "appid": active.appid,
                    "state": active.state,
                    "events": ledger.events(active.operation_id),
                }
            }
        )
        return 0

    return _fail("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())

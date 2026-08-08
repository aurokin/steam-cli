"""``steam-agent-broker``: the execution entry point (ADR 0027/0028).

The broker runs as the desktop user and is driven directly by the trusted
manager agent (single-identity model, ADR 0028).  The inert planner
(``steam-agent``) never imports this module.  Subcommands:

- ``init``       scaffold the state directory and a deny-all policy template
- ``request``    accept one operation-plan JSON on stdin; under an
                 ``allow`` grant the request may auto-authorize in-process
- ``confirm``    consume a nonce with an actor identity
- ``run``        execute the single authorized (or resumable) operation
- ``reconcile``  map any non-terminal operation to its one recovery action
- ``status``     print the active operation and recent events

Diagnostics go to stderr; stdout is deterministic JSON.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys

from steam_agent.execution.content_plane import SteamcmdAdapter
from steam_agent.execution.executor import (
    Executor,
    ExecutorLockedError,
    safe_install_dir_name,
)
from steam_agent.execution.ledger import (
    ConfirmationRejected,
    ExecutionLedger,
    InvalidTransition,
    LedgerError,
)
from steam_agent.execution.linux_session import LinuxSession
from steam_agent.execution.policy import (
    PolicyError,
    load_policy,
    write_policy_template,
)

_SUPPORTED_OPERATIONS = frozenset({"install", "verify", "launch"})
# Generous for a plan (well under a page of JSON in practice).
_PLAN_BYTE_LIMIT = 64 * 1024


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


@contextmanager
def _state_lock(state_dir: Path):
    """Serialize reconfiguration with intake (broker-owned dir; blocking)."""

    # Owner-only from the first moment it exists: mkdir's mode is masked by
    # the umask (never widened), so 0o700 closes the pre-chmod window a
    # permissive umask would otherwise open for another local user to
    # pre-create policy/state.  The chmod still repairs a pre-existing dir.
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    handle = (state_dir / "state.lock").open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def _floor_denial(library: Path, min_free_gb: int | None) -> str | None:
    """Reason the disk floor blocks auto-confirmation, or None if it passes.

    ``load_policy`` guarantees a floor exists whenever any grant is
    ``allow``, so a missing floor here is a defect, not a passing check.
    """

    if min_free_gb is None:
        return "no disk floor configured"
    try:
        free = shutil.disk_usage(library / "steamapps").free
    except OSError:
        return "free disk space could not be measured"
    if free < min_free_gb * 10**9:
        return f"free disk {free // 10**9} GB below the {min_free_gb} GB floor"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steam-agent-broker",
        description="Execution broker (ADR 0027/0028).",
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Scaffold state dir and policy.")
    init.add_argument("--library", type=Path, required=True)
    init.add_argument("--steamcmd", type=Path, required=True)
    init.add_argument("--machine-id", default=None)

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
    status = commands.add_parser(
        "status", help="Show active operation, events, and recent outcomes."
    )
    status.add_argument("--limit", type=int, default=5)
    commands.add_parser("policy", help="Show the effective execution policy.")
    return parser


def _load_config(state_dir: Path) -> dict[str, str]:
    config_path = state_dir / "broker.json"
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PolicyError("broker not initialized; run init first") from error
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PolicyError("broker.json is corrupt; rerun init") from error
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("library"), str)
        or not isinstance(config.get("steamcmd"), str)
    ):
        raise PolicyError("broker.json is corrupt; rerun init")
    return config


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
        with _state_lock(state_dir):
            # Reconfiguring while an operation is active could point a
            # confirmed plan at a different library, script, or machine key.
            # The state lock serializes this check-and-write with intake.
            if (state_dir / "ledger.sqlite3").exists():
                ledger = ExecutionLedger(state_dir / "ledger.sqlite3")
                ledger.expire_lapsed()  # a lapsed row must not block re-init
                active = ledger.active()
                ledger.close()
                if active is not None:
                    return _fail(
                        "an operation is active; refusing to reconfigure the broker"
                    )
            # Ledger, policy, logs, and the steamcmd credential cache live
            # here; never trust the umask to keep other local users out.
            state_dir.chmod(0o700)
            # An omitted --machine-id preserves the configured identity;
            # re-init must never silently retarget the single-active key.
            machine_id = arguments.machine_id
            if machine_id is None:
                if (state_dir / "broker.json").exists():
                    # A corrupt config must not silently retarget the
                    # single-active key to the default: repairing an
                    # existing broker requires reasserting its identity.
                    try:
                        machine_id = _load_config(state_dir).get(
                            "machine_id", "local"
                        )
                    except PolicyError:
                        return _fail(
                            "broker.json is corrupt; rerun init with"
                            " --machine-id to reassert this broker's identity"
                        )
                else:
                    machine_id = "local"
            # Idempotent re-init: keep an existing (possibly owner-edited)
            # policy rather than failing, so a partial init is repairable.
            wrote_template = False
            if not (state_dir / "policy.toml").exists():
                try:
                    write_policy_template(state_dir / "policy.toml")
                except PolicyError as error:
                    return _fail(str(error))
                wrote_template = True
            config_temporary = state_dir / "broker.json.tmp"
            config_temporary.write_text(
                json.dumps(
                    {
                        # Absolute paths: later commands run from any cwd.
                        "library": str(arguments.library.resolve()),
                        "steamcmd": str(arguments.steamcmd.resolve()),
                        "machine_id": str(machine_id),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            # Atomic: an interrupted init must never leave partial JSON.
            config_temporary.replace(state_dir / "broker.json")
        _emit(
            {
                "initialized": True,
                "policy": (
                    "deny-all template written"
                    if wrote_template
                    else "existing policy retained"
                ),
            }
        )
        return 0

    # Policy gates intake and execution only, and is re-read at each
    # decision point so file edits take effect immediately.  reconcile and
    # status never load it: recovery (client restoration, journal repair)
    # can never be blocked behind policy repair.
    if arguments.command == "request":
        try:
            policy = load_policy(state_dir / "policy.toml")
        except PolicyError as error:
            return _fail(str(error))
        # Bounded read: the whole plan (unknown fields included) is later
        # persisted into the ledger, so an unbounded stream could exhaust
        # memory or state-disk space.
        raw_plan = sys.stdin.read(_PLAN_BYTE_LIMIT + 1)
        if len(raw_plan) > _PLAN_BYTE_LIMIT:
            return _fail("operation plan exceeds the size limit")
        try:
            plan = json.loads(raw_plan)
        except json.JSONDecodeError:
            return _fail("stdin is not a JSON operation plan")
        if not isinstance(plan, dict):
            return _fail("operation plan must be a JSON object")
        if plan.get("schema") != "operation-plan/0.1":
            return _fail("unsupported plan schema")
        operation = str(plan.get("operation", ""))
        if operation not in _SUPPORTED_OPERATIONS:
            return _fail(f"operation {operation!r} is not executable")
        if policy.grant_for(operation) not in {"confirm", "allow"}:
            return _fail(f"policy denies {operation!r}")
        target = plan.get("target", {})
        if not isinstance(target, dict):
            return _fail("plan target is malformed")
        install_dir_name = plan.get("install_dir_name")
        # Empty means unspecified, same as absent: the executor falls back
        # to the existing manifest's directory or app_<appid>.
        if install_dir_name is not None and install_dir_name != "" and (
            not isinstance(install_dir_name, str)
            or not safe_install_dir_name(install_dir_name)
        ):
            return _fail("install_dir_name must be a string path component")
        idempotency_key = plan.get("idempotency_key")
        # A string only, with sane bounds: a missing/empty key would let a
        # keyless plan execute and then permanently poison the ledger's
        # unique key index; non-strings (1 vs "1") would collapse distinct
        # plans into one key.
        if not isinstance(idempotency_key, str) or not (
            8 <= len(idempotency_key) <= 128
        ):
            return _fail("plan idempotency_key must be a string of 8-128 characters")
        appid = target.get("appid")
        # A JSON integer only: int() would silently coerce 480.9 or true
        # into a different AppID than the plan the human confirms.  Steam
        # AppIDs are 32-bit; anything larger is malformed (and would
        # overflow the ledger's INTEGER column).
        if (
            not isinstance(appid, int)
            or isinstance(appid, bool)
            or appid <= 0
            or appid > 0xFFFFFFFF
        ):
            return _fail("plan target appid is malformed")
        if operation == "launch" and not policy.launch_permitted(appid):
            return _fail("appid is not on the launch allowlist")
        with _state_lock(state_dir):
            try:
                config = _load_config(state_dir)
            except PolicyError as error:
                return _fail(str(error))
            # The single-active constraint is keyed by machine_id; it must
            # come from broker configuration, never the submitted plan.
            machine_id = str(config.get("machine_id", "local"))
            plan_machine = target.get("machine_id")
            if plan_machine is not None and str(plan_machine) != machine_id:
                return _fail("plan targets a different machine than this broker")
            ledger, _ = _components(state_dir)
            try:
                operation_id, nonce = ledger.request(
                    plan_key=idempotency_key,
                    plan_document=json.dumps(plan, sort_keys=True),
                    operation=operation,
                    appid=appid,
                    account_alias=str(arguments.account),
                    machine_id=machine_id,
                    policy_version=policy.version,
                )
            except LedgerError as error:
                return _fail(str(error))
            payload: dict[str, object] = {
                "operation_id": operation_id,
                "nonce": nonce,
                "state": "pending_confirmation",
            }
            # Auto-confirmation (ADR 0028): re-read the policy under the
            # state lock so the grant and version recorded as the
            # authorizing actor are the ones in effect now, not the
            # pre-lock intake read.  An unreadable re-read degrades to the
            # ordinary pending flow; the confirm verb governs from there.
            try:
                current = load_policy(state_dir / "policy.toml")
            except PolicyError:
                current = None
            if (
                current is not None
                and current.grant_for(operation) == "allow"
                and (operation != "launch" or current.launch_permitted(appid))
            ):
                denied = _floor_denial(
                    Path(config["library"]), current.min_free_gb
                )
                if denied is not None:
                    # Degraded, not dead: the emitted nonce stays
                    # explicitly confirmable until it expires.
                    payload["auto_confirm_denied"] = denied
                else:
                    try:
                        ledger.confirm(
                            nonce=nonce, actor=f"policy:{current.version}"
                        )
                    except ConfirmationRejected as error:
                        return _fail(str(error))
                    payload = {
                        "operation_id": operation_id,
                        "state": "authorized",
                    }
        _emit(payload)
        return 0

    if arguments.command == "policy":
        # Read-only visibility for the manager agent; an unreadable policy
        # is itself information (execution is denied until repaired).
        try:
            policy = load_policy(state_dir / "policy.toml")
        except PolicyError as error:
            return _fail(str(error))
        _emit(
            {
                "version": policy.version,
                "grants": policy.grants,
                "limits": {"min_free_gb": policy.min_free_gb},
                "launch_allowlist": sorted(policy.launch_allowlist),
            }
        )
        return 0

    try:
        ledger, executor = _components(state_dir)
    except PolicyError as error:
        return _fail(str(error))

    if arguments.command == "confirm":
        try:
            operation_id = ledger.confirm(
                nonce=arguments.nonce, actor=arguments.actor
            )
        except ConfirmationRejected as error:
            return _fail(str(error))
        # Re-check the policy, read at this decision point: revoking a grant
        # must dead-end a nonce minted while the grant was still active.
        operation = ledger.get(operation_id)
        # expected_from: a concurrent run may already have advanced this
        # row past authorized; terminalizing an in-flight execution would
        # strand its stopped client (terminal rows are invisible to
        # reconciliation).  The run path re-checks policy itself.
        try:
            policy = load_policy(state_dir / "policy.toml")
        except PolicyError as error:
            try:
                ledger.transition(
                    operation_id,
                    "aborted",
                    detail="policy unreadable at confirmation",
                    expected_from="authorized",
                )
            except InvalidTransition:
                pass  # already executing; run enforces policy from here
            return _fail(str(error))
        if policy.grant_for(operation.operation) not in {
            "confirm",
            "allow",
        } or (
            operation.operation == "launch"
            and not policy.launch_permitted(operation.appid)
        ):
            try:
                ledger.transition(
                    operation_id,
                    "aborted",
                    detail="policy revoked before confirmation",
                    expected_from="authorized",
                )
            except InvalidTransition:
                pass  # already executing; run enforces policy from here
            return _fail(f"policy now denies {operation.operation!r}")
        _emit({"operation_id": operation_id, "state": "authorized"})
        return 0

    if arguments.command == "run":
        operation_id = arguments.operation_id
        if operation_id is None:
            ledger.expire_lapsed()
            active = ledger.active()
            if active is None or active.state not in {"authorized", "interrupted"}:
                return _fail("no authorized operation to run")
            operation_id = active.operation_id
        try:
            operation = ledger.get(operation_id)
        except LedgerError as error:
            return _fail(str(error))
        # An unreadable policy follows the same restoration-and-abort path
        # as an explicit denial: either way execution is not permitted, and
        # an interrupted operation's stopped client must not stay stopped
        # until someone repairs the policy file.
        denial: str | None = None
        try:
            policy = load_policy(state_dir / "policy.toml")
        except PolicyError as error:
            denial = str(error)
            denial_detail = "policy unreadable before execution"
        else:
            if policy.grant_for(operation.operation) not in {
                "confirm",
                "allow",
            } or (
                operation.operation == "launch"
                and not policy.launch_permitted(operation.appid)
            ):
                # Removing an AppID from the allowlist revokes it as surely
                # as flipping the grant; both are re-read at every decision
                # point, including this one.
                denial = f"policy now denies {operation.operation!r}"
                denial_detail = "policy revoked before execution"
        if denial is not None and operation.state not in {
            "authorized",
            "interrupted",
        }:
            return _fail(denial)
        if denial is not None:
            # An interrupted operation may have left the client stopped;
            # restore before terminalizing (terminal rows are invisible to
            # reconciliation) and leave the state for retry if that fails.
            try:
                released = executor.release_client(operation_id)
            except ExecutorLockedError as error:
                return _fail(str(error))
            if not released:
                return _fail(
                    f"{denial};"
                    " client restore failed; operation left for reconcile"
                )
            try:
                ledger.transition(
                    operation_id,
                    "aborted",
                    detail=denial_detail,
                    expected_from=operation.state,
                )
            except InvalidTransition:
                return _fail(f"{denial}; operation state changed; not aborted")
            return _fail(denial)
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
        return 0 if report.outcome in {"confirmed", "dispatched"} else 1

    if arguments.command == "reconcile":
        try:
            _emit({"actions": executor.reconcile()})
        except ExecutorLockedError as error:
            return _fail(str(error))
        return 0

    if arguments.command == "status":
        # SQLite reads LIMIT -1 as unbounded; keep the recent list bounded.
        if arguments.limit < 0:
            return _fail("--limit must be non-negative")
        ledger.expire_lapsed()
        active = ledger.active()
        recent = [
            {
                "operation_id": row[0],
                "operation": row[1],
                "appid": row[2],
                "state": row[3],
                "detail": row[4],
                "updated_at": row[5],
            }
            for row in ledger.recent(arguments.limit)
        ]
        if active is None:
            _emit({"active": None, "recent": recent})
            return 0
        _emit(
            {
                "active": {
                    "operation_id": active.operation_id,
                    "operation": active.operation,
                    "appid": active.appid,
                    "state": active.state,
                    "detail": active.detail,
                    "execution_window_expires_at": (
                        active.execution_window_expires_at
                    ),
                    "events": ledger.events(active.operation_id),
                },
                "recent": recent,
            }
        )
        return 0

    return _fail("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())

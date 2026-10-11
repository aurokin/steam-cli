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
from datetime import datetime, timezone
from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
from contextvars import ContextVar
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import TYPE_CHECKING

from steam_agent import __version__
from steam_agent.execution.agent_requests import (
    DirectRequest,
    record_or_reuse,
    require_content_provisioning,
)
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
    LedgerOperation,
    UnsupportedLedger,
)
from steam_agent.execution.linux_session import LinuxSession
from steam_agent.execution.windows_session import WindowsSession
from steam_agent.execution.client_executor import ClientExecutor
from steam_agent.execution.runtime import RuntimeObserver
from steam_agent.execution.locking import FileLockError, acquire_file_lock
from steam_agent.execution.policy import (
    PolicyError,
    load_policy,
    write_policy_template,
)

if TYPE_CHECKING:
    from steam_agent.execution.desktop_bridge import NativeTaskScheduler
    from steam_agent.execution.windows_filesystem import WindowsFilesystem

_SUPPORTED_OPERATIONS = frozenset({"install", "verify", "launch", "stop"})
# Generous for a plan (well under a page of JSON in practice).
_PLAN_BYTE_LIMIT = 64 * 1024
_failure: ContextVar[str | None] = ContextVar("broker_failure", default=None)


def _default_state_dir() -> Path:
    override = os.environ.get("STEAM_BROKER_STATE")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "steam-broker"


def _emit(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


def _fail(message: str) -> int:
    _failure.set(message)
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
    handle = acquire_file_lock(state_dir / "state.lock", blocking=True)
    try:
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
    parser.add_argument("--format", choices=("json",), default=None,
                        help="Select the versioned broker-result/0.1 envelope.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--expected-machine", default=None,
                        help="Refuse direct actions on a different configured machine alias.")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Experimental Windows same-user interactive desktop bridge.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Scaffold state dir and policy.")
    init.add_argument("--library", type=Path, required=True)
    init.add_argument("--steamcmd", type=Path, default=None)
    init.add_argument("--account", default=None)
    init.add_argument("--steam-login", default=None)
    init.add_argument("--steam-executable", type=Path, default=None)
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
    for operation in sorted(_SUPPORTED_OPERATIONS):
        direct = commands.add_parser(
            operation, help=f"Request {operation}; execute when authorized."
        )
        direct.add_argument("appid", type=int)
        direct.add_argument("--request-id", required=True)
        direct.add_argument("--account", default=None)
        direct.add_argument("--request-only", action="store_true")
        if operation == "stop":
            direct.add_argument("--force", action="store_true")
    runtime = commands.add_parser(
        "runtime", help="Observe attributed game processes on this host."
    )
    runtime.add_argument("--appid", type=int, default=None)
    inspect = commands.add_parser("inspect", help="Inspect one durable operation.")
    inspect.add_argument("operation_id", type=int, nargs="?")
    inspect.add_argument("--request-id", default=None)
    commands.add_parser(
        "capabilities", help="Show host execution support and policy grants."
    )
    jobs = commands.add_parser("desktop-jobs", help="Discover original desktop jobs read-only.")
    jobs.add_argument("--limit", type=int, default=20)
    collect = commands.add_parser("desktop-collect", help="Collect an original desktop job receipt.")
    collect.add_argument("job_id")
    collect.add_argument("--cleanup", action="store_true",
                         help="Remove only a completed, exactly owned job with a receipt.")
    preflight = commands.add_parser("preflight", help="Observe advisory operation readiness without authorization.")
    preflight.add_argument("operation", choices=sorted(_SUPPORTED_OPERATIONS))
    preflight.add_argument("appid", type=int)
    preflight.add_argument("--account", default=None)
    preflight.add_argument("--force", action="store_true")
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
    if any(
        key in config and not isinstance(config[key], str)
        for key in ("account_alias", "steam_login", "steam_executable", "machine_id")
    ):
        raise PolicyError("broker.json is corrupt; rerun init")
    return config


def _provision_windows_init_state(
    state_dir: Path, *, filesystem: WindowsFilesystem | None = None,
    scheduler: NativeTaskScheduler | None = None,
) -> None:
    """Provision only init's chosen private tree and known immediate state files.

    NTFS handle identities reject reparse points and multiply-linked files.
    Existing ancestors are inspected, never repermissioned. Existing nonempty
    targets require a valid broker configuration before any ACL is changed.
    Credential caches and other nested state need separate provisioning.
    """
    from steam_agent.execution.desktop_bridge import DesktopBridgeError, NativeTaskScheduler
    from steam_agent.execution.windows_filesystem import WindowsFilesystem, WindowsFilesystemError

    state_dir = state_dir.absolute()
    known = (
        "broker.json", "policy.toml", "ledger.sqlite3", "ledger.sqlite3-wal",
        "ledger.sqlite3-shm", "state.lock",
    )
    try:
        files = filesystem if filesystem is not None else WindowsFilesystem()
        acl = scheduler if scheduler is not None else NativeTaskScheduler()
        missing: list[Path] = []
        existing = state_dir
        while not existing.exists():
            missing.append(existing)
            existing = existing.parent
        files.identity(existing, directory=True)
        owned_files: list[Path] = []
        if not missing:
            entries = []
            for index, entry in enumerate(state_dir.iterdir()):
                if index >= 1000:
                    raise PolicyError("Windows broker state requires explicit private provisioning")
                entries.append(entry)
            for name in known:
                path = state_dir / name
                if path.exists() or path.is_symlink():
                    files.identity(path, directory=False)
                    owned_files.append(path)
            if entries:
                _load_config(state_dir)
        for path in reversed(missing):
            path.mkdir(mode=0o700)
            files.identity(path, directory=True)
            acl.protect_directory(path)
        if not missing:
            acl.protect_directory(state_dir)
        for path in owned_files:
            acl.protect_directory(path)
    except (DesktopBridgeError, WindowsFilesystemError, OSError, PolicyError):
        raise PolicyError("Windows broker state requires explicit private provisioning") from None


def _dispatch_denial(state_dir: Path, operation: LedgerOperation) -> str | None:
    try:
        policy = load_policy(state_dir / "policy.toml")
    except PolicyError:
        return "policy unreadable before dispatch"
    if policy.grant_for(operation.operation) == "deny":
        return "policy revoked before dispatch"
    if operation.operation == "launch" and not policy.launch_permitted(operation.appid):
        return "launch allowlist revoked before dispatch"
    return None


def _components(state_dir: Path) -> tuple[ExecutionLedger, Executor]:
    config = _load_config(state_dir)
    library = Path(config["library"])
    ledger = ExecutionLedger(state_dir / "ledger.sqlite3")
    session = (
        WindowsSession(
            library=library,
            steam_executable=Path(config["steam_executable"])
            if config.get("steam_executable")
            else None,
        )
        if sys.platform == "win32"
        else LinuxSession(library=library)
    )
    content = (
        SteamcmdAdapter(
            steamcmd_script=Path(config["steamcmd"]),
            private_home=state_dir / "steamcmd-home",
            log_dir=state_dir / "logs",
        )
        if config["steamcmd"]
        else None
    )
    executor = ClientExecutor(
        ledger=ledger,
        session=session,
        content=content,
        library=library,
        state_dir=state_dir,
        dispatch_policy=lambda operation: _dispatch_denial(state_dir, operation),
        account_login=lambda alias: (
            config.get("steam_login") if alias == config.get("account_alias") else None
        ),
    )
    return ledger, executor


def _main(argv: list[str] | None = None) -> int:
    command_line = list(argv) if argv is not None else sys.argv[1:]
    arguments = build_parser().parse_args(command_line)
    state_dir = arguments.state_dir or _default_state_dir()
    if arguments.command in {"desktop-jobs", "desktop-collect"}:
        from steam_agent.execution.desktop_bridge import (
            DesktopBridgeError, discover_desktop_jobs, inspect_desktop_job,
        )

        try:
            if arguments.command == "desktop-jobs":
                _emit(discover_desktop_jobs(state_dir, limit=arguments.limit))
            else:
                _emit(inspect_desktop_job(state_dir, arguments.job_id, cleanup=arguments.cleanup))
        except DesktopBridgeError:
            return _fail("desktop job inspection unavailable")
        return 0
    if arguments.desktop:
        from steam_agent.execution.desktop_bridge import dispatch_in_desktop

        forwarded: list[str] = []
        index = 0
        while index < len(command_line):
            argument = command_line[index]
            if argument == "--desktop":
                index += 1
                continue
            if argument == "--state-dir":
                index += 2
                continue
            if argument.startswith("--state-dir="):
                index += 1
                continue
            forwarded.append(argument)
            index += 1
        result = dispatch_in_desktop(forwarded, state_dir)
        payload = result.payload
        legacy = payload.get("result")
        if (arguments.format is None and payload.get("schema") == "broker-result/0.1"
                and isinstance(legacy, dict)):
            payload = legacy
        _emit(payload)
        return result.exit_code

    if arguments.command == "preflight":
        if not 1 <= arguments.appid <= 0xFFFFFFFF or (arguments.force and arguments.operation != "stop"):
            return _fail("preflight target is malformed")
        from steam_agent.execution.readiness import operation_preflight

        config = _load_config(state_dir)
        if (arguments.expected_machine is not None
                and arguments.expected_machine != config.get("machine_id", "local")):
            return _fail("expected machine differs from this broker")
        policy = load_policy(state_dir / "policy.toml")
        session = (
            WindowsSession(
                library=Path(config["library"]),
                steam_executable=Path(config["steam_executable"]) if config.get("steam_executable") else None,
            ) if sys.platform == "win32"
            else LinuxSession(library=Path(config["library"])) if sys.platform == "linux"
            else None
        )
        _emit(operation_preflight(
            config, policy, DirectRequest(arguments.operation, arguments.appid, "preflight-only",
                                         account_alias=arguments.account, force=arguments.force),
            platform=sys.platform, session=session, disk_floor=_floor_denial,
        ))
        return 0

    if arguments.command in _SUPPORTED_OPERATIONS:
        # Check identity before even opening the writable ledger or acquiring
        # the state lock. Aliases guard routing mistakes, not host attestation.
        config = _load_config(state_dir)
        if (arguments.expected_machine is not None
                and arguments.expected_machine != config.get("machine_id", "local")):
            return _fail("expected machine differs from this broker")
        with _state_lock(state_dir):
            config = _load_config(state_dir)
            if (arguments.expected_machine is not None
                    and arguments.expected_machine != config.get("machine_id", "local")):
                return _fail("expected machine differs from this broker")
            ledger = ExecutionLedger(state_dir / "ledger.sqlite3")
            try:
                payload = record_or_reuse(
                    ledger,
                    config,
                    state_dir / "policy.toml",
                    DirectRequest(
                        operation=arguments.command,
                        appid=arguments.appid,
                        request_id=arguments.request_id,
                        account_alias=arguments.account,
                        force=getattr(arguments, "force", False),
                    ),
                    platform=sys.platform,
                    disk_floor=_floor_denial,
                )
            except (PolicyError, LedgerError) as error:
                return _fail(str(error))
            finally:
                ledger.close()
        if payload["state"] in {"authorized", "interrupted"} and not arguments.request_only:
            return _main([
                "--state-dir", str(state_dir), "run",
                "--operation-id", str(payload["operation_id"]),
            ])
        _emit(payload)
        return 0 if payload["state"] in {
            "pending_confirmation", "authorized", "confirmed", "dispatched",
        } else 1

    if arguments.command in {"runtime", "capabilities"}:
        if (
            getattr(arguments, "appid", None) is not None
            and not 1 <= arguments.appid <= 0xFFFFFFFF
        ):
            return _fail("appid is malformed")
        if arguments.command == "runtime":
            _emit(
                {
                    "schema": "runtime-observation/0.1",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    **RuntimeObserver().observe(arguments.appid).to_json(),
                }
            )
            return 0
        try:
            config = _load_config(state_dir)
            policy = load_policy(state_dir / "policy.toml")
        except PolicyError as error:
            return _fail(str(error))
        platform_supported = sys.platform in {"linux", "win32"}
        _emit(
            {
                "schema": "broker-capabilities/0.1",
                "platform": sys.platform,
                "machine_id": config.get("machine_id", "local"),
                "runtime": "implemented_live_unqualified"
                if platform_supported
                else "unsupported",
                "launch": {
                    "grant": policy.grant_for("launch"),
                    "allowed_appids": sorted(policy.launch_allowlist),
                },
                "stop": {
                    "grant": policy.grant_for("stop"),
                    "allowed_appids": sorted(policy.stop_allowlist),
                    "allow_force": policy.stop_allow_force,
                },
                "content": {
                    "platform_supported": sys.platform == "linux",
                    "provisioned": bool(
                        config.get("steamcmd") and config.get("steam_login")
                    ),
                    "install_grant": policy.grant_for("install"),
                    "verify_grant": policy.grant_for("verify"),
                },
            }
        )
        return 0

    if arguments.command == "init":
        if sys.platform == "win32":
            _provision_windows_init_state(state_dir)
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
                        machine_id = _load_config(state_dir).get("machine_id", "local")
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
            existing_config = {}
            if (state_dir / "broker.json").exists():
                try:
                    existing_config = _load_config(state_dir)
                except PolicyError:
                    pass  # explicit machine-id permits repairing corrupt config
            config_temporary = state_dir / "broker.json.tmp"
            config_temporary.write_text(
                json.dumps(
                    {
                        # Absolute paths: later commands run from any cwd.
                        "library": str(arguments.library.resolve()),
                        "steamcmd": str(arguments.steamcmd.resolve())
                        if arguments.steamcmd
                        else existing_config.get("steamcmd", ""),
                        "account_alias": arguments.account
                        or existing_config.get("account_alias", "desktop"),
                        "steam_login": arguments.steam_login
                        if arguments.steam_login is not None
                        else existing_config.get("steam_login", ""),
                        "steam_executable": str(arguments.steam_executable.resolve())
                        if arguments.steam_executable
                        else existing_config.get("steam_executable", ""),
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
        if plan.get("schema") not in {"operation-plan/0.1", "operation-intent/0.1"}:
            return _fail("unsupported plan schema")
        operation = str(plan.get("operation", ""))
        if operation not in _SUPPORTED_OPERATIONS:
            return _fail(f"operation {operation!r} is not executable")
        if policy.grant_for(operation) not in {"confirm", "allow"}:
            return _fail(f"policy denies {operation!r}")
        if operation == "stop" and plan.get("schema") != "operation-intent/0.1":
            return _fail("stop requires an operation-intent/0.1 request")
        if not isinstance(plan.get("force", False), bool):
            return _fail("force must be a boolean")
        if plan.get("force", False) and operation != "stop":
            return _fail("force is supported only for stop")
        target = plan.get("target", {})
        if not isinstance(target, dict):
            return _fail("plan target is malformed")
        install_dir_name = plan.get("install_dir_name")
        # Empty means unspecified, same as absent: the executor falls back
        # to the existing manifest's directory or app_<appid>.
        if (
            install_dir_name is not None
            and install_dir_name != ""
            and (
                not isinstance(install_dir_name, str)
                or not safe_install_dir_name(install_dir_name)
            )
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
        if operation == "stop" and not policy.stop_permitted(
            appid, force=plan.get("force", False)
        ):
            return _fail("appid or force is not permitted by the stop policy")
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
            try:
                require_content_provisioning(
                    config, operation=operation, account_alias=str(arguments.account),
                    platform=sys.platform,
                )
            except PolicyError as error:
                return _fail(str(error))
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
                ledger.close()
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
                and (
                    operation != "stop"
                    or current.stop_permitted(appid, force=plan.get("force", False))
                )
            ):
                denied = (
                    None
                    if operation == "stop"
                    else _floor_denial(Path(config["library"]), current.min_free_gb)
                )
                if denied is not None:
                    # Degraded, not dead: the emitted nonce stays
                    # explicitly confirmable until it expires.
                    payload["auto_confirm_denied"] = denied
                else:
                    try:
                        ledger.confirm(nonce=nonce, actor=f"policy:{current.version}")
                    except ConfirmationRejected as error:
                        ledger.close()
                        return _fail(str(error))
                    payload = {
                        "operation_id": operation_id,
                        "state": "authorized",
                    }
            ledger.close()
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
                "stop_allowlist": sorted(policy.stop_allowlist),
                "stop_allow_force": policy.stop_allow_force,
            }
        )
        return 0

    if arguments.command == "inspect":
        if (arguments.operation_id is None) == (arguments.request_id is None):
            return _fail("inspect requires one operation ID or --request-id")
        if not (state_dir / "ledger.sqlite3").is_file():
            return _fail("request not found")
        ledger = ExecutionLedger(state_dir / "ledger.sqlite3", readonly=True)
        try:
            operation = (
                ledger.get(arguments.operation_id) if arguments.operation_id is not None
                else ledger.lookup_request_id(arguments.request_id)
            )
            if operation is None:
                return _fail("request not found")
            _emit(
                {
                    "operation_id": operation.operation_id,
                    "operation": operation.operation,
                    "appid": operation.appid,
                    "machine_id": operation.machine_id,
                    "state": operation.state,
                    "detail": operation.detail,
                    "execution_window_expires_at": operation.execution_window_expires_at,
                    "events": ledger.events(operation.operation_id),
                }
            )
            return 0
        except LedgerError as error:
            return _fail(str(error))
        finally:
            ledger.close()

    try:
        ledger, executor = _components(state_dir)
    except PolicyError as error:
        return _fail(str(error))
    except UnsupportedLedger:
        return _fail("unsupported ledger schema, state or operation")

    try:
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
            if (
                policy.grant_for(operation.operation)
                not in {
                    "confirm",
                    "allow",
                }
                or (
                    operation.operation == "launch"
                    and not policy.launch_permitted(operation.appid)
                )
                or (
                    operation.operation == "stop"
                    and not policy.stop_permitted(
                        operation.appid,
                        force=ledger.plan_document(operation_id).get("force", False),
                    )
                )
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
                if (
                    policy.grant_for(operation.operation)
                    not in {
                        "confirm",
                        "allow",
                    }
                    or (
                        operation.operation == "launch"
                        and not policy.launch_permitted(operation.appid)
                    )
                    or (
                        operation.operation == "stop"
                        and not policy.stop_permitted(
                            operation.appid,
                            force=ledger.plan_document(operation_id).get(
                                "force", False
                            ),
                        )
                    )
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
                        f"{denial}; client restore failed; operation left for reconcile"
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
    finally:
        ledger.close()


def _invoke(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except PolicyError as error:
        return _fail(str(error))
    except UnsupportedLedger:
        return _fail("unsupported ledger schema, state or operation")
    except (FileLockError, LedgerError, sqlite3.Error):
        return _fail("broker state unavailable or locked")
    except OSError:
        return _fail("broker filesystem operation failed")


def main(argv: list[str] | None = None) -> int:
    command_line = list(argv) if argv is not None else sys.argv[1:]
    selected = "--format=json" in command_line or any(
        command_line[index:index + 2] == ["--format", "json"]
        for index in range(len(command_line))
    )
    if not selected or "--version" in command_line or "--help" in command_line or "-h" in command_line:
        return _invoke(command_line)
    from steam_agent.execution.receipts import serialize_result

    try:
        with redirect_stderr(io.StringIO()):
            arguments = build_parser().parse_args(command_line)
    except SystemExit:
        _emit(serialize_result(
            None, exit_code=2, command="unknown", state_dir=_default_state_dir(),
            machine_id=None, failure="malformed command arguments",
        ))
        return 2
    state_dir = arguments.state_dir or _default_state_dir()
    marker = _failure.set(None)
    try:
        output = io.StringIO()
        # Selected failures have bounded public codes. Legacy diagnostics keep
        # their original behavior; desktop transport never forwards stderr.
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = _invoke(command_line)
        try:
            payload = json.loads(output.getvalue()) if output.getvalue().strip() else None
        except ValueError:
            payload = None
            _failure.set("broker result unavailable")
            code = 2
        if not isinstance(payload, dict):
            payload = None
        # The desktop worker already serialized the exact invocation result.
        if payload is not None and payload.get("schema") == "broker-result/0.1":
            _emit(payload)
            return code
        try:
            machine_id = _load_config(state_dir).get("machine_id", "local")
        except PolicyError:
            machine_id = None
        _emit(serialize_result(
            payload, exit_code=code, command=arguments.command, state_dir=state_dir,
            machine_id=machine_id, failure=_failure.get(),
            request_id=getattr(arguments, "request_id", None),
            operation_id=getattr(arguments, "operation_id", None),
        ))
        return code
    finally:
        _failure.reset(marker)


if __name__ == "__main__":
    raise SystemExit(main())

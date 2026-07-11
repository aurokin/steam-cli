"""Argument parsing and process boundary for the M1 CLI."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import getpass
import hashlib
import os
from pathlib import Path
import re
import sys
import sqlite3
import stat
import time
from typing import Any, Iterator, Sequence
import unicodedata
import warnings

from steam_agent import __version__
from steam_agent.application import (
    default_credential_dir,
    default_database_path,
    discover_steam_root,
    installed_item,
    sync_installed,
    usable_steam_root,
)
from steam_agent.credentials import (
    CredentialError,
    CredentialRef,
    NativeKeyringStore,
    ProtectedFileStore,
    SecretValue,
)
from steam_agent.contracts import (
    CompletenessStatus,
    ErrorCode,
    ErrorRecord,
    WarningRecord,
    completeness,
    encode_json,
    error_envelope,
    success_envelope,
)
from steam_agent.storage import AccountConflict, Storage, StorageError
from steam_agent.local_accounts import (
    AmbiguousLocalAccounts,
    LocalAccountError,
    LocalAccountRegistryUnavailable,
    MalformedLocalAccountRegistry,
    NoLocalAccount,
    discover_local_accounts,
    select_primary_local_account,
)
from steam_agent.steam_web_api import SteamApiError, SteamWebApiClient


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNAVAILABLE = 3
SECRET_FLAGS = frozenset(
    {"--api-key", "--token", "--password", "--cookie", "--client-secret"}
)
_SAFE_WARNING_SOURCE = re.compile(r"(?:libraryfolders\.vdf|appmanifest_\d+\.acf)\Z")
_OWNED_CAPABILITY = "owned.visible.read"
_OWNED_PROBE_FRESHNESS_SECONDS = 24 * 60 * 60
_PROVIDER_MINIMUM_INTERVAL_SECONDS = 1.0


class CliUsageError(ValueError):
    """An argparse failure safe to serialize without a traceback."""


class AgentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = AgentArgumentParser(
        prog="steam-agent",
        description="Local-first Steam evidence and operations for agents.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--data-dir", type=Path, help="Override the local data directory.")
    parser.add_argument("--format", choices=("json", "table"), default="json")
    commands = parser.add_subparsers(dest="command", required=True)

    status_parser = commands.add_parser("status", help="Show local data and M1 readiness.")
    _add_leaf_format(status_parser)
    capabilities_parser = commands.add_parser(
        "capabilities", help="Show available M1 capabilities."
    )
    _add_leaf_format(capabilities_parser)
    doctor = commands.add_parser("doctor", help="Check local M1 prerequisites.")
    _add_leaf_format(doctor)
    doctor.add_argument("--offline", action="store_true", help="Do not use the network.")

    sync = commands.add_parser("sync", help="Synchronize a capability.")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    installed = sync_commands.add_parser("installed", help="Scan installed Steam games.")
    _add_leaf_format(installed)
    installed.add_argument("--machine", default="local")
    installed.add_argument("--steam-root", type=Path)

    games = commands.add_parser("games", help="Query normalized games.")
    game_commands = games.add_subparsers(dest="games_command", required=True)
    query = game_commands.add_parser("query", help="Query games in a scope.")
    _add_leaf_format(query)
    query.add_argument("--scope", choices=("installed",), required=True)
    query.add_argument("--machine", default="local")
    query.add_argument("--include-paths", action="store_true")

    accounts = commands.add_parser("accounts", help="Configure Steam account identities.")
    account_commands = accounts.add_subparsers(dest="accounts_command", required=True)
    discover_accounts = account_commands.add_parser(
        "discover", help="Inspect local Steam account candidates without exposing identifiers."
    )
    _add_leaf_format(discover_accounts)
    discover_accounts.add_argument("--steam-root", type=Path)
    discover_accounts.add_argument("--include-identifiers", action="store_true")
    configure_account = account_commands.add_parser(
        "configure", help="Persist an explicitly selected local account alias."
    )
    _add_leaf_format(configure_account)
    configure_account.add_argument("--alias", default="primary")
    selection = configure_account.add_mutually_exclusive_group(required=True)
    selection.add_argument("--from-local-most-recent", action="store_true")
    selection.add_argument("--steam-id64")
    configure_account.add_argument("--steam-root", type=Path)
    account_status = account_commands.add_parser(
        "status", help="Show redacted configured-account status."
    )
    _add_leaf_format(account_status)
    account_status.add_argument("--alias", default="primary")
    account_status.add_argument("--include-identifiers", action="store_true")
    remove_account = account_commands.add_parser(
        "remove", help="Remove a local account alias and its probe metadata."
    )
    _add_leaf_format(remove_account)
    remove_account.add_argument("--alias", default="primary")
    remove_account.add_argument("--yes", action="store_true")

    auth = commands.add_parser("auth", help="Manage provider credentials without argv secrets.")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_set = auth_commands.add_parser("set", help="Store a credential from hidden input.")
    _add_leaf_format(auth_set)
    auth_set.add_argument("provider", choices=("steam-web-api",))
    auth_set.add_argument("--backend", choices=("os", "file"), default="os")
    auth_set.add_argument("--yes-file-risk", action="store_true")
    auth_status = auth_commands.add_parser("status", help="Show redacted credential status.")
    _add_leaf_format(auth_status)
    auth_status.add_argument("provider", choices=("steam-web-api",))
    auth_remove = auth_commands.add_parser("remove", help="Remove a locally stored credential.")
    _add_leaf_format(auth_remove)
    auth_remove.add_argument("provider", choices=("steam-web-api",))
    auth_remove.add_argument("--yes", action="store_true")

    owned = commands.add_parser("owned", help="Inspect visible-owned capability state.")
    owned_commands = owned.add_subparsers(dest="owned_command", required=True)
    owned_capability = owned_commands.add_parser(
        "capability", help="Show account, credential, and probe state without network access."
    )
    _add_leaf_format(owned_capability)
    owned_capability.add_argument("--account", default="primary")
    owned_probe = owned_commands.add_parser(
        "probe", help="Explicitly probe visible-owned access without retaining the payload."
    )
    _add_leaf_format(owned_probe)
    owned_probe.add_argument("--account", default="primary")
    return parser


def _add_leaf_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("json", "table"),
        default=argparse.SUPPRESS,
        help="Override output format for this command.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    if any(
        argument.split("=", 1)[0] in SECRET_FLAGS for argument in effective_argv
    ):
        namespace = argparse.Namespace(format=_parse_error_format(effective_argv))
        return _emit_error(
            namespace,
            command="cli",
            code=ErrorCode.SECRET_ON_ARGV,
            message="Secrets are not accepted as command-line arguments.",
            remediation="Use the future hidden auth prompt or a documented secret file.",
            exit_code=2,
        )
    parser = build_parser()
    try:
        args = parser.parse_args(effective_argv)
    except CliUsageError:
        namespace = argparse.Namespace(format=_parse_error_format(effective_argv))
        return _emit_error(
            namespace,
            command="cli",
            code=ErrorCode.INVALID_ARGUMENT,
            message="The command arguments are invalid.",
            remediation="Run steam-agent --help for supported commands and options.",
            exit_code=2,
        )
    database_path = (
        args.data_dir.expanduser() / "steam-agent.sqlite3"
        if args.data_dir
        else default_database_path()
    )
    try:
        return _dispatch(args, database_path)
    except KeyboardInterrupt:
        return _emit_error(
            args,
            command=_command_name(args),
            code=ErrorCode.INTERNAL_ERROR,
            message="Operation canceled.",
            retryable=True,
        )
    except CredentialError as exc:
        return _emit_error(
            args,
            command=_command_name(args),
            code=exc.code,
            message=str(exc),
            retryable=False,
        )
    except LocalAccountError as exc:
        return _emit_error(
            args,
            command=_command_name(args),
            code=_local_account_error_code(exc),
            message="The local Steam account selection is unavailable.",
            retryable=False,
        )
    except AccountConflict:
        return _emit_error(
            args,
            command=_command_name(args),
            code=ErrorCode.ACCOUNT_CONFLICT,
            message="The account alias or Steam identity is already configured differently.",
            remediation="Remove the conflicting alias before configuring a different identity.",
            retryable=False,
        )
    except (sqlite3.DatabaseError, StorageError, OSError):
        return _emit_error(
            args,
            command=_command_name(args),
            code=ErrorCode.DATABASE_ERROR,
            message="The local data store is unavailable or corrupt.",
            retryable=False,
        )
    except Exception as exc:
        print(f"steam-agent: {type(exc).__name__}", file=sys.stderr)
        return _emit_error(
            args,
            command=_command_name(args),
            code=ErrorCode.INTERNAL_ERROR,
            message="The command failed unexpectedly.",
            retryable=False,
        )


def _dispatch(args: argparse.Namespace, database_path: Path) -> int:
    if args.command == "status":
        count = 0
        if database_path.exists():
            with Storage(database_path) as storage:
                count = len(storage.list_installed("local"))
        return _emit_success(
            args,
            command="status",
            data={
                "version": __version__,
                "database_initialized": database_path.exists(),
                "installed_count": count,
            },
        )
    if args.command == "capabilities":
        root = discover_steam_root()
        capability_completeness = _installed_read_completeness(root)
        return _emit_success(
            args,
            command="capabilities",
            completeness_value=capability_completeness,
            data={
                "capabilities": [
                    {
                        "name": "installed.read",
                        "state": "ready" if root else "unavailable",
                        "auth_scope": "local_os_user",
                        "network_required": False,
                        "interface_status": "unofficial_local_read_only",
                    }
                ]
            },
        )
    if args.command == "doctor":
        root = discover_steam_root()
        return _emit_success(
            args,
            command="doctor",
            data={"offline": bool(args.offline), "installed_read": "ready" if root else "unavailable"},
            completeness_value=_installed_read_completeness(root),
        )
    if args.command == "sync" and args.sync_command == "installed":
        root = args.steam_root or discover_steam_root()
        if root is None:
            invalid_override = bool(os.environ.get("STEAM_AGENT_STEAM_ROOT"))
            return _emit_error(
                args,
                command="sync.installed",
                code=(
                    ErrorCode.STEAM_ROOT_INACCESSIBLE
                    if invalid_override
                    else ErrorCode.STEAM_NOT_FOUND
                ),
                message=(
                    "The configured Steam root is missing or inaccessible."
                    if invalid_override
                    else "No Steam installation was found."
                ),
                remediation="Pass --steam-root with the Steam installation directory.",
            )
        if not usable_steam_root(root):
            return _emit_error(
                args,
                command="sync.installed",
                code=ErrorCode.STEAM_ROOT_INACCESSIBLE,
                message="The configured Steam root is missing or inaccessible.",
                remediation="Pass --steam-root with a readable Steam installation directory.",
            )
        with Storage(database_path) as storage:
            result = sync_installed(
                storage,
                steam_root=root,
                machine_id=args.machine,
            )
        warnings = [
            WarningRecord(
                code=warning.code,
                message=warning.message,
                source=_warning_source(warning.path),
            )
            for warning in result.scan.warnings
        ]
        status = (
            CompletenessStatus.COMPLETE
            if result.run.status == "complete"
            else CompletenessStatus.PARTIAL
        )
        return _emit_success(
            args,
            command="sync.installed",
            context={"machine_id": args.machine},
            completeness_value=completeness(status, warnings=warnings),
            data={
                "sync_run_id": result.run.id,
                "sync_status": result.run.status,
                "records_seen": result.run.records_seen,
                "recorded_appids": list(result.recorded_appids),
                "skipped_appids": list(result.skipped_appids),
                "parser_version": result.scan.parser_version,
            },
        )
    if args.command == "games" and args.games_command == "query":
        with Storage(database_path) as storage:
            installed_snapshot = storage.read_installed_snapshot(args.machine)
        games = installed_snapshot.games
        latest = installed_snapshot.latest
        latest_complete = installed_snapshot.latest_complete
        query_completeness: dict[str, Any]
        snapshot: dict[str, Any]
        if latest is None:
            query_completeness = completeness(
                CompletenessStatus.UNAVAILABLE,
                missing_capabilities=["installed.read"],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.NOT_SYNCED,
                        message="Installed games have not been synchronized for this machine.",
                    )
                ],
            )
            snapshot = {"last_attempt_status": None, "last_successful_sync_at": None}
        elif latest.status == "complete":
            query_completeness = completeness(CompletenessStatus.COMPLETE)
            snapshot = {
                "last_attempt_status": latest.status,
                "last_successful_sync_at": latest.completed_at,
            }
        elif latest.status == "running":
            running_warning = WarningRecord(
                code=ErrorCode.SYNC_IN_PROGRESS,
                message="An installed-games synchronization is currently in progress.",
            )
            if latest_complete is None:
                query_completeness = completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["installed.read"],
                    warnings=[running_warning],
                )
                successful_at = None
            else:
                query_completeness = completeness(
                    CompletenessStatus.COMPLETE,
                    warnings=[running_warning],
                )
                successful_at = latest_complete.completed_at
            snapshot = {
                "last_attempt_status": latest.status,
                "last_successful_sync_at": successful_at,
            }
        elif latest_complete is None:
            query_completeness = completeness(
                CompletenessStatus.UNAVAILABLE,
                missing_capabilities=["installed.read"],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.PARTIAL_SCAN,
                        message="The latest scan was incomplete and no last-good snapshot exists.",
                    )
                ],
            )
            snapshot = {
                "last_attempt_status": latest.status,
                "last_successful_sync_at": None,
            }
        else:
            query_completeness = completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=["installed.read"],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message="The latest scan was incomplete; results use the last-good snapshot.",
                    )
                ],
            )
            snapshot = {
                "last_attempt_status": latest.status,
                "last_successful_sync_at": latest_complete.completed_at,
            }
        return _emit_success(
            args,
            command="games.query",
            context={"machine_id": args.machine, "scopes": ["installed"]},
            completeness_value=query_completeness,
            data={
                "items": [
                    installed_item(game, include_paths=args.include_paths)
                    for game in games
                ],
                "next_cursor": None,
                "snapshot": snapshot,
            },
        )
    if args.command == "accounts":
        return _dispatch_accounts(args, database_path)
    if args.command == "auth":
        return _dispatch_auth(args, database_path)
    if args.command == "owned":
        return _dispatch_owned(args, database_path)
    raise AssertionError("argparse accepted an unhandled command")


def _dispatch_accounts(args: argparse.Namespace, database_path: Path) -> int:
    if args.accounts_command in ("configure", "remove"):
        with _credential_operation_lock(database_path):
            return _dispatch_accounts_locked(args, database_path)
    return _dispatch_accounts_locked(args, database_path)


def _dispatch_accounts_locked(args: argparse.Namespace, database_path: Path) -> int:
    if args.accounts_command == "discover":
        discovery = discover_local_accounts(_account_steam_root(args))
        try:
            select_primary_local_account(discovery)
            selection = "available"
        except NoLocalAccount:
            selection = "none"
        except AmbiguousLocalAccounts:
            selection = "ambiguous"
        status = (
            CompletenessStatus.COMPLETE
            if selection == "available"
            else CompletenessStatus.UNAVAILABLE
        )
        warnings: list[WarningRecord] = []
        if selection == "ambiguous":
            warnings.append(
                WarningRecord(
                    code=ErrorCode.ACCOUNT_AMBIGUOUS,
                    message="Multiple local Steam accounts require explicit selection.",
                )
            )
        elif selection == "none":
            warnings.append(
                WarningRecord(
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="No local Steam account candidate was found.",
                )
            )
        return _emit_success(
            args,
            command="accounts.discover",
            completeness_value=completeness(
                status,
                warnings=warnings,
                missing_capabilities=(
                    ["account.identity"] if status == CompletenessStatus.UNAVAILABLE else []
                ),
            ),
            data={
                "candidate_count": len(discovery.candidates),
                "primary_selection": selection,
                "support_level": discovery.support_level,
                "identifiers_included": bool(args.include_identifiers),
                **(
                    {
                        "candidates": [
                            {
                                "steam_id64": candidate.steam_id64,
                                "most_recent": candidate.most_recent,
                            }
                            for candidate in discovery.candidates
                        ]
                    }
                    if args.include_identifiers
                    else {}
                ),
            },
        )
    if args.accounts_command == "configure":
        discovery = discover_local_accounts(_account_steam_root(args))
        if args.from_local_most_recent:
            selected = select_primary_local_account(discovery)
        else:
            selected = next(
                (
                    candidate
                    for candidate in discovery.candidates
                    if candidate.steam_id64 == args.steam_id64
                ),
                None,
            )
            if selected is None:
                return _emit_error(
                    args,
                    command="accounts.configure",
                    code=ErrorCode.ACCOUNT_SELECTION_NOT_FOUND,
                    message="The selected Steam identity is not present in the local registry.",
                    remediation="Run accounts discover --include-identifiers and choose a listed identity.",
                    exit_code=2,
                )
        try:
            with Storage(database_path) as storage:
                account = storage.configure_steam_account(
                    alias=args.alias,
                    steam_id64=selected.steam_id64,
                    configured_at=_utc_now(),
                )
        except ValueError:
            return _emit_error(
                args,
                command="accounts.configure",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        return _emit_success(
            args,
            command="accounts.configure",
            data={
                "alias": account.alias,
                "provider": account.provider,
                "configured": True,
                "source_kind": account.source_kind,
                "identifier_included": False,
            },
        )
    if args.accounts_command == "status":
        try:
            with Storage(database_path) as storage:
                account = storage.get_account(args.alias)
        except ValueError:
            return _emit_error(
                args,
                command="accounts.status",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            return _emit_success(
                args,
                command="accounts.status",
                completeness_value=completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["account.identity"],
                    warnings=[
                        WarningRecord(
                            code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                            message="The requested account alias is not configured.",
                        )
                    ],
                ),
                data={"alias": args.alias, "configured": False},
            )
        data: dict[str, Any] = {
            "alias": account.alias,
            "provider": account.provider,
            "configured": True,
            "source_kind": account.source_kind,
            "identifier_included": bool(args.include_identifiers),
        }
        if args.include_identifiers:
            data["steam_id64"] = account.provider_account_id
        return _emit_success(args, command="accounts.status", data=data)
    if args.accounts_command == "remove":
        if not args.yes:
            return _emit_error(
                args,
                command="accounts.remove",
                code=ErrorCode.CONFIRMATION_REQUIRED,
                message="Account removal requires --yes.",
            )
        try:
            with Storage(database_path) as storage:
                removed = storage.remove_account(args.alias)
        except ValueError:
            return _emit_error(
                args,
                command="accounts.remove",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        return _emit_success(
            args,
            command="accounts.remove",
            data={"alias": args.alias, "removed": removed},
        )
    raise AssertionError("unhandled accounts command")


def _dispatch_auth(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        return _dispatch_auth_locked(args, database_path)


def _dispatch_auth_locked(args: argparse.Namespace, database_path: Path) -> int:
    credential_ref = _steam_credential_ref(database_path)
    if args.auth_command == "set":
        if args.backend == "file" and not args.yes_file_risk:
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.FILE_STORE_NOT_APPROVED,
                message="Protected-file storage requires --yes-file-risk.",
            )
        store = _credential_store(args.backend)
        store_probe = store.probe()
        if not store_probe.available:
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.CREDENTIAL_STORE_UNAVAILABLE,
                message="The selected credential store is unavailable.",
            )
        if not sys.stdin.isatty():
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.INTERACTIVE_INPUT_REQUIRED,
                message="Credential setup requires a terminal with hidden input.",
            )
        try:
            first = _hidden_input("Steam Web API key: ")
            second = _hidden_input("Confirm Steam Web API key: ")
        except getpass.GetPassWarning:
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.INTERACTIVE_INPUT_REQUIRED,
                message="Hidden credential input is unavailable in this terminal.",
            )
        if first != second or not _valid_secret_input(first):
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The credential was invalid or confirmation did not match.",
                exit_code=2,
            )
        secret = SecretValue(first)
        with Storage(database_path) as storage:
            existing = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            if existing is not None and existing.backend != args.backend:
                return _emit_error(
                    args,
                    command="auth.set",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="Remove the existing credential before changing backends.",
                    exit_code=2,
                )
            if existing is not None:
                store = _credential_store(
                    existing.backend, existing.backend_locator
                )
                store_probe = store.probe()
            previous_secret = store.resolve(credential_ref)
            put_completed = False
            try:
                store.put(credential_ref, secret)
                put_completed = True
                storage.upsert_credential_and_clear_probes(
                    provider=credential_ref.provider,
                    kind=credential_ref.kind,
                    profile_id=credential_ref.profile_id,
                    backend=args.backend,
                    backend_locator=store_probe.backend,
                    configured_at=_utc_now(),
                    capability=_OWNED_CAPABILITY,
                )
            except BaseException:
                try:
                    if previous_secret is None:
                        deleted = store.delete(credential_ref)
                        if put_completed and not deleted:
                            raise CredentialError(
                                "CREDENTIAL_ROLLBACK_FAILED"
                            )
                    else:
                        store.put(credential_ref, previous_secret)
                except CredentialError:
                    # A backend may mutate before reporting a failed put. If
                    # compensation cannot establish the prior/absent state,
                    # rollback failure is the only honest result regardless of
                    # the original backend error.
                    raise CredentialError("CREDENTIAL_ROLLBACK_FAILED") from None
                raise
        return _emit_success(
            args,
            command="auth.set",
            data={
                "provider": args.provider,
                "configured": True,
                "backend": args.backend,
                "secret_included": False,
                "validated": False,
            },
        )
    if args.auth_command == "status":
        with Storage(database_path) as storage:
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
        snapshot = _credential_snapshot(metadata, credential_ref)
        status = (
            CompletenessStatus.COMPLETE
            if snapshot["state"] == "configured"
            else CompletenessStatus.UNAVAILABLE
        )
        warnings = _credential_warnings(snapshot["state"])
        return _emit_success(
            args,
            command="auth.status",
            completeness_value=completeness(
                status,
                warnings=warnings,
                missing_capabilities=(
                    ["credential:steam_web_api_user_key"]
                    if status == CompletenessStatus.UNAVAILABLE
                    else []
                ),
            ),
            data={
                "provider": args.provider,
                "configured": snapshot["state"] == "configured",
                "state": snapshot["state"],
                "backend": snapshot["backend"],
                "protection": snapshot["protection"],
                "secret_included": False,
            },
        )
    if args.auth_command == "remove":
        if not args.yes:
            return _emit_error(
                args,
                command="auth.remove",
                code=ErrorCode.CONFIRMATION_REQUIRED,
                message="Credential removal requires --yes.",
            )
        with Storage(database_path) as storage:
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            if metadata is None:
                removed = False
            else:
                store = _credential_store(metadata.backend, metadata.backend_locator)
                try:
                    previous_secret = store.resolve(credential_ref)
                except CredentialError as exc:
                    if exc.code != "CREDENTIAL_READ_FAILED":
                        raise
                    # An unsafe file or undecodable OS-store entry must remain
                    # removable without reading it. It cannot be restored on a
                    # later DB failure because its contents were deliberately
                    # not retained.
                    previous_secret = None
                deleted = store.delete(credential_ref)
                if previous_secret is not None and not deleted:
                    raise CredentialError("CREDENTIAL_DELETE_FAILED")
                try:
                    storage.remove_credential_and_clear_probes(
                        provider=credential_ref.provider,
                        kind=credential_ref.kind,
                        profile_id=credential_ref.profile_id,
                        capability=_OWNED_CAPABILITY,
                    )
                except BaseException:
                    if previous_secret is not None:
                        try:
                            store.put(credential_ref, previous_secret)
                        except CredentialError:
                            raise CredentialError(
                                "CREDENTIAL_ROLLBACK_FAILED"
                            ) from None
                    else:
                        raise CredentialError(
                            "CREDENTIAL_ROLLBACK_FAILED"
                        ) from None
                    raise
                removed = True
        return _emit_success(
            args,
            command="auth.remove",
            data={
                "provider": args.provider,
                "removed": removed,
                "valve_key_revoked": False,
                "secret_included": False,
            },
        )
    raise AssertionError("unhandled auth command")


def _dispatch_owned(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        return _dispatch_owned_locked(args, database_path)


def _dispatch_owned_locked(args: argparse.Namespace, database_path: Path) -> int:
    credential_ref = _steam_credential_ref(database_path)
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command=f"owned.{args.owned_command}",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        metadata = storage.get_credential_reference(
            provider=credential_ref.provider,
            kind=credential_ref.kind,
            profile_id=credential_ref.profile_id,
        )
        if args.owned_command == "probe" and account is not None and metadata is not None:
            credential = _resolve_credential(metadata, credential_ref)
            if credential["state"] == "configured":
                now = _utc_now()
                if not _reserve_provider_request(now):
                    return _emit_error(
                        args,
                        command="owned.probe",
                        code=ErrorCode.REQUEST_THROTTLED,
                        message="The local provider request interval has not elapsed.",
                        retryable=True,
                    )
                try:
                    result = _steam_web_api_client().probe_visible_owned_games(
                        steamid=account.provider_account_id,
                        api_key=credential["secret"],
                    )
                    probe_state = result.probe_state
                    retryable = result.retryable
                except SteamApiError as exc:
                    probe_state = _provider_probe_state(exc.code)
                    retryable = exc.retryable
                storage.save_provider_probe(
                    capability=_OWNED_CAPABILITY,
                    account_alias=account.alias,
                    probe_state=probe_state,
                    checked_at=now,
                    retryable=retryable,
                )
        probe = (
            None
            if account is None
            else storage.get_provider_probe(
                capability=_OWNED_CAPABILITY, account_alias=account.alias
            )
        )
    capability, completeness_value = _owned_capability_snapshot(
        account=account,
        metadata=metadata,
        probe=probe,
        credential_ref=credential_ref,
    )
    return _emit_success(
        args,
        command=f"owned.{args.owned_command}",
        completeness_value=completeness_value,
        data={"capability": capability},
    )


def _account_steam_root(args: argparse.Namespace) -> Path:
    root = getattr(args, "steam_root", None) or discover_steam_root()
    if root is None:
        raise LocalAccountRegistryUnavailable("Steam account registry unavailable")
    return Path(root)


def _steam_credential_ref(database_path: Path) -> CredentialRef:
    """Scope an opaque OS-store account to one local data profile."""

    canonical = str(database_path.expanduser().resolve(strict=False)).encode("utf-8")
    profile_id = f"data-{hashlib.sha256(canonical).hexdigest()[:32]}"
    return CredentialRef("steam", "web-api-key", profile_id)


@contextmanager
def _credential_operation_lock(database_path: Path) -> Iterator[None]:
    """Serialize secret-store mutations and their SQLite metadata per profile."""

    parent = database_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        parent_info = parent.lstat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
    lock_path = parent / ".credential-operation.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(lock_path, flags | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(lock_path, flags, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:
            import msvcrt

            if created:
                os.write(descriptor, b"0")
            else:
                initialization_deadline = time.monotonic() + 5.0
                while os.fstat(descriptor).st_size == 0:
                    if time.monotonic() >= initialization_deadline:
                        raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
                    time.sleep(0.05)
            lock_deadline = time.monotonic() + 300.0
            while True:
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    winerror = getattr(exc, "winerror", None)
                    if winerror not in (None, 33) or exc.errno not in (
                        None,
                        errno.EACCES,
                        errno.EAGAIN,
                    ):
                        raise CredentialError(
                            "CREDENTIAL_STORE_UNAVAILABLE"
                        ) from None
                    if time.monotonic() >= lock_deadline:
                        raise CredentialError("CREDENTIAL_STORE_LOCKED") from None
                    time.sleep(0.05)
        yield
    finally:
        if os.name == "nt":
            try:
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(descriptor)


def _credential_store(backend: str, backend_locator: str | None = None) -> Any:
    if backend == "os":
        return NativeKeyringStore(backend_id=backend_locator)
    if backend == "file":
        return ProtectedFileStore(default_credential_dir(), approved=True)
    raise ValueError("unsupported credential backend")


def _credential_snapshot(
    metadata: Any, credential_ref: CredentialRef
) -> dict[str, Any]:
    if metadata is None:
        return {"state": "missing", "backend": None, "protection": None}
    resolved = _resolve_credential(metadata, credential_ref)
    return {
        "state": resolved["state"],
        "backend": metadata.backend,
        "protection": "os" if metadata.backend == "os" else "file",
    }


def _resolve_credential(
    metadata: Any, credential_ref: CredentialRef
) -> dict[str, Any]:
    try:
        secret = _credential_store(
            metadata.backend, metadata.backend_locator
        ).resolve(credential_ref)
    except CredentialError as exc:
        state = (
            "store_locked"
            if exc.code == "CREDENTIAL_STORE_LOCKED"
            else "store_unavailable"
        )
        return {"state": state, "secret": None, "error_code": exc.code}
    if secret is None:
        return {"state": "missing", "secret": None, "error_code": None}
    return {"state": "configured", "secret": secret, "error_code": None}


def _credential_warnings(state: str) -> list[WarningRecord]:
    if state == "configured":
        return []
    if state == "store_locked":
        return [
            WarningRecord(
                code=ErrorCode.CREDENTIAL_STORE_LOCKED,
                message="The configured credential store is locked.",
            )
        ]
    if state == "store_unavailable":
        return [
            WarningRecord(
                code=ErrorCode.CREDENTIAL_STORE_UNAVAILABLE,
                message="The configured credential store is unavailable.",
            )
        ]
    return [
        WarningRecord(
            code=ErrorCode.AUTH_REQUIRED,
            message="A Steam Web API user key has not been configured.",
        )
    ]


def _owned_capability_snapshot(
    *, account: Any, metadata: Any, probe: Any, credential_ref: CredentialRef
) -> tuple[dict[str, Any], dict[str, Any]]:
    credential = _credential_snapshot(metadata, credential_ref)
    identity_state = "configured" if account is not None else "missing"
    probe_state = "not_checked" if probe is None else probe.probe_state
    if probe is not None and _probe_is_stale(probe.checked_at):
        probe_state = "stale"
    warnings: list[WarningRecord] = []
    missing: list[str] = []
    if identity_state == "missing":
        missing.append("account.identity")
        warnings.append(
            WarningRecord(
                code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                message="The requested account alias is not configured.",
            )
        )
    if credential["state"] != "configured":
        missing.append("credential:steam_web_api_user_key")
        warnings.extend(_credential_warnings(credential["state"]))
    if not missing and probe_state == "not_checked":
        missing.append(_OWNED_CAPABILITY)
        warnings.append(
            WarningRecord(
                code=ErrorCode.CAPABILITY_NOT_PROBED,
                message="Visible-owned access has not been probed.",
            )
        )
    elif probe_state != "ready" and probe_state != "not_checked":
        missing.append(_OWNED_CAPABILITY)
        warnings.append(_probe_warning(probe_state))
    is_ready = not missing and probe_state == "ready"
    capability = {
        "name": _OWNED_CAPABILITY,
        "support": "supported",
        "interface_status": "official_documented",
        "identity": identity_state,
        "credential": credential["state"],
        "credential_backend": credential["backend"],
        "probe": probe_state,
        "last_checked_at": None if probe is None else probe.checked_at,
        "probe_retryable": None if probe is None else probe.retryable,
        "network_required": True,
        "identifiers_included": False,
        "limitations": [
            "individually_private_games_may_be_omitted",
            "unplayed_free_entitlements_are_not_complete",
        ],
    }
    return capability, completeness(
        CompletenessStatus.COMPLETE if is_ready else CompletenessStatus.UNAVAILABLE,
        missing_capabilities=missing,
        warnings=warnings,
    )


def _probe_warning(state: str) -> WarningRecord:
    mapping = {
        "authentication_failed": (
            ErrorCode.AUTHENTICATION_FAILED,
            "Steam rejected the configured API key.",
        ),
        "data_inaccessible": (
            ErrorCode.OWNED_GAMES_INACCESSIBLE_OR_UNKNOWN_ACCOUNT,
            "Owned-game data was inaccessible or the account response was ambiguous.",
        ),
        "rate_limited": (
            ErrorCode.PROVIDER_RATE_LIMITED,
            "Steam rate-limited the capability probe.",
        ),
        "provider_unavailable": (
            ErrorCode.PROVIDER_UNAVAILABLE,
            "Steam was unavailable during the capability probe.",
        ),
        "contract_changed": (
            ErrorCode.PROVIDER_RESPONSE_INVALID,
            "Steam returned an unsupported response shape.",
        ),
        "invalid_request": (
            ErrorCode.PROVIDER_RESPONSE_INVALID,
            "Steam rejected the capability probe request.",
        ),
        "stale": (
            ErrorCode.CAPABILITY_PROBE_STALE,
            "The last visible-owned capability probe is stale.",
        ),
    }
    code, message = mapping.get(
        state,
        (ErrorCode.PROVIDER_RESPONSE_INVALID, "The provider probe did not complete."),
    )
    return WarningRecord(code=code, message=message)


def _provider_probe_state(code: str) -> str:
    return {
        "AUTHENTICATION_FAILED": "authentication_failed",
        "RATE_LIMITED": "rate_limited",
        "PROVIDER_UNAVAILABLE": "provider_unavailable",
        "INVALID_REQUEST": "invalid_request",
        "PROVIDER_RESPONSE_INVALID": "contract_changed",
    }.get(code, "contract_changed")


def _local_account_error_code(exc: LocalAccountError) -> str:
    if isinstance(exc, AmbiguousLocalAccounts):
        return str(ErrorCode.ACCOUNT_AMBIGUOUS)
    if isinstance(exc, MalformedLocalAccountRegistry):
        return str(ErrorCode.ACCOUNT_REGISTRY_MALFORMED)
    if isinstance(exc, LocalAccountRegistryUnavailable):
        return str(ErrorCode.ACCOUNT_REGISTRY_UNAVAILABLE)
    return str(ErrorCode.ACCOUNT_NOT_CONFIGURED)


def _valid_secret_input(value: str) -> bool:
    return (
        16 <= len(value) <= 4096
        and value.isascii()
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _hidden_input(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def _probe_is_stale(checked_at: str) -> bool:
    checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    return (_utc_now() - checked).total_seconds() > _OWNED_PROBE_FRESHNESS_SECONDS


def _steam_web_api_client() -> SteamWebApiClient:
    return SteamWebApiClient()


def _provider_budget_database_path() -> Path:
    """One OS-user-local request budget shared by every data profile."""

    return default_credential_dir().parent / "provider-request-budget.sqlite3"


def _reserve_provider_request(requested_at: datetime) -> bool:
    with Storage(_provider_budget_database_path()) as storage:
        return storage.reserve_provider_request(
            provider="steam-web-api",
            budget_scope="user-key",
            requested_at=requested_at,
            minimum_interval_seconds=_PROVIDER_MINIMUM_INTERVAL_SECONDS,
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _emit_success(
    args: argparse.Namespace,
    *,
    command: str,
    data: dict[str, Any],
    context: dict[str, Any] | None = None,
    completeness_value: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> int:
    envelope = success_envelope(
        command=command,
        data=data,
        context=context,
        completeness_value=completeness_value,
        generated_at=generated_at,
    )
    if args.format == "json":
        print(encode_json(envelope))
    else:
        _print_table(command, envelope)
    return EXIT_OK


def _emit_error(
    args: argparse.Namespace,
    *,
    command: str,
    code: str,
    message: str,
    retryable: bool = False,
    remediation: str | None = None,
    exit_code: int | None = None,
) -> int:
    envelope = error_envelope(
        command=command,
        error=ErrorRecord(
            code=str(code),
            message=message,
            retryable=retryable,
            remediation=remediation,
        ),
    )
    if getattr(args, "format", "json") == "json":
        print(encode_json(envelope))
    else:
        print(f"{code}: {message}", file=sys.stderr)
    if exit_code is not None:
        return exit_code
    return (
        EXIT_UNAVAILABLE
        if code in (ErrorCode.STEAM_NOT_FOUND, ErrorCode.STEAM_ROOT_INACCESSIBLE)
        else EXIT_ERROR
    )


def _command_name(args: argparse.Namespace) -> str:
    parts = [getattr(args, "command", "unknown")]
    for name in (
        "sync_command",
        "games_command",
        "accounts_command",
        "auth_command",
        "owned_command",
    ):
        value = getattr(args, name, None)
        if value:
            parts.append(value)
    return ".".join(parts)


def _parse_error_format(argv: Sequence[str]) -> str:
    """Honor only syntactically valid requests for table-form parse errors."""

    for index, argument in enumerate(argv):
        if argument == "--format" and index + 1 < len(argv):
            if argv[index + 1] == "table":
                return "table"
        elif argument == "--format=table":
            return "table"
    return "json"


def _warning_source(path: Path | None) -> str | None:
    """Expose only recognized Steam metadata filenames, never directories."""

    if path is None:
        return None
    return path.name if _SAFE_WARNING_SOURCE.fullmatch(path.name) else None


def _installed_read_completeness(root: Path | None) -> dict[str, Any]:
    if root is not None:
        return completeness(CompletenessStatus.COMPLETE)
    invalid_override = bool(os.environ.get("STEAM_AGENT_STEAM_ROOT"))
    return completeness(
        CompletenessStatus.UNAVAILABLE,
        missing_capabilities=["installed.read"],
        warnings=[
            WarningRecord(
                code=(
                    ErrorCode.STEAM_ROOT_INACCESSIBLE
                    if invalid_override
                    else ErrorCode.STEAM_NOT_FOUND
                ),
                message=(
                    "The configured Steam root is missing or inaccessible."
                    if invalid_override
                    else "No default Steam installation was found; pass --steam-root when syncing."
                ),
            )
        ],
    )


def _table_field(value: object) -> str:
    """Render one physical table field without terminal/control injection."""

    if value is None:
        return ""
    escaped: list[str] = []
    for character in str(value):
        if character == "\\":
            escaped.append("\\\\")
        elif character == "\t":
            escaped.append("\\t")
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            escaped.append(f"\\u{ord(character):04x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def _print_table_fields(*values: object) -> None:
    print("\t".join(_table_field(value) for value in values))


def _print_table(command: str, envelope: dict[str, Any]) -> None:
    if command == "games.query":
        query_completeness = envelope["completeness"]
        _print_table_fields("COMPLETENESS", query_completeness["status"])
        for capability in query_completeness["missing_capabilities"]:
            _print_table_fields("MISSING_CAPABILITY", capability)
        for capability in query_completeness["stale_capabilities"]:
            _print_table_fields("STALE_CAPABILITY", capability)
        for warning in query_completeness["warnings"]:
            fields = ["WARNING", warning["code"], warning["message"]]
            if warning.get("source"):
                fields.append(warning["source"])
            _print_table_fields(*fields)
        _print_table_fields("APPID", "NAME", "STATE", "SIZE")
        for item in envelope["data"]["items"]:
            _print_table_fields(
                item["appid"], item["name"], item["state"], item["size_bytes"]
            )
        return
    for key, value in envelope["data"].items():
        _print_table_fields(key, value)


__all__ = ["build_parser", "main"]

"""Argument parsing and process boundary for the M1 CLI."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
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
from steam_agent.owned_library import (
    OWNED_DISCLOSURE_VERSION,
    OwnedSyncError,
    owned_item,
    sync_owned,
)
from steam_agent.catalog_inventory import CatalogSyncError, sync_catalog
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
from steam_agent.steam_wishlist import SteamWishlistClient
from steam_agent.wishlist_library import (
    WISHLIST_DISCLOSURE_VERSION,
    WishlistSyncError,
    sync_wishlist,
)
from steam_agent.steam_store_catalog import (
    CatalogApiError,
    SteamStoreCatalogClient,
)
from steam_agent.provider_auth import ProviderAuthClient, ProviderAuthError
from steam_agent.gg_deals import GgDealsClient, GgDealsError
from steam_agent.cheapshark import CheapSharkClient, CheapSharkError
from steam_agent.price_library import PriceSyncError, sync_wishlist_prices
from steam_agent.deal_query import build_deal_query_from_snapshot


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNAVAILABLE = 3
SECRET_FLAGS = frozenset(
    {"--api-key", "--token", "--password", "--cookie", "--client-secret"}
)
_SAFE_WARNING_SOURCE = re.compile(r"(?:libraryfolders\.vdf|appmanifest_\d+\.acf)\Z")
_OWNED_CAPABILITY = "owned.visible.read"
_OWNED_PROBE_FRESHNESS_SECONDS = 24 * 60 * 60
_OWNED_SYNC_FRESHNESS_SECONDS = 24 * 60 * 60
_WISHLIST_SYNC_FRESHNESS_SECONDS = 24 * 60 * 60
_CATALOG_SYNC_FRESHNESS_SECONDS = 24 * 60 * 60
_SYNC_ABANDONED_SECONDS = 15 * 60
_PROVIDER_MINIMUM_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class _CredentialProviderSpec:
    cli_name: str
    storage_provider: str
    kind: str
    prompt_label: str
    display_label: str
    missing_capability: str
    dependent_capability: str | None = None


_CREDENTIAL_PROVIDERS = {
    spec.cli_name: spec
    for spec in (
        _CredentialProviderSpec(
            "steam-web-api",
            "steam",
            "web-api-key",
            "Steam Web API key",
            "Steam Web API user key",
            "credential:steam_web_api_user_key",
            _OWNED_CAPABILITY,
        ),
        _CredentialProviderSpec(
            "isthereanydeal",
            "isthereanydeal",
            "api-key",
            "IsThereAnyDeal API key",
            "IsThereAnyDeal API key",
            "credential:isthereanydeal_api_key",
        ),
        _CredentialProviderSpec(
            "steamgriddb",
            "steamgriddb",
            "api-key",
            "SteamGridDB API key",
            "SteamGridDB API key",
            "credential:steamgriddb_api_key",
        ),
        _CredentialProviderSpec(
            "gg-deals",
            "gg-deals",
            "api-key",
            "GG.deals API key",
            "GG.deals API key",
            "credential:gg_deals_api_key",
        ),
    )
}
_AUTH_PROVIDER_NAMES = tuple(_CREDENTIAL_PROVIDERS)
_AUTH_PROBE_PROVIDER_NAMES = ("steamgriddb", "gg-deals")
_AUTH_PROBE_INTERVAL_SECONDS = {
    "isthereanydeal": 3.0,
    "steamgriddb": 1.0,
    "gg-deals": 1.0,
}


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
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--data-dir", type=Path, help="Override the local data directory."
    )
    parser.add_argument("--format", choices=("json", "table"), default="json")
    commands = parser.add_subparsers(dest="command", required=True)

    status_parser = commands.add_parser(
        "status", help="Show local data and M1 readiness."
    )
    _add_leaf_format(status_parser)
    capabilities_parser = commands.add_parser(
        "capabilities", help="Show available M1 capabilities."
    )
    _add_leaf_format(capabilities_parser)
    doctor = commands.add_parser("doctor", help="Check local M1 prerequisites.")
    _add_leaf_format(doctor)
    doctor.add_argument(
        "--offline", action="store_true", help="Do not use the network."
    )

    sync = commands.add_parser("sync", help="Synchronize a capability.")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    installed = sync_commands.add_parser(
        "installed", help="Scan installed Steam games."
    )
    _add_leaf_format(installed)
    installed.add_argument("--machine", default="local")
    installed.add_argument("--steam-root", type=Path)
    owned_sync = sync_commands.add_parser(
        "owned", help="Synchronize the visible owned library."
    )
    _add_leaf_format(owned_sync)
    owned_sync.add_argument("--account", default="primary")
    owned_sync.add_argument(
        "--acknowledge-local-storage",
        action="store_true",
        help="Accept the versioned local storage and backup disclosure.",
    )
    catalog_sync = sync_commands.add_parser(
        "catalog", help="Synchronize bounded catalog evidence for observed AppIDs."
    )
    _add_leaf_format(catalog_sync)
    catalog_sync.add_argument("--account", default="primary")
    catalog_sync.add_argument("--machine", default="local")
    wishlist_sync = sync_commands.add_parser(
        "wishlist", help="Synchronize the provisional Steam wishlist."
    )
    _add_leaf_format(wishlist_sync)
    wishlist_sync.add_argument("--account", default="primary")
    wishlist_sync.add_argument(
        "--acknowledge-local-storage",
        action="store_true",
        help="Accept the versioned wishlist storage and backup disclosure.",
    )
    prices_sync = sync_commands.add_parser(
        "prices", help="Synchronize current and historical-low wishlist evidence."
    )
    _add_leaf_format(prices_sync)
    prices_sync.add_argument("--scope", choices=("wishlist",), required=True)
    prices_sync.add_argument("--account", default="primary")
    prices_sync.add_argument("--country", required=True)
    prices_sync.add_argument(
        "--provider", choices=("auto", "gg-deals", "cheapshark"), default="auto"
    )
    prices_sync.add_argument("--max-items", type=int)

    games = commands.add_parser("games", help="Query normalized games.")
    game_commands = games.add_subparsers(dest="games_command", required=True)
    query = game_commands.add_parser("query", help="Query games in a scope.")
    _add_leaf_format(query)
    query.add_argument(
        "--scope", choices=("installed", "owned", "wishlist", "library"), required=True
    )
    query.add_argument("--machine", default="local")
    query.add_argument("--account", default="primary")
    query.add_argument("--include-paths", action="store_true")

    deals = commands.add_parser("deals", help="Query cached wishlist deal evidence.")
    deal_commands = deals.add_subparsers(dest="deals_command", required=True)
    deal_query = deal_commands.add_parser(
        "query", help="Rank cached deal evidence for a wishlist."
    )
    _add_leaf_format(deal_query)
    deal_query.add_argument("--scope", choices=("wishlist",), required=True)
    deal_query.add_argument("--account", required=True)
    deal_query.add_argument("--country", required=True)
    deal_query.add_argument(
        "--store-class",
        choices=("official", "keyshop", "unknown"),
        default="official",
    )

    accounts = commands.add_parser(
        "accounts", help="Configure Steam account identities."
    )
    account_commands = accounts.add_subparsers(dest="accounts_command", required=True)
    discover_accounts = account_commands.add_parser(
        "discover",
        help="Inspect local Steam account candidates without exposing identifiers.",
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
        "remove",
        help=(
            "Remove an account alias and all account-scoped Steam data while "
            "preserving the shared API key."
        ),
    )
    _add_leaf_format(remove_account)
    remove_account.add_argument("--alias", default="primary")
    remove_account.add_argument("--yes", action="store_true")

    auth = commands.add_parser(
        "auth", help="Manage provider credentials without argv secrets."
    )
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_set = auth_commands.add_parser(
        "set", help="Store a credential from hidden input."
    )
    _add_leaf_format(auth_set)
    auth_set.add_argument("provider", choices=_AUTH_PROVIDER_NAMES)
    auth_set.add_argument("--backend", choices=("os", "file"), default="os")
    auth_set.add_argument("--yes-file-risk", action="store_true")
    auth_status = auth_commands.add_parser(
        "status", help="Show redacted credential status."
    )
    _add_leaf_format(auth_status)
    auth_status.add_argument("provider", choices=_AUTH_PROVIDER_NAMES)
    auth_probe = auth_commands.add_parser(
        "probe",
        help="Explicitly validate a third-party credential without retaining a body.",
    )
    _add_leaf_format(auth_probe)
    auth_probe.add_argument("provider", choices=_AUTH_PROBE_PROVIDER_NAMES)
    auth_remove = auth_commands.add_parser(
        "remove", help="Remove a locally stored credential."
    )
    _add_leaf_format(auth_remove)
    auth_remove.add_argument("provider", choices=_AUTH_PROVIDER_NAMES)
    auth_remove.add_argument("--yes", action="store_true")

    owned = commands.add_parser("owned", help="Inspect visible-owned capability state.")
    owned_commands = owned.add_subparsers(dest="owned_command", required=True)
    owned_capability = owned_commands.add_parser(
        "capability",
        help="Show account, credential, and probe state without network access.",
    )
    _add_leaf_format(owned_capability)
    owned_capability.add_argument("--account", default="primary")
    owned_probe = owned_commands.add_parser(
        "probe",
        help="Explicitly probe visible-owned access without retaining the payload.",
    )
    _add_leaf_format(owned_probe)
    owned_probe.add_argument("--account", default="primary")

    data = commands.add_parser("data", help="Delete locally retained provider data.")
    data_commands = data.add_subparsers(dest="data_command", required=True)
    delete_data = data_commands.add_parser(
        "delete", help="Delete retained Steam Web API account data."
    )
    _add_leaf_format(delete_data)
    delete_data.add_argument(
        "--provider", choices=("steam-web-api", "gg-deals", "cheapshark"), required=True
    )
    target = delete_data.add_mutually_exclusive_group(required=True)
    target.add_argument("--account")
    target.add_argument("--all", action="store_true")
    delete_data.add_argument("--yes", action="store_true")
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
    if any(argument.split("=", 1)[0] in SECRET_FLAGS for argument in effective_argv):
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
            data={
                "offline": bool(args.offline),
                "installed_read": "ready" if root else "unavailable",
            },
            completeness_value=_installed_read_completeness(root),
        )
    if args.command == "sync" and args.sync_command == "owned":
        return _dispatch_sync_owned(args, database_path)
    if args.command == "sync" and args.sync_command == "catalog":
        return _dispatch_sync_catalog(args, database_path)
    if args.command == "sync" and args.sync_command == "wishlist":
        return _dispatch_sync_wishlist(args, database_path)
    if args.command == "sync" and args.sync_command == "prices":
        return _dispatch_sync_prices(args, database_path)
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
        if args.scope == "wishlist":
            return _dispatch_wishlist_games_query(args, database_path)
        if args.scope in ("owned", "library"):
            return _dispatch_account_games_query(args, database_path)
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
    if args.command == "deals" and args.deals_command == "query":
        return _dispatch_deals_query(args, database_path)
    if args.command == "accounts":
        return _dispatch_accounts(args, database_path)
    if args.command == "auth":
        return _dispatch_auth(args, database_path)
    if args.command == "owned":
        return _dispatch_owned(args, database_path)
    if args.command == "data":
        return _dispatch_data(args, database_path)
    raise AssertionError("argparse accepted an unhandled command")


def _dispatch_deals_query(args: argparse.Namespace, database_path: Path) -> int:
    country = args.country.upper()
    if (
        len(country) != 2
        or not country.isascii()
        or not country.isalpha()
        or country != "US"
    ):
        return _emit_error(
            args,
            command="deals.query",
            code=(
                "UNSUPPORTED_COUNTRY"
                if len(country) == 2 and country.isascii() and country.isalpha()
                else ErrorCode.INVALID_ARGUMENT
            ),
            message=(
                "Cached GG.deals and CheapShark evidence is currently US/USD only."
                if len(country) == 2 and country.isascii() and country.isalpha()
                else "Country must be a two-letter code."
            ),
            exit_code=2,
        )
    generated_at = _utc_now()
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command="deals.query",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            return _emit_success(
                args,
                command="deals.query",
                generated_at=generated_at,
                context={
                    "account_alias": args.account,
                    "scopes": ["wishlist", "deals"],
                    "country": country,
                    "currency": "USD",
                    "store_class": args.store_class,
                    "identifiers_included": False,
                },
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
                data={
                    "items": [],
                    "empty": False,
                    "next_cursor": None,
                    "ranking": {
                        "schema": "deal-evidence/0.1",
                        "deterministic_order": True,
                    },
                    "snapshots": {"wishlist": None, "prices": None},
                    "fallback": {
                        "ladder": [
                            {"rung": 0, "provider": "gg-deals", "mode": "api"},
                            {
                                "rung": 1,
                                "provider": "cheapshark",
                                "mode": "api",
                            },
                            {
                                "rung": 2,
                                "provider": "manual-reference",
                                "mode": "manual_only",
                            },
                        ],
                        "providers_attempted": [],
                        "providers_used": [],
                    },
                    "limitations": [
                        "price evidence is US/USD only",
                        "historical lows are summaries rather than a full event graph",
                        "manual references are never read or fetched by Steam Agent",
                    ],
                },
            )
        spec = _CREDENTIAL_PROVIDERS["gg-deals"]
        credential_ref = _provider_credential_ref(database_path, spec)
        gg_configured = (
            storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            is not None
        )
        snapshot = storage.read_wishlist_deal_snapshot(
            account_id=account.id,
            country=country,
            now=generated_at,
        )
    result = build_deal_query_from_snapshot(
        snapshot,
        account_alias=account.alias,
        country=country,
        store_class=args.store_class,
        generated_at=generated_at,
        gg_credential_configured=gg_configured,
    )
    return _emit_success(
        args,
        command="deals.query",
        generated_at=generated_at,
        context=result["context"],  # type: ignore[arg-type]
        completeness_value=result["completeness"],  # type: ignore[arg-type]
        data=result["data"],  # type: ignore[arg-type]
    )


def _dispatch_sync_owned(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        credential_ref = _steam_credential_ref(database_path)
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            if account is None:
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            consent = storage.get_owned_data_consent(account.id)
            if (
                consent is None
                or consent.disclosure_version != OWNED_DISCLOSURE_VERSION
            ):
                if not args.acknowledge_local_storage:
                    return _emit_error(
                        args,
                        command="sync.owned",
                        code=ErrorCode.DATA_POLICY_ACKNOWLEDGMENT_REQUIRED,
                        message=(
                            "Valve data is stored as-is in the selected local data "
                            "directory: AppID, optional name, lifetime playtime, "
                            "inclusion basis, provenance, and coarse sync metadata. "
                            "Visible-owned is not complete license truth; individually "
                            "private games and unplayed free entitlements may be omitted, "
                            "and sequential request differences may reflect a concurrent "
                            "library change. Storage countries follow the device, selected "
                            "filesystem, replicas, and user-controlled backups. Account "
                            "deletion preserves the shared key; all-provider deletion "
                            "removes its local key/reference but does not revoke it at "
                            "Valve. SQLite secure deletion cannot erase external backups, "
                            "snapshots, journals, or storage-media remapping."
                        ),
                        remediation=(
                            "Rerun with --acknowledge-local-storage to accept this "
                            "versioned local-storage policy."
                        ),
                    )
                storage.record_owned_data_consent(
                    account_id=account.id,
                    disclosure_version=OWNED_DISCLOSURE_VERSION,
                    accepted_at=_utc_now(),
                    backups_acknowledged=True,
                )
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            if metadata is None:
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=ErrorCode.AUTH_REQUIRED,
                    message="A Steam Web API user key has not been configured.",
                )
            resolved = _resolve_credential(metadata, credential_ref)
            if resolved["state"] != "configured":
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=_credential_error_code(resolved["state"]),
                    message="The Steam Web API credential is unavailable.",
                )

            def request_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "steam-web-api",
                        _utc_now(),
                        _PROVIDER_MINIMUM_INTERVAL_SECONDS,
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise OwnedSyncError("REQUEST_THROTTLED", retryable=True)

            try:
                result = sync_owned(
                    storage,
                    account_id=account.id,
                    steamid=account.provider_account_id,
                    api_key=resolved["secret"],
                    client=_steam_web_api_client(),
                    request_gate=request_gate,
                    clock=_utc_now,
                )
            except OwnedSyncError as exc:
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=exc.code,
                    message="The visible-owned synchronization did not complete.",
                    retryable=exc.retryable,
                )
        return _emit_success(
            args,
            command="sync.owned",
            context={"account_alias": account.alias, "identifiers_included": False},
            data={
                "sync_run_id": result.run.id,
                "sync_status": result.run.status,
                "records_seen": result.run.records_seen,
                "visible_owned_count": result.visible_owned_count,
                "played_free_count": result.played_free_count,
                "disclosure_version": OWNED_DISCLOSURE_VERSION,
                "limitations": [
                    "individually_private_games_may_be_omitted",
                    "unplayed_free_entitlements_are_not_complete",
                    "sequential_request_difference_may_reflect_concurrent_library_change",
                ],
            },
        )


def _credential_error_code(state: str) -> str:
    return {
        "missing": str(ErrorCode.CREDENTIAL_NOT_FOUND),
        "store_locked": str(ErrorCode.CREDENTIAL_STORE_LOCKED),
        "store_unavailable": str(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE),
    }.get(state, str(ErrorCode.CREDENTIAL_READ_FAILED))


def _dispatch_sync_wishlist(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        credential_ref = _steam_credential_ref(database_path)
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            if account is None:
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            consent = storage.get_wishlist_data_consent(account.id)
            if (
                consent is None
                or consent.disclosure_version != WISHLIST_DISCLOSURE_VERSION
            ):
                if not args.acknowledge_local_storage:
                    return _emit_error(
                        args,
                        command="sync.wishlist",
                        code=ErrorCode.DATA_POLICY_ACKNOWLEDGMENT_REQUIRED,
                        message=(
                            "The provisional Steam wishlist stores one last-good local "
                            "projection containing AppID, priority, date added, "
                            "provenance, and coarse sync metadata. It retains no raw "
                            "response body. An inaccessible or authentication-like "
                            "empty response cannot establish an empty wishlist. "
                            "Account deletion removes this projection; external backups "
                            "and storage snapshots remain user-controlled."
                        ),
                        remediation=(
                            "Rerun with --acknowledge-local-storage to accept this "
                            "versioned local-storage policy."
                        ),
                    )
                storage.record_wishlist_data_consent(
                    account_id=account.id,
                    disclosure_version=WISHLIST_DISCLOSURE_VERSION,
                    accepted_at=_utc_now(),
                    backups_acknowledged=True,
                )
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            if metadata is None:
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=ErrorCode.AUTH_REQUIRED,
                    message="A Steam Web API user key has not been configured.",
                )
            resolved = _resolve_credential(metadata, credential_ref)
            if resolved["state"] != "configured":
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=_credential_error_code(resolved["state"]),
                    message="The Steam Web API credential is unavailable.",
                )

            def request_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "steam-web-api",
                        _utc_now(),
                        _PROVIDER_MINIMUM_INTERVAL_SECONDS,
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise WishlistSyncError("REQUEST_THROTTLED", retryable=True)

            try:
                result = sync_wishlist(
                    storage,
                    account_id=account.id,
                    steamid=account.provider_account_id,
                    api_key=resolved["secret"],
                    client=_steam_wishlist_client(),
                    request_gate=request_gate,
                    clock=_utc_now,
                )
            except WishlistSyncError as exc:
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=exc.code,
                    message="The wishlist synchronization did not complete.",
                    retryable=exc.retryable,
                )
    return _emit_success(
        args,
        command="sync.wishlist",
        context={"account_alias": account.alias, "identifiers_included": False},
        data={
            "sync_run_id": result.run.id,
            "sync_status": result.run.status,
            "records_seen": result.run.records_seen,
            "wishlist_count": result.item_count,
            "disclosure_version": WISHLIST_DISCLOSURE_VERSION,
            "support_level": "official_undocumented_provisional",
            "limitations": [
                "provider_contract_is_provisional",
                "empty_auth_like_response_is_ambiguous",
                "sequential_pair_may_detect_concurrent_wishlist_change",
            ],
        },
    )


def _dispatch_sync_prices(args: argparse.Namespace, database_path: Path) -> int:
    country = args.country.upper()
    if (
        len(country) != 2
        or not country.isascii()
        or not country.isalpha()
        or country != "US"
    ):
        return _emit_error(
            args,
            command="sync.prices",
            code=(
                "UNSUPPORTED_COUNTRY"
                if len(country) == 2 and country.isascii() and country.isalpha()
                else ErrorCode.INVALID_ARGUMENT
            ),
            message=(
                "GG.deals and CheapShark are currently supported only for US/USD."
                if len(country) == 2 and country.isascii() and country.isalpha()
                else "Country must be a two-letter code."
            ),
            exit_code=2,
        )
    if args.max_items is not None and not 1 <= args.max_items <= 10_000:
        return _emit_error(
            args,
            command="sync.prices",
            code=ErrorCode.INVALID_ARGUMENT,
            message="--max-items must be between 1 and 10000.",
            exit_code=2,
        )
    with _credential_operation_lock(database_path):
        spec = _CREDENTIAL_PROVIDERS["gg-deals"]
        credential_ref = _provider_credential_ref(database_path, spec)
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                return _emit_error(
                    args,
                    command="sync.prices",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            if account is None:
                return _emit_error(
                    args,
                    command="sync.prices",
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            resolved = (
                {"state": "missing", "secret": None}
                if metadata is None
                else _resolve_credential(metadata, credential_ref)
            )
            if args.provider == "gg-deals" and resolved["state"] != "configured":
                return _emit_error(
                    args,
                    command="sync.prices",
                    code=(
                        ErrorCode.AUTH_REQUIRED
                        if resolved["state"] == "missing"
                        else _credential_error_code(resolved["state"])
                    ),
                    message="The GG.deals API credential is unavailable.",
                )

            def gg_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "gg-deals", _utc_now(), _PROVIDER_MINIMUM_INTERVAL_SECONDS
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise GgDealsError("REQUEST_THROTTLED", retryable=True)

            def cheap_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "cheapshark", _utc_now(), _PROVIDER_MINIMUM_INTERVAL_SECONDS
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise CheapSharkError("REQUEST_THROTTLED", retryable=True)

            try:
                result = sync_wishlist_prices(
                    storage,
                    account_id=account.id,
                    country=country,
                    provider=args.provider,
                    gg_api_key=(
                        resolved.get("secret")
                        if resolved["state"] == "configured"
                        else None
                    ),
                    max_items=args.max_items,
                    gg_client=_gg_deals_client(gg_gate),
                    cheapshark_client=_cheapshark_client(cheap_gate),
                    clock=_utc_now,
                )
            except PriceSyncError as exc:
                return _emit_error(
                    args,
                    command="sync.prices",
                    code=exc.code,
                    message="Wishlist price synchronization did not complete.",
                    retryable=exc.retryable,
                )
    warnings = []
    if args.provider == "auto" and resolved["state"] != "configured":
        warnings.append(
            WarningRecord(
                code=_credential_error_code(resolved["state"]),
                message=(
                    "GG.deals was not attempted because its credential is unavailable; "
                    "the bounded CheapShark fallback was used."
                ),
            )
        )
    if result.completeness == "partial":
        warnings.append(
            WarningRecord(
                code=ErrorCode.PARTIAL_SCAN,
                message=(
                    "The requested synchronization did not complete every required "
                    "deal-evidence evaluation."
                ),
            )
        )
    return _emit_success(
        args,
        command="sync.prices",
        context={
            "account_alias": account.alias,
            "country": country,
            "currency": "USD",
            "scope": "wishlist",
            "identifiers_included": False,
        },
        completeness_value=completeness(
            CompletenessStatus(result.completeness), warnings=warnings
        ),
        data={
            "sync_runs": [
                {
                    "id": run.id,
                    "provider": run.provider,
                    "status": run.status,
                    "error_code": run.error_code,
                }
                for run in result.runs
            ],
            "provider_selection": args.provider,
            "providers_used": list(result.providers_used),
            "providers_attempted": list(result.providers_attempted),
            "evaluated_items": result.evaluated_items,
            "total_items": result.total_items,
            "observed_items": result.observed_items,
            "fallback_evaluated": result.fallback_evaluated,
            "fallback_total": result.fallback_total,
            "current_freshness_seconds": 6 * 60 * 60,
            "historical_low_freshness_seconds": 24 * 60 * 60,
            "hard_expiry_seconds": 7 * 24 * 60 * 60,
            "raw_payload_retained": False,
            "limitations": [
                "GG.deals exposes summary lows rather than a price-event graph",
                "CheapShark is USD-only and groups offers at game level",
                "provider links are manual-only and are never followed",
            ],
        },
    )


def _dispatch_sync_catalog(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        credential_ref = _steam_credential_ref(database_path)
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                return _emit_error(
                    args,
                    command="sync.catalog",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            if account is None:
                return _emit_error(
                    args,
                    command="sync.catalog",
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            # Demand derivation deliberately avoids catalog reads. A malformed
            # retained catalog projection must not prevent an explicit repair.
            demanded = storage.read_catalog_demand(account.id, args.machine)
            secret: SecretValue | None = None
            if demanded:
                metadata = storage.get_credential_reference(
                    provider=credential_ref.provider,
                    kind=credential_ref.kind,
                    profile_id=credential_ref.profile_id,
                )
                if metadata is None:
                    return _emit_error(
                        args,
                        command="sync.catalog",
                        code=ErrorCode.AUTH_REQUIRED,
                        message="A Steam Web API user key has not been configured.",
                    )
                resolved = _resolve_credential(metadata, credential_ref)
                if resolved["state"] != "configured":
                    return _emit_error(
                        args,
                        command="sync.catalog",
                        code=_credential_error_code(resolved["state"]),
                        message="The Steam Web API credential is unavailable.",
                    )
                secret = resolved["secret"]

            def request_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "steam-web-api",
                        _utc_now(),
                        _PROVIDER_MINIMUM_INTERVAL_SECONDS,
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise CatalogApiError("REQUEST_THROTTLED", retryable=True)

            try:
                result = sync_catalog(
                    storage,
                    account_id=account.id,
                    machine_id=args.machine,
                    demanded_appids=demanded,
                    api_key=secret,
                    client=SteamStoreCatalogClient(request_gate=request_gate),
                    clock=_utc_now,
                )
            except CatalogSyncError as exc:
                return _emit_error(
                    args,
                    command="sync.catalog",
                    code=exc.code,
                    message="The bounded Steam catalog synchronization did not complete.",
                    retryable=exc.retryable,
                )
    return _emit_success(
        args,
        command="sync.catalog",
        context={
            "account_alias": account.alias,
            "machine_id": args.machine,
            "identifiers_included": False,
        },
        data={
            "sync_run_id": result.run.id,
            "sync_status": result.run.status,
            "demanded_count": result.demanded_count,
            "game_count": result.game_count,
            "non_game_count": result.non_game_count,
            "not_observed_count": result.not_observed_count,
            "page_count": result.page_count,
            "persistence_scope": "demanded_appids_only",
            "upstream_scan_scope": "ordered_catalog_through_highest_demanded_appid",
            "identity_limitations": [
                "packages_not_collected",
                "bundles_not_collected",
                "editions_not_collected",
                "non_game_subtype_not_distinguished",
            ],
        },
    )


def _account_snapshot_completeness(
    snapshot: Any, *, capability: str, subject: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    latest = snapshot.latest
    latest_complete = snapshot.latest_complete
    last_good_stale = False
    if (
        capability in ("owned.visible.read", "wishlist.read")
        and latest_complete is not None
    ):
        last_good_at = datetime.fromisoformat(
            latest_complete.completed_at.replace("Z", "+00:00")
        )
        freshness_seconds = (
            _OWNED_SYNC_FRESHNESS_SECONDS
            if capability == "owned.visible.read"
            else _WISHLIST_SYNC_FRESHNESS_SECONDS
        )
        last_good_stale = (
            _utc_now() - last_good_at
        ).total_seconds() > freshness_seconds
    if latest is None:
        return completeness(
            CompletenessStatus.UNAVAILABLE,
            missing_capabilities=[capability],
            warnings=[
                WarningRecord(
                    code=ErrorCode.NOT_SYNCED,
                    message=f"{subject} have not been synchronized.",
                )
            ],
        ), {"last_attempt_status": None, "last_successful_sync_at": None}
    if latest.status == "complete":
        if last_good_stale:
            return completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=[capability],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message=f"The {subject.lower()} snapshot is older than the freshness policy.",
                    )
                ],
            ), {
                "last_attempt_status": "complete",
                "last_successful_sync_at": latest.completed_at,
            }
        return completeness(CompletenessStatus.COMPLETE), {
            "last_attempt_status": "complete",
            "last_successful_sync_at": latest.completed_at,
        }
    if latest.status == "running":
        started_at = datetime.fromisoformat(latest.started_at.replace("Z", "+00:00"))
        abandoned = (_utc_now() - started_at).total_seconds() > _SYNC_ABANDONED_SECONDS
        warning = WarningRecord(
            code=(
                ErrorCode.SYNC_ABANDONED if abandoned else ErrorCode.SYNC_IN_PROGRESS
            ),
            message=(
                f"The last {subject.lower()} synchronization appears abandoned."
                if abandoned
                else f"A {subject.lower()} synchronization is in progress."
            ),
        )
        if latest_complete is None:
            value = completeness(
                CompletenessStatus.UNAVAILABLE,
                missing_capabilities=[capability],
                warnings=[warning],
            )
        elif last_good_stale:
            value = completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=[capability],
                warnings=[
                    warning,
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message=(
                            f"The {subject.lower()} snapshot is older than the "
                            "freshness policy."
                        ),
                    ),
                ],
            )
        else:
            value = completeness(
                CompletenessStatus.PARTIAL
                if abandoned
                else CompletenessStatus.COMPLETE,
                stale_capabilities=[capability] if abandoned else [],
                warnings=[warning],
            )
        return value, {
            "last_attempt_status": "running",
            "last_successful_sync_at": (
                None if latest_complete is None else latest_complete.completed_at
            ),
        }
    has_last_good = latest_complete is not None
    attempt_was_partial = latest.status == "partial"
    warning_code = (
        str(ErrorCode.STALE_LAST_GOOD)
        if has_last_good
        else (latest.error_code or str(ErrorCode.STALE_LAST_GOOD))
    )
    warning = WarningRecord(
        code=warning_code,
        message=(
            (
                f"The latest {subject.lower()} synchronization "
                f"{'was incomplete' if attempt_was_partial else 'failed'}; "
                "the last-good snapshot is preserved."
            )
            if has_last_good
            else (
                f"The latest {subject.lower()} synchronization "
                f"{'was incomplete' if attempt_was_partial else 'failed'} and no "
                "complete snapshot exists."
            )
        ),
    )
    if latest_complete is None:
        value = completeness(
            CompletenessStatus.UNAVAILABLE,
            missing_capabilities=[capability],
            warnings=[warning],
        )
    else:
        value = completeness(
            CompletenessStatus.PARTIAL,
            stale_capabilities=[capability],
            warnings=[warning],
        )
    return value, {
        "last_attempt_status": latest.status,
        "last_error_code": latest.error_code,
        "last_successful_sync_at": (
            None if latest_complete is None else latest_complete.completed_at
        ),
    }


def _owned_provenance(snapshot: Any) -> dict[str, Any] | None:
    provenance = snapshot.latest_complete_provenance
    if provenance is None:
        return None
    return {
        "sync_run_id": provenance.sync_run_id,
        "provider": provenance.provider,
        "support_level": provenance.support_level,
        "include_appinfo": provenance.include_appinfo,
        "base": {
            "include_played_free_games": (provenance.base_include_played_free_games),
            "retrieved_at": provenance.base_retrieved_at,
            "reported_count": provenance.base_reported_count,
        },
        "expanded": {
            "include_played_free_games": (
                provenance.expanded_include_played_free_games
            ),
            "retrieved_at": provenance.expanded_retrieved_at,
            "reported_count": provenance.expanded_reported_count,
        },
        "classification_method": provenance.classification_method,
    }


def _catalog_sources(snapshot: Any) -> list[dict[str, Any]]:
    return [
        {
            "sync_run_id": source.sync_run_id,
            "provider": source.provider,
            "support_level": source.support_level,
            "streams": [
                {
                    "stream": stream.stream,
                    "termination": stream.termination,
                    "scanned_through_appid": stream.scanned_through_appid,
                    "filter_context": dict(stream.filter_context),
                    "pages": [
                        {
                            "page_number": page.page_number,
                            "requested_last_appid": page.requested_last_appid,
                            "first_appid": page.first_appid,
                            "last_appid": page.last_appid,
                            "item_count": page.item_count,
                            "have_more_results": page.have_more_results,
                            "retrieved_at": page.retrieved_at,
                        }
                        for page in stream.pages
                    ],
                }
                for stream in source.streams
            ],
        }
        for source in snapshot.sources
    ]


def _catalog_completeness(
    snapshot: Any, *, demanded_appids: set[int]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not demanded_appids:
        return completeness(CompletenessStatus.COMPLETE), {
            "last_attempt_status": None,
            "last_error_code": None,
            "last_attempt_sync_run_id": None,
            "relevant_attempts": [],
            "freshness_window_seconds": _CATALOG_SYNC_FRESHNESS_SECONDS,
            "sources": [],
            "oldest_fact_observed_at": None,
            "newest_fact_observed_at": None,
            "stale_fact_count": 0,
        }
    observed = {fact.appid for fact in snapshot.facts}
    missing = demanded_appids - observed
    relevant_attempts = tuple(snapshot.attempts)
    if not relevant_attempts and snapshot.latest is not None:
        # Compatibility for callers constructing the pre-aggregate snapshot
        # shape directly; storage-backed scoped reads always provide attempts.
        attempt_values = ((snapshot.latest, tuple(sorted(demanded_appids))),)
    else:
        attempt_values = tuple(
            (attempt.run, attempt.appids) for attempt in relevant_attempts
        )
    attempted_appids = {appid for _, appids in attempt_values for appid in appids}
    missing_attempts = demanded_appids - attempted_appids
    sole_attempt = attempt_values[0][0] if len(attempt_values) == 1 else None
    metadata = {
        "last_attempt_status": None if sole_attempt is None else sole_attempt.status,
        "last_error_code": None if sole_attempt is None else sole_attempt.error_code,
        "last_attempt_sync_run_id": None if sole_attempt is None else sole_attempt.id,
        "relevant_attempts": [
            {
                "sync_run_id": run.id,
                "status": run.status,
                "error_code": run.error_code,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "appids": list(appids),
            }
            for run, appids in attempt_values
        ],
        "freshness_window_seconds": _CATALOG_SYNC_FRESHNESS_SECONDS,
        "sources": _catalog_sources(snapshot),
    }
    observed_times = [
        datetime.fromisoformat(fact.observed_at.replace("Z", "+00:00"))
        for fact in snapshot.facts
    ]
    stale_fact_count = sum(
        (_utc_now() - observed_at).total_seconds() > _CATALOG_SYNC_FRESHNESS_SECONDS
        for observed_at in observed_times
    )
    metadata.update(
        {
            "oldest_fact_observed_at": (
                None
                if not observed_times
                else min(observed_times).isoformat().replace("+00:00", "Z")
            ),
            "newest_fact_observed_at": (
                None
                if not observed_times
                else max(observed_times).isoformat().replace("+00:00", "Z")
            ),
            "stale_fact_count": stale_fact_count,
        }
    )
    failed_attempts = tuple(
        run for run, _ in attempt_values if run.status in ("failed", "partial")
    )
    running_attempts = tuple(
        run for run, _ in attempt_values if run.status == "running"
    )
    abandoned_running = bool(running_attempts) and any(
        (
            _utc_now() - datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))
        ).total_seconds()
        > _SYNC_ABANDONED_SECONDS
        for run in running_attempts
    )
    refresh_warning = (
        None
        if not running_attempts
        else WarningRecord(
            code=(
                ErrorCode.SYNC_ABANDONED
                if abandoned_running
                else ErrorCode.SYNC_IN_PROGRESS
            ),
            message=(
                "The catalog synchronization appears abandoned."
                if abandoned_running
                else "A catalog synchronization is in progress."
            ),
        )
    )
    if missing or missing_attempts:
        warnings = [
            WarningRecord(
                code=ErrorCode.NOT_SYNCED,
                message=(
                    "Catalog facts or scoped synchronization attempts are "
                    "missing for observed application identities."
                ),
            )
        ]
        if refresh_warning is not None:
            warnings.append(refresh_warning)
        if failed_attempts:
            failed_code = next(
                (
                    run.error_code
                    for run in reversed(failed_attempts)
                    if run.error_code is not None
                ),
                str(ErrorCode.STALE_LAST_GOOD),
            )
            warnings.append(
                WarningRecord(
                    code=failed_code,
                    message=(
                        "A relevant catalog synchronization failed or was "
                        "incomplete before it produced a last-good fact."
                    ),
                )
            )
        active_or_failed = bool(running_attempts or failed_attempts)
        return completeness(
            (
                CompletenessStatus.PARTIAL
                if active_or_failed and not missing_attempts
                else CompletenessStatus.UNAVAILABLE
            ),
            missing_capabilities=["catalog.application.read"],
            warnings=warnings,
        ), metadata
    if failed_attempts:
        warnings = [
            WarningRecord(
                code=ErrorCode.STALE_LAST_GOOD,
                message=(
                    "At least one demanded AppID has a newer failed or incomplete "
                    "catalog attempt; retained subject facts remain last-good."
                ),
            )
        ]
        if refresh_warning is not None:
            warnings.append(refresh_warning)
        return completeness(
            CompletenessStatus.PARTIAL,
            stale_capabilities=["catalog.application.read"],
            warnings=warnings,
        ), metadata
    if running_attempts:
        assert refresh_warning is not None
        if stale_fact_count:
            return completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=["catalog.application.read"],
                warnings=[
                    refresh_warning,
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message=(
                            "One or more retained catalog facts are older than "
                            "the 24-hour freshness window."
                        ),
                    ),
                ],
            ), metadata
        return completeness(
            (
                CompletenessStatus.PARTIAL
                if abandoned_running
                else CompletenessStatus.COMPLETE
            ),
            stale_capabilities=(
                ["catalog.application.read"] if abandoned_running else []
            ),
            warnings=[refresh_warning],
        ), metadata
    if stale_fact_count:
        return completeness(
            CompletenessStatus.PARTIAL,
            stale_capabilities=["catalog.application.read"],
            warnings=[
                WarningRecord(
                    code=ErrorCode.STALE_LAST_GOOD,
                    message=(
                        "One or more retained catalog facts are older than the "
                        "24-hour freshness window."
                    ),
                )
            ],
        ), metadata
    return completeness(CompletenessStatus.COMPLETE), metadata


def _dispatch_wishlist_games_query(
    args: argparse.Namespace, database_path: Path
) -> int:
    if args.include_paths:
        return _emit_error(
            args,
            command="games.query",
            code=ErrorCode.INVALID_ARGUMENT,
            message="--include-paths is available only for installed-scope queries.",
            exit_code=2,
        )
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command="games.query",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            return _emit_success(
                args,
                command="games.query",
                context={
                    "account_alias": args.account,
                    "scopes": ["wishlist"],
                    "identifiers_included": False,
                },
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
                data={
                    "items": [],
                    "empty": False,
                    "next_cursor": None,
                    "source": None,
                    "snapshot": {
                        "last_attempt_status": None,
                        "last_successful_sync_at": None,
                    },
                },
            )
        snapshot = storage.read_wishlist_snapshot(account.id)
    value, metadata = _account_snapshot_completeness(
        snapshot, capability="wishlist.read", subject="Wishlist items"
    )
    stable_ids = dict(snapshot.stable_game_ids_by_appid)
    source = snapshot.latest_complete_provenance
    return _emit_success(
        args,
        command="games.query",
        context={
            "account_alias": account.alias,
            "scopes": ["wishlist"],
            "identifiers_included": False,
        },
        completeness_value=value,
        data={
            "items": [
                {
                    "appid": game.appid,
                    "game_id": f"game:{stable_ids[game.appid]}",
                    "wishlisted": True,
                    "priority": game.priority,
                    "date_added_unix": game.date_added,
                    "observed_at": game.observed_at,
                    "evidence_ids": [game.evidence_id],
                }
                for game in snapshot.games
            ],
            "empty": bool(snapshot.latest_complete is not None and not snapshot.games),
            "next_cursor": None,
            "source": (
                None
                if source is None
                else {
                    "provider": source.provider,
                    "support_level": source.support_level,
                    "validation_method": source.validation_method,
                    "item_list_retrieved_at": source.item_list_retrieved_at,
                    "item_count_retrieved_at": source.item_count_retrieved_at,
                    "reported_count": source.item_count_reported_count,
                }
            ),
            "snapshot": metadata,
            "limitations": [
                "provider_contract_is_provisional",
                "empty_auth_like_response_is_ambiguous",
            ],
        },
    )


def _dispatch_account_games_query(args: argparse.Namespace, database_path: Path) -> int:
    if args.include_paths:
        return _emit_error(
            args,
            command="games.query",
            code=ErrorCode.INVALID_ARGUMENT,
            message="--include-paths is available only for installed-scope queries.",
            exit_code=2,
        )
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command="games.query",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            unavailable_snapshot = {
                "last_attempt_status": None,
                "last_successful_sync_at": None,
            }
            if args.scope == "owned":
                empty_data: dict[str, Any] = {
                    "items": [],
                    "empty": False,
                    "limitations": [
                        "individually_private_games_may_be_omitted",
                        "unplayed_free_entitlements_are_not_complete",
                        "sequential_request_difference_may_reflect_concurrent_library_change",
                    ],
                    "next_cursor": None,
                    "source": None,
                    "snapshot": unavailable_snapshot,
                }
            else:
                empty_data = {
                    "items": [],
                    "limitations": [
                        "individually_private_games_may_be_omitted",
                        "unplayed_free_entitlements_are_not_complete",
                        "sequential_request_difference_may_reflect_concurrent_library_change",
                    ],
                    "next_cursor": None,
                    "snapshots": {
                        "owned": {**unavailable_snapshot, "source": None},
                        "installed": unavailable_snapshot,
                        "catalog": {
                            "last_attempt_status": None,
                            "last_error_code": None,
                            "sources": [],
                        },
                    },
                }
            return _emit_success(
                args,
                command="games.query",
                context={
                    "account_alias": args.account,
                    "scopes": (
                        ["owned", "installed", "catalog"]
                        if args.scope == "library"
                        else ["owned"]
                    ),
                    "identifiers_included": False,
                    **({"machine_id": args.machine} if args.scope == "library" else {}),
                },
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
                data=empty_data,
            )
        if args.scope == "owned":
            owned_snapshot = storage.read_owned_snapshot(account.id)
            owned_game_ids = dict(owned_snapshot.stable_game_ids_by_appid)
            owned_completeness, metadata = _account_snapshot_completeness(
                owned_snapshot,
                capability="owned.visible.read",
                subject="Owned games",
            )
            return _emit_success(
                args,
                command="games.query",
                context={
                    "account_alias": account.alias,
                    "scopes": ["owned"],
                    "identifiers_included": False,
                },
                completeness_value=owned_completeness,
                data={
                    "items": [
                        {
                            **owned_item(game),
                            "game_id": f"game:{owned_game_ids[game.appid]}",
                        }
                        for game in owned_snapshot.games
                    ],
                    "empty": bool(
                        owned_snapshot.latest_complete is not None
                        and not owned_snapshot.games
                    ),
                    "limitations": [
                        "individually_private_games_may_be_omitted",
                        "unplayed_free_entitlements_are_not_complete",
                        "sequential_request_difference_may_reflect_concurrent_library_change",
                    ],
                    "next_cursor": None,
                    "source": _owned_provenance(owned_snapshot),
                    "snapshot": metadata,
                },
            )
        library = storage.read_library_snapshot(account.id, args.machine)

    owned_completeness, owned_metadata = _account_snapshot_completeness(
        library.owned,
        capability="owned.visible.read",
        subject="Owned games",
    )
    installed_completeness, installed_metadata = _account_snapshot_completeness(
        library.installed,
        capability="installed.read",
        subject="Installed games",
    )
    catalog_completeness, catalog_metadata = _catalog_completeness(
        library.catalog,
        demanded_appids={
            *(game.appid for game in library.owned.games),
            *(game.appid for game in library.installed.games),
        },
    )
    owned_usable = library.owned.latest_complete is not None
    installed_usable = library.installed.latest_complete is not None
    installed_types_by_appid = {
        game.appid: game.app_type for game in library.installed.games
    }
    entity_ids = dict(library.stable_game_ids_by_appid)
    by_appid: dict[int, dict[str, Any]] = {}
    for game in library.owned.games:
        by_appid[game.appid] = {
            **owned_item(game),
            "game_id": f"game:{entity_ids[game.appid]}",
            "installed": False if installed_usable else None,
            "app_type": "unknown",
            "names": {"owned": game.name, "installed": None},
        }
    for game in library.installed.games:
        item = by_appid.setdefault(
            game.appid,
            {
                "appid": game.appid,
                "game_id": f"game:{entity_ids[game.appid]}",
                "name": game.name,
                "visible_in_owned_games": False if owned_usable else None,
                "inclusion_basis": None,
                "playtime_forever_minutes": None,
                "observed_at": game.observed_at,
                "evidence_ids": [],
                "family_available": None,
                "purchasable": None,
                "playable_now": None,
                "names": {"owned": None, "installed": game.name},
            },
        )
        item["installed"] = True
        item["app_type"] = game.app_type
        item["names"]["installed"] = game.name
        if item["name"] is None and game.name is not None:
            item["name"] = game.name
        item["evidence_ids"] = sorted({*item["evidence_ids"], game.evidence_id})
    catalog_by_appid = {fact.appid: fact for fact in library.catalog.facts}
    for appid, item in by_appid.items():
        fact = catalog_by_appid.get(appid)
        item["catalog_classification"] = None if fact is None else fact.classification
        item["catalog_observed_at"] = None if fact is None else fact.observed_at
        item["catalog_evidence_ids"] = [] if fact is None else [fact.evidence_id]
        item["app_types"] = {
            "installed": installed_types_by_appid.get(appid),
            "catalog": None if fact is None else fact.classification,
        }
        if fact is not None and fact.classification in ("game", "non_game"):
            item["app_type"] = fact.classification
        if fact is not None:
            item["evidence_ids"] = sorted({*item["evidence_ids"], fact.evidence_id})
        item["identity"] = {
            "entity_kind": "application",
            "external_identities": [
                {
                    "provider": "steam",
                    "identity_kind": "application_appid",
                    "value": str(appid),
                }
            ],
            "package": None,
            "bundle": None,
            "edition": None,
        }
    warnings = [
        *owned_completeness["warnings"],
        *installed_completeness["warnings"],
        *catalog_completeness["warnings"],
    ]
    missing = sorted(
        {
            *owned_completeness["missing_capabilities"],
            *installed_completeness["missing_capabilities"],
            *catalog_completeness["missing_capabilities"],
        }
    )
    stale = sorted(
        {
            *owned_completeness["stale_capabilities"],
            *installed_completeness["stale_capabilities"],
            *catalog_completeness["stale_capabilities"],
        }
    )
    if missing and not (owned_usable or installed_usable):
        status = CompletenessStatus.UNAVAILABLE
    elif missing:
        status = CompletenessStatus.PARTIAL
    elif stale or any(
        value["status"] == "partial"
        for value in (
            owned_completeness,
            installed_completeness,
            catalog_completeness,
        )
    ):
        status = CompletenessStatus.PARTIAL
    else:
        status = CompletenessStatus.COMPLETE
    return _emit_success(
        args,
        command="games.query",
        context={
            "account_alias": account.alias,
            "machine_id": args.machine,
            "scopes": ["owned", "installed", "catalog"],
            "identifiers_included": False,
        },
        completeness_value=completeness(
            status,
            warnings=warnings,
            missing_capabilities=missing,
            stale_capabilities=stale,
        ),
        data={
            "items": [by_appid[appid] for appid in sorted(by_appid)],
            "limitations": [
                "individually_private_games_may_be_omitted",
                "unplayed_free_entitlements_are_not_complete",
                "sequential_request_difference_may_reflect_concurrent_library_change",
            ],
            "next_cursor": None,
            "snapshots": {
                "owned": {
                    **owned_metadata,
                    "source": _owned_provenance(library.owned),
                },
                "installed": installed_metadata,
                "catalog": catalog_metadata,
            },
        },
    )


def _dispatch_data(args: argparse.Namespace, database_path: Path) -> int:
    if args.data_command != "delete":
        raise AssertionError("unhandled data command")
    if not args.yes:
        return _emit_error(
            args,
            command="data.delete",
            code=ErrorCode.CONFIRMATION_REQUIRED,
            message="Steam Web API data deletion requires --yes.",
        )
    if args.provider in {"gg-deals", "cheapshark"}:
        return _delete_price_provider_data(args, database_path)
    with _credential_operation_lock(database_path):
        if args.account is not None:
            with Storage(database_path) as storage:
                try:
                    account = storage.get_account(args.account)
                except ValueError:
                    return _emit_error(
                        args,
                        command="data.delete",
                        code=ErrorCode.INVALID_ARGUMENT,
                        message="The account alias is invalid.",
                        exit_code=2,
                    )
                if account is None:
                    return _emit_success(
                        args,
                        command="data.delete",
                        data={
                            "scope": "account",
                            "account_alias": args.account,
                            "removed": False,
                            "owned_observations_removed": 0,
                            "owned_current_removed": 0,
                            "wishlist_observations_removed": 0,
                            "wishlist_current_removed": 0,
                            "price_observations_removed": 0,
                            "price_current_removed": 0,
                            "price_subjects_removed": 0,
                            "sync_runs_removed": 0,
                            "probes_removed": 0,
                            "consents_removed": 0,
                            "evidence_removed": 0,
                            "orphan_apps_removed": 0,
                            "shared_credential_preserved": True,
                            "backup_copies_require_separate_deletion": True,
                        },
                    )
                result = storage.delete_steam_account_data(account.id)
            return _emit_success(
                args,
                command="data.delete",
                data={
                    "scope": "account",
                    "account_alias": args.account,
                    "removed": result.account_removed,
                    "owned_observations_removed": result.owned_observations_removed,
                    "owned_current_removed": result.owned_current_removed,
                    "wishlist_observations_removed": result.wishlist_observations_removed,
                    "wishlist_current_removed": result.wishlist_current_removed,
                    "price_observations_removed": result.price_observations_removed,
                    "price_current_removed": result.price_current_removed,
                    "price_subjects_removed": result.price_subjects_removed,
                    "sync_runs_removed": result.sync_runs_removed,
                    "probes_removed": result.probes_removed,
                    "consents_removed": result.consents_removed,
                    "evidence_removed": result.evidence_removed,
                    "orphan_apps_removed": result.orphan_apps_removed,
                    "shared_credential_preserved": True,
                    "backup_copies_require_separate_deletion": True,
                },
            )
        return _delete_all_steam_web_api_data(args, database_path)


def _delete_price_provider_data(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        if args.account is not None:
            with Storage(database_path) as storage:
                try:
                    account = storage.get_account(args.account)
                except ValueError:
                    return _emit_error(
                        args,
                        command="data.delete",
                        code=ErrorCode.INVALID_ARGUMENT,
                        message="The account alias is invalid.",
                        exit_code=2,
                    )
                if account is None:
                    return _emit_success(
                        args,
                        command="data.delete",
                        data={
                            "scope": "account-provider",
                            "provider": args.provider,
                            "account_alias": args.account,
                            "price_observations_removed": 0,
                            "price_current_removed": 0,
                            "price_subjects_removed": 0,
                            "sync_runs_removed": 0,
                            "evidence_removed": 0,
                            "account_preserved": True,
                            "credential_preserved": True,
                            "backup_copies_require_separate_deletion": True,
                        },
                    )
                deletion = storage.delete_price_data(
                    provider=args.provider, account_id=account.id
                )
            return _emit_success(
                args,
                command="data.delete",
                data={
                    "scope": "account-provider",
                    "provider": args.provider,
                    "account_alias": args.account,
                    "price_observations_removed": deletion.observations_removed,
                    "price_current_removed": deletion.current_removed,
                    "price_subjects_removed": deletion.subjects_removed,
                    "sync_runs_removed": deletion.sync_runs_removed,
                    "evidence_removed": deletion.evidence_removed,
                    "account_preserved": True,
                    "credential_preserved": True,
                    "backup_copies_require_separate_deletion": True,
                },
            )

        metadata = None
        credential_ref = None
        store = None
        previous_secret = None
        credential_unreadable = False
        credential_deleted = False
        if args.provider == "gg-deals":
            credential_ref = _provider_credential_ref(
                database_path, _CREDENTIAL_PROVIDERS["gg-deals"]
            )
            with Storage(database_path) as storage:
                metadata = storage.get_credential_reference(
                    provider=credential_ref.provider,
                    kind=credential_ref.kind,
                    profile_id=credential_ref.profile_id,
                )
            if metadata is not None:
                store = _credential_store(metadata.backend, metadata.backend_locator)
                try:
                    previous_secret = store.resolve(credential_ref)
                except CredentialError as exc:
                    if exc.code != "CREDENTIAL_READ_FAILED":
                        raise
                    credential_unreadable = True
        try:
            if store is not None and (
                previous_secret is not None or credential_unreadable
            ):
                if not store.delete(credential_ref):
                    raise CredentialError(str(ErrorCode.CREDENTIAL_DELETE_FAILED))
                credential_deleted = True
            with Storage(database_path) as storage:
                deletion = storage.delete_price_data(
                    provider=args.provider,
                    credential_kind=(None if metadata is None else credential_ref.kind),
                    credential_profile_id=(
                        None if metadata is None else credential_ref.profile_id
                    ),
                )
        except BaseException:
            if store is not None and previous_secret is not None:
                try:
                    store.put(credential_ref, previous_secret)
                except BaseException:
                    return _emit_error(
                        args,
                        command="data.delete",
                        code=ErrorCode.CREDENTIAL_ROLLBACK_FAILED,
                        message="Provider deletion failed and the key could not be restored.",
                    )
            elif credential_deleted and credential_unreadable:
                return _emit_error(
                    args,
                    command="data.delete",
                    code=ErrorCode.CREDENTIAL_ROLLBACK_FAILED,
                    message=(
                        "Provider deletion failed after an unreadable locally managed "
                        "key was removed. The database was retained, but the key could "
                        "not be restored."
                    ),
                )
            raise
        return _emit_success(
            args,
            command="data.delete",
            data={
                "scope": "provider-all",
                "provider": args.provider,
                "price_observations_removed": deletion.observations_removed,
                "price_current_removed": deletion.current_removed,
                "price_subjects_removed": deletion.subjects_removed,
                "sync_runs_removed": deletion.sync_runs_removed,
                "evidence_removed": deletion.evidence_removed,
                "credential_refs_removed": deletion.credential_refs_removed,
                "local_credential_removed": credential_deleted,
                "steam_account_data_preserved": True,
                "other_provider_data_preserved": True,
                "backup_copies_require_separate_deletion": True,
            },
        )


def _delete_all_steam_web_api_data(
    args: argparse.Namespace, database_path: Path
) -> int:
    credential_ref = _steam_credential_ref(database_path)
    with Storage(database_path) as storage:
        metadata = storage.get_credential_reference(
            provider=credential_ref.provider,
            kind=credential_ref.kind,
            profile_id=credential_ref.profile_id,
        )
    store = None
    previous_secret = None
    credential_unreadable = False
    credential_deleted = False
    if metadata is not None:
        store = _credential_store(metadata.backend, metadata.backend_locator)
        try:
            previous_secret = store.resolve(credential_ref)
        except CredentialError as exc:
            if exc.code != "CREDENTIAL_READ_FAILED":
                raise
            credential_unreadable = True
    try:
        if store is not None and (previous_secret is not None or credential_unreadable):
            if not store.delete(credential_ref):
                raise CredentialError(str(ErrorCode.CREDENTIAL_DELETE_FAILED))
            credential_deleted = True
        with Storage(database_path) as storage:
            deletion = storage.delete_all_steam_account_data(
                credential_provider=(
                    None if metadata is None else credential_ref.provider
                ),
                credential_kind=None if metadata is None else credential_ref.kind,
                credential_profile_id=(
                    None if metadata is None else credential_ref.profile_id
                ),
            )
    except BaseException:
        if store is not None and previous_secret is not None:
            try:
                store.put(credential_ref, previous_secret)
            except BaseException:
                return _emit_error(
                    args,
                    command="data.delete",
                    code=ErrorCode.CREDENTIAL_ROLLBACK_FAILED,
                    message=(
                        "Account-data deletion failed and the locally managed key "
                        "could not be restored."
                    ),
                )
        elif credential_deleted and credential_unreadable:
            return _emit_error(
                args,
                command="data.delete",
                code=ErrorCode.CREDENTIAL_ROLLBACK_FAILED,
                message=(
                    "Account-data deletion failed after an unreadable locally "
                    "managed key was removed. The database was retained, but "
                    "the key could not be restored."
                ),
            )
        raise
    return _emit_success(
        args,
        command="data.delete",
        data={
            "scope": "all-steam-web-api",
            "accounts_removed": deletion.accounts_removed,
            "owned_observations_removed": deletion.owned_observations_removed,
            "owned_current_removed": deletion.owned_current_removed,
            "wishlist_observations_removed": deletion.wishlist_observations_removed,
            "wishlist_current_removed": deletion.wishlist_current_removed,
            "price_observations_removed": deletion.price_observations_removed,
            "price_current_removed": deletion.price_current_removed,
            "price_subjects_removed": deletion.price_subjects_removed,
            "sync_runs_removed": deletion.sync_runs_removed,
            "probes_removed": deletion.probes_removed,
            "consents_removed": deletion.consents_removed,
            "evidence_removed": deletion.evidence_removed,
            "orphan_apps_removed": deletion.orphan_apps_removed,
            "credential_refs_removed": deletion.credential_refs_removed,
            "catalog_observations_removed": (deletion.catalog_observations_removed),
            "catalog_current_removed": deletion.catalog_current_removed,
            "catalog_sync_runs_removed": deletion.catalog_sync_runs_removed,
            "catalog_metadata_removed": deletion.catalog_metadata_removed,
            "catalog_streams_removed": deletion.catalog_streams_removed,
            "catalog_pages_removed": deletion.catalog_pages_removed,
            "catalog_evidence_removed": deletion.catalog_evidence_removed,
            "shared_credential_preserved": deletion.shared_credential_preserved,
            "local_credential_removed": (
                previous_secret is not None or credential_unreadable
            ),
            "credential_already_absent": (
                metadata is not None
                and previous_secret is None
                and not credential_unreadable
            ),
            "valve_key_revoked": False,
            "backup_copies_require_separate_deletion": True,
        },
    )


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
                    ["account.identity"]
                    if status == CompletenessStatus.UNAVAILABLE
                    else []
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
                account = storage.get_account(args.alias)
                deletion = (
                    None
                    if account is None
                    else storage.delete_steam_account_data(account.id)
                )
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
            data={
                "alias": args.alias,
                "removed": deletion is not None and deletion.account_removed,
                "owned_observations_removed": (
                    0 if deletion is None else deletion.owned_observations_removed
                ),
                "owned_current_removed": (
                    0 if deletion is None else deletion.owned_current_removed
                ),
                "wishlist_observations_removed": (
                    0 if deletion is None else deletion.wishlist_observations_removed
                ),
                "wishlist_current_removed": (
                    0 if deletion is None else deletion.wishlist_current_removed
                ),
                "price_observations_removed": (
                    0 if deletion is None else deletion.price_observations_removed
                ),
                "price_current_removed": (
                    0 if deletion is None else deletion.price_current_removed
                ),
                "price_subjects_removed": (
                    0 if deletion is None else deletion.price_subjects_removed
                ),
                "sync_runs_removed": 0
                if deletion is None
                else deletion.sync_runs_removed,
                "probes_removed": 0 if deletion is None else deletion.probes_removed,
                "consents_removed": 0
                if deletion is None
                else deletion.consents_removed,
                "evidence_removed": 0
                if deletion is None
                else deletion.evidence_removed,
                "orphan_apps_removed": (
                    0 if deletion is None else deletion.orphan_apps_removed
                ),
                "shared_credential_preserved": True,
                "backup_copies_require_separate_deletion": True,
            },
        )
    raise AssertionError("unhandled accounts command")


def _dispatch_auth(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        return _dispatch_auth_locked(args, database_path)


def _dispatch_auth_locked(args: argparse.Namespace, database_path: Path) -> int:
    spec = _CREDENTIAL_PROVIDERS[args.provider]
    credential_ref = _provider_credential_ref(database_path, spec)
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
            first = _hidden_input(f"{spec.prompt_label}: ")
            second = _hidden_input(f"Confirm {spec.prompt_label}: ")
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
                store = _credential_store(existing.backend, existing.backend_locator)
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
                    capability=spec.dependent_capability,
                )
            except BaseException:
                try:
                    if previous_secret is None:
                        deleted = store.delete(credential_ref)
                        if put_completed and not deleted:
                            raise CredentialError("CREDENTIAL_ROLLBACK_FAILED")
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
        warnings = _credential_warnings(
            snapshot["state"], credential_label=spec.display_label
        )
        return _emit_success(
            args,
            command="auth.status",
            completeness_value=completeness(
                status,
                warnings=warnings,
                missing_capabilities=(
                    [spec.missing_capability]
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
    if args.auth_command == "probe":
        with Storage(database_path) as storage:
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
        resolved = (
            {"state": "missing", "secret": None, "error_code": None}
            if metadata is None
            else _resolve_credential(metadata, credential_ref)
        )
        if resolved["state"] != "configured":
            if resolved["state"] == "store_locked":
                code = ErrorCode.CREDENTIAL_STORE_LOCKED
                message = "The configured credential store is locked."
            elif resolved["state"] == "store_unavailable":
                code = ErrorCode.CREDENTIAL_STORE_UNAVAILABLE
                message = "The configured credential store is unavailable."
            else:
                code = ErrorCode.AUTH_REQUIRED
                message = f"A {spec.display_label} has not been configured."
            return _emit_error(
                args,
                command="auth.probe",
                code=code,
                message=message,
            )
        now = _utc_now()
        if not _reserve_provider_request(
            args.provider,
            now,
            _AUTH_PROBE_INTERVAL_SECONDS[args.provider],
        ):
            return _emit_error(
                args,
                command="auth.probe",
                code=ErrorCode.REQUEST_THROTTLED,
                message="The local provider request interval has not elapsed.",
                retryable=True,
            )
        try:
            result = _provider_auth_client().probe(
                provider=args.provider,
                api_key=resolved["secret"],
            )
        except ProviderAuthError as exc:
            return _emit_error(
                args,
                command="auth.probe",
                code=exc.code,
                message="The provider credential probe did not succeed.",
                retryable=exc.retryable,
            )
        return _emit_success(
            args,
            command="auth.probe",
            data={
                "provider": args.provider,
                "validation_state": result.state,
                "validated": True,
                "retryable": result.retryable,
                "response_retained": False,
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
                        capability=spec.dependent_capability,
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
                        raise CredentialError("CREDENTIAL_ROLLBACK_FAILED") from None
                    raise
                removed = True
        data = {
            "provider": args.provider,
            "removed": removed,
            "secret_included": False,
        }
        if args.provider == "steam-web-api":
            data["valve_key_revoked"] = False
        return _emit_success(
            args,
            command="auth.remove",
            data=data,
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
        if (
            args.owned_command == "probe"
            and account is not None
            and metadata is not None
        ):
            credential = _resolve_credential(metadata, credential_ref)
            if credential["state"] == "configured":
                now = _utc_now()
                if not _reserve_provider_request(
                    "steam-web-api", now, _PROVIDER_MINIMUM_INTERVAL_SECONDS
                ):
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


def _provider_credential_ref(
    database_path: Path, spec: _CredentialProviderSpec
) -> CredentialRef:
    """Scope an opaque provider credential to one local data profile."""
    canonical = str(database_path.expanduser().resolve(strict=False)).encode("utf-8")
    profile_id = f"data-{hashlib.sha256(canonical).hexdigest()[:32]}"
    return CredentialRef(spec.storage_provider, spec.kind, profile_id)


def _steam_credential_ref(database_path: Path) -> CredentialRef:
    return _provider_credential_ref(
        database_path, _CREDENTIAL_PROVIDERS["steam-web-api"]
    )


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
                        raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE") from None
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


def _resolve_credential(metadata: Any, credential_ref: CredentialRef) -> dict[str, Any]:
    try:
        secret = _credential_store(metadata.backend, metadata.backend_locator).resolve(
            credential_ref
        )
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


def _credential_warnings(
    state: str, *, credential_label: str = "Steam Web API user key"
) -> list[WarningRecord]:
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
            message=f"A {credential_label} has not been configured.",
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


def _steam_wishlist_client() -> SteamWishlistClient:
    return SteamWishlistClient()


def _gg_deals_client(request_gate: Any) -> GgDealsClient:
    return GgDealsClient(request_gate=request_gate)


def _cheapshark_client(request_gate: Any) -> CheapSharkClient:
    return CheapSharkClient(request_gate=request_gate)


def _provider_budget_database_path() -> Path:
    """One OS-user-local request budget shared by every data profile."""

    return default_credential_dir().parent / "provider-request-budget.sqlite3"


def _reserve_provider_request(
    provider: str,
    requested_at: datetime,
    minimum_interval_seconds: float,
) -> bool:
    with Storage(_provider_budget_database_path()) as storage:
        return storage.reserve_provider_request(
            provider=provider,
            budget_scope="user-key",
            requested_at=requested_at,
            minimum_interval_seconds=minimum_interval_seconds,
        )


def _provider_auth_client() -> ProviderAuthClient:
    return ProviderAuthClient()


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
        "deals_command",
        "accounts_command",
        "auth_command",
        "owned_command",
        "data_command",
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
    if isinstance(value, bool):
        return "true" if value else "false"
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
    if command == "deals.query":
        query_completeness = envelope["completeness"]
        _print_table_fields("COMPLETENESS", query_completeness["status"])
        for capability in query_completeness["missing_capabilities"]:
            _print_table_fields("MISSING_CAPABILITY", capability)
        for capability in query_completeness["stale_capabilities"]:
            _print_table_fields("STALE_CAPABILITY", capability)
        for warning in query_completeness["warnings"]:
            _print_table_fields("WARNING", warning["code"], warning["message"])
        _print_table_fields(
            "APPID",
            "BUCKET",
            "GRADE",
            "CURRENT_MINOR",
            "LOW_MINOR",
            "FALLBACK_RUNG",
        )
        for item in envelope["data"]["items"]:
            deal = item["deal"]
            current = deal["current_offer"]
            low = deal["historical_low"]
            _print_table_fields(
                item["appid"],
                deal["bucket"],
                deal["evidence_grade"],
                "" if current is None else current["price"]["amount_minor"],
                "" if low is None else low["price"]["amount_minor"],
                deal["fallback_rung"],
            )
        return
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
        scopes = envelope.get("context", {}).get("scopes", [])
        if scopes == ["installed"] or (
            not scopes and all("state" in item for item in envelope["data"]["items"])
        ):
            _print_table_fields("APPID", "NAME", "STATE", "SIZE")
            for item in envelope["data"]["items"]:
                _print_table_fields(
                    item["appid"], item["name"], item["state"], item["size_bytes"]
                )
        elif scopes == ["owned"]:
            _print_table_fields("APPID", "NAME", "VISIBLE", "BASIS", "PLAYTIME")
            for item in envelope["data"]["items"]:
                _print_table_fields(
                    item["appid"],
                    item["name"],
                    item["visible_in_owned_games"],
                    item["inclusion_basis"],
                    item["playtime_forever_minutes"],
                )
        elif scopes == ["wishlist"]:
            _print_table_fields("APPID", "WISHLISTED", "PRIORITY", "DATE_ADDED")
            for item in envelope["data"]["items"]:
                _print_table_fields(
                    item["appid"],
                    item["wishlisted"],
                    item["priority"],
                    item["date_added_unix"],
                )
        else:
            _print_table_fields(
                "APPID", "NAME", "VISIBLE", "INSTALLED", "TYPE", "PLAYTIME"
            )
            for item in envelope["data"]["items"]:
                _print_table_fields(
                    item["appid"],
                    item["name"],
                    item["visible_in_owned_games"],
                    item["installed"],
                    item["app_type"],
                    item["playtime_forever_minutes"],
                )
        return
    for key, value in envelope["data"].items():
        _print_table_fields(key, value)


__all__ = ["build_parser", "main"]

"""Argument parsing and process boundary for the M1 CLI."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import sys
import sqlite3
from typing import Any, Sequence
import unicodedata

from steam_agent import __version__
from steam_agent.application import (
    default_database_path,
    discover_steam_root,
    installed_item,
    sync_installed,
    usable_steam_root,
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
from steam_agent.storage import Storage, StorageError


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNAVAILABLE = 3
SECRET_FLAGS = frozenset(
    {"--api-key", "--token", "--password", "--cookie", "--client-secret"}
)
_SAFE_WARNING_SOURCE = re.compile(r"(?:libraryfolders\.vdf|appmanifest_\d+\.acf)\Z")


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
    raise AssertionError("argparse accepted an unhandled command")


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
    for name in ("sync_command", "games_command"):
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

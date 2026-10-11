#!/usr/bin/env python3
"""Exercise the built wheel through its installed ``steam-agent`` process."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


def _run(
    command: list[str], *, expected_code: int = 0
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != expected_code:
        raise RuntimeError(
            f"command returned {result.returncode}, expected {expected_code}: "
            f"{command[0]} {command[1]}"
        )
    return result


def _wheel_path(value: Path | None) -> Path:
    if value is not None:
        candidate = value.resolve()
        if not candidate.is_file() or candidate.suffix != ".whl":
            raise RuntimeError("--wheel must name one built wheel")
        return candidate
    wheels = sorted(Path("dist").glob("steam_agent-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("dist must contain exactly one steam-agent wheel")
    return wheels[0].resolve()


def _versions(database: Path) -> tuple[int, ...]:
    with sqlite3.connect(database) as connection:
        return tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )


def _expected_versions() -> tuple[int, ...]:
    migrations = (
        Path(__file__).resolve().parents[1] / "src" / "steam_agent" / "migrations"
    )
    versions = tuple(
        int(path.name.split("_", 1)[0])
        for path in sorted(migrations.glob("[0-9][0-9][0-9]_*.sql"))
    )
    if not versions:
        raise RuntimeError("source migration set is missing")
    return versions


def _tables(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _columns(database: Path, table: str) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }


def _broker_fixture_smoke(python: Path, root: Path) -> None:
    """Reopen installed broker contracts across real disposable processes.

    Only fixture adapters can dispatch; the installed parser, intake, execution
    lock, ledger and receipt serializer remain real. Isolated Python excludes
    checkout and PYTHONPATH imports even though the fixture itself is external.
    """

    fixture = Path(__file__).resolve().parents[1] / "tests" / "support" / "broker_process_fixture.py"
    if not fixture.is_file():
        raise RuntimeError("disposable installed-broker fixture is unavailable")
    fixture_root = root / "broker-fixture"
    library = fixture_root / "library"
    (library / "steamapps").mkdir(parents=True)

    def invoke(*arguments: str, expected_code: int = 0) -> dict[str, object]:
        completed = _run(
            [str(python), "-I", str(fixture), "--root", str(fixture_root), "--",
             "--format", "json", *arguments], expected_code=expected_code,
        )
        payload = json.loads(completed.stdout)
        if payload.get("schema") != "broker-result/0.1" or completed.stderr:
            raise RuntimeError("installed broker did not return its typed JSON contract")
        if str(fixture_root) in completed.stdout or "synthetic_login" in completed.stdout:
            raise RuntimeError("installed broker receipt exposed private fixture context")
        return payload

    invoke(
        "init", "--library", str(library), "--machine-id", "synthetic-machine",
        "--account", "synthetic", "--steam-login", "synthetic_login",
        "--steamcmd", str(fixture_root / "unused-steamcmd"),
    )
    state = fixture_root / "state"
    ledger = state / "ledger.sqlite3"
    unknown = invoke("inspect", "--request-id", "package-unknown-request", expected_code=2)
    if unknown["attempt"] != {"outcome": "refused", "reason_code": "request_not_found"} or unknown["receipt"] is not None or ledger.exists():
        raise RuntimeError("installed read-only request lookup created state or lost refusal truth")
    (state / "policy.toml").write_text(
        '[grants]\nlaunch="allow"\nstop="allow"\n'
        '[limits]\nmin_free_gb=0\n[launch]\nallowed_appids=[480]\n'
        '[stop]\nallowed_appids=[480]\n', encoding="utf-8",
    )
    request = ("launch", "480", "--request-id", "package-authorized-request")
    recorded = invoke(*request, "--request-only")
    if recorded["receipt"]["state"] != "authorized" or recorded["attempt"]["outcome"] != "request_recorded":
        raise RuntimeError("installed direct request was not policy-authorized")
    operation_id = recorded["receipt"]["operation_id"]
    reused = invoke(*request, "--request-only")
    if reused["receipt"]["operation_id"] != operation_id or reused["attempt"]["outcome"] != "receipt_reused":
        raise RuntimeError("installed request ID did not retain its original binding")
    observed = invoke("inspect", "--request-id", "package-authorized-request")
    if observed["receipt"]["operation_id"] != operation_id or observed["receipt"]["state"] != "authorized":
        raise RuntimeError("installed lookup could not reopen its original authorized request")
    conflict = invoke("stop", "480", "--request-id", "package-authorized-request", "--request-only", expected_code=2)
    if conflict["attempt"]["reason_code"] != "request_conflict":
        raise RuntimeError("installed broker accepted conflicting immutable request content")
    dispatched = invoke(*request)
    repeated = invoke(*request)
    if dispatched["receipt"]["state"] != "dispatched" or repeated["receipt"]["state"] != "dispatched" or repeated["attempt"]["outcome"] != "receipt_reused":
        raise RuntimeError("installed acknowledged dispatch did not retain its terminal receipt")
    effects = fixture_root / "effects.jsonl"
    launch_effects = [json.loads(line) for line in effects.read_text().splitlines() if json.loads(line)["event"] == "launch"]
    if len(launch_effects) != 1:
        raise RuntimeError("installed same-request reuse dispatched more than once")

    pending_request = ("launch", "480", "--request-id", "package-pending-request")
    pending = invoke(*pending_request, "--request-only")
    pending_id = pending["receipt"]["operation_id"]
    # Seed an interrupted durable effect intent using the installed ledger API;
    # subsequent processes must reconcile uncertainty without adapter replay.
    _run([
        str(python), "-I", "-c",
        "import sys; from pathlib import Path; from steam_agent.execution.ledger import ExecutionLedger; "
        "ledger=ExecutionLedger(Path(sys.argv[1])); ledger.transition(int(sys.argv[2]), 'dispatch_pending'); ledger.close()",
        str(ledger), str(pending_id),
    ])
    invoke("reconcile")
    invoke("reconcile")
    uncertain = invoke("inspect", "--request-id", "package-pending-request")
    repeated_uncertain = invoke(*pending_request, expected_code=1)
    if uncertain["receipt"]["state"] != "unconfirmed" or repeated_uncertain["receipt"]["dispatch_certainty"] != "uncertain" or repeated_uncertain["retry"] != {"category": "new_intent_required", "automatic": False}:
        raise RuntimeError("installed dispatch-pending recovery lost uncertainty or advertised replay")
    after = [json.loads(line) for line in effects.read_text().splitlines() if json.loads(line)["event"] == "launch"]
    if after != launch_effects:
        raise RuntimeError("installed uncertain dispatch was replayed")
    with sqlite3.connect(ledger) as connection:
        if connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] != 2:
            raise RuntimeError("installed request reuse or conflict created another operation")
    connection.close()

    # A downgraded binary must refuse newer durable truth before rewriting
    # indexes, expiring rows, or interpreting an unknown effect as retryable.
    mutations = (
        "UPDATE operations SET schema='operation-execution/9.9'",
        "UPDATE operations SET state='future_terminal'",
        "UPDATE operations SET operation='future_content_operation'",
        "PRAGMA user_version=1",
    )
    for mutation in mutations:
        with closing(sqlite3.connect(ledger)) as connection, connection:
            connection.execute("UPDATE operations SET schema='operation-execution/0.1', state='unconfirmed', operation='launch'")
            connection.execute("PRAGMA user_version=0")
            connection.execute(mutation)
            connection.execute("DROP INDEX one_active_operation")
            connection.execute(
                "CREATE UNIQUE INDEX one_active_operation ON operations(machine_id)"
                " WHERE state NOT IN ('future_terminal','unconfirmed')"
            )
        with closing(sqlite3.connect(ledger)) as connection, connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            before = tuple(connection.iterdump())
        original = ledger.read_bytes()
        for arguments in (
            ("inspect", "--request-id", "package-pending-request"),
            ("reconcile",),
        ):
            refused = invoke(*arguments, expected_code=2)
            if refused["attempt"] != {"outcome": "refused", "reason_code": "unsupported"} or refused["receipt"] is not None:
                raise RuntimeError("installed broker did not refuse unsupported durable ledger truth")
            if ledger.read_bytes() != original:
                raise RuntimeError("installed broker changed an unsupported ledger")
            with closing(sqlite3.connect(ledger)) as connection, connection:
                if tuple(connection.iterdump()) != before or connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise RuntimeError("installed broker migrated or rewrote unsupported ledger truth")


def smoke(wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="steam-agent-package-smoke-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        _run([sys.executable, "-m", "venv", str(environment)])
        if sys.platform == "win32":
            executable = environment / "Scripts" / "steam-agent.exe"
            python = environment / "Scripts" / "python.exe"
        else:
            executable = environment / "bin" / "steam-agent"
            python = environment / "bin" / "python"
        requirements = root / "runtime-requirements.txt"
        _run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ]
        )
        _run(
            [
                "uv",
                "pip",
                "install",
                "--quiet",
                "--python",
                str(python),
                "--requirement",
                str(requirements),
            ]
        )
        _run(
            [
                "uv",
                "pip",
                "install",
                "--quiet",
                "--python",
                str(python),
                "--no-deps",
                str(wheel),
            ]
        )

        broker = environment / (
            "Scripts/steam-agent-broker.exe"
            if sys.platform == "win32"
            else "bin/steam-agent-broker"
        )
        _run([str(broker), "stop", "--help"])
        runtime = json.loads(_run([str(broker), "runtime"]).stdout)
        if runtime["schema"] != "runtime-observation/0.1":
            raise RuntimeError("installed broker runtime schema is unavailable")
        catalog = json.loads(
            _run([str(executable), "capabilities", "--all", "--format", "json"]).stdout
        )
        if (
            catalog["data"]["catalog_schema"] != "capability-catalog/0.1"
            or catalog["data"]["runtime_checked"]
        ):
            raise RuntimeError("installed agent capability catalog is unavailable")

        installed_version = _run(
            [
                str(python),
                "-c",
                "import importlib.metadata; print(importlib.metadata.version('steam-agent'))",
            ]
        ).stdout.strip()
        _run([
            str(python), "-I", "-c",
            "import sys; from pathlib import Path; import steam_agent; "
            "assert Path(steam_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())",
        ])
        _broker_fixture_smoke(python, root)
        version = _run([str(executable), "--version"])
        if version.stdout.strip() != f"steam-agent {installed_version}":
            raise RuntimeError("installed entry point returned an unexpected version")

        games_help = _run([str(executable), "games", "query", "--help"])
        if not all(option in games_help.stdout for option in ("--appid", "--include-names")):
            raise RuntimeError("installed help does not expose exact cached membership and names")
        for scope in ("owned", "wishlist", "activity", "achievements"):
            source_help = _run([str(executable), "sync", scope, "--help"])
            if "--credential-source" not in source_help.stdout or "process-env" not in source_help.stdout:
                raise RuntimeError("installed help does not expose explicit acquisition credentials")
        wishlist_plan_help = _run([str(executable), "wishlist", "plan", "--help"])
        purchase_plan_help = _run([str(executable), "deals", "plan", "--help"])
        if not all(option in wishlist_plan_help.stdout for option in ("--appid", "--action", "--baseline-sync-id")) or not all(option in purchase_plan_help.stdout for option in ("--appid", "--country", "--store-class")):
            raise RuntimeError("installed help does not expose inert account proposal contracts")
        help_result = _run([str(executable), "sync", "wishlist", "--help"])
        if "--acknowledge-local-storage" not in help_result.stdout:
            raise RuntimeError("installed help does not expose the wishlist contract")
        deals_help = _run([str(executable), "deals", "query", "--help"])
        if not all(
            option in deals_help.stdout
            for option in ("--scope", "--account", "--country", "--store-class")
        ):
            raise RuntimeError("installed help does not expose the deal-query contract")
        feedback_help = _run([str(executable), "feedback", "estimate", "--help"])
        if not all(
            option in feedback_help.stdout
            for option in (
                "--minimum-session-minutes",
                "--remaining-minutes",
                "--clear-minimum-session-minutes",
                "--clear-remaining-minutes",
            )
        ):
            raise RuntimeError("installed help does not expose explicit estimates")
        rate_help = _run([str(executable), "feedback", "rate", "--help"])
        trait_help = _run([str(executable), "feedback", "trait", "--help"])
        clear_state_help = _run([str(executable), "feedback", "clear-state", "--help"])
        if (
            "--clear" not in rate_help.stdout
            or "--clear" not in trait_help.stdout
            or "appid" not in clear_state_help.stdout
        ):
            raise RuntimeError(
                "installed help does not expose explicit feedback clearing"
            )
        activity_help = _run([str(executable), "sync", "activity", "--help"])
        achievements_help = _run([str(executable), "sync", "achievements", "--help"])
        if "--acknowledge-local-storage" not in activity_help.stdout or not all(
            option in achievements_help.stdout
            for option in (
                "--scope",
                "--appid",
                "--max-items",
                "--acknowledge-local-storage",
            )
        ):
            raise RuntimeError("installed help does not expose M4 activity contracts")
        recommendations_help = _run(
            [str(executable), "recommendations", "query", "--help"]
        )
        if not all(
            option in recommendations_help.stdout
            for option in (
                "--account",
                "--machine",
                "--scope",
                "--recipe",
                "--time-minutes",
                "--require",
                "--unknown",
                "--override",
                "--explain",
                "--format",
            )
        ):
            raise RuntimeError(
                "installed help does not expose recommendation contracts"
            )
        reviews_help = _run([str(executable), "sync", "reviews", "--help"])
        wishlist_fit_help = _run(
            [str(executable), "recommendations", "wishlist", "--help"]
        )
        if not all(
            option in reviews_help.stdout
            for option in (
                "--scope",
                "--account",
                "--max-items",
                "--acknowledge-local-storage",
            )
        ) or not all(
            option in wishlist_fit_help.stdout
            for option in (
                "--account",
                "--country",
                "--store-class",
                "--unknown",
                "--override",
            )
        ):
            raise RuntimeError("installed help does not expose wishlist-fit contracts")
        operations_help = _run([str(executable), "operations", "observe", "--help"])
        storage_rank_help = _run([str(executable), "storage", "rank", "--help"])
        operation_plan_help = _run([str(executable), "operations", "plan", "--help"])
        if (
            "--machine" not in operations_help.stdout
            or not all(
                option in storage_rank_help.stdout
                for option in (
                    "--recipe",
                    "--machine",
                    "--target-bytes",
                    "--budget-bytes",
                    "--limit",
                )
            )
            or not all(
                option in operation_plan_help.stdout
                for option in (
                    "--account",
                    "--machine",
                    "--destination-library-ordinal",
                    "--expires-minutes",
                )
            )
        ):
            raise RuntimeError("installed help does not expose M7 operation contracts")

        data_dir = root / "data"
        m7_fresh = json.loads(
            _run(
                [
                    str(executable),
                    "--data-dir",
                    str(root / "m7-fresh"),
                    "operations",
                    "observe",
                    "--machine",
                    "local",
                ],
                expected_code=1,
            ).stdout
        )
        if (
            m7_fresh.get("command") != "operations.observe"
            or m7_fresh.get("error", {}).get("code") != "NOT_SYNCED"
        ):
            raise RuntimeError("fresh-profile M7 observation truth state is invalid")
        query = [
            str(executable),
            "--data-dir",
            str(data_dir),
            "games",
            "query",
            "--scope",
            "wishlist",
        ]
        first = json.loads(_run(query).stdout)
        if (
            first.get("schema_version") != "0.1"
            or first.get("completeness", {}).get("status") != "unavailable"
            or first.get("completeness", {}).get("missing_capabilities")
            != ["account.identity"]
            or first.get("data", {}).get("empty") is not False
        ):
            raise RuntimeError("fresh-profile wishlist truth state is invalid")
        filtered = json.loads(_run([*query, "--appid", "480", "--include-names"]).stdout)
        if filtered["completeness"] != first["completeness"] or filtered["data"]["items"] or filtered["data"]["empty"] is not False or filtered["context"]["appid_filter"] != [480]:
            raise RuntimeError("installed exact filter flattened unavailable membership to empty")
        for route, options, plan_schema in (
            ("wishlist", ["--action", "add"], "wishlist-change-proposal/0.1"),
            ("deals", ["--country", "US"], "purchase-decision-card/0.1"),
        ):
            proposal = json.loads(_run([
                str(executable), "--data-dir", str(data_dir), route, "plan",
                "--account", "primary", "--appid", "480", *options,
            ]).stdout)
            if proposal.get("completeness", {}).get("status") != "unavailable" or proposal.get("data", {}).get("schema") != plan_schema or proposal["data"]["execution_supported"] is not False or proposal["context"]["cache_only"] is not True:
                raise RuntimeError("installed fresh-profile account proposal lost inert unavailable truth")
        recommendation = json.loads(
            _run(
                [
                    str(executable),
                    "--data-dir",
                    str(data_dir),
                    "recommendations",
                    "query",
                    "--account",
                    "primary",
                    "--recipe",
                    "resume/0.1",
                ]
            ).stdout
        )
        if (
            recommendation.get("completeness", {}).get("status") != "unavailable"
            or recommendation.get("completeness", {}).get("missing_capabilities")
            != ["account.identity"]
            or recommendation.get("data", {}).get("empty") is not False
        ):
            raise RuntimeError("fresh-profile recommendation truth state is invalid")
        wishlist_fit = json.loads(
            _run(
                [
                    str(executable),
                    "--data-dir",
                    str(data_dir),
                    "recommendations",
                    "wishlist",
                    "--account",
                    "primary",
                    "--country",
                    "US",
                ]
            ).stdout
        )
        if (
            wishlist_fit.get("completeness", {}).get("status") != "unavailable"
            or wishlist_fit.get("data", {}).get("purchase_recommendation_supported")
            is not False
            or wishlist_fit.get("data", {}).get("empty") is not False
        ):
            raise RuntimeError("fresh-profile wishlist-fit truth state is invalid")

        database = data_dir / "steam-agent.sqlite3"
        before = _versions(database)
        expected = _expected_versions()
        if (
            before != expected
            or "wishlist_current" not in _tables(database)
            or "targeted" not in _columns(database, "price_sync_demand")
            or "explicit_feedback_current" not in _tables(database)
            or "preference_rules_current" not in _tables(database)
            or "activity_current" not in _tables(database)
            or "achievement_sync_demand" not in _tables(database)
            or "achievement_player_current" not in _tables(database)
            or "review_sync_demand" not in _tables(database)
            or "review_current" not in _tables(database)
        ):
            raise RuntimeError(
                "installed wheel did not apply the complete source schema"
            )
        second = json.loads(_run(query).stdout)
        after = _versions(database)
        if (
            not before
            or before != after
            or second["completeness"] != first["completeness"]
        ):
            raise RuntimeError("database migration reopen was not idempotent")

        # Explicit account setup must work in the installed process with no
        # Steam registry. Keep this separate from fresh-profile query checks.
        account_data = root / "explicit-account"
        account_args = [str(executable), "--data-dir", str(account_data), "accounts"]
        identifier = "76561198000000000"
        configured = _run(account_args + [
            "configure", "--alias", "primary", "--steam-id64", identifier,
            "--steam-root", str(root / "absent-steam"),
        ])
        configuration = json.loads(configured.stdout)
        if (
            configured.stderr or identifier in configured.stdout
            or configuration["data"]["source_kind"] != "explicit_steam_id"
            or configuration["data"]["configured"] is not True
        ):
            raise RuntimeError("installed explicit account setup required Steam or exposed identity")
        status = _run(account_args + ["status", "--alias", "primary"])
        if status.stderr or identifier in status.stdout or not json.loads(status.stdout)["data"]["configured"]:
            raise RuntimeError("installed explicit account did not reopen with redacted status")
        explicit = json.loads(_run(account_args + ["status", "--include-identifiers"]).stdout)
        if explicit["data"]["steam_id64"] != identifier:
            raise RuntimeError("installed explicit account lost its identity binding")

        sentinel = "PACKAGE-SMOKE-SECRET-CANARY"
        rejected = _run(
            [str(executable), "--api-key", sentinel, "status"], expected_code=2
        )
        combined = rejected.stdout + rejected.stderr
        if sentinel in combined:
            raise RuntimeError("secret-like argv value was echoed")
        rejection = json.loads(rejected.stdout)
        if rejection.get("error", {}).get("code") != "SECRET_ON_ARGV":
            raise RuntimeError("secret-like argv did not return the typed rejection")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    smoke(_wheel_path(args.wheel))
    print("installed-wheel smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

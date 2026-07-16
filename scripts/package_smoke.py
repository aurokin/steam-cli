#!/usr/bin/env python3
"""Exercise the built wheel through its installed ``steam-agent`` process."""

from __future__ import annotations

import argparse
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

        installed_version = _run(
            [
                str(python),
                "-c",
                "import importlib.metadata; print(importlib.metadata.version('steam-agent'))",
            ]
        ).stdout.strip()
        version = _run([str(executable), "--version"])
        if version.stdout.strip() != f"steam-agent {installed_version}":
            raise RuntimeError("installed entry point returned an unexpected version")

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
        clear_state_help = _run(
            [str(executable), "feedback", "clear-state", "--help"]
        )
        if (
            "--clear" not in rate_help.stdout
            or "--clear" not in trait_help.stdout
            or "appid" not in clear_state_help.stdout
        ):
            raise RuntimeError("installed help does not expose explicit feedback clearing")
        activity_help = _run([str(executable), "sync", "activity", "--help"])
        achievements_help = _run([str(executable), "sync", "achievements", "--help"])
        if "--acknowledge-local-storage" not in activity_help.stdout or not all(
            option in achievements_help.stdout
            for option in ("--scope", "--appid", "--max-items", "--acknowledge-local-storage")
        ):
            raise RuntimeError("installed help does not expose M4 activity contracts")
        recommendations_help = _run(
            [str(executable), "recommendations", "query", "--help"]
        )
        if not all(
            option in recommendations_help.stdout
            for option in (
                "--account", "--machine", "--scope", "--recipe",
                "--time-minutes", "--require", "--unknown", "--override",
                "--explain", "--format",
            )
        ):
            raise RuntimeError("installed help does not expose recommendation contracts")
        reviews_help = _run([str(executable), "sync", "reviews", "--help"])
        wishlist_fit_help = _run(
            [str(executable), "recommendations", "wishlist", "--help"]
        )
        if not all(
            option in reviews_help.stdout
            for option in ("--scope", "--account", "--max-items", "--acknowledge-local-storage")
        ) or not all(
            option in wishlist_fit_help.stdout
            for option in ("--account", "--country", "--store-class", "--unknown", "--override")
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
        recommendation = json.loads(
            _run(
                [
                    str(executable), "--data-dir", str(data_dir),
                    "recommendations", "query", "--account", "primary",
                    "--recipe", "resume/0.1",
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
                    str(executable), "--data-dir", str(data_dir),
                    "recommendations", "wishlist", "--account", "primary",
                    "--country", "US",
                ]
            ).stdout
        )
        if (
            wishlist_fit.get("completeness", {}).get("status") != "unavailable"
            or wishlist_fit.get("data", {}).get("purchase_recommendation_supported") is not False
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

"""Materialize M7 operational scenarios.

Every M7 state's fresh/stale semantics depends on sync ordering and bounded
windows.  The shared offsets are anchored to the scenario's frozen time so
the same fixture reproduces the expected states on every run.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from steam_agent.steam_declared_facts import DECLARED_FACTS_DISCLOSURE_VERSION
from steam_agent.storage import Storage

from .materialize import (
    UnsupportedScenarioError,
    declared_app_facts_payload,
    materialization_now,
    parse_detail,
    scenario_account_alias,
    scenario_machine_key,
    seed_identity,
    subject_appid,
    write_installed,
    write_owned_snapshot,
)


def build(scenario: Mapping[str, Any], data_dir: Path) -> None:
    machine_key = scenario_machine_key(scenario)
    account_alias = scenario_account_alias(scenario)
    now = materialization_now(scenario)

    installed: list[tuple[int, int, str]] = []
    owned: list[int] = []
    declared: list[int] = []
    failed_scan_after = False
    auxiliary_library_appid: int | None = None
    library_roots: dict[int, str] = {}

    for fact in scenario["fixture"]["facts"]:
        appid = subject_appid(fact)
        state = fact["state"]
        detail = parse_detail(fact.get("detail"))
        if state == "fresh_installed":
            installed.append(
                (appid, int(detail.get("size", 0)), detail.get("build", "1"))
            )
        elif state == "fresh_installed_2gb":
            installed.append((appid, 2_000_000_000, "1"))
        elif state == "fresh_installed_4gb":
            installed.append((appid, 4_000_000_000, "1"))
        elif state == "retained_after_failed_scan":
            installed.append(
                (appid, int(detail.get("size", 4_000_000_000)), detail.get("build", "1"))
            )
            failed_scan_after = True
        elif state in {
            "verify_plan_requested",
            "uninstall_plan_requested",
            "launch_plan_requested",
        }:
            installed.append((appid, 1_000_000_000, "1"))
            owned.append(appid)
        elif state == "move_plan_destination_two":
            installed.append((appid, 1_000_000_000, "1"))
            owned.append(appid)
            # Library roots are reconstructed from installed observations;
            # one inert auxiliary observation makes ordinal two real without
            # exposing either synthetic path through the plan document.
            auxiliary_library_appid = 9_000_000 + appid
            installed.append((auxiliary_library_appid, 1, "1"))
            library_roots[appid] = "/synthetic/library-1"
            library_roots[auxiliary_library_appid] = "/synthetic/library-2"
        elif state == "travel_declared_fit":
            owned.append(appid)
            declared.append(appid)
        else:
            raise UnsupportedScenarioError(f"no M7 fixture builder for {state!r}")

    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = seed_identity(
            storage,
            machine_key=machine_key,
            account_alias=account_alias,
            now=now,
        )
        write_owned_snapshot(
            storage,
            account.id,
            [
                *owned,
                *(
                    appid
                    for appid, _, _ in installed
                    if appid != auxiliary_library_appid
                ),
            ],
            now,
        )
        write_installed(
            storage,
            machine_key,
            installed,
            now,
            library_roots=library_roots,
        )
        if failed_scan_after:
            failed_run = storage.begin_sync(
                provider="local_steam",
                capability="installed",
                machine_id=machine_key,
                started_at=now + timedelta(seconds=30),
            )
            storage.finish_installed_sync(
                failed_run.id, status="failed", completed_at=now + timedelta(seconds=30)
            )

        if declared:
            storage.record_compatibility_data_consent(
                account_id=account.id,
                disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
                accepted_at=now,
                backups_acknowledged=True,
            )
            declared_run, _, _ = storage.begin_declared_app_sync(
                account_id=account.id,
                machine_id=machine_key,
                demanded_appids=declared,
                country="US",
                language="english",
                max_items=len(declared),
                skip_fresh_terminal=True,
                started_at=now,
                disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
            )
            for appid in declared:
                storage.record_declared_app_result(
                    declared_run.id,
                    account_id=account.id,
                    appid=appid,
                    state="ready",
                    observed_at=now,
                    facts=declared_app_facts_payload(
                        appid, linux_minimum="Storage: 1 GB available space"
                    ),
                )
            storage.finish_declared_app_sync(declared_run.id, completed_at=now)


__all__ = ["build"]

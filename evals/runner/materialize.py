"""Materialize scenario fixtures into a real ``--data-dir`` cache.

Each builder writes synthetic normalized evidence through public ``Storage``
APIs only, per the evaluation strategy: the runner must not make provider
requests. Timestamps are written relative to wall-clock now because the
installed CLI has no clock override; every M7 state's fresh/stale semantics
depend on sync ordering and bounded windows, not on the scenario's frozen
time, so recent offsets reproduce the expected states.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Mapping

from steam_agent.steam_declared_facts import DECLARED_FACTS_DISCLOSURE_VERSION
from steam_agent.storage import (
    EvidenceInput,
    InstalledObservation,
    Machine,
    OwnedObservation,
    Storage,
)

_SUBJECT = re.compile(r"\Asynthetic:appid:(\d+)\Z")
_SYNTHETIC_STEAM_ID64 = "76561198999999999"

GIBIBYTE = 1 << 30


class UnsupportedScenarioError(ValueError):
    """The scenario's milestone or fixture state has no materializer."""


def _parse_detail(detail: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in (detail or "").split(";"):
        key, separator, value = token.strip().partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def _subject_appid(fact: Mapping[str, Any]) -> int:
    match = _SUBJECT.match(fact["subject"])
    if match is None:
        raise UnsupportedScenarioError(f"unsupported subject {fact['subject']!r}")
    return int(match.group(1))


def required_argument_value(scenario: Mapping[str, Any], flag: str, default: str) -> str:
    for requirement in scenario["tool_policy"].get("required", ()):
        arguments = requirement.get("arguments", [])
        if flag in arguments:
            index = arguments.index(flag)
            if index + 1 < len(arguments):
                return arguments[index + 1]
    return default


def materialize(scenario: Mapping[str, Any], data_dir: Path) -> None:
    """Write the scenario fixture into ``data_dir / steam-agent.sqlite3``."""

    milestone = scenario["milestone"]
    if milestone != "M7":
        raise UnsupportedScenarioError(
            f"no materializer for milestone {milestone!r} yet"
        )
    _materialize_m7(scenario, data_dir)


def _materialize_m7(scenario: Mapping[str, Any], data_dir: Path) -> None:
    machine_key = required_argument_value(scenario, "--machine", "synthetic-machine")
    account_alias = required_argument_value(scenario, "--account", "synthetic")
    now = datetime.now(timezone.utc) - timedelta(minutes=1)

    installed: list[tuple[int, int, str]] = []
    owned: list[int] = []
    declared: list[int] = []
    failed_scan_after = False

    for fact in scenario["fixture"]["facts"]:
        appid = _subject_appid(fact)
        state = fact["state"]
        detail = _parse_detail(fact.get("detail"))
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
        elif state in {"verify_plan_requested", "move_plan_destination_two"}:
            installed.append((appid, 1_000_000_000, "1"))
            owned.append(appid)
        elif state == "travel_declared_fit":
            owned.append(appid)
            declared.append(appid)
        else:
            raise UnsupportedScenarioError(f"no M7 fixture builder for {state!r}")

    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        storage.upsert_machine(
            Machine(machine_key, "Synthetic machine", "linux", "x86_64"),
            observed_at=now,
        )
        account = storage.configure_steam_account(
            alias=account_alias,
            steam_id64=_SYNTHETIC_STEAM_ID64,
            configured_at=now,
        )
        storage.record_owned_data_consent(
            account_id=account.id,
            disclosure_version="owned-visible-v1",
            accepted_at=now,
            backups_acknowledged=True,
        )
        owned_run = storage.begin_sync(
            provider="steam_web_api",
            capability="owned.visible.read",
            account_id=account.id,
            started_at=now,
        )
        owned_appids = sorted({*owned, *(appid for appid, _, _ in installed)})
        storage.complete_owned_snapshot(
            owned_run.id,
            tuple(
                OwnedObservation(
                    appid, 0, "visible_owned", now, f"Synthetic title {appid}"
                )
                for appid in owned_appids
            ),
            base_retrieved_at=now,
            expanded_retrieved_at=now,
            base_reported_count=len(owned_appids),
            expanded_reported_count=len(owned_appids),
            completed_at=now,
        )

        installed_run = storage.begin_sync(
            provider="local_steam",
            capability="installed",
            machine_id=machine_key,
            started_at=now,
        )
        for appid, size_bytes, build_id in installed:
            storage.record_installed_observation(
                installed_run.id,
                InstalledObservation(
                    appid=appid,
                    library_root="/synthetic/library",
                    install_dir=f"/synthetic/library/steamapps/common/title-{appid}",
                    observed_at=now,
                    name=f"Synthetic title {appid}",
                    build_id=build_id,
                    size_bytes=size_bytes,
                    manifest_path=(
                        f"/synthetic/library/steamapps/appmanifest_{appid}.acf"
                    ),
                    manifest_mtime=now,
                ),
                EvidenceInput(
                    provider="local_steam",
                    capability="installed",
                    source_kind="local_file",
                    source_locator=(
                        f"/synthetic/library/steamapps/appmanifest_{appid}.acf"
                    ),
                    retrieved_at=now,
                    support_level="local_heuristic",
                    payload={"synthetic": True},
                ),
            )
        storage.finish_installed_sync(
            installed_run.id, status="complete", completed_at=now
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
                    facts=_declared_payload(appid),
                )
            storage.finish_declared_app_sync(declared_run.id, completed_at=now)


def _declared_payload(appid: int) -> dict[str, object]:
    return {
        "schema_id": "declared-app-facts/0.2",
        "appid": appid,
        "context": {"country": "US", "language": "english"},
        "platforms": {
            "state": "declared",
            "windows": False,
            "macos": False,
            "linux": True,
        },
        "requirements": [
            {
                "platform": platform,
                "state": "declared" if platform == "linux" else "undeclared",
                "minimum": (
                    "Storage: 1 GB available space" if platform == "linux" else None
                ),
                "recommended": None,
            }
            for platform in ("linux", "macos", "windows")
        ],
        "languages": {"state": "undeclared", "items": [], "unrecognized_count": 0},
        "categories": {
            "state": "undeclared",
            "known_slugs": [],
            "unknown_ids": [],
            "source": "steam_store_appdetails",
            "numeric_ids": [],
        },
        "genres": {
            "state": "undeclared",
            "source": "steam_store_appdetails",
            "items": [],
        },
        "coming_soon": {"state": "unknown", "localized_date_display": None},
        "controller_support": None,
        "external_account_notice": {"state": "unknown", "text": None},
        "drm_notice": {"state": "unknown", "text": None},
        "source": {
            "provider": "steam_store",
            "support_level": "provisional",
            "source_locator": "steam_store_appdetails",
            "human_reference_url": (
                f"https://store.steampowered.com/app/{appid}/?cc=US&l=english"
            ),
            "access_mode": "manual_only",
            "automation_supported": False,
        },
    }

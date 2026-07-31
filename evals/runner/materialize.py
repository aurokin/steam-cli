"""Materialize scenario fixtures into a real ``--data-dir`` cache.

Family builders live in ``materialize_<milestone>.py`` next to this module and
are dispatched by the scenario's milestone.  Each builder writes synthetic
normalized evidence through public ``Storage`` APIs only, per the evaluation
strategy: the runner must not make provider requests.

Timestamps are written relative to one minute before each scenario's
``frozen_time``.  The runner invokes the installed CLI through a tiny
workspace-local launcher that
uses the same clock, so fixture freshness does not depend on the day the eval
is run.  Deliberately stale evidence sits inside
``(fresh_window, HARD_RETENTION)`` (24 hours for the six-hour windows), and
deliberately future evidence uses six hours ahead.

This module owns only the dispatcher and the helpers shared by the family
builders.  Family builders import from here and never edit it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from steam_agent.storage import (
    Account,
    EvidenceInput,
    InstalledObservation,
    Machine,
    OwnedObservation,
    Storage,
)

_SUBJECT = re.compile(r"\Asynthetic:appid:(\d+)\Z")
_TARGET_MACHINE = re.compile(r"\Amachine:(.+)\Z")
_SYNTHETIC_STEAM_ID64 = "76561198999999999"

GIBIBYTE = 1 << 30
STALE_OFFSET = timedelta(hours=24)
FUTURE_OFFSET = timedelta(hours=6)


class UnsupportedScenarioError(ValueError):
    """The scenario's milestone or fixture state has no materializer."""


def materialize(scenario: Mapping[str, Any], data_dir: Path) -> None:
    """Write the scenario fixture into ``data_dir / steam-agent.sqlite3``."""

    milestone = scenario["milestone"]
    module_name = f"{__package__}.materialize_{str(milestone).lower()}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        raise UnsupportedScenarioError(
            f"no materializer for milestone {milestone!r} yet"
        ) from error
    module.build(scenario, data_dir)


def parse_detail(detail: str | None) -> dict[str, str]:
    """Split a fixture ``detail`` string into its ``key=value`` pairs."""

    values: dict[str, str] = {}
    for token in (detail or "").split(";"):
        key, separator, value = token.strip().partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def subject_appid(fact: Mapping[str, Any]) -> int:
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


def scenario_machine_key(scenario: Mapping[str, Any]) -> str:
    """Resolve the machine key the required command actually addresses."""

    explicit = required_argument_value(scenario, "--machine", "")
    if explicit:
        return explicit
    context = required_argument_value(scenario, "--context-machine", "")
    if context:
        return context
    target = required_argument_value(scenario, "--target", "")
    match = _TARGET_MACHINE.match(target)
    if match is not None:
        return match.group(1)
    if target:
        # Non-machine targets (``valve:steam-deck``) still need a configured
        # evidence-context machine.
        return "synthetic-machine"
    return "local"


def scenario_account_alias(scenario: Mapping[str, Any]) -> str:
    return required_argument_value(scenario, "--account", "synthetic")


def materialization_now(scenario: Mapping[str, Any]) -> datetime:
    """Return the fresh-evidence anchor before the scenario clock."""

    value = scenario.get("frozen_time")
    if not isinstance(value, str):
        raise UnsupportedScenarioError("scenario has no frozen_time")
    try:
        frozen = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise UnsupportedScenarioError(
            f"scenario has invalid frozen_time {value!r}"
        ) from error
    if frozen.tzinfo is None:
        raise UnsupportedScenarioError("scenario frozen_time must be timezone-aware")
    return frozen.astimezone(timezone.utc) - timedelta(minutes=1)


def seed_identity(
    storage: Storage,
    *,
    machine_key: str,
    account_alias: str,
    now: datetime,
) -> Account:
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
    return account


def write_owned_snapshot(
    storage: Storage,
    account_id: int,
    appids: Sequence[int],
    now: datetime,
) -> None:
    ordered = sorted(set(appids))
    run = storage.begin_sync(
        provider="steam_web_api",
        capability="owned.visible.read",
        account_id=account_id,
        started_at=now,
    )
    storage.complete_owned_snapshot(
        run.id,
        tuple(
            OwnedObservation(appid, 0, "visible_owned", now, f"Synthetic title {appid}")
            for appid in ordered
        ),
        base_retrieved_at=now,
        expanded_retrieved_at=now,
        base_reported_count=len(ordered),
        expanded_reported_count=len(ordered),
        completed_at=now,
    )


def write_installed(
    storage: Storage,
    machine_key: str,
    entries: Sequence[tuple[int, int, str]],
    now: datetime,
    *,
    library_roots: Mapping[int, str] | None = None,
) -> int:
    """Record one complete installed scan; omitted AppIDs become known-false."""

    run = storage.begin_sync(
        provider="local_steam",
        capability="installed",
        machine_id=machine_key,
        started_at=now,
    )
    for appid, size_bytes, build_id in entries:
        library_root = (library_roots or {}).get(appid, "/synthetic/library")
        storage.record_installed_observation(
            run.id,
            InstalledObservation(
                appid=appid,
                library_root=library_root,
                install_dir=f"{library_root}/steamapps/common/title-{appid}",
                observed_at=now,
                name=f"Synthetic title {appid}",
                build_id=build_id,
                size_bytes=size_bytes,
                manifest_path=f"{library_root}/steamapps/appmanifest_{appid}.acf",
                manifest_mtime=now,
            ),
            EvidenceInput(
                provider="local_steam",
                capability="installed",
                source_kind="local_file",
                source_locator=f"{library_root}/steamapps/appmanifest_{appid}.acf",
                retrieved_at=now,
                support_level="local_heuristic",
                payload={"synthetic": True},
            ),
        )
    storage.finish_installed_sync(run.id, status="complete", completed_at=now)
    return run.id


def declared_app_facts_payload(
    appid: int,
    *,
    linux_supported: bool = True,
    windows_supported: bool = False,
    linux_minimum: str | None = None,
    category_slugs: Sequence[str] = (),
    schema_id: str = "declared-app-facts/0.2",
) -> dict[str, object]:
    """Build one normalized declared-facts payload the store will accept."""

    payload: dict[str, object] = {
        "schema_id": schema_id,
        "appid": appid,
        "context": {"country": "US", "language": "english"},
        "platforms": {
            "state": "declared",
            "windows": windows_supported,
            "macos": False,
            "linux": linux_supported,
        },
        "requirements": [
            {
                "platform": platform,
                "state": (
                    "declared"
                    if platform == "linux" and linux_minimum is not None
                    else "undeclared"
                ),
                "minimum": linux_minimum if platform == "linux" else None,
                "recommended": None,
            }
            for platform in ("linux", "macos", "windows")
        ],
        "languages": {"state": "undeclared", "items": [], "unrecognized_count": 0},
        "categories": {
            "state": "declared" if category_slugs else "undeclared",
            "known_slugs": list(category_slugs),
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
    if schema_id == "declared-app-facts/0.1":
        payload.pop("genres")
        payload.pop("coming_soon")
    return payload


__all__ = [
    "GIBIBYTE",
    "FUTURE_OFFSET",
    "STALE_OFFSET",
    "UnsupportedScenarioError",
    "declared_app_facts_payload",
    "materialization_now",
    "materialize",
    "parse_detail",
    "required_argument_value",
    "scenario_account_alias",
    "scenario_machine_key",
    "seed_identity",
    "subject_appid",
    "write_installed",
    "write_owned_snapshot",
]

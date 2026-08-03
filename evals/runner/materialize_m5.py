"""Materialize M5 compatibility scenarios.

Machine-target scenarios with candidate evidence seed one complete system
profile plus per-app declared facts through the public sync writers, so the
CLI's own bounded minimum-requirement parser -- not the fixture -- decides each
gate.  A request with no candidate evidence keeps the system profile absent.
Where a scenario needs a component to stay unknown, the declared minimum text
is written to be unparseable by that parser; if a future parser learns to
compare it, adjust the scenario prose, never the parser.

``INSTALLED_FRESH`` is fifteen minutes.  No asserted field depends on it; the
installed observation is written one minute before the scenario clock so the
storage-free minimum path stays selected for installed candidates.

Valve Deck targets have no CLI writer: ``compatibility_query`` never
reconstructs ``exact_target_review``. Their deterministic preflight therefore
executes the same compatibility domain oracle directly and grades the frozen
scenario assertions against its serialized assessment.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from steam_agent.steam_declared_facts import DECLARED_FACTS_DISCLOSURE_VERSION
from steam_agent.compatibility import (
    CompatibilityCandidate,
    CompatibilityTarget,
    PrimitiveEvidence,
    assess_compatibility,
    unknown as compatibility_unknown,
    valve_deck_review,
)
from steam_agent.storage import Storage, WishlistObservation
from steam_agent.system_profile import SYSTEM_PROFILE_DISCLOSURE_VERSION, fact, unknown
from steam_agent.wishlist_library import WISHLIST_DISCLOSURE_VERSION

from .materialize import (
    GIBIBYTE,
    UnsupportedScenarioError,
    declared_app_facts_payload,
    materialization_now,
    scenario_account_alias,
    scenario_machine_key,
    seed_identity,
    subject_appid,
    write_installed,
    write_owned_snapshot,
)

COMPARABLE_MINIMUM = (
    "Memory: 4 GiB RAM\n"
    "Storage: 4 GiB available space\n"
    "Architecture: x86_64"
)
OPAQUE_MINIMUM = (
    "Memory: 4 GiB RAM\n"
    "Storage: 4 GiB available space\n"
    "Architecture: x86_64\n"
    "Processor: OPAQUE CPU X\n"
    "Graphics: OPAQUE GPU Y"
)

_COMPARABLE_STATES = {
    "machine_mandatory_pass",
    "installed_owned_compatible",
    "only_second_candidate_available",
    "screen_reader_absent_evidence",
}
_OPAQUE_STATES = {"opaque_cpu_gpu", "minimum_unknown_with_override"}
_DECK_STATES = {"deck_playable", "deck_unsupported"}
# The canonical wishlist compatibility route (ADR 0014) needs a wishlist
# snapshot old enough to be non-authoritative while its last-good items still
# expand the app-facts scope, and candidates absent from the visible-owned
# projection so ``readiness:visible_owned`` stays unknown.
_WISHLIST_STATES = {
    "wishlisted_comparable_minimum_stale_scope",
    "wishlisted_compatible_uninstalled",
}
_STALE_WISHLIST_OFFSET = timedelta(hours=30)


def _compatibility_fact(
    now: datetime, *, state: str = "pass", suffix: str
) -> PrimitiveEvidence:
    return PrimitiveEvidence(
        state,  # type: ignore[arg-type]
        "synthetic",
        "local",
        now,
        "fresh",
        (f"eval:{suffix}",),
    )


def valve_deck_oracle_document(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """Execute the exact pure domain oracle for a frozen Deck fixture."""

    scenario_id = scenario.get("id")
    required = scenario.get("tool_policy", {}).get("required", ())
    facts = scenario.get("fixture", {}).get("facts", ())
    if (
        scenario_id not in {"m5-c03", "m5-c04"}
        or len(required) != 1
        or required[0].get("command") != "steam-agent compatibility assess"
        or len(facts) != 1
    ):
        raise UnsupportedScenarioError("invalid Valve Deck oracle fixture")
    arguments = required[0].get("arguments")
    if not isinstance(arguments, list) or not arguments:
        raise UnsupportedScenarioError("invalid Valve Deck oracle invocation")
    try:
        appid = int(arguments[0])
        target_index = arguments.index("--target")
        target_value = arguments[target_index + 1]
        fact_appid = int(facts[0]["subject"].rsplit(":", 1)[1])
        state = facts[0]["state"]
        now = materialization_now(scenario)
    except (AttributeError, IndexError, TypeError, ValueError):
        raise UnsupportedScenarioError("invalid Valve Deck oracle fixture") from None
    if (
        target_value != "valve:steam-deck"
        or appid != fact_appid
        or state not in _DECK_STATES
    ):
        raise UnsupportedScenarioError("invalid Valve Deck oracle fixture")
    target = CompatibilityTarget("valve_deck", "steam-deck", "steamos")
    candidate = CompatibilityCandidate(
        appid=appid,
        target=target,
        declared_native_build=_compatibility_fact(now, suffix="native-build"),
        effective_execution_support=_compatibility_fact(now, suffix="os"),
        architecture=_compatibility_fact(now, suffix="arch"),
        meets_minimum=(
            compatibility_unknown("minimum_not_observed")
            if state == "deck_unsupported"
            else _compatibility_fact(now, suffix="minimum")
        ),
        exact_target_review=valve_deck_review(
            "playable" if state == "deck_playable" else "unsupported",
            target=target,
            source="valve",
            observed_at=now,
            freshness="fresh",
            evidence_ids=("eval:deck",),
        ),
        likely_good_experience=compatibility_unknown("performance_not_benchmarked"),
        installed=_compatibility_fact(now, suffix="installed"),
        owned=_compatibility_fact(now, suffix="owned"),
    )
    assessment = assess_compatibility((appid,), (candidate,), target=target)
    return {"data": _json_ready(asdict(assessment))}


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_ready(item) for item in value]
    return value


def _system_profile() -> dict[str, Any]:
    return {
        "schema_id": "system-profile/0.1",
        "os": {
            "family": fact("known", value="linux", evidence_refs=("platform:system",)),
            "name": fact(
                "known", value="Synthetic OS", evidence_refs=("platform:system",)
            ),
            "version": fact("known", value="1", evidence_refs=("platform:release",)),
            "build": unknown("not_reported", "platform:build"),
            "kernel": fact("known", value="1", evidence_refs=("platform:release",)),
        },
        "cpu": {
            "architecture": fact(
                "known", value="x86_64", evidence_refs=("platform:machine",)
            ),
            "model": fact(
                "known", value="SYNTHETIC CPU", evidence_refs=("linux:proc-cpuinfo",)
            ),
            "physical_cores": fact(
                "known", value=4, evidence_refs=("platform:cpu-count",)
            ),
            "logical_processors": fact(
                "known", value=8, evidence_refs=("platform:cpu-count",)
            ),
            "features": fact(
                "known", value=["avx2"], evidence_refs=("linux:proc-cpuinfo",)
            ),
        },
        "memory": {
            "total_bytes": fact(
                "known", value=16 * GIBIBYTE, evidence_refs=("linux:proc-meminfo",)
            )
        },
        "graphics": fact(
            "known",
            value=[
                {
                    "adapter_id": "gpu-0",
                    "name": "SYNTHETIC GPU",
                    "vendor_id": None,
                    "device_id": None,
                    "memory": {"kind": "unknown", "bytes": None},
                    "driver_version": None,
                    "apis": ["vulkan"],
                }
            ],
            evidence_refs=("linux:drm-allowlist",),
        ),
        "storage": fact(
            "known",
            value=[
                {
                    "role": "system",
                    "capacity_bytes": 100 * GIBIBYTE,
                    "available_bytes": 50 * GIBIBYTE,
                    "filesystem": None,
                    "medium": "unknown",
                }
            ],
            evidence_refs=("filesystem:system-role",),
        ),
        "gamepad": unknown("not_observed", "platform:input"),
        "vr": unknown("not_observed", "platform:vr"),
    }


class _Plan:
    def __init__(self, appid: int) -> None:
        self.appid = appid
        self.owned = True
        self.installed = True
        self.wishlisted = False
        self.stale_wishlist = False
        self.declared: dict[str, Any] | None = None


def _plan(appid: int, state: str) -> _Plan | None:
    if state in _DECK_STATES:
        raise UnsupportedScenarioError(
            "valve deck review evidence has no CLI writer "
            "(compatibility_query exact_target_review is always None)"
        )
    plan = _Plan(appid)
    if state in _COMPARABLE_STATES:
        plan.declared = declared_app_facts_payload(
            appid, linux_supported=True, linux_minimum=COMPARABLE_MINIMUM
        )
    elif state in _OPAQUE_STATES:
        plan.declared = declared_app_facts_payload(
            appid, linux_supported=True, linux_minimum=OPAQUE_MINIMUM
        )
    elif state == "no_native_linux_proton_unknown":
        plan.installed = False
        plan.declared = declared_app_facts_payload(
            appid, linux_supported=False, windows_supported=True
        )
    elif state in _WISHLIST_STATES:
        plan.owned = False
        plan.installed = False
        plan.wishlisted = True
        plan.stale_wishlist = state == "wishlisted_comparable_minimum_stale_scope"
        plan.declared = declared_app_facts_payload(
            appid, linux_supported=True, linux_minimum=COMPARABLE_MINIMUM
        )
    elif state == "requested_without_evidence":
        return None
    else:
        raise UnsupportedScenarioError(f"no M5 fixture builder for {state!r}")
    return plan


def _write_wishlist(
    storage: Storage,
    *,
    account_id: int,
    appids: Sequence[int],
    observed_at: datetime,
) -> None:
    """Write one complete wishlist snapshot at the scenario-selected time."""

    storage.record_wishlist_data_consent(
        account_id=account_id,
        disclosure_version=WISHLIST_DISCLOSURE_VERSION,
        accepted_at=observed_at,
        backups_acknowledged=True,
    )
    run = storage.begin_sync(
        provider="steam_web_api",
        capability="wishlist.read",
        account_id=account_id,
        started_at=observed_at,
    )
    observations = tuple(
        WishlistObservation(appid, priority, int(observed_at.timestamp()), observed_at)
        for priority, appid in enumerate(sorted(appids))
    )
    storage.complete_wishlist_snapshot(
        run.id,
        observations,
        item_list_retrieved_at=observed_at,
        item_count_retrieved_at=observed_at,
        item_list_reported_count=len(observations),
        item_count_reported_count=len(observations),
        completed_at=observed_at,
    )


def _write_declared(
    storage: Storage,
    *,
    account_id: int,
    machine_key: str,
    plans: Sequence[_Plan],
    now: Any,
) -> None:
    demanded = [plan.appid for plan in plans if plan.declared is not None]
    if not demanded:
        return
    storage.record_compatibility_data_consent(
        account_id=account_id,
        disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
        accepted_at=now,
        backups_acknowledged=True,
    )
    run, _, _ = storage.begin_declared_app_sync(
        account_id=account_id,
        machine_id=machine_key,
        demanded_appids=demanded,
        country="US",
        language="english",
        max_items=len(demanded),
        skip_fresh_terminal=True,
        started_at=now,
        disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
    )
    for plan in plans:
        if plan.declared is None:
            continue
        storage.record_declared_app_result(
            run.id,
            account_id=account_id,
            appid=plan.appid,
            state="ready",
            observed_at=now,
            facts=plan.declared,
        )
    storage.finish_declared_app_sync(run.id, completed_at=now)


def build(scenario: Mapping[str, Any], data_dir: Path) -> None:
    machine_key = scenario_machine_key(scenario)
    account_alias = scenario_account_alias(scenario)
    now = materialization_now(scenario)

    plans = [
        plan
        for fact_entry in scenario["fixture"]["facts"]
        if (plan := _plan(subject_appid(fact_entry), fact_entry["state"])) is not None
    ]
    wishlisted = [plan.appid for plan in plans if plan.wishlisted]
    stale_wishlist = any(plan.stale_wishlist for plan in plans)
    identity_at = now - _STALE_WISHLIST_OFFSET if stale_wishlist else now

    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = seed_identity(
            storage,
            machine_key=machine_key,
            account_alias=account_alias,
            now=identity_at,
        )
        if plans:
            storage.record_system_profile_consent(
                machine_id=machine_key,
                disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
                accepted_at=now,
                backups_acknowledged=True,
            )
            profile_run = storage.begin_system_profile_sync(
                machine_id=machine_key,
                disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
                started_at=now,
            )
            storage.complete_system_profile_sync(
                profile_run.id,
                profile=_system_profile(),
                observed_at=now,
                completed_at=now,
                disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
            )
            write_owned_snapshot(
                storage, account.id, [plan.appid for plan in plans if plan.owned], now
            )
            write_installed(
                storage,
                machine_key,
                [
                    (plan.appid, 1_000_000_000, "1")
                    for plan in plans
                    if plan.installed
                ],
                now,
            )
        _write_declared(
            storage,
            account_id=account.id,
            machine_key=machine_key,
            plans=plans,
            now=now,
        )
        if wishlisted:
            _write_wishlist(
                storage,
                account_id=account.id,
                appids=wishlisted,
                observed_at=(
                    now - _STALE_WISHLIST_OFFSET if stale_wishlist else now
                ),
            )


__all__ = ["build", "valve_deck_oracle_document"]

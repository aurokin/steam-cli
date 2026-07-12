from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Mapping

import pytest

from steam_agent.compatibility import (
    CompatibilityTarget,
    MachineCapacity,
    MinimumRequirements,
    PrimitiveEvidence,
    compare_minimum_requirements,
)
from steam_agent.compatibility_query import (
    LocalObservation,
    ManualCompatibilityReference,
    MinimumEvaluation,
    SystemSnapshot,
    manual_references,
    reconstruct_compatibility,
)


NOW = datetime(2030, 1, 15, 12, tzinfo=timezone.utc)
WINDOWS = CompatibilityTarget("machine", "desk", "windows")
LINUX = CompatibilityTarget("machine", "desk", "linux")
DECK = CompatibilityTarget("valve_deck", "valve-deck", "steamos")


def system(
    platform: str = "windows", *, observed_at: datetime = NOW, target_key: str = "desk"
) -> SystemSnapshot:
    return SystemSnapshot(
        target_key,
        {
            "schema_id": "system-profile/0.1",
            "os": {"family": {"state": "known", "value": platform}},
            "cpu": {"architecture": {"state": "known", "value": "x86_64"}},
            "memory": {"total_bytes": {"state": "known", "value": 16 << 30}},
            "storage": {"state": "known", "value": {"available_bytes": 50 << 30}},
            "graphics": {"state": "known", "value": {"models": ["PRIVATE GPU"]}},
        },
        observed_at,
        "system-snapshot-1",
    )


def facts(
    appid: int,
    *,
    windows: bool = True,
    linux: bool = False,
    categories: tuple[str, ...] = (),
    languages: tuple[Mapping[str, object], ...] = (),
    controller: str | None = None,
    external: str = "undeclared",
    drm: str = "undeclared",
) -> Mapping[str, Any]:
    return {
        "schema_id": "declared-app-facts/0.1",
        "appid": appid,
        "context": {"country": "US", "language": "english"},
        "platforms": {
            "state": "declared",
            "windows": windows,
            "macos": False,
            "linux": linux,
        },
        "requirements": [],
        "languages": {
            "state": "declared",
            "items": list(languages),
            "unrecognized_count": 0,
        },
        "categories": {
            "state": "declared",
            "known_slugs": list(categories),
            "unknown_ids": [],
        },
        "controller_support": controller,
        "external_account_notice": {
            "state": external,
            "text": "private prose" if external == "declared" else None,
        },
        "drm_notice": {
            "state": drm,
            "text": "private prose" if drm == "declared" else None,
        },
        "source": {"support_level": "provisional"},
    }


def declared(
    appid: int,
    *,
    observed_at: datetime = NOW,
    payload: Mapping[str, Any] | None = None,
    demand_state: str = "ready",
    demand_at: datetime | None = None,
) -> Mapping[str, Any]:
    return {
        "items": (
            {
                "appid": appid,
                "facts": facts(appid) if payload is None else payload,
                "observed_at": observed_at.isoformat(),
            },
        ),
        "latest_demand": (
            {
                "appid": appid,
                "evaluated": demand_state != "unattempted",
                "state": demand_state,
                "observed_at": (demand_at or observed_at).isoformat(),
            },
        ),
    }


def local(
    source: str,
    presence: str = "present",
    *,
    observed_at: datetime = NOW,
) -> LocalObservation:
    return LocalObservation(presence, observed_at, source, "snapshot-7")  # type: ignore[arg-type]


def result(
    *,
    target: CompatibilityTarget = WINDOWS,
    snapshot: Mapping[str, Any] | None = None,
    system_snapshot: SystemSnapshot | None = None,
    installed: Mapping[int, LocalObservation] | None = None,
    owned: Mapping[int, LocalObservation] | None = None,
    minimum_evaluator: object | None = None,
):
    return reconstruct_compatibility(
        (10,),
        target=target,
        generated_at=NOW,
        system=system("linux" if target.platform in {"linux", "steamos"} else target.platform)
        if system_snapshot is None
        else system_snapshot,
        declared_snapshot=declared(10) if snapshot is None else snapshot,
        installed=installed,
        owned=owned,
        minimum_evaluator=minimum_evaluator,  # type: ignore[arg-type]
    ).assessment.results[0]


def test_every_requested_appid_is_retained_without_any_snapshot() -> None:
    query = reconstruct_compatibility(
        (30, 10, 20),
        target=WINDOWS,
        generated_at=NOW,
        system=None,
        declared_snapshot=None,
    )
    assert query.assessment.requested_appids == (10, 20, 30)
    assert tuple(item.appid for item in query.assessment.results) == (10, 20, 30)
    assert all(item.compatibility == "unknown" for item in query.assessment.results)
    assert tuple(appid for appid, _ in query.references) == (10, 20, 30)


def test_windows_declared_false_is_effective_failure() -> None:
    item = result(snapshot=declared(10, payload=facts(10, windows=False)))
    gates = {gate.name: gate for gate in item.gates}
    assert gates["declared_native_build"].effective == "fail"
    assert gates["effective_execution_support"].effective == "fail"
    assert item.compatibility == "incompatible"


def test_linux_declared_false_is_not_a_proton_incompatibility_claim() -> None:
    item = result(target=LINUX, snapshot=declared(10, payload=facts(10, linux=False)))
    gates = {gate.name: gate for gate in item.gates}
    assert gates["declared_native_build"].effective == "fail"
    assert gates["effective_execution_support"].effective == "unknown"
    assert gates["effective_execution_support"].effective_unknown_reason == "proton_route_not_evaluated"
    assert item.compatibility == "unknown"


def test_deck_never_borrows_generic_linux_evidence_as_an_exact_rating() -> None:
    item = result(target=DECK, snapshot=declared(10, payload=facts(10, linux=True)))
    exact = next(gate for gate in item.gates if gate.name == "exact_target_review")
    assert exact.effective == "unknown"
    assert exact.effective_unknown_reason == "exact_target_review_not_observed"
    assert item.compatibility == "unknown"


def test_positive_features_and_full_audio_are_exposed_without_negative_inference() -> None:
    payload = facts(
        10,
        categories=("adjustable_text_size", "full_controller_support"),
        languages=(
            {"code": "english", "full_audio": True},
            {"code": "french", "full_audio": False},
        ),
        controller="full",
    )
    item = result(snapshot=declared(10, payload=payload))
    assert [feature.name for feature in item.accessibility] == ["adjustable-text-size"]
    assert [feature.name for feature in item.input] == [
        "full-controller-support",
    ]
    assert [feature.name for feature in item.language] == [
        "english",
        "english-full-audio",
        "french",
    ]
    assert all(feature.support.state == "pass" for feature in (*item.accessibility, *item.input, *item.language))


def test_input_category_classification_uses_an_explicit_allowlist() -> None:
    payload = facts(
        10,
        categories=(
            "steam_input_api_support",
            "dualsense_usb_support",
            "gamepad_preferred",
            "narrated_game_menus",
            "future_unreviewed_category",
        ),
    )
    item = result(snapshot=declared(10, payload=payload))
    assert [feature.name for feature in item.input] == [
        "dualsense-usb-support",
        "gamepad-preferred",
        "steam-input-api-support",
    ]
    assert [feature.name for feature in item.accessibility] == [
        "narrated-game-menus"
    ]


def test_positive_drm_and_account_notices_become_runtime_conditions_without_prose() -> None:
    item = result(
        snapshot=declared(
            10, payload=facts(10, external="declared", drm="declared")
        )
    )
    assert [(risk.name, risk.presence, risk.impact) for risk in item.runtime_risks] == [
        ("declared-drm", "present", "runtime"),
        ("external-account", "present", "manual"),
    ]
    rendered = json.dumps(asdict(item), default=str)
    assert "private prose" not in rendered


def test_undeclared_notices_are_explicit_absence_evidence() -> None:
    assert [risk.presence for risk in result().runtime_risks] == ["absent", "absent"]


def test_missing_notice_state_is_attributed_unknown_not_absence() -> None:
    payload = dict(facts(10))
    payload.pop("drm_notice")
    item = result(snapshot=declared(10, payload=payload))
    drm = next(risk for risk in item.runtime_risks if risk.name == "declared-drm")
    assert drm.presence == "unknown"
    assert drm.evidence.source == "steam_store_appdetails"
    assert drm.evidence.evidence_ids


def test_owned_absence_is_unknown_not_nonownership() -> None:
    item = result(owned={10: local("visible_owned", "absent")})
    gate = next(gate for gate in item.gates if gate.name == "readiness:visible_owned")
    assert gate.original == "unknown"
    assert gate.original_unknown_reason == "visible_owned_absence_is_not_nonownership"
    assert gate.original_source == "visible_owned"
    assert gate.original_evidence_ids


def test_visible_owned_presence_is_current_for_twenty_four_hours() -> None:
    item = result(
        owned={
            10: local(
                "visible_owned", "present", observed_at=NOW - timedelta(hours=25)
            )
        }
    )
    gate = next(gate for gate in item.gates if gate.name == "readiness:visible_owned")
    assert (gate.original, gate.original_freshness, gate.effective) == (
        "pass", "stale", "unknown"
    )


@pytest.mark.parametrize("source", ["installed_projection", "visible_owned"])
def test_newer_failed_local_attempt_makes_last_good_stale(source: str) -> None:
    observation = LocalObservation(
        "present",
        NOW - timedelta(minutes=5),
        source,  # type: ignore[arg-type]
        "last-good",
        latest_attempt_at=NOW,
        latest_attempt_status="failed",
    )
    kwargs = (
        {"installed": {10: observation}}
        if source == "installed_projection"
        else {"owned": {10: observation}}
    )
    item = result(**kwargs)
    gate_name = (
        "readiness:installed"
        if source == "installed_projection"
        else "readiness:visible_owned"
    )
    gate = next(gate for gate in item.gates if gate.name == gate_name)
    assert gate.original_freshness == "stale"
    assert gate.effective == "unknown"


def test_install_observations_are_current_for_only_fifteen_minutes() -> None:
    fresh = result(installed={10: local("installed_projection", "absent")})
    assert fresh.playable_now == "fail"
    stale = result(
        installed={
            10: local(
                "installed_projection", "absent", observed_at=NOW - timedelta(minutes=16)
            )
        }
    )
    gate = next(gate for gate in stale.gates if gate.name == "readiness:installed")
    assert (gate.original, gate.effective) == ("fail", "unknown")
    assert stale.playable_now == "unknown"


@pytest.mark.parametrize(
    ("age", "freshness", "effective"),
    [
        (timedelta(days=7), "fresh", "pass"),
        (timedelta(days=8), "stale", "pass"),
        (timedelta(days=31), "expired", "unknown"),
    ],
)
def test_declared_freshness_has_separate_fresh_stale_and_expired_windows(
    age: timedelta, freshness: str, effective: str
) -> None:
    item = result(snapshot=declared(10, observed_at=NOW - age))
    gate = next(gate for gate in item.gates if gate.name == "declared_native_build")
    assert gate.original_freshness == freshness
    assert gate.effective == effective


def test_stale_system_target_cannot_drive_current_execution_support() -> None:
    item = result(system_snapshot=system(observed_at=NOW - timedelta(days=31)))
    gate = next(gate for gate in item.gates if gate.name == "effective_execution_support")
    assert gate.original_freshness == "expired"
    assert gate.effective == "unknown"


def test_newer_failed_system_attempt_marks_promoted_current_stale() -> None:
    base = system(observed_at=NOW - timedelta(minutes=5))
    stale = SystemSnapshot(
        base.target_key,
        base.profile,
        base.observed_at,
        base.snapshot_id,
        latest_attempt_at=NOW,
        latest_attempt_status="partial",
        latest_attempt_id="attempt-2",
    )
    item = result(system_snapshot=stale)
    gate = next(gate for gate in item.gates if gate.name == "effective_execution_support")
    assert gate.original_freshness == "stale"


def test_newer_failed_declared_demand_marks_retained_current_stale() -> None:
    item = result(
        snapshot=declared(
            10,
            observed_at=NOW - timedelta(hours=1),
            demand_state="failed",
            demand_at=NOW,
        )
    )
    native = next(gate for gate in item.gates if gate.name == "declared_native_build")
    assert native.original == "pass"
    assert native.original_freshness == "stale"


def test_target_key_and_platform_conflicts_are_preserved_as_conflicts() -> None:
    key_conflict = result(system_snapshot=system(target_key="other-machine"))
    assert "declared_native_build" in key_conflict.conflicts
    platform_conflict = result(system_snapshot=system("linux"))
    assert "effective_execution_support" in platform_conflict.conflicts


def test_newer_not_found_demand_conflicts_with_retained_current_facts() -> None:
    item = result(
        snapshot=declared(
            10,
            observed_at=NOW - timedelta(hours=1),
            demand_state="not_found",
            demand_at=NOW,
        )
    )
    native = next(gate for gate in item.gates if gate.name == "declared_native_build")
    assert native.original == "unknown"
    assert native.original_conflict is True


class SafeMinimumEvaluator:
    def evaluate(
        self,
        *,
        appid: int,
        platform: str,
        normalized_facts: Mapping[str, Any],
        system_profile: Mapping[str, Any] | None,
        declared_observed_at: datetime,
        system_observed_at: datetime | None,
        system_profile_freshness: str,
        storage_available_freshness: str,
        generated_at: datetime,
    ) -> MinimumEvaluation:
        del (
            normalized_facts,
            system_profile,
            declared_observed_at,
            system_observed_at,
            system_profile_freshness,
            storage_available_freshness,
            generated_at,
        )
        return MinimumEvaluation(
            PrimitiveEvidence(
                "pass", "structured-minimum", "provisional", NOW, "fresh",
                (f"lineage:arch:{appid:024x}"[-38:],),
            ),
            PrimitiveEvidence(
                "pass", "structured-minimum", "provisional", NOW, "fresh",
                (f"lineage:minimum:{appid:024x}"[-38:],),
            ),
        )


def test_minimum_parser_integration_is_small_and_can_complete_safe_gates() -> None:
    item = result(minimum_evaluator=SafeMinimumEvaluator())
    assert item.compatibility == "compatible"
    assert next(g for g in item.gates if g.name == "architecture").effective == "pass"
    assert next(g for g in item.gates if g.name == "meets_minimum").effective == "pass"
    assert item.likely_good_experience.state == "unknown"


def test_lineage_and_output_never_expose_hardware_requirement_paths_titles_or_ids() -> None:
    payload = facts(10)
    payload = {**payload, "requirements": [{"minimum": "SECRET REQUIREMENT PROSE"}]}
    query = reconstruct_compatibility(
        (10,), target=WINDOWS, generated_at=NOW,
        system=system(), declared_snapshot=declared(10, payload=payload),
        installed={10: local("installed_projection")},
        owned={10: local("visible_owned")},
    )
    rendered = json.dumps(asdict(query), default=str)
    for private in (
        "PRIVATE GPU", "SECRET REQUIREMENT PROSE", "/Users/", "steamid",
        "account_id", "title",
    ):
        assert private not in rendered
    evidence_ids = [
        evidence_id
        for item in query.assessment.results
        for gate in item.gates
        for evidence_id in gate.original_evidence_ids
    ]
    assert evidence_ids and all(len(value) <= 64 for value in evidence_ids)


def test_manual_references_are_exact_typed_and_manual_only() -> None:
    references = manual_references(10)
    assert [reference.provider for reference in references] == [
        "steam", "steamdb", "protondb", "pcgamingwiki",
    ]
    assert all(reference.access_mode == "manual_only" for reference in references)
    assert all(reference.automation_supported is False for reference in references)
    with pytest.raises(ValueError, match="allowlisted"):
        ManualCompatibilityReference(
            10, "steamdb", "inspect_price_history", "https://evil.example/app/10/"
        )


def test_engine_accepts_provisional_support_without_promoting_it() -> None:
    evidence = PrimitiveEvidence(
        "pass", "steam_store_appdetails", "provisional", NOW, "fresh",
        ("lineage:test:0123456789abcdef01234567",),
    )
    assert evidence.support_level == "provisional"


def test_no_hidden_requirement_comparison_is_performed_by_the_default_path() -> None:
    # The pure exact comparator itself also refuses opaque CPU/GPU model prose;
    # query reconstruction leaves this behind MinimumEvaluator.
    comparison = compare_minimum_requirements(
        machine=MachineCapacity(16_384, 100_000, "x86_64", ("x86_64",)),
        requirement=MinimumRequirements(
            8_192, 10_000, "x86_64", "CPU model", "GPU model", "declared", "declared"
        ),
    )
    assert comparison.overall == "unknown"
    assert {"cpu", "gpu"} <= set(comparison.unknowns)


def test_default_bounded_parser_can_detect_a_decisive_memory_failure() -> None:
    payload = {
        **facts(10),
        "requirements": [
            {
                "platform": "windows",
                "state": "declared",
                "minimum": "Memory: 32 GiB RAM\nProcessor: Opaque\nGraphics: Opaque",
                "recommended": None,
            }
        ],
    }
    item = result(snapshot=declared(10, payload=payload))
    minimum = next(gate for gate in item.gates if gate.name == "meets_minimum")
    assert minimum.effective == "fail"
    assert item.compatibility == "incompatible"
    assert "Opaque" not in json.dumps(asdict(item), default=str)


def test_default_minimum_parser_rejects_non_english_context() -> None:
    payload = {
        **facts(10),
        "context": {"country": "US", "language": "german"},
        "requirements": [
            {
                "platform": "windows",
                "state": "declared",
                "minimum": "Memory: 32 GiB RAM",
                "recommended": None,
            }
        ],
    }
    item = result(snapshot=declared(10, payload=payload))
    minimum = next(gate for gate in item.gates if gate.name == "meets_minimum")
    assert minimum.effective == "unknown"
    assert item.compatibility == "unknown"


def test_future_declared_observation_is_attributed_unknown() -> None:
    item = result(snapshot=declared(10, observed_at=NOW + timedelta(seconds=1)))
    native = next(gate for gate in item.gates if gate.name == "declared_native_build")
    assert native.original == "unknown"
    assert native.original_unknown_reason == "observation_time_in_future"
    assert native.original_source == "steam_store_appdetails"
    assert native.original_evidence_ids


def test_future_system_observation_is_attributed_unknown() -> None:
    item = result(system_snapshot=system(observed_at=NOW + timedelta(seconds=1)))
    execution = next(
        gate for gate in item.gates if gate.name == "effective_execution_support"
    )
    assert execution.original == "unknown"
    assert execution.original_unknown_reason == "observation_time_in_future"
    assert execution.original_evidence_ids


@pytest.mark.parametrize("source", ["installed_projection", "visible_owned"])
def test_future_local_observation_is_attributed_unknown(source: str) -> None:
    observation = local(source, observed_at=NOW + timedelta(seconds=1))
    kwargs = (
        {"installed": {10: observation}}
        if source == "installed_projection"
        else {"owned": {10: observation}}
    )
    item = result(**kwargs)
    gate_name = (
        "readiness:installed"
        if source == "installed_projection"
        else "readiness:visible_owned"
    )
    gate = next(gate for gate in item.gates if gate.name == gate_name)
    assert gate.original == "unknown"
    assert gate.original_unknown_reason == "observation_time_in_future"
    assert gate.original_source == source
    assert gate.original_evidence_ids


def test_reconstruction_completeness_is_explicit_and_per_subject() -> None:
    query = reconstruct_compatibility(
        (10, 20),
        target=WINDOWS,
        generated_at=NOW,
        system=system(),
        declared_snapshot=declared(10),
        installed={
            10: local("installed_projection"),
            20: local(
                "installed_projection", observed_at=NOW - timedelta(minutes=16)
            ),
        },
        owned={10: local("visible_owned")},
    )
    assert "operations.ready.read" in query.completeness.missing_capabilities
    assert "compatibility.declared.read" in query.completeness.missing_capabilities
    assert "account.visible_owned.read" in query.completeness.missing_capabilities
    assert "library.installed.read" in query.completeness.stale_capabilities
    by_appid = {item.appid: item for item in query.completeness.subjects}
    assert by_appid[10].missing == ("operations.ready.read",)
    assert "compatibility.declared.read" in by_appid[20].missing
    assert "account.visible_owned.read" in by_appid[20].missing
    assert "library.installed.read" in by_appid[20].stale

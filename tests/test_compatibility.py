from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from steam_agent.compatibility import (
    CompatibilityCandidate,
    CompatibilityTarget,
    FeatureEvidence,
    FeatureRequirement,
    GateOverride,
    MachineCapacity,
    MinimumRequirements,
    PrimitiveEvidence,
    RuntimeRisk,
    assess_compatibility,
    compare_minimum_requirements,
    unknown,
    valve_deck_review,
)


NOW = datetime(2030, 1, 15, 12, tzinfo=timezone.utc)
MACHINE = CompatibilityTarget("machine", "synthetic-machine", "linux")
DECK = CompatibilityTarget("valve_deck", "valve-deck", "steamos")


def fact(state: str = "pass", *, condition: str | None = None, suffix: str = "fact") -> PrimitiveEvidence:
    return PrimitiveEvidence(
        state,  # type: ignore[arg-type]
        "synthetic",
        "local",
        NOW,
        "fresh",
        (f"eval:{suffix}",),
        condition=condition,
    )


def candidate(appid: int, **changes: object) -> CompatibilityCandidate:
    base = CompatibilityCandidate(
        appid,
        MACHINE,
        fact(suffix="native-build"),
        fact(suffix="os"),
        fact(suffix="arch"),
        fact(suffix="minimum"),
        None,
        unknown("performance_not_benchmarked"),
        fact(suffix="installed"),
        fact(suffix="owned"),
    )
    return replace(base, **changes)


def result(item: CompatibilityCandidate, *, target: CompatibilityTarget = MACHINE, **kwargs: object):
    if item.target != target:
        item = replace(item, target=target)
    return assess_compatibility((item.appid,), (item,), target=target, **kwargs).results[0]


def test_compatible_only_when_machine_mandatory_gates_pass() -> None:
    item = result(candidate(10))
    assert item.compatibility == "compatible"
    assert item.playable_now == "unknown"
    assert "playable_now_requires_m7_operational_facts" in item.reasons
    assert {gate.name: gate.mandatory for gate in item.gates} == {
        "declared_native_build": False,
        "effective_execution_support": True,
        "architecture": True,
        "meets_minimum": True,
        "exact_target_review": False,
        "readiness:installed": False,
        "readiness:visible_owned": False,
    }


def test_decisive_fail_precedes_conditions_and_unknowns() -> None:
    item = candidate(
        11,
        effective_execution_support=fact("fail", suffix="os-fail"),
        meets_minimum=unknown("minimum_not_normalized"),
        runtime_risks=(
            RuntimeRisk("manual-launcher", "present", "manual", fact(condition=None, suffix="risk")),
        ),
    )
    assessed = result(item)
    assert assessed.compatibility == "incompatible"
    assert assessed.playable_now == "fail"


def test_required_unknown_precedes_known_manual_or_runtime_condition() -> None:
    item = candidate(
        12,
        meets_minimum=unknown("cpu_requirement_not_comparable"),
        runtime_risks=(RuntimeRisk("manual-launcher", "present", "manual", fact(suffix="risk")),),
    )
    assessed = result(item)
    assert assessed.compatibility == "unknown"
    risk = next(gate for gate in assessed.gates if gate.name == "runtime:manual-launcher")
    assert risk.effective_condition == "runtime_risk:manual-launcher"
    assert "meets_minimum" in assessed.unknowns


def test_blocking_runtime_risk_is_incompatible() -> None:
    item = candidate(
        13,
        runtime_risks=(RuntimeRisk("unsupported-anticheat", "present", "blocking", fact(suffix="risk")),),
    )
    assert result(item).compatibility == "incompatible"


def test_accessibility_absence_is_unknown_and_explicit_failure_is_distinct() -> None:
    requirement = (FeatureRequirement("accessibility", "screen-reader"),)
    absent = result(candidate(14), requirements=requirement)
    gate = next(gate for gate in absent.gates if gate.name == "accessibility:screen-reader")
    assert gate.effective == "unknown"
    assert gate.original_unknown_reason == "required_feature_not_observed"
    explicit = candidate(
        14,
        features=(FeatureEvidence("accessibility", "screen-reader", fact("fail", suffix="a11y")),),
    )
    assert result(explicit, requirements=requirement).compatibility == "incompatible"


def test_named_override_preserves_original_state_and_lineage() -> None:
    override = GateOverride("user-accepts-risk", 15, "meets_minimum", "pass", ("query:override",), NOW)
    assessed = result(
        candidate(15, meets_minimum=unknown("opaque_cpu_and_gpu")),
        overrides=(override,),
    )
    gate = next(gate for gate in assessed.gates if gate.name == "meets_minimum")
    assert (gate.original, gate.effective) == ("unknown", "pass")
    assert gate.override_name == "user-accepts-risk"
    assert gate.override_evidence_ids == ("query:override",)
    assert assessed.compatibility == "compatible"


def test_requested_appids_are_retained_and_sorted_when_evidence_is_missing() -> None:
    batch = assess_compatibility((30, 10, 20), (candidate(20),), target=MACHINE)
    assert batch.requested_appids == (10, 20, 30)
    assert tuple(item.appid for item in batch.results) == (10, 20, 30)
    assert batch.results[0].compatibility == "unknown"
    assert batch.results[2].compatibility == "unknown"


def test_valve_deck_ratings_are_exact_target_scoped() -> None:
    verified = valve_deck_review(
        "verified", target=DECK, source="valve", observed_at=NOW,
        freshness="fresh", evidence_ids=("valve:deck:1",),
    )
    playable = valve_deck_review(
        "playable", target=DECK, source="valve", observed_at=NOW,
        freshness="fresh", evidence_ids=("valve:deck:2",),
    )
    unsupported = valve_deck_review(
        "unsupported", target=DECK, source="valve", observed_at=NOW,
        freshness="fresh", evidence_ids=("valve:deck:3",),
    )
    assert result(candidate(40, exact_target_review=verified), target=DECK).compatibility == "compatible"
    assert result(candidate(41, exact_target_review=playable), target=DECK).compatibility == "conditional"
    assert result(candidate(42, exact_target_review=unsupported), target=DECK).compatibility == "incompatible"
    assert result(candidate(43), target=DECK).compatibility == "unknown"
    with pytest.raises(ValueError, match="exact Valve Deck target"):
        valve_deck_review("unknown", target=MACHINE)


def test_verified_deck_does_not_require_less_specific_generic_minimums() -> None:
    verified = valve_deck_review(
        "verified", target=DECK, source="valve", observed_at=NOW,
        freshness="fresh", evidence_ids=("valve:deck:verified",),
    )
    item = candidate(
        43,
        effective_execution_support=unknown("generic_os_missing"),
        architecture=unknown("generic_architecture_missing"),
        meets_minimum=unknown("generic_minimum_missing"),
        exact_target_review=verified,
    )
    assessed = result(item, target=DECK)
    assert assessed.compatibility == "compatible"
    assert next(g for g in assessed.gates if g.name == "exact_target_review").mandatory
    assert not next(g for g in assessed.gates if g.name == "meets_minimum").mandatory


def test_review_for_another_exact_target_is_not_reused() -> None:
    other_deck = CompatibilityTarget("valve_deck", "other-valve-deck", "steamos")
    review = valve_deck_review(
        "verified", target=other_deck, source="valve", observed_at=NOW,
        freshness="fresh", evidence_ids=("valve:other-deck",),
    )
    assessed = result(candidate(44, exact_target_review=review), target=DECK)
    gate = next(gate for gate in assessed.gates if gate.name == "exact_target_review")
    assert assessed.compatibility == "unknown"
    assert gate.original_unknown_reason == "target_review_is_for_a_different_target"


def test_known_not_installed_fails_playable_now_without_changing_compatibility() -> None:
    assessed = result(candidate(50, installed=fact("fail", suffix="not-installed")))
    assert assessed.compatibility == "compatible"
    assert assessed.playable_now == "fail"
    assert "known_not_installed" in assessed.reasons


def test_no_declared_linux_build_does_not_claim_proton_incompatibility() -> None:
    assessed = result(
        candidate(
            53,
            declared_native_build=fact("fail", suffix="no-native-linux"),
            effective_execution_support=unknown("proton_route_not_evaluated"),
        )
    )
    native = next(g for g in assessed.gates if g.name == "declared_native_build")
    execution = next(g for g in assessed.gates if g.name == "effective_execution_support")
    assert native.original == "fail" and not native.mandatory
    assert execution.effective == "unknown" and execution.mandatory
    assert assessed.compatibility == "unknown"


def test_visible_owned_absence_never_hard_fails_readiness() -> None:
    assessed = result(candidate(54, owned=fact("fail", suffix="not-visible-owned")))
    ownership = next(g for g in assessed.gates if g.name == "readiness:visible_owned")
    assert ownership.effective == "fail" and not ownership.mandatory
    assert assessed.compatibility == "compatible"
    assert assessed.playable_now == "unknown"


def test_expired_install_failure_cannot_drive_playable_now_failure() -> None:
    expired_absence = replace(fact("fail", suffix="expired-install"), freshness="expired")
    assessed = result(candidate(55, installed=expired_absence))
    installed = next(g for g in assessed.gates if g.name == "readiness:installed")
    assert (installed.original, installed.effective) == ("fail", "unknown")
    assert installed.effective_unknown_reason == "evidence_expired"
    assert assessed.playable_now == "unknown"
    assert "known_not_installed" not in assessed.reasons


def test_stale_install_failure_is_not_claimed_as_current_readiness() -> None:
    stale_absence = replace(fact("fail", suffix="stale-install"), freshness="stale")
    assessed = result(candidate(59, installed=stale_absence))
    installed = next(g for g in assessed.gates if g.name == "readiness:installed")
    assert (installed.original, installed.effective) == ("fail", "unknown")
    assert installed.effective_unknown_reason == "evidence_stale_for_ready_now"
    assert installed.effective_freshness == "stale"
    assert assessed.playable_now == "unknown"
    assert "known_not_installed" not in assessed.reasons


def test_expired_likely_experience_is_serialized_as_unknown() -> None:
    expired = replace(fact(suffix="old-performance"), freshness="expired")
    assessed = result(candidate(56, likely_good_experience=expired))
    assert assessed.likely_good_experience_original.state == "pass"
    assert assessed.likely_good_experience.state == "unknown"
    assert assessed.likely_good_experience.unknown_reason == "evidence_expired"


def test_candidate_machine_evidence_cannot_be_reused_for_another_machine() -> None:
    other = CompatibilityTarget("machine", "other-machine", "linux")
    with pytest.raises(ValueError, match="target does not match"):
        assess_compatibility((57,), (candidate(57),), target=other)


def test_stale_last_good_is_visible_and_expired_evidence_becomes_unknown() -> None:
    stale = replace(fact(suffix="stale-os"), freshness="stale")
    stale_result = result(candidate(51, effective_execution_support=stale))
    stale_gate = next(gate for gate in stale_result.gates if gate.name == "effective_execution_support")
    assert stale_result.compatibility == "compatible"
    assert stale_result.completeness == "partial"
    assert stale_gate.original_freshness == "stale"

    expired = replace(fact(suffix="expired-os"), freshness="expired")
    expired_result = result(candidate(52, effective_execution_support=expired))
    expired_gate = next(gate for gate in expired_result.gates if gate.name == "effective_execution_support")
    assert expired_result.compatibility == "unknown"
    assert (expired_gate.original, expired_gate.effective) == ("pass", "unknown")
    assert expired_gate.original_unknown_reason is None
    assert expired_gate.effective_unknown_reason == "evidence_expired"


def test_minimum_comparison_only_compares_exact_bounded_values() -> None:
    machine = MachineCapacity(16_384, 100_000, "x86_64")
    exact = compare_minimum_requirements(
        machine,
        MinimumRequirements(
            8_192, 50_000, "x86_64",
            cpu_state="authoritative_none", gpu_state="authoritative_none",
        ),
    )
    assert exact.overall == "pass"
    opaque = compare_minimum_requirements(
        machine,
        MinimumRequirements(
            8_192, 50_000, "x86_64", "FasterBrand 9", "Graphico Ultra",
            "declared", "declared",
        ),
    )
    assert opaque.overall == "unknown"
    assert opaque.cpu == opaque.gpu == "unknown"
    assert opaque.unknowns == ("cpu", "gpu")
    too_large = compare_minimum_requirements(
        machine,
        MinimumRequirements(
            32_768, 50_000, "x86_64",
            cpu_state="authoritative_none", gpu_state="authoritative_none",
        ),
    )
    assert too_large.overall == "fail" and too_large.ram == "fail"
    other_arch = compare_minimum_requirements(
        machine,
        MinimumRequirements(
            8_192, 50_000, "arm64",
            cpu_state="authoritative_none", gpu_state="authoritative_none",
        ),
    )
    assert other_arch.overall == "unknown" and other_arch.architecture == "unknown"

    explicit_x86 = compare_minimum_requirements(
        MachineCapacity(16_384, 100_000, "x86_64", ("x86",)),
        MinimumRequirements(
            8_192, 50_000, "x86",
            cpu_state="authoritative_none", gpu_state="authoritative_none",
        ),
    )
    assert explicit_x86.architecture == "pass" and explicit_x86.overall == "pass"


def test_missing_cpu_gpu_requirements_are_not_authoritative_no_constraint() -> None:
    comparison = compare_minimum_requirements(
        MachineCapacity(16_384, 100_000, "x86_64"),
        MinimumRequirements(8_192, 50_000, "x86_64"),
    )
    assert comparison.cpu == comparison.gpu == "unknown"
    assert comparison.overall == "unknown"


def test_override_effective_metadata_is_self_consistent_and_original_is_preserved() -> None:
    original = unknown("unparsed_requirement")
    override = GateOverride("accept-gap", 58, "meets_minimum", "pass", ("query:accept",), NOW)
    assessed = result(candidate(58, meets_minimum=original), overrides=(override,))
    gate = next(g for g in assessed.gates if g.name == "meets_minimum")
    assert gate.original == "unknown"
    assert gate.original_unknown_reason == "unparsed_requirement"
    assert gate.original_source is None
    assert gate.effective == "pass"
    assert gate.effective_unknown_reason is None
    assert gate.effective_source == "query_override:accept-gap"
    assert gate.effective_freshness == "fresh"
    assert gate.effective_evidence_ids == ("query:accept",)


def test_large_requested_set_is_stable_and_bounded() -> None:
    requested = tuple(range(1, 5_001))
    batch = assess_compatibility(requested, (), target=MACHINE)
    assert len(batch.results) == 5_000
    assert batch.results[0].appid == 1 and batch.results[-1].appid == 5_000

    requirements = tuple(
        FeatureRequirement("language", f"language-{index}")
        for index in range(256)
    )
    with pytest.raises(ValueError, match="total-work bound"):
        assess_compatibility(requested, (), target=MACHINE, requirements=requirements)


@pytest.mark.parametrize(
    "factory, message",
    [
        (lambda: PrimitiveEvidence("unknown", None, "unknown", None, "unknown"), "requires an explanation"),
        (lambda: PrimitiveEvidence("pass", None, "local", NOW, "fresh", ("e",)), "requires source"),
        (lambda: PrimitiveEvidence("pass", "x", "local", NOW, "fresh", ("e",), conflict=True), "must remain unknown"),
        (lambda: CompatibilityTarget("valve_deck", "deck", "linux"), "must use steamos"),
        (lambda: GateOverride("override", 1, "gate", "pass", (), NOW), "requires evidence"),
    ],
)
def test_strict_validation(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_composite_validation_rejects_duplicates_extras_and_unknown_overrides() -> None:
    with pytest.raises(ValueError, match="requested AppIDs must be unique"):
        assess_compatibility((1, 1), (), target=MACHINE)
    with pytest.raises(ValueError, match="explicitly requested"):
        assess_compatibility((1,), (candidate(2),), target=MACHINE)
    with pytest.raises(ValueError, match="not evaluated"):
        assess_compatibility(
            (1,), (candidate(1),), target=MACHINE,
            overrides=(GateOverride("named", 1, "not-a-gate", "pass", ("e",), NOW),),
        )

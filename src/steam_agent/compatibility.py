"""Pure deterministic compatibility assessments for the M5 ``0.1`` contract.

This module deliberately performs no I/O and makes no performance estimates.
Callers must normalize provider and local-machine observations before invoking
it.  In particular, CPU and GPU model names are never ordered or benchmarked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Literal


SCHEMA = "compatibility/0.1"
MAX_APPID = (1 << 32) - 1
MAX_BYTES = (1 << 63) - 1
MAX_ITEMS = 100_000
MAX_COMPONENTS = 256
MAX_EVIDENCE_IDS = 64
MAX_TEXT = 256

GateState = Literal["pass", "fail", "unknown"]
Freshness = Literal["fresh", "stale", "expired", "unknown"]
SupportLevel = Literal["official", "documented", "community", "local", "unknown"]
CompatibilityState = Literal["compatible", "incompatible", "conditional", "unknown"]
TargetKind = Literal["machine", "valve_deck"]
Platform = Literal["windows", "macos", "linux", "steamos"]
FeatureKind = Literal["accessibility", "input", "language"]

_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?\Z")
_GATE = re.compile(r"[a-z0-9](?:[a-z0-9._:-]{0,126}[a-z0-9])?\Z")
_ARCHITECTURES = {"x86", "x86_64", "arm64"}


def _int(value: object, *, name: str, minimum: int = 0, maximum: int = MAX_BYTES) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _text(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TEXT
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(f"{name} must be a bounded printable string")
    return value


def _time(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_EVIDENCE_IDS:
        raise ValueError("evidence_ids must be a bounded tuple")
    checked = tuple(_text(value, name="evidence_id") for value in values)
    if len(checked) != len(set(checked)):
        raise ValueError("evidence_ids must be unique")
    return tuple(sorted(checked))


@dataclass(frozen=True, slots=True)
class CompatibilityTarget:
    kind: TargetKind
    key: str
    platform: Platform

    def __post_init__(self) -> None:
        if self.kind not in {"machine", "valve_deck"}:
            raise ValueError("target kind is invalid")
        if not isinstance(self.key, str) or _SLUG.fullmatch(self.key) is None:
            raise ValueError("target key must be a bounded slug")
        if self.platform not in {"windows", "macos", "linux", "steamos"}:
            raise ValueError("target platform is invalid")
        if self.kind == "valve_deck" and self.platform != "steamos":
            raise ValueError("Valve Deck targets must use steamos")


@dataclass(frozen=True, slots=True)
class PrimitiveEvidence:
    """A normalized three-valued fact with complete lineage and uncertainty."""

    state: GateState
    source: str | None
    support_level: SupportLevel
    observed_at: datetime | None
    freshness: Freshness
    evidence_ids: tuple[str, ...] = ()
    conflict: bool = False
    unknown_reason: str | None = None
    condition: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"pass", "fail", "unknown"}:
            raise ValueError("evidence state is invalid")
        if self.support_level not in {"official", "documented", "community", "local", "unknown"}:
            raise ValueError("support level is invalid")
        if self.freshness not in {"fresh", "stale", "expired", "unknown"}:
            raise ValueError("freshness is invalid")
        if self.source is not None:
            _text(self.source, name="source")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _time(self.observed_at, name="observed_at"))
        ids = _ids(self.evidence_ids)
        object.__setattr__(self, "evidence_ids", ids)
        if not isinstance(self.conflict, bool):
            raise ValueError("conflict must be boolean")
        if self.unknown_reason is not None:
            _text(self.unknown_reason, name="unknown_reason")
        if self.condition is not None:
            _text(self.condition, name="condition")
        if self.conflict and self.state != "unknown":
            raise ValueError("conflicting evidence must remain unknown")
        if self.state == "unknown":
            if self.unknown_reason is None:
                raise ValueError("unknown evidence requires an explanation")
            if self.condition is not None:
                raise ValueError("unknown evidence cannot assert a known condition")
        else:
            if self.unknown_reason is not None:
                raise ValueError("known evidence cannot carry an unknown explanation")
            if self.source is None or self.observed_at is None or not ids:
                raise ValueError("known evidence requires source, observation time, and lineage")
            if self.freshness == "unknown":
                raise ValueError("known evidence requires known freshness")
        if self.state == "fail" and self.condition is not None:
            raise ValueError("failed evidence cannot also be a pass condition")


def unknown(reason: str, *, conflict: bool = False) -> PrimitiveEvidence:
    return PrimitiveEvidence("unknown", None, "unknown", None, "unknown", conflict=conflict, unknown_reason=reason)


@dataclass(frozen=True, slots=True)
class RuntimeRisk:
    name: str
    presence: Literal["present", "absent", "unknown"]
    impact: Literal["blocking", "manual", "runtime"]
    evidence: PrimitiveEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SLUG.fullmatch(self.name) is None:
            raise ValueError("runtime risk name must be a bounded slug")
        if self.presence not in {"present", "absent", "unknown"}:
            raise ValueError("runtime risk presence is invalid")
        if self.impact not in {"blocking", "manual", "runtime"}:
            raise ValueError("runtime risk impact is invalid")
        if not isinstance(self.evidence, PrimitiveEvidence):
            raise ValueError("runtime risk evidence is invalid")
        expected = {"present": "pass", "absent": "fail", "unknown": "unknown"}[self.presence]
        if self.evidence.state != expected:
            raise ValueError("runtime risk presence and evidence disagree")


@dataclass(frozen=True, slots=True)
class FeatureEvidence:
    kind: FeatureKind
    name: str
    support: PrimitiveEvidence

    def __post_init__(self) -> None:
        if self.kind not in {"accessibility", "input", "language"}:
            raise ValueError("feature kind is invalid")
        if not isinstance(self.name, str) or _SLUG.fullmatch(self.name) is None:
            raise ValueError("feature name must be a bounded slug")
        if not isinstance(self.support, PrimitiveEvidence):
            raise ValueError("feature support evidence is invalid")


@dataclass(frozen=True, slots=True)
class FeatureRequirement:
    kind: FeatureKind
    name: str

    def __post_init__(self) -> None:
        if self.kind not in {"accessibility", "input", "language"}:
            raise ValueError("feature requirement kind is invalid")
        if not isinstance(self.name, str) or _SLUG.fullmatch(self.name) is None:
            raise ValueError("feature requirement name must be a bounded slug")


@dataclass(frozen=True, slots=True)
class TargetReview:
    target: CompatibilityTarget
    rating: Literal["verified", "playable", "unsupported", "unknown"]
    evidence: PrimitiveEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.target, CompatibilityTarget):
            raise ValueError("review target is invalid")
        if self.rating not in {"verified", "playable", "unsupported", "unknown"}:
            raise ValueError("target review rating is invalid")
        if not isinstance(self.evidence, PrimitiveEvidence):
            raise ValueError("target review evidence is invalid")
        expected = {
            "verified": "pass", "playable": "pass",
            "unsupported": "fail", "unknown": "unknown",
        }[self.rating]
        if self.evidence.state != expected:
            raise ValueError("target review rating and evidence disagree")
        if self.rating == "playable" and self.evidence.condition is None:
            raise ValueError("Playable target reviews require a condition")
        if self.rating != "playable" and self.evidence.condition is not None:
            raise ValueError("only Playable target reviews may carry a condition")


@dataclass(frozen=True, slots=True)
class CompatibilityCandidate:
    appid: int
    native_os: PrimitiveEvidence
    architecture: PrimitiveEvidence
    meets_minimum: PrimitiveEvidence
    exact_target_review: TargetReview | None
    likely_good_experience: PrimitiveEvidence
    installed: PrimitiveEvidence
    owned: PrimitiveEvidence
    runtime_risks: tuple[RuntimeRisk, ...] = ()
    features: tuple[FeatureEvidence, ...] = ()

    def __post_init__(self) -> None:
        _int(self.appid, name="appid", minimum=1, maximum=MAX_APPID)
        for name in (
            "native_os", "architecture", "meets_minimum", "likely_good_experience",
            "installed", "owned",
        ):
            if not isinstance(getattr(self, name), PrimitiveEvidence):
                raise ValueError(f"{name} must be PrimitiveEvidence")
        if self.exact_target_review is not None and not isinstance(self.exact_target_review, TargetReview):
            raise ValueError("exact_target_review must be TargetReview or unknown")
        if not isinstance(self.runtime_risks, tuple) or any(not isinstance(item, RuntimeRisk) for item in self.runtime_risks):
            raise ValueError("runtime_risks must be a tuple of RuntimeRisk")
        if not isinstance(self.features, tuple) or any(not isinstance(item, FeatureEvidence) for item in self.features):
            raise ValueError("features must be a tuple of FeatureEvidence")
        if len(self.runtime_risks) > MAX_COMPONENTS or len(self.features) > MAX_COMPONENTS:
            raise ValueError("candidate risks and features exceed the supported bound")
        risk_names = [item.name for item in self.runtime_risks]
        feature_names = [(item.kind, item.name) for item in self.features]
        if len(risk_names) != len(set(risk_names)) or len(feature_names) != len(set(feature_names)):
            raise ValueError("risk and feature names must be unique")
        object.__setattr__(self, "runtime_risks", tuple(sorted(self.runtime_risks, key=lambda item: item.name)))
        object.__setattr__(self, "features", tuple(sorted(self.features, key=lambda item: (item.kind, item.name))))


@dataclass(frozen=True, slots=True)
class GateOverride:
    name: str
    appid: int
    gate: str
    effective: GateState
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SLUG.fullmatch(self.name) is None:
            raise ValueError("override name must be a bounded slug")
        _int(self.appid, name="override appid", minimum=1, maximum=MAX_APPID)
        if not isinstance(self.gate, str) or _GATE.fullmatch(self.gate) is None:
            raise ValueError("override gate is invalid")
        if self.effective not in {"pass", "fail", "unknown"}:
            raise ValueError("override effective state is invalid")
        ids = _ids(self.evidence_ids)
        if not ids:
            raise ValueError("override requires evidence lineage")
        object.__setattr__(self, "evidence_ids", ids)


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    original: GateState
    effective: GateState
    mandatory: bool
    condition: str | None
    source: str | None
    support_level: SupportLevel
    observed_at: datetime | None
    freshness: Freshness
    conflict: bool
    unknown_reason: str | None
    evidence_ids: tuple[str, ...]
    override_name: str | None
    override_evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompatibilityAssessment:
    appid: int
    target: CompatibilityTarget
    compatibility: CompatibilityState
    playable_now: GateState
    likely_good_experience: PrimitiveEvidence
    gates: tuple[GateResult, ...]
    runtime_risks: tuple[RuntimeRisk, ...]
    accessibility: tuple[FeatureEvidence, ...]
    input: tuple[FeatureEvidence, ...]
    language: tuple[FeatureEvidence, ...]
    reasons: tuple[str, ...]
    unknowns: tuple[str, ...]
    conflicts: tuple[str, ...]
    completeness: Literal["complete", "partial"]


@dataclass(frozen=True, slots=True)
class CompatibilityBatch:
    schema: Literal["compatibility/0.1"]
    target: CompatibilityTarget
    requested_appids: tuple[int, ...]
    results: tuple[CompatibilityAssessment, ...]
    completeness: Literal["complete", "partial"]


def assess_compatibility(
    requested_appids: tuple[int, ...],
    candidates: tuple[CompatibilityCandidate, ...],
    *,
    target: CompatibilityTarget,
    requirements: tuple[FeatureRequirement, ...] = (),
    overrides: tuple[GateOverride, ...] = (),
) -> CompatibilityBatch:
    """Assess every requested AppID without dropping missing or unknown subjects."""

    if not isinstance(target, CompatibilityTarget):
        raise ValueError("target must be CompatibilityTarget")
    if not isinstance(requested_appids, tuple) or not isinstance(candidates, tuple):
        raise ValueError("requested_appids and candidates must be tuples")
    if not isinstance(requirements, tuple) or not isinstance(overrides, tuple):
        raise ValueError("requirements and overrides must be tuples")
    if len(requested_appids) > MAX_ITEMS:
        raise ValueError("requested AppID set exceeds the supported bound")
    if len(requirements) > MAX_COMPONENTS or len(overrides) > MAX_ITEMS:
        raise ValueError("requirements or overrides exceed the supported bound")
    for appid in requested_appids:
        _int(appid, name="requested appid", minimum=1, maximum=MAX_APPID)
    if len(requested_appids) != len(set(requested_appids)):
        raise ValueError("requested AppIDs must be unique")
    if any(not isinstance(item, CompatibilityCandidate) for item in candidates):
        raise ValueError("candidates must contain CompatibilityCandidate values")
    if any(not isinstance(item, FeatureRequirement) for item in requirements):
        raise ValueError("requirements must contain FeatureRequirement values")
    if any(not isinstance(item, GateOverride) for item in overrides):
        raise ValueError("overrides must contain GateOverride values")
    candidate_ids = [item.appid for item in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate AppIDs must be unique")
    requested = set(requested_appids)
    if not set(candidate_ids).issubset(requested):
        raise ValueError("candidate AppIDs must be explicitly requested")
    req_keys = [(item.kind, item.name) for item in requirements]
    if len(req_keys) != len(set(req_keys)):
        raise ValueError("feature requirements must be unique")
    override_keys = [(item.appid, item.gate) for item in overrides]
    if len(override_keys) != len(set(override_keys)):
        raise ValueError("overrides must be unique by AppID and gate")
    if any(item.appid not in requested for item in overrides):
        raise ValueError("override AppID was not requested")

    by_appid = {item.appid: item for item in candidates}
    results = tuple(
        _assess(
            by_appid.get(appid, _missing_candidate(appid)),
            target,
            tuple(sorted(requirements, key=lambda item: (item.kind, item.name))),
            tuple(item for item in overrides if item.appid == appid),
        )
        for appid in sorted(requested)
    )
    completeness: Literal["complete", "partial"] = (
        "complete" if all(item.completeness == "complete" for item in results) else "partial"
    )
    return CompatibilityBatch(SCHEMA, target, tuple(sorted(requested)), results, completeness)


def _missing_candidate(appid: int) -> CompatibilityCandidate:
    missing = unknown("candidate_evidence_unavailable")
    return CompatibilityCandidate(appid, missing, missing, missing, None, missing, missing, missing)


def _assess(
    candidate: CompatibilityCandidate,
    target: CompatibilityTarget,
    requirements: tuple[FeatureRequirement, ...],
    overrides: tuple[GateOverride, ...],
) -> CompatibilityAssessment:
    # Valve's Deck rating is already an exact-target assessment.  Requiring
    # generic store minimums in addition would incorrectly turn Verified into
    # unknown whenever those less-specific facts are absent.  Generic machine
    # targets instead require the three exact comparisons below.
    mandatory_names = (
        {"exact_target_review"}
        if target.kind == "valve_deck"
        else {"native_os", "architecture", "meets_minimum"}
    )

    raw: list[tuple[str, PrimitiveEvidence, bool]] = [
        ("native_os", candidate.native_os, "native_os" in mandatory_names),
        ("architecture", candidate.architecture, "architecture" in mandatory_names),
        ("meets_minimum", candidate.meets_minimum, "meets_minimum" in mandatory_names),
        (
            "exact_target_review",
            _exact_review_for_target(candidate.exact_target_review, target),
            "exact_target_review" in mandatory_names,
        ),
    ]
    for risk in candidate.runtime_risks:
        if risk.presence == "present":
            state: GateState = "fail" if risk.impact == "blocking" else "pass"
            condition = None if risk.impact == "blocking" else f"runtime_risk:{risk.name}"
            evidence = PrimitiveEvidence(
                state, risk.evidence.source, risk.evidence.support_level,
                risk.evidence.observed_at, risk.evidence.freshness,
                risk.evidence.evidence_ids, condition=condition,
            )
        elif risk.presence == "absent":
            evidence = PrimitiveEvidence(
                "pass", risk.evidence.source, risk.evidence.support_level,
                risk.evidence.observed_at, risk.evidence.freshness,
                risk.evidence.evidence_ids,
            )
        else:
            evidence = risk.evidence
        raw.append((f"runtime:{risk.name}", evidence, True))

    feature_map = {(item.kind, item.name): item.support for item in candidate.features}
    for requirement in requirements:
        name = f"{requirement.kind}:{requirement.name}"
        raw.append((name, feature_map.get((requirement.kind, requirement.name), unknown("required_feature_not_observed")), True))

    gate_names = {name for name, _, _ in raw}
    if any(item.gate not in gate_names for item in overrides):
        raise ValueError("override names a gate that was not evaluated")
    override_map = {item.gate: item for item in overrides}
    gates = tuple(_gate(name, evidence, mandatory, override_map.get(name)) for name, evidence, mandatory in raw)

    mandatory = tuple(gate for gate in gates if gate.mandatory)
    decisive_fail = any(gate.effective == "fail" for gate in mandatory)
    conditional = any(gate.effective == "pass" and gate.condition is not None for gate in mandatory)
    required_unknown = any(gate.effective == "unknown" for gate in mandatory)
    if decisive_fail:
        state: CompatibilityState = "incompatible"
    elif conditional:
        state = "conditional"
    elif required_unknown:
        state = "unknown"
    else:
        state = "compatible"

    # M5 does not possess M7 process, queue, entitlement-session, or launch
    # facts.  Even an installed, owned, compatible title is therefore not
    # promoted to "playable now".
    if candidate.installed.state == "fail" or candidate.owned.state == "fail" or state == "incompatible":
        playable: GateState = "fail"
    else:
        playable = "unknown"

    unknowns = tuple(sorted(gate.name for gate in gates if gate.effective == "unknown"))
    conflicts = tuple(sorted(gate.name for gate in gates if gate.conflict))
    reasons = tuple(
        sorted(
            {
                *("decisive_compatibility_gate_failed" for _ in (0,) if state == "incompatible"),
                *("known_manual_or_runtime_condition" for _ in (0,) if state == "conditional"),
                *("required_compatibility_evidence_unknown" for _ in (0,) if state == "unknown"),
                *("all_mandatory_compatibility_gates_passed" for _ in (0,) if state == "compatible"),
                *("playable_now_requires_m7_operational_facts" for _ in (0,) if playable == "unknown"),
                *("known_not_installed" for _ in (0,) if candidate.installed.state == "fail"),
            }
        )
    )
    incomplete_freshness = any(gate.freshness in {"stale", "expired", "unknown"} for gate in mandatory)
    partial = (
        bool(unknowns or conflicts)
        or incomplete_freshness
        or candidate.likely_good_experience.state == "unknown"
        or candidate.likely_good_experience.freshness in {"stale", "expired", "unknown"}
        or playable == "unknown"
    )
    features = candidate.features
    return CompatibilityAssessment(
        candidate.appid, target, state, playable, candidate.likely_good_experience,
        gates, candidate.runtime_risks,
        tuple(item for item in features if item.kind == "accessibility"),
        tuple(item for item in features if item.kind == "input"),
        tuple(item for item in features if item.kind == "language"),
        reasons, unknowns, conflicts, "partial" if partial else "complete",
    )


def _gate(name: str, evidence: PrimitiveEvidence, mandatory: bool, override: GateOverride | None) -> GateResult:
    freshness_degraded = evidence.state != "unknown" and evidence.freshness == "expired"
    effective: GateState = "unknown" if freshness_degraded else evidence.state
    if override is not None:
        effective = override.effective
    return GateResult(
        name, evidence.state, effective,
        mandatory, evidence.condition, evidence.source, evidence.support_level,
        evidence.observed_at, evidence.freshness, evidence.conflict,
        "evidence_expired" if freshness_degraded else evidence.unknown_reason,
        evidence.evidence_ids,
        None if override is None else override.name,
        () if override is None else override.evidence_ids,
    )


def _exact_review_for_target(review: TargetReview | None, target: CompatibilityTarget) -> PrimitiveEvidence:
    if review is None:
        return unknown("exact_target_review_not_observed")
    if review.target != target:
        return unknown("target_review_is_for_a_different_target")
    return review.evidence


@dataclass(frozen=True, slots=True)
class MachineCapacity:
    ram_mib: int | None
    storage_mib: int | None
    architecture: str | None

    def __post_init__(self) -> None:
        for name in ("ram_mib", "storage_mib"):
            value = getattr(self, name)
            if value is not None:
                _int(value, name=name, maximum=MAX_BYTES // (1024 * 1024))
        if self.architecture is not None and self.architecture not in _ARCHITECTURES:
            raise ValueError("machine architecture is unsupported")


@dataclass(frozen=True, slots=True)
class MinimumRequirements:
    ram_mib: int | None
    storage_mib: int | None
    architecture: str | None
    cpu_text: str | None = None
    gpu_text: str | None = None

    def __post_init__(self) -> None:
        for name in ("ram_mib", "storage_mib"):
            value = getattr(self, name)
            if value is not None:
                _int(value, name=name, maximum=MAX_BYTES // (1024 * 1024))
        if self.architecture is not None and self.architecture not in _ARCHITECTURES:
            raise ValueError("requirement architecture is unsupported")
        for name in ("cpu_text", "gpu_text"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name=name)


@dataclass(frozen=True, slots=True)
class MinimumComparison:
    overall: GateState
    ram: GateState
    storage: GateState
    architecture: GateState
    cpu: GateState
    gpu: GateState
    unknowns: tuple[str, ...]


def compare_minimum_requirements(machine: MachineCapacity, requirement: MinimumRequirements) -> MinimumComparison:
    """Compare only exact bounded quantities; opaque CPU/GPU names stay unknown."""

    if not isinstance(machine, MachineCapacity) or not isinstance(requirement, MinimumRequirements):
        raise ValueError("machine and requirement must be normalized DTOs")
    ram = _quantity_gate(machine.ram_mib, requirement.ram_mib)
    storage = _quantity_gate(machine.storage_mib, requirement.storage_mib)
    architecture: GateState = (
        "unknown" if machine.architecture is None or requirement.architecture is None
        else "pass" if machine.architecture == requirement.architecture else "fail"
    )
    cpu: GateState = "unknown" if requirement.cpu_text is not None else "pass"
    gpu: GateState = "unknown" if requirement.gpu_text is not None else "pass"
    states = {"ram": ram, "storage": storage, "architecture": architecture, "cpu": cpu, "gpu": gpu}
    overall: GateState = "fail" if "fail" in states.values() else "unknown" if "unknown" in states.values() else "pass"
    return MinimumComparison(overall, ram, storage, architecture, cpu, gpu, tuple(name for name, state in states.items() if state == "unknown"))


def _quantity_gate(actual: int | None, required: int | None) -> GateState:
    if actual is None or required is None:
        return "unknown"
    return "pass" if actual >= required else "fail"


def valve_deck_review(
    rating: Literal["verified", "playable", "unsupported", "unknown"],
    *,
    target: CompatibilityTarget,
    source: str | None = None,
    observed_at: datetime | None = None,
    freshness: Freshness = "unknown",
    evidence_ids: tuple[str, ...] = (),
    condition: str | None = None,
) -> TargetReview:
    """Normalize Valve's exact-target Deck rating without widening its scope."""

    if target.kind != "valve_deck" or target.platform != "steamos":
        raise ValueError("Valve Deck ratings require an exact Valve Deck target")
    if rating == "unknown":
        evidence = PrimitiveEvidence("unknown", source, "official", observed_at, freshness, evidence_ids, unknown_reason="valve_deck_rating_unknown")
        return TargetReview(target, rating, evidence)
    mapping: dict[str, GateState] = {"verified": "pass", "playable": "pass", "unsupported": "fail"}
    if rating not in mapping:
        raise ValueError("Valve Deck rating is invalid")
    if rating == "playable" and condition is None:
        condition = "valve_deck_playable_requires_user_attention"
    if rating != "playable" and condition is not None:
        raise ValueError("only Playable ratings may carry a condition")
    evidence = PrimitiveEvidence(mapping[rating], source, "official", observed_at, freshness, evidence_ids, condition=condition)
    return TargetReview(target, rating, evidence)

"""Pure, deterministic M7 storage and travel ranking recipes.

These recipes rank already-normalized evidence. They never inspect disks, call
providers, open Steam, or imply that an uninstall/install action is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal


SCHEMA: Final = "storage-ranking/0.1"
MAX_BYTES: Final = (1 << 63) - 1
MAX_CANDIDATES: Final = 10_000

EvidenceState = Literal["present", "absent", "unknown"]
Freshness = Literal["fresh", "stale", "unknown"]
GateState = Literal["pass", "fail", "unknown"]
Eligibility = Literal["eligible", "conditional", "excluded"]


@dataclass(frozen=True, slots=True)
class ReclaimCandidate:
    appid: int
    name: str | None
    installed: EvidenceState
    freshness: Freshness
    size_bytes: int | None
    evidence_ids: tuple[int | str, ...] = ()

    def __post_init__(self) -> None:
        _validate_appid(self.appid)
        _validate_name(self.name)
        if self.installed not in {"present", "absent", "unknown"}:
            raise ValueError("installed state is invalid")
        if self.freshness not in {"fresh", "stale", "unknown"}:
            raise ValueError("freshness is invalid")
        _validate_optional_bytes(self.size_bytes)
        _validate_evidence_ids(self.evidence_ids)


@dataclass(frozen=True, slots=True)
class TravelCandidate:
    appid: int
    name: str | None
    ownership: EvidenceState
    ownership_freshness: Freshness
    installed: EvidenceState
    installed_freshness: Freshness
    compatibility: GateState
    storage_lower_bytes: int | None
    storage_upper_bytes: int | None
    preference_score_bps: int | None = None
    evidence_ids: tuple[int | str, ...] = ()

    def __post_init__(self) -> None:
        _validate_appid(self.appid)
        _validate_name(self.name)
        if self.ownership not in {"present", "absent", "unknown"}:
            raise ValueError("ownership state is invalid")
        if self.ownership_freshness not in {"fresh", "stale", "unknown"}:
            raise ValueError("ownership freshness is invalid")
        if self.installed not in {"present", "absent", "unknown"}:
            raise ValueError("installed state is invalid")
        if self.installed_freshness not in {"fresh", "stale", "unknown"}:
            raise ValueError("installed freshness is invalid")
        if self.compatibility not in {"pass", "fail", "unknown"}:
            raise ValueError("compatibility state is invalid")
        if (self.storage_lower_bytes is None) != (self.storage_upper_bytes is None):
            raise ValueError("storage interval must be entirely known or unknown")
        if self.storage_lower_bytes is not None:
            _validate_bytes(self.storage_lower_bytes)
            assert self.storage_upper_bytes is not None
            _validate_bytes(self.storage_upper_bytes)
            if not 0 < self.storage_lower_bytes <= self.storage_upper_bytes:
                raise ValueError("storage interval is invalid")
        if self.preference_score_bps is not None and (
            isinstance(self.preference_score_bps, bool)
            or not isinstance(self.preference_score_bps, int)
            or not -10_000 <= self.preference_score_bps <= 10_000
        ):
            raise ValueError("preference score must be bounded basis points")
        _validate_evidence_ids(self.evidence_ids)


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    state: GateState
    reason: str


@dataclass(frozen=True, slots=True)
class UnknownActionRisk:
    save_state: Literal["unknown"] = "unknown"
    mod_state: Literal["unknown"] = "unknown"
    cloud_state: Literal["unknown"] = "unknown"
    warning: str = "content_size_does_not_prove_action_safety"


@dataclass(frozen=True, slots=True)
class ReclaimResult:
    appid: int
    name: str | None
    eligibility: Eligibility
    gates: tuple[Gate, ...]
    reclaim_bytes: int | None
    meets_target_alone: bool | None
    target_fraction_bps: int | None
    freshness: Freshness
    action_risk: UnknownActionRisk
    evidence_ids: tuple[int | str, ...]


@dataclass(frozen=True, slots=True)
class TravelResult:
    appid: int
    name: str | None
    eligibility: Eligibility
    gates: tuple[Gate, ...]
    declared_minimum_storage_lower_bytes: int | None
    declared_minimum_storage_upper_bytes: int | None
    actual_install_footprint: Literal["unknown"]
    download_bytes: Literal["unknown"]
    update_bytes: Literal["unknown"]
    download_time: Literal["unknown"]
    bandwidth: Literal["unknown"]
    transfer_queue: Literal["unknown"]
    completion_time: Literal["unknown"]
    preference_score_bps: int | None
    action_risk: UnknownActionRisk
    evidence_ids: tuple[int | str, ...]


@dataclass(frozen=True, slots=True)
class ReclaimRanking:
    schema: Literal["storage-ranking/0.1"]
    recipe: Literal["reclaim-space/0.1"]
    target_bytes: int
    results: tuple[ReclaimResult, ...]


@dataclass(frozen=True, slots=True)
class TravelRanking:
    schema: Literal["storage-ranking/0.1"]
    recipe: Literal["travel-install/0.1"]
    budget_bytes: int
    results: tuple[TravelResult, ...]


def rank_reclaim_space(
    candidates: tuple[ReclaimCandidate, ...], *, target_bytes: int
) -> ReclaimRanking:
    """Rank content-size evidence without recommending an uninstall action."""

    _validate_candidates(candidates)
    _validate_positive_bytes(target_bytes, "target_bytes")
    results: list[ReclaimResult] = []
    for item in candidates:
        installed_gate = _presence_gate(item.installed, "not_installed")
        freshness_gate = Gate(
            "installed_observation_fresh",
            "pass" if item.freshness == "fresh" else "unknown",
            (
                "observation_fresh"
                if item.freshness == "fresh"
                else f"observation_{item.freshness}"
            ),
        )
        size_gate = Gate(
            "reclaim_size_known",
            "pass" if item.size_bytes is not None else "unknown",
            "manifest_size_known" if item.size_bytes is not None else "size_not_observed",
        )
        gates = (installed_gate, freshness_gate, size_gate)
        eligibility = _eligibility(gates)
        reclaim = item.size_bytes
        fraction = None if reclaim is None else (reclaim * 10_000) // target_bytes
        results.append(
            ReclaimResult(
                appid=item.appid,
                name=item.name,
                eligibility=eligibility,
                gates=gates,
                reclaim_bytes=reclaim,
                meets_target_alone=None if reclaim is None else reclaim >= target_bytes,
                target_fraction_bps=fraction,
                freshness=item.freshness,
                action_risk=UnknownActionRisk(),
                evidence_ids=item.evidence_ids,
            )
        )
    order = {"eligible": 0, "conditional": 1, "excluded": 2}
    results.sort(
        key=lambda result: (
            order[result.eligibility],
            result.reclaim_bytes is None,
            -(result.reclaim_bytes or 0),
            result.appid,
        )
    )
    return ReclaimRanking(SCHEMA, "reclaim-space/0.1", target_bytes, tuple(results))


def rank_travel_install(
    candidates: tuple[TravelCandidate, ...], *, budget_bytes: int
) -> TravelRanking:
    """Rank conditional travel candidates without predicting an installation."""

    _validate_candidates(candidates)
    _validate_positive_bytes(budget_bytes, "budget_bytes")
    results: list[TravelResult] = []
    for item in candidates:
        ownership_gate = _fresh_presence_gate(
            item.ownership, item.ownership_freshness, "visible_owned"
        )
        installed_gate = _absence_gate(item.installed, item.installed_freshness)
        compatibility_gate = Gate(
            "compatibility",
            item.compatibility,
            {
                "pass": "compatible_for_selected_target",
                "fail": "known_incompatible_for_selected_target",
                "unknown": "compatibility_not_established",
            }[item.compatibility],
        )
        storage_gate = _storage_fit_gate(item, budget_bytes)
        gates = (ownership_gate, installed_gate, compatibility_gate, storage_gate)
        eligibility: Eligibility = (
            "excluded" if any(gate.state == "fail" for gate in gates) else "conditional"
        )
        results.append(
            TravelResult(
                appid=item.appid,
                name=item.name,
                eligibility=eligibility,
                gates=gates,
                declared_minimum_storage_lower_bytes=item.storage_lower_bytes,
                declared_minimum_storage_upper_bytes=item.storage_upper_bytes,
                actual_install_footprint="unknown",
                download_bytes="unknown",
                update_bytes="unknown",
                download_time="unknown",
                bandwidth="unknown",
                transfer_queue="unknown",
                completion_time="unknown",
                preference_score_bps=item.preference_score_bps,
                action_risk=UnknownActionRisk(
                    warning="declared_minimum_storage_does_not_prove_install_feasibility"
                ),
                evidence_ids=item.evidence_ids,
            )
        )
    eligibility_order = {"conditional": 0, "excluded": 1, "eligible": 0}
    state_order = {"pass": 0, "unknown": 1, "fail": 2}
    results.sort(
        key=lambda result: (
            eligibility_order[result.eligibility],
            state_order[result.gates[0].state],
            state_order[result.gates[1].state],
            state_order[result.gates[2].state],
            state_order[result.gates[3].state],
            -(result.preference_score_bps or 0),
            result.declared_minimum_storage_upper_bytes is None,
            result.declared_minimum_storage_upper_bytes or 0,
            result.appid,
        )
    )
    return TravelRanking(SCHEMA, "travel-install/0.1", budget_bytes, tuple(results))


def _presence_gate(state: EvidenceState, absent_reason: str) -> Gate:
    return Gate(
        "installed_present" if absent_reason == "not_installed" else "visible_owned",
        "pass" if state == "present" else "fail" if state == "absent" else "unknown",
        "presence_observed" if state == "present" else absent_reason if state == "absent" else "presence_unknown",
    )


def _fresh_presence_gate(
    state: EvidenceState, freshness: Freshness, name: str
) -> Gate:
    if state == "absent":
        return Gate(name, "fail", "not_visible_owned")
    if state == "unknown":
        return Gate(name, "unknown", "ownership_unknown")
    if freshness != "fresh":
        return Gate(name, "unknown", f"ownership_{freshness}")
    return Gate(name, "pass", "fresh_visible_owned_presence")


def _absence_gate(state: EvidenceState, freshness: Freshness) -> Gate:
    if freshness != "fresh":
        return Gate("not_installed", "unknown", f"installed_snapshot_{freshness}")
    return Gate(
        "not_installed",
        "fail" if state == "present" else "pass" if state == "absent" else "unknown",
        "already_installed" if state == "present" else "fresh_absence_observed" if state == "absent" else "installation_unknown",
    )


def _storage_fit_gate(item: TravelCandidate, budget_bytes: int) -> Gate:
    lower = item.storage_lower_bytes
    upper = item.storage_upper_bytes
    if lower is None or upper is None:
        return Gate("declared_minimum_storage_fit", "unknown", "minimum_storage_not_parsed")
    if upper <= budget_bytes:
        return Gate("declared_minimum_storage_fit", "pass", "declared_upper_bound_within_budget")
    if lower > budget_bytes:
        return Gate("declared_minimum_storage_fit", "fail", "declared_lower_bound_exceeds_budget")
    return Gate("declared_minimum_storage_fit", "unknown", "budget_inside_unit_ambiguity_interval")


def _eligibility(gates: tuple[Gate, ...]) -> Eligibility:
    if any(gate.state == "fail" for gate in gates):
        return "excluded"
    if any(gate.state == "unknown" for gate in gates):
        return "conditional"
    return "eligible"


def _validate_candidates(candidates: tuple[object, ...]) -> None:
    if not isinstance(candidates, tuple) or len(candidates) > MAX_CANDIDATES:
        raise ValueError("candidates must be a bounded tuple")
    appids = [getattr(item, "appid", None) for item in candidates]
    if len(appids) != len(set(appids)):
        raise ValueError("candidate AppIDs must be unique")


def _validate_appid(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= (1 << 32) - 1:
        raise ValueError("appid must be a positive uint32")


def _validate_name(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str)
        or len(value) > 512
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError("name must be a bounded printable string or None")


def _validate_bytes(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_BYTES:
        raise ValueError("byte count must be a bounded non-negative integer")


def _validate_optional_bytes(value: int | None) -> None:
    if value is not None:
        _validate_bytes(value)


def _validate_positive_bytes(value: int, name: str) -> None:
    _validate_bytes(value)
    if value == 0:
        raise ValueError(f"{name} must be positive")


def _validate_evidence_ids(value: tuple[int | str, ...]) -> None:
    if not isinstance(value, tuple) or len(value) > 32 or len(value) != len(set(value)):
        raise ValueError("evidence IDs must be a bounded unique tuple")
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, str))
        or (isinstance(item, int) and item <= 0)
        or (isinstance(item, str) and (not item or len(item) > 128))
        for item in value
    ):
        raise ValueError("evidence ID is invalid")


__all__ = [
    "ReclaimCandidate",
    "ReclaimRanking",
    "TravelCandidate",
    "TravelRanking",
    "rank_reclaim_space",
    "rank_travel_install",
]

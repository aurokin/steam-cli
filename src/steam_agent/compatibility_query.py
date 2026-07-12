"""Pure reconstruction of cache snapshots into M5 compatibility assessments.

The boundary accepts already-read, normalized snapshots.  It performs no I/O,
does not retain requirement prose or hardware details, and never infers CPU or
GPU performance.  Provider-specific minimum parsing can be supplied through
the small :class:`MinimumEvaluator` protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Literal, Mapping, Protocol
from urllib.parse import quote

from steam_agent.compatibility import (
    CompatibilityBatch,
    CompatibilityCandidate,
    CompatibilityTarget,
    FeatureEvidence,
    FeatureRequirement,
    Freshness,
    GateOverride,
    PrimitiveEvidence,
    RuntimeRisk,
    assess_compatibility,
    unknown,
)
from steam_agent.requirement_parser import (
    DeclaredRequirementsText,
    SystemCapacity,
    compare_declared_minimum,
    parse_declared_minimum,
)


DECLARED_FRESH = timedelta(days=7)
DECLARED_EXPIRES = timedelta(days=30)
SYSTEM_FRESH = timedelta(days=30)
INSTALLED_FRESH = timedelta(minutes=15)
OWNED_FRESH = timedelta(hours=24)
MAX_APPIDS = 100_000
MAX_REFERENCE_URL = 512

Presence = Literal["present", "absent", "unknown"]
AttemptStatus = Literal["complete", "partial", "failed", "running"]

INPUT_FEATURE_SLUGS = frozenset(
    {
        "partial_controller_support",
        "full_controller_support",
        "tracked_controller_support",
        "dualshock_usb_support",
        "dualshock_bluetooth_support",
        "dualsense_usb_support",
        "dualsense_bluetooth_support",
        "steam_input_api_support",
        "gamepad_preferred",
        "keyboard_only_option",
        "mouse_only_option",
        "touch_only_option",
    }
)
ACCESSIBILITY_FEATURE_SLUGS = frozenset(
    {
        "adjustable_text_size",
        "subtitle_options",
        "color_alternatives",
        "camera_comfort",
        "custom_volume_controls",
        "stereo_sound",
        "surround_sound",
        "narrated_game_menus",
        "chat_speech_to_text",
        "chat_text_to_speech",
        "playable_without_timed_input",
        "adjustable_difficulty",
        "save_anytime",
        "playable_at_your_own_pace",
        "playable_without_vision",
        "contrast_controls",
    }
)


@dataclass(frozen=True, slots=True)
class LocalObservation:
    """One privacy-bounded local membership observation for an exact AppID."""

    presence: Presence
    observed_at: datetime | None
    source: Literal["installed_projection", "visible_owned"]
    snapshot_id: str | int | None = None
    latest_attempt_at: datetime | None = None
    latest_attempt_status: AttemptStatus | None = None
    promoted_sync_run_id: str | int | None = None
    latest_attempt_id: str | int | None = None

    def __post_init__(self) -> None:
        if self.presence not in {"present", "absent", "unknown"}:
            raise ValueError("local observation presence is invalid")
        if self.source not in {"installed_projection", "visible_owned"}:
            raise ValueError("local observation source is invalid")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if self.presence != "unknown" and self.observed_at is None:
            raise ValueError("known local observations require observed_at")
        if self.snapshot_id is not None and not isinstance(self.snapshot_id, (str, int)):
            raise ValueError("snapshot_id must be a string, integer, or null")
        if self.latest_attempt_at is not None:
            object.__setattr__(
                self,
                "latest_attempt_at",
                _utc(self.latest_attempt_at, "latest_attempt_at"),
            )
        if (self.latest_attempt_at is None) != (self.latest_attempt_status is None):
            raise ValueError("latest attempt time and status must be supplied together")
        if self.latest_attempt_status not in {None, "complete", "partial", "failed", "running"}:
            raise ValueError("latest attempt status is invalid")
        for name in ("promoted_sync_run_id", "latest_attempt_id"):
            if getattr(self, name) is not None and not isinstance(
                getattr(self, name), (str, int)
            ):
                raise ValueError(f"{name} is invalid")


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    """The exact-target wrapper around ``query_system_profile`` output."""

    target_key: str
    profile: Mapping[str, Any] | None
    observed_at: datetime | None
    snapshot_id: str | int | None = None
    latest_attempt_at: datetime | None = None
    latest_attempt_status: AttemptStatus | None = None
    latest_attempt_id: str | int | None = None
    promoted_sync_run_id: str | int | None = None

    def __post_init__(self) -> None:
        # CompatibilityTarget owns the canonical key grammar.
        CompatibilityTarget("machine", self.target_key, "linux")
        if self.profile is not None and not isinstance(self.profile, Mapping):
            raise ValueError("system profile must be a mapping or null")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if self.profile is not None and self.observed_at is None:
            raise ValueError("a system profile requires observed_at")
        if self.latest_attempt_at is not None:
            object.__setattr__(
                self,
                "latest_attempt_at",
                _utc(self.latest_attempt_at, "latest_attempt_at"),
            )
        if (self.latest_attempt_at is None) != (self.latest_attempt_status is None):
            raise ValueError("latest system attempt time and status must be supplied together")
        if self.latest_attempt_status not in {None, "complete", "partial", "failed", "running"}:
            raise ValueError("latest system attempt status is invalid")
        if self.latest_attempt_id is not None and not isinstance(
            self.latest_attempt_id, (str, int)
        ):
            raise ValueError("latest system attempt ID is invalid")
        if self.promoted_sync_run_id is not None and not isinstance(
            self.promoted_sync_run_id, (str, int)
        ):
            raise ValueError("promoted system run ID is invalid")


@dataclass(frozen=True, slots=True)
class MinimumEvaluation:
    """Safe structured comparisons returned by a separately reviewed parser."""

    architecture: PrimitiveEvidence
    meets_minimum: PrimitiveEvidence
    meets_minimum_without_storage: PrimitiveEvidence | None = None


class MinimumEvaluator(Protocol):
    """Narrow integration point; implementations must not make performance claims."""

    def evaluate(
        self,
        *,
        appid: int,
        platform: str,
        normalized_facts: Mapping[str, Any],
        system_profile: Mapping[str, Any] | None,
        declared_observed_at: datetime,
        declared_projection_identity: str | int | None,
        system_observed_at: datetime | None,
        system_snapshot_id: str | int | None,
        system_promoted_run_id: str | int | None,
        system_latest_attempt_id: str | int | None,
        system_profile_freshness: Freshness,
        storage_available_freshness: Freshness,
        generated_at: datetime,
    ) -> MinimumEvaluation: ...


class ConservativeMinimumEvaluator:
    """Bridge the bounded requirement parser without retaining source prose."""

    def evaluate(
        self,
        *,
        appid: int,
        platform: str,
        normalized_facts: Mapping[str, Any],
        system_profile: Mapping[str, Any] | None,
        declared_observed_at: datetime,
        declared_projection_identity: str | int | None,
        system_observed_at: datetime | None,
        system_snapshot_id: str | int | None,
        system_promoted_run_id: str | int | None,
        system_latest_attempt_id: str | int | None,
        system_profile_freshness: Freshness,
        storage_available_freshness: Freshness,
        generated_at: datetime,
    ) -> MinimumEvaluation:
        minimum_text = _minimum_text(normalized_facts, platform)
        parsed = parse_declared_minimum(
            DeclaredRequirementsText("minimum", minimum_text)
        )
        capacity = _system_capacity(system_profile)
        comparison = compare_declared_minimum(capacity, parsed)
        observed = (
            min(declared_observed_at, system_observed_at)
            if system_observed_at is not None
            else declared_observed_at
        )
        declared_freshness = _declared_freshness(
            declared_observed_at, generated_at
        )
        architecture = _comparison_evidence(
            comparison.architecture.state,
            comparison.architecture.reason,
            appid,
            observed,
            _worst_freshness(declared_freshness, system_profile_freshness),
            "minimum-architecture",
            declared_projection_identity,
            system_snapshot_id,
            system_promoted_run_id,
            system_latest_attempt_id,
        )
        # A current free-space failure is useful only for fifteen minutes when
        # it is decisive.  If a non-storage component independently fails, the
        # slower system-profile evidence remains sufficient for that failure.
        without_storage_state, without_storage_reason = _minimum_overall(
            comparison, ignore_storage=True
        )
        decisive_freshness = (
            system_profile_freshness
            if without_storage_state == "fail"
            else (
                system_profile_freshness
                if comparison.storage.state != "fail"
                else storage_available_freshness
            )
        )
        minimum_freshness = _worst_freshness(
            declared_freshness,
            decisive_freshness,
        )
        minimum_state, minimum_reason = _minimum_overall(
            comparison, ignore_storage=False
        )
        minimum = _comparison_evidence(
            minimum_state,
            minimum_reason,
            appid,
            observed,
            minimum_freshness,
            "minimum-overall",
            declared_projection_identity,
            system_snapshot_id,
            system_promoted_run_id,
            system_latest_attempt_id,
        )
        without_storage = _comparison_evidence(
            without_storage_state,
            without_storage_reason,
            appid,
            observed,
            _worst_freshness(declared_freshness, system_profile_freshness),
            "minimum-overall-without-storage",
            declared_projection_identity,
            system_snapshot_id,
            system_promoted_run_id,
            system_latest_attempt_id,
        )
        return MinimumEvaluation(architecture, minimum, without_storage)


@dataclass(frozen=True, slots=True)
class ManualCompatibilityReference:
    appid: int
    provider: Literal["steam", "steamdb", "protondb", "pcgamingwiki"]
    purpose: Literal[
        "view_store_page", "inspect_price_history", "inspect_linux_reports",
        "inspect_pc_compatibility_notes",
    ]
    url: str
    access_mode: Literal["manual_only"] = "manual_only"
    automation_supported: Literal[False] = False

    def __post_init__(self) -> None:
        if isinstance(self.appid, bool) or not isinstance(self.appid, int) or not 1 <= self.appid <= (1 << 32) - 1:
            raise ValueError("manual reference AppID is invalid")
        expected = _manual_reference_values(self.appid)[self.provider]
        if (self.purpose, self.url) != expected or len(self.url) > MAX_REFERENCE_URL:
            raise ValueError("manual reference is not an exact allowlisted URL")
        if self.access_mode != "manual_only" or self.automation_supported is not False:
            raise ValueError("compatibility references are manual-only")


@dataclass(frozen=True, slots=True)
class SubjectSourceGaps:
    appid: int
    missing: tuple[str, ...]
    stale: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconstructionCompleteness:
    missing_capabilities: tuple[str, ...]
    stale_capabilities: tuple[str, ...]
    subjects: tuple[SubjectSourceGaps, ...]


@dataclass(frozen=True, slots=True)
class CompatibilityQueryResult:
    generated_at: datetime
    assessment: CompatibilityBatch
    references: tuple[tuple[int, tuple[ManualCompatibilityReference, ...]], ...]
    completeness: ReconstructionCompleteness

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", _utc(self.generated_at, "generated_at"))
        expected = self.assessment.requested_appids
        if tuple(appid for appid, _ in self.references) != expected:
            raise ValueError("manual references must cover every requested AppID")


def reconstruct_compatibility(
    requested_appids: tuple[int, ...],
    *,
    target: CompatibilityTarget,
    generated_at: datetime,
    system: SystemSnapshot | None,
    declared_snapshot: Mapping[str, Any] | None,
    installed: Mapping[int, LocalObservation] | None = None,
    owned: Mapping[int, LocalObservation] | None = None,
    requirements: tuple[FeatureRequirement, ...] = (),
    overrides: tuple[GateOverride, ...] = (),
    minimum_evaluator: MinimumEvaluator | None = None,
) -> CompatibilityQueryResult:
    """Join frozen normalized inputs while retaining every requested AppID."""

    now = _utc(generated_at, "generated_at")
    requested = _appids(requested_appids)
    if len(requested) > MAX_APPIDS:
        raise ValueError("requested AppID set exceeds the supported bound")
    installed_map = _local_map(installed, requested, "installed")
    owned_map = _local_map(owned, requested, "owned")
    declared_items, demand = _declared_maps(declared_snapshot, requested)
    evaluator = minimum_evaluator or ConservativeMinimumEvaluator()
    candidates = tuple(
        _candidate(
            appid,
            target=target,
            generated_at=now,
            system=system,
            item=declared_items.get(appid),
            demand=demand.get(appid),
            installed=installed_map.get(appid),
            owned=owned_map.get(appid),
            minimum_evaluator=evaluator,
        )
        for appid in requested
    )
    assessment = assess_compatibility(
        requested,
        candidates,
        target=target,
        requirements=requirements,
        overrides=overrides,
    )
    references = tuple((appid, manual_references(appid)) for appid in requested)
    completeness = _reconstruction_completeness(
        requested,
        now=now,
        system=system,
        declared_items=declared_items,
        demand=demand,
        installed=installed_map,
        owned=owned_map,
    )
    return CompatibilityQueryResult(now, assessment, references, completeness)


def _candidate(
    appid: int,
    *,
    target: CompatibilityTarget,
    generated_at: datetime,
    system: SystemSnapshot | None,
    item: Mapping[str, Any] | None,
    demand: Mapping[str, Any] | None,
    installed: LocalObservation | None,
    owned: LocalObservation | None,
    minimum_evaluator: MinimumEvaluator | None,
) -> CompatibilityCandidate:
    target_conflict = system is not None and target.kind == "machine" and system.target_key != target.key
    platform, platform_conflict = _target_platform(system, target)
    facts, declared_at, declared_freshness, declared_conflict = _declared(
        item, demand, appid, generated_at
    )
    conflict = target_conflict or platform_conflict or declared_conflict
    native = _native(
        facts, platform, declared_at, declared_freshness, appid, conflict
    )
    effective = _effective_execution(
        native, platform, target, system, generated_at, appid
    )
    if conflict:
        architecture = native
        minimum = native
    elif facts is None or declared_at is None:
        architecture = unknown("minimum_architecture_not_normalized")
        minimum = unknown("minimum_requirements_not_safely_comparable")
    else:
        evaluated = minimum_evaluator.evaluate(
            appid=appid,
            platform=platform,
            normalized_facts=facts,
            system_profile=None if system is None else system.profile,
            declared_observed_at=declared_at,
            declared_projection_identity=_declared_projection_identity(item, facts),
            system_observed_at=None if system is None else system.observed_at,
            system_snapshot_id=None if system is None else system.snapshot_id,
            system_promoted_run_id=(
                None if system is None else system.promoted_sync_run_id
            ),
            system_latest_attempt_id=(
                None if system is None else system.latest_attempt_id
            ),
            system_profile_freshness=_system_freshness(
                system, generated_at, SYSTEM_FRESH
            ),
            storage_available_freshness=_system_freshness(
                system, generated_at, INSTALLED_FRESH
            ),
            generated_at=generated_at,
        )
        if not isinstance(evaluated, MinimumEvaluation):
            raise ValueError("minimum evaluator returned an invalid result")
        architecture = evaluated.architecture
        minimum = (
            evaluated.meets_minimum_without_storage
            if installed is not None
            and installed.presence == "present"
            and _local_freshness(
                installed, generated_at, installed_kind=True
            ) == "fresh"
            and evaluated.meets_minimum_without_storage is not None
            else evaluated.meets_minimum
        )
    features = _features(
        facts, declared_at, declared_freshness, appid, conflict
    )
    risks = _risks(facts, declared_at, declared_freshness, appid, conflict)
    return CompatibilityCandidate(
        appid=appid,
        target=target,
        declared_native_build=native,
        effective_execution_support=effective,
        architecture=architecture,
        meets_minimum=minimum,
        exact_target_review=None,
        likely_good_experience=unknown("performance_not_benchmarked"),
        installed=_local_evidence(installed, appid, generated_at, installed_kind=True),
        owned=_local_evidence(owned, appid, generated_at, installed_kind=False),
        runtime_risks=risks,
        features=features,
    )


def _target_platform(system: SystemSnapshot | None, target: CompatibilityTarget) -> tuple[str, bool]:
    platform = "linux" if target.platform == "steamos" else target.platform
    if target.kind != "machine" or system is None or system.profile is None:
        return platform, False
    family = _fact_value(_nested(system.profile, "os", "family"))
    if family is None:
        return platform, False
    return platform, family != platform


def _declared(
    item: Mapping[str, Any] | None,
    demand: Mapping[str, Any] | None,
    appid: int,
    now: datetime,
) -> tuple[Mapping[str, Any] | None, datetime | None, Freshness, bool]:
    if item is None or item.get("facts") is None:
        return None, None, "unknown", False
    facts = item.get("facts")
    if not isinstance(facts, Mapping):
        return None, None, "unknown", True
    conflict = facts.get("appid") != appid
    observed = _parse_time(item.get("observed_at"))
    freshness = (
        "unknown" if observed is None else _declared_freshness(observed, now)
    )
    # A newer exact not-found result and retained last-good declarations are
    # intentionally contradictory, never silently merged into current truth.
    cached_not_found = (
        demand is not None
        and demand.get("evaluated") is False
        and demand.get("error_code") == "NOT_FOUND_CACHE"
    )
    if demand is not None and (
        (demand.get("evaluated") is True and demand.get("state") == "not_found")
        or cached_not_found
    ):
        demand_at = _parse_time(demand.get("observed_at"))
        if demand_at is not None and (observed is None or demand_at >= observed):
            conflict = True
    elif (
        demand is not None
        and demand.get("evaluated") is True
        and demand.get("state") not in {None, "ready", "not_found"}
    ):
        demand_at = _parse_time(demand.get("observed_at")) or _parse_time(
            demand.get("started_at")
        )
        current_run_id = item.get("promoted_sync_run_id")
        demand_run_id = demand.get("sync_run_id")
        same_time_is_newer_or_ambiguous = (
            demand_at == observed
            and (
                not isinstance(current_run_id, int)
                or isinstance(current_run_id, bool)
                or not isinstance(demand_run_id, int)
                or isinstance(demand_run_id, bool)
                or demand_run_id > current_run_id
            )
        )
        if (
            demand_at is not None
            and observed is not None
            and (demand_at > observed or same_time_is_newer_or_ambiguous)
        ):
            if freshness == "fresh":
                freshness = "stale"
    return facts, observed, freshness, conflict


def _native(
    facts: Mapping[str, Any] | None,
    platform: str,
    observed_at: datetime | None,
    freshness: Freshness,
    appid: int,
    conflict: bool,
) -> PrimitiveEvidence:
    if conflict:
        if facts is not None and observed_at is not None:
            return _attributed_unknown(
                "target_or_subject_identity_conflict",
                source="steam_store_appdetails",
                support="provisional",
                observed_at=observed_at,
                freshness=freshness,
                evidence_id=_lineage(
                    "declared-identity-conflict", appid, observed_at, platform
                ),
                conflict=True,
            )
        return unknown("target_or_subject_identity_conflict", conflict=True)
    if facts is None or observed_at is None:
        return unknown("declared_platform_evidence_unavailable")
    platforms = facts.get("platforms")
    if not isinstance(platforms, Mapping) or platforms.get("state") != "declared":
        return unknown("declared_platform_evidence_unavailable")
    value = platforms.get(platform)
    if not isinstance(value, bool):
        return unknown("declared_platform_evidence_unavailable")
    return _known(
        "pass" if value else "fail", "steam_store_appdetails", "provisional",
        observed_at, freshness,
        _lineage("declared-platform", appid, observed_at, platform, value),
    )


def _effective_execution(
    native: PrimitiveEvidence,
    platform: str,
    target: CompatibilityTarget,
    system: SystemSnapshot | None,
    now: datetime,
    appid: int,
) -> PrimitiveEvidence:
    if native.conflict or native.state == "unknown":
        return native
    if platform == "linux":
        if native.state == "fail":
            return unknown("proton_route_not_evaluated")
    elif native.state == "fail":
        return native
    if target.kind == "valve_deck":
        return native
    if system is None or system.profile is None or system.observed_at is None:
        return unknown("target_system_profile_unavailable")
    family = _fact_value(_nested(system.profile, "os", "family"))
    if not isinstance(family, str):
        return unknown("target_operating_system_not_observed")
    if family != platform:
        return unknown("target_or_subject_identity_conflict", conflict=True)
    system_freshness = _system_freshness(system, now, SYSTEM_FRESH)
    combined_freshness: Freshness = (
        "expired"
        if "expired" in {native.freshness, system_freshness}
        else "unknown"
        if "unknown" in {native.freshness, system_freshness}
        else "stale"
        if "stale" in {native.freshness, system_freshness}
        else "fresh"
    )
    observed = min(native.observed_at, system.observed_at)  # type: ignore[arg-type]
    evidence_ids = tuple(
        sorted(
            (
                *native.evidence_ids,
                _lineage(
                    "system-target", appid, system.observed_at,
                    target.key, system.snapshot_id,
                    system.promoted_sync_run_id, system.latest_attempt_id,
                ),
            )
        )
    )
    if combined_freshness == "unknown":
        return PrimitiveEvidence(
            "unknown",
            "layered_declared_and_local_target",
            "provisional",
            observed,
            combined_freshness,
            evidence_ids,
            unknown_reason="observation_time_in_future",
        )
    return PrimitiveEvidence(
        "pass", "layered_declared_and_local_target", "provisional",
        observed, combined_freshness, evidence_ids,
    )


def _features(
    facts: Mapping[str, Any] | None,
    observed_at: datetime | None,
    freshness: Freshness,
    appid: int,
    conflict: bool,
) -> tuple[FeatureEvidence, ...]:
    if facts is None or observed_at is None or conflict:
        return ()
    result: dict[tuple[str, str], FeatureEvidence] = {}
    categories = facts.get("categories")
    if isinstance(categories, Mapping) and categories.get("state") == "declared":
        slugs = categories.get("known_slugs")
        if isinstance(slugs, (list, tuple)):
            for raw in slugs:
                if isinstance(raw, str) and raw in (
                    INPUT_FEATURE_SLUGS | ACCESSIBILITY_FEATURE_SLUGS
                ):
                    kind = (
                        "input"
                        if raw in INPUT_FEATURE_SLUGS
                        else "accessibility"
                    )
                    name = _slug(raw)
                    support = _known("pass", "steam_store_appdetails", "provisional", observed_at, freshness, _lineage("feature", appid, observed_at, kind, name))
                    result[(kind, name)] = FeatureEvidence(kind, name, support)  # type: ignore[arg-type]
    controller = facts.get("controller_support")
    if controller in {"full", "partial"}:
        name = f"{controller}-controller-support"
        result[("input", name)] = FeatureEvidence(
            "input", name,
            _known("pass", "steam_store_appdetails", "provisional", observed_at, freshness, _lineage("controller", appid, observed_at, controller)),
        )
    languages = facts.get("languages")
    if isinstance(languages, Mapping) and languages.get("state") == "declared":
        values = languages.get("items")
        if isinstance(values, (list, tuple)):
            for value in values:
                if not isinstance(value, Mapping) or not isinstance(value.get("code"), str):
                    continue
                code = _slug(value["code"])
                result[("language", code)] = FeatureEvidence(
                    "language", code,
                    _known("pass", "steam_store_appdetails", "provisional", observed_at, freshness, _lineage("language", appid, observed_at, code)),
                )
                if value.get("full_audio") is True:
                    full = f"{code}-full-audio"
                    result[("language", full)] = FeatureEvidence(
                        "language", full,
                        _known("pass", "steam_store_appdetails", "provisional", observed_at, freshness, _lineage("full-audio", appid, observed_at, code)),
                    )
    return tuple(sorted(result.values(), key=lambda value: (value.kind, value.name)))


def _risks(
    facts: Mapping[str, Any] | None,
    observed_at: datetime | None,
    freshness: Freshness,
    appid: int,
    conflict: bool,
) -> tuple[RuntimeRisk, ...]:
    if facts is None or observed_at is None or conflict:
        return ()
    result = []
    for field, name, impact in (
        ("external_account_notice", "external-account", "manual"),
        ("drm_notice", "declared-drm", "runtime"),
    ):
        value = facts.get(field)
        state = value.get("state") if isinstance(value, Mapping) else "unknown"
        if state == "declared":
            evidence = _known("pass", "steam_store_appdetails", "provisional", observed_at, freshness, _lineage("runtime", appid, observed_at, name))
            presence = "present" if evidence.state == "pass" else "unknown"
            result.append(RuntimeRisk(name, presence, impact, evidence))  # type: ignore[arg-type]
        elif state == "undeclared":
            evidence = _known(
                "fail",
                "steam_store_appdetails",
                "provisional",
                observed_at,
                freshness,
                _lineage("runtime-undeclared", appid, observed_at, name),
            )
            presence = "absent" if evidence.state == "fail" else "unknown"
            result.append(RuntimeRisk(name, presence, impact, evidence))  # type: ignore[arg-type]
        # Omission is not asserted as absence.  It simply contributes no risk;
        # instead it is represented as an attributed unknown risk.
        else:
            result.append(
                RuntimeRisk(
                    name,
                    "unknown",
                    impact,  # type: ignore[arg-type]
                    _attributed_unknown(
                        "runtime_notice_state_unknown",
                        source="steam_store_appdetails",
                        support="provisional",
                        observed_at=observed_at,
                        freshness=freshness,
                        evidence_id=_lineage(
                            "runtime-unknown", appid, observed_at, name
                        ),
                    ),
                )
            )
    return tuple(result)


def _local_evidence(
    value: LocalObservation | None,
    appid: int,
    now: datetime,
    *,
    installed_kind: bool,
) -> PrimitiveEvidence:
    if value is None:
        return unknown("local_membership_not_observed")
    if value.observed_at is None:
        return unknown("local_membership_not_observed")
    evidence_id = _lineage(
        value.source, appid, value.observed_at, value.snapshot_id,
        value.promoted_sync_run_id, value.latest_attempt_id,
    )
    if value.presence == "unknown":
        return _attributed_unknown(
            "local_membership_not_observed",
            source=value.source,
            support="local",
            observed_at=value.observed_at,
            freshness=(
                "unknown"
                if _age(value.observed_at, now) is None
                else "stale"
            ),
            evidence_id=evidence_id,
        )
    freshness = _local_freshness(value, now, installed_kind=installed_kind)
    if not installed_kind and value.presence == "absent":
        return _attributed_unknown(
            "visible_owned_absence_is_not_nonownership",
            source=value.source,
            support="local",
            observed_at=value.observed_at,
            freshness=freshness,
            evidence_id=evidence_id,
        )
    return _known(
        "pass" if value.presence == "present" else "fail",
        value.source,
        "local",
        value.observed_at,
        freshness,
        evidence_id,
    )


def _known(
    state: Literal["pass", "fail"],
    source: str,
    support: Literal["provisional", "local"],
    observed_at: datetime,
    freshness: Freshness,
    evidence_id: str,
) -> PrimitiveEvidence:
    if freshness == "unknown":
        return _attributed_unknown(
            "observation_time_in_future",
            source=source,
            support=support,
            observed_at=observed_at,
            freshness=freshness,
            evidence_id=evidence_id,
        )
    return PrimitiveEvidence(
        state, source, support, observed_at, freshness, (evidence_id,)
    )


def _attributed_unknown(
    reason: str,
    *,
    source: str,
    support: Literal["provisional", "local"],
    observed_at: datetime,
    freshness: Freshness,
    evidence_id: str,
    conflict: bool = False,
) -> PrimitiveEvidence:
    return PrimitiveEvidence(
        "unknown",
        source,
        support,
        observed_at,
        freshness,
        (evidence_id,),
        conflict=conflict,
        unknown_reason=reason,
    )


def _minimum_text(facts: Mapping[str, Any], platform: str) -> str | None:
    context = facts.get("context")
    if not isinstance(context, Mapping) or context.get("language") != "english":
        return None
    requirements = facts.get("requirements")
    if not isinstance(requirements, (list, tuple)):
        return None
    matches = [
        item
        for item in requirements
        if isinstance(item, Mapping) and item.get("platform") == platform
    ]
    if len(matches) != 1 or matches[0].get("state") != "declared":
        return None
    minimum = matches[0].get("minimum")
    return minimum if isinstance(minimum, str) else None


def _system_capacity(profile: Mapping[str, Any] | None) -> SystemCapacity:
    if profile is None:
        return SystemCapacity(None, None, None)
    memory = _fact_value(_nested(profile, "memory", "total_bytes"))
    architecture = _fact_value(_nested(profile, "cpu", "architecture"))
    storage_value = _fact_value(profile.get("storage"))
    storage_available: int | None = None
    if isinstance(storage_value, (list, tuple)):
        primary = [
            item
            for item in storage_value
            if isinstance(item, Mapping) and item.get("role") == "steam_primary"
        ]
        if not primary:
            primary = [
                item
                for item in storage_value
                if isinstance(item, Mapping) and item.get("role") == "system"
            ]
        if len(primary) == 1 and isinstance(primary[0].get("available_bytes"), int):
            storage_available = primary[0]["available_bytes"]
    return SystemCapacity(
        memory if isinstance(memory, int) and not isinstance(memory, bool) else None,
        storage_available,
        architecture if architecture in {"x86", "x86_64", "arm64"} else None,  # type: ignore[arg-type]
    )


def _comparison_evidence(
    state: Literal["pass", "fail", "unknown"],
    reason: str,
    appid: int,
    observed_at: datetime,
    freshness: Freshness,
    kind: str,
    declared_projection_identity: str | int | None,
    system_snapshot_id: str | int | None,
    system_promoted_run_id: str | int | None,
    system_latest_attempt_id: str | int | None,
) -> PrimitiveEvidence:
    # Opaque source identities bind this derivation to the exact promoted
    # system evidence.  They are hashed into lineage and never serialized.
    evidence_id = _lineage(
        kind,
        appid,
        observed_at,
        state,
        reason,
        declared_projection_identity,
        system_snapshot_id,
        system_promoted_run_id,
        system_latest_attempt_id,
    )
    if state == "unknown":
        return PrimitiveEvidence(
            "unknown",
            "bounded_minimum_parser",
            "provisional",
            observed_at,
            freshness,
            (evidence_id,),
            unknown_reason=reason,
        )
    return _known(
        state,
        "bounded_minimum_parser",
        "provisional",
        observed_at,
        freshness,
        evidence_id,
    )


def _declared_projection_identity(
    item: Mapping[str, Any] | None, facts: Mapping[str, Any]
) -> str | int:
    """Return an opaque exact-projection identity, defensively deriving one."""

    supplied = None if item is None else item.get("projection_identity")
    if isinstance(supplied, (str, int)) and not isinstance(supplied, bool):
        return supplied
    canonical = json.dumps(
        {
            "facts": facts,
            "promoted_sync_run_id": (
                None if item is None else item.get("promoted_sync_run_id")
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"declared-projection:{hashlib.sha256(canonical).hexdigest()[:24]}"


def _overall_reason(comparison: Any) -> str:
    components = (
        comparison.memory,
        comparison.storage,
        comparison.architecture,
        comparison.cpu,
        comparison.gpu,
    )
    if comparison.overall == "pass":
        return "all_safely_comparable_minimum_components_passed"
    if comparison.overall == "fail":
        return "at_least_one_minimum_capacity_component_failed"
    if any(component.state == "fail" for component in components):
        return "at_least_one_minimum_capacity_component_failed"
    return "minimum_components_not_all_safely_comparable"


def _minimum_overall(
    comparison: Any, *, ignore_storage: bool
) -> tuple[Literal["pass", "fail", "unknown"], str]:
    """Separate install-space readiness from compatibility for installed apps."""

    if not ignore_storage:
        return comparison.overall, _overall_reason(comparison)
    components = (
        comparison.memory,
        comparison.architecture,
        comparison.cpu,
        comparison.gpu,
    )
    state: Literal["pass", "fail", "unknown"] = (
        "fail"
        if any(component.state == "fail" for component in components)
        else "unknown"
        if any(component.state == "unknown" for component in components)
        else "pass"
    )
    reason = (
        "installed_app_storage_is_readiness_not_compatibility"
        if state == "pass"
        else "at_least_one_non_storage_minimum_component_failed"
        if state == "fail"
        else "non_storage_minimum_components_not_all_safely_comparable"
    )
    return state, reason


def _worst_freshness(*values: Freshness) -> Freshness:
    order = {"fresh": 0, "stale": 1, "expired": 2, "unknown": 3}
    return max(values, key=order.__getitem__)


def _declared_freshness(observed_at: datetime, now: datetime) -> Freshness:
    age = _age(observed_at, now)
    if age is None:
        return "unknown"
    if age <= DECLARED_FRESH:
        return "fresh"
    if age <= DECLARED_EXPIRES:
        return "stale"
    return "expired"


def _system_freshness(
    system: SystemSnapshot | None,
    now: datetime,
    policy: timedelta,
) -> Freshness:
    if system is None or system.observed_at is None:
        return "unknown"
    age = _age(system.observed_at, now)
    if age is None:
        return "unknown"
    freshness: Freshness = "fresh" if age <= policy else "expired"
    if freshness == "fresh" and _incomplete_attempt_supersedes(
        observed_at=system.observed_at,
        promoted_run_id=system.promoted_sync_run_id,
        latest_attempt_at=system.latest_attempt_at,
        latest_attempt_id=system.latest_attempt_id,
        latest_attempt_status=system.latest_attempt_status,
    ):
        return "stale"
    return freshness


def _reconstruction_completeness(
    requested: tuple[int, ...],
    *,
    now: datetime,
    system: SystemSnapshot | None,
    declared_items: Mapping[int, Mapping[str, Any]],
    demand: Mapping[int, Mapping[str, Any]],
    installed: Mapping[int, LocalObservation],
    owned: Mapping[int, LocalObservation],
) -> ReconstructionCompleteness:
    missing_capabilities = {"operations.ready.read"}
    stale_capabilities: set[str] = set()
    system_state = _system_freshness(system, now, SYSTEM_FRESH)
    if system is None or system.profile is None:
        missing_capabilities.add("system_profile.read")
    elif system_state != "fresh":
        stale_capabilities.add("system_profile.read")
    if not declared_items:
        missing_capabilities.add("compatibility.declared.read")
    if not installed:
        missing_capabilities.add("library.installed.read")
    if not owned:
        missing_capabilities.add("account.visible_owned.read")

    subjects: list[SubjectSourceGaps] = []
    for appid in requested:
        missing = {"operations.ready.read"}
        stale: set[str] = set()
        conflicts: set[str] = set()
        if system is None or system.profile is None:
            missing.add("system_profile.read")
        elif system_state != "fresh":
            stale.add("system_profile.read")

        item = declared_items.get(appid)
        row_demand = demand.get(appid)
        facts, _, declared_state, conflict = _declared(
            item, row_demand, appid, now
        )
        if facts is None:
            missing.add("compatibility.declared.read")
        elif conflict:
            conflicts.add("compatibility.declared.read")
        elif declared_state != "fresh":
            stale.add("compatibility.declared.read")

        installed_item = installed.get(appid)
        installed_state = _local_freshness(
            installed_item, now, installed_kind=True
        )
        if installed_item is None or installed_item.observed_at is None:
            missing.add("library.installed.read")
        elif installed_state != "fresh":
            stale.add("library.installed.read")

        owned_item = owned.get(appid)
        owned_state = _local_freshness(owned_item, now, installed_kind=False)
        if owned_item is None or owned_item.observed_at is None:
            missing.add("account.visible_owned.read")
        elif owned_state != "fresh":
            stale.add("account.visible_owned.read")
        subjects.append(
            SubjectSourceGaps(
                appid,
                tuple(sorted(missing)),
                tuple(sorted(stale)),
                tuple(sorted(conflicts)),
            )
        )
    if any(
        "compatibility.declared.read" in item.stale
        or "compatibility.declared.read" in item.conflicts
        for item in subjects
    ):
        stale_capabilities.add("compatibility.declared.read")
    if any("library.installed.read" in item.stale for item in subjects):
        stale_capabilities.add("library.installed.read")
    if any("account.visible_owned.read" in item.stale for item in subjects):
        stale_capabilities.add("account.visible_owned.read")
    for capability in (
        "compatibility.declared.read",
        "library.installed.read",
        "account.visible_owned.read",
    ):
        if any(capability in item.missing for item in subjects):
            missing_capabilities.add(capability)
    return ReconstructionCompleteness(
        tuple(sorted(missing_capabilities)),
        tuple(sorted(stale_capabilities)),
        tuple(subjects),
    )


def _local_freshness(
    value: LocalObservation | None,
    now: datetime,
    *,
    installed_kind: bool,
) -> Freshness:
    if value is None or value.observed_at is None:
        return "unknown"
    age = _age(value.observed_at, now)
    if age is None:
        return "unknown"
    freshness: Freshness = (
        "fresh"
        if age <= (INSTALLED_FRESH if installed_kind else OWNED_FRESH)
        else "stale"
    )
    if _incomplete_attempt_supersedes(
        observed_at=value.observed_at,
        promoted_run_id=value.promoted_sync_run_id,
        latest_attempt_at=value.latest_attempt_at,
        latest_attempt_id=value.latest_attempt_id,
        latest_attempt_status=value.latest_attempt_status,
    ):
        return "stale"
    return freshness


def _incomplete_attempt_supersedes(
    *,
    observed_at: datetime,
    promoted_run_id: str | int | None,
    latest_attempt_at: datetime | None,
    latest_attempt_id: str | int | None,
    latest_attempt_status: AttemptStatus | None,
) -> bool:
    """Conservatively order an incomplete attempt against a last-good row."""

    if latest_attempt_at is None or latest_attempt_status in {None, "complete"}:
        return False
    if latest_attempt_at > observed_at:
        return True
    if latest_attempt_at < observed_at:
        return False
    if latest_attempt_id == promoted_run_id and latest_attempt_id is not None:
        return False
    if isinstance(latest_attempt_id, int) and not isinstance(latest_attempt_id, bool):
        if isinstance(promoted_run_id, int) and not isinstance(promoted_run_id, bool):
            return latest_attempt_id > promoted_run_id
    # Equal timestamps with missing or non-orderable lineage are ambiguous.
    return True


def _declared_maps(
    snapshot: Mapping[str, Any] | None,
    requested: tuple[int, ...],
) -> tuple[dict[int, Mapping[str, Any]], dict[int, Mapping[str, Any]]]:
    if snapshot is None:
        return {}, {}
    if not isinstance(snapshot, Mapping):
        raise ValueError("declared snapshot must be a mapping or null")
    return (
        _indexed_rows(snapshot.get("items", ()), requested, "declared items"),
        _indexed_rows(snapshot.get("latest_demand", ()), requested, "declared demand"),
    )


def _indexed_rows(value: object, requested: tuple[int, ...], name: str) -> dict[int, Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_APPIDS:
        raise ValueError(f"{name} must be a bounded sequence")
    requested_set = set(requested)
    result: dict[int, Mapping[str, Any]] = {}
    for row in value:
        if not isinstance(row, Mapping):
            raise ValueError(f"{name} contains an invalid row")
        appid = row.get("appid")
        if appid not in requested_set or appid in result:
            raise ValueError(f"{name} has an unexpected or duplicate AppID")
        result[appid] = row  # type: ignore[index]
    return result


def _local_map(
    values: Mapping[int, LocalObservation] | None,
    requested: tuple[int, ...],
    name: str,
) -> Mapping[int, LocalObservation]:
    if values is None:
        return {}
    if not isinstance(values, Mapping) or len(values) > MAX_APPIDS:
        raise ValueError(f"{name} observations must be a bounded mapping")
    requested_set = set(requested)
    for appid, value in values.items():
        if appid not in requested_set or not isinstance(value, LocalObservation):
            raise ValueError(f"{name} observation subject is invalid")
    return values


def _appids(values: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(values, tuple):
        raise ValueError("requested AppIDs must be a tuple")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= (1 << 32) - 1:
            raise ValueError("requested AppID is invalid")
    if len(values) != len(set(values)):
        raise ValueError("requested AppIDs must be unique")
    return tuple(sorted(values))


def _fact_value(value: object) -> object | None:
    return value.get("value") if isinstance(value, Mapping) and value.get("state") == "known" else None


def _nested(value: Mapping[str, Any], *keys: str) -> object:
    current: object = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _lineage(kind: str, appid: int, observed_at: datetime, *parts: object) -> str:
    material = "\x1f".join(
        (kind, str(appid), observed_at.isoformat(), *(str(value) for value in parts))
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"lineage:{_slug(kind)}:{digest}"


def _slug(value: str) -> str:
    normalized = "".join(character if character.isalnum() else "-" for character in value.casefold())
    normalized = "-".join(part for part in normalized.split("-") if part)
    return (normalized or "unknown")[:63].rstrip("-")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), "timestamp")
    except ValueError:
        return None


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _age(observed_at: datetime, now: datetime) -> timedelta | None:
    age = now - observed_at
    return None if age < timedelta(0) else age


def manual_references(appid: int) -> tuple[ManualCompatibilityReference, ...]:
    values = _manual_reference_values(appid)
    return tuple(
        ManualCompatibilityReference(appid, provider, purpose, url)
        for provider, (purpose, url) in values.items()
    )


def _manual_reference_values(appid: int) -> dict[str, tuple[str, str]]:
    if isinstance(appid, bool) or not isinstance(appid, int) or not 1 <= appid <= (1 << 32) - 1:
        raise ValueError("manual reference AppID is invalid")
    encoded = quote(str(appid), safe="")
    return {
        "steam": ("view_store_page", f"https://store.steampowered.com/app/{encoded}/"),
        "steamdb": ("inspect_price_history", f"https://steamdb.info/app/{encoded}/"),
        "protondb": ("inspect_linux_reports", f"https://www.protondb.com/app/{encoded}"),
        "pcgamingwiki": (
            "inspect_pc_compatibility_notes",
            f"https://www.pcgamingwiki.com/w/index.php?search={encoded}",
        ),
    }


__all__ = [
    "CompatibilityQueryResult",
    "ConservativeMinimumEvaluator",
    "LocalObservation",
    "ManualCompatibilityReference",
    "MinimumEvaluation",
    "MinimumEvaluator",
    "ReconstructionCompleteness",
    "SubjectSourceGaps",
    "SystemSnapshot",
    "manual_references",
    "reconstruct_compatibility",
]

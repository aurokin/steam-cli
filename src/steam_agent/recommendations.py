"""Pure, deterministic M4 play-recommendation recipes.

This module deliberately performs no I/O.  It consumes an already-normalized,
atomic cache snapshot and returns inspectable gates and score components.  The
recipe identifiers and integer weights below are public behavior: changing
them requires a new recipe version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Literal


MAX_APPID = (1 << 32) - 1
MAX_INT64 = (1 << 63) - 1
MAX_MINUTES = MAX_APPID
MAX_RULE_WEIGHT = 100
MAX_CANDIDATES = 100_000
MAX_EVIDENCE_IDS = 64
MAX_TEXT = 256

RESUME_RECIPE = "resume/0.1"
FINISHABILITY_RECIPE = "finishability/0.1"
PREFERENCE_FIT_RECIPE = "preference-fit/0.1"
RecipeId = Literal[
    "resume/0.1", "finishability/0.1", "preference-fit/0.1"
]
GateState = Literal["pass", "fail", "unknown"]
UnknownPolicy = Literal["include", "exclude"]
Freshness = Literal["fresh", "stale", "expired", "unknown"]
Eligibility = Literal["eligible", "conditional", "excluded"]
ComponentState = Literal["applied", "not_applicable", "unknown", "stale", "expired"]

TRAIT_RE = re.compile(r"user:[a-z0-9](?:[a-z0-9._-]{0,57}[a-z0-9])?\Z")
REQUIREMENT_RE = re.compile(
    r"(?:installed|user:[a-z0-9](?:[a-z0-9._-]{0,57}[a-z0-9])?)\Z"
)

# Bounded score constants for the immutable 0.1 recipes.
RESUME_RECENT_WINDOW = 30
RESUME_LAST_PLAYED_7D = 25
RESUME_LAST_PLAYED_30D = 15
RESUME_LAST_PLAYED_90D = 5
RESUME_SUSTAINED_PLAY = 10
RESUME_ACHIEVEMENT_PROGRESS = 10
PREFERENCE_LIKED = 60
PREFERENCE_DISLIKED = -60
PREFERENCE_FINISHED = -20
PREFERENCE_ABANDONED = -50
PREFERENCE_RECENT_BEHAVIOR = 10
PREFERENCE_SUSTAINED_BEHAVIOR = 5
FINISH_KNOWN_ESTIMATE = 60
FINISH_ACHIEVEMENT_PROGRESS = 10

# Machine-inspectable documentation for the accepted 0.1 arithmetic.  Each
# tuple is (recipe or "all", emitted rule_id, inclusive minimum, inclusive
# maximum).  A trailing ``:*`` identifies a bounded dynamic rule-ID family.
RECIPE_SCORING_TABLE: tuple[tuple[str, str, int, int], ...] = (
    ("all", "explicit_like", PREFERENCE_LIKED, PREFERENCE_LIKED),
    ("all", "explicit_dislike", PREFERENCE_DISLIKED, PREFERENCE_DISLIKED),
    ("all", "explicit_rating", 0, 0),
    ("all", "explicit_finished", PREFERENCE_FINISHED, PREFERENCE_FINISHED),
    ("all", "user_abandoned", PREFERENCE_ABANDONED, PREFERENCE_ABANDONED),
    ("all", "explicit_play_state", 0, 0),
    ("all", "profile_rule:*", -MAX_RULE_WEIGHT, MAX_RULE_WEIGHT),
    (RESUME_RECIPE, "recent_sustained_play", 0, RESUME_RECENT_WINDOW),
    (RESUME_RECIPE, "last_played_recency", 0, RESUME_LAST_PLAYED_7D),
    (RESUME_RECIPE, "lifetime_play_momentum", 0, RESUME_SUSTAINED_PLAY),
    (RESUME_RECIPE, "achievement_progress_weak", 0, RESUME_ACHIEVEMENT_PROGRESS),
    (FINISHABILITY_RECIPE, "explicit_remaining_estimate", 0, FINISH_KNOWN_ESTIMATE),
    (FINISHABILITY_RECIPE, "achievement_progress_weak", 0, FINISH_ACHIEVEMENT_PROGRESS),
    (PREFERENCE_FIT_RECIPE, "recent_play_behavior", 0, PREFERENCE_RECENT_BEHAVIOR),
    (PREFERENCE_FIT_RECIPE, "lifetime_play_behavior", 0, PREFERENCE_SUSTAINED_BEHAVIOR),
)


def _int(value: object, *, name: str, minimum: int = 0, maximum: int = MAX_INT64) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _timestamp(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _ids(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_EVIDENCE_IDS:
        raise ValueError("evidence_ids must be a bounded tuple")
    if any(not isinstance(item, str) or not item or len(item) > MAX_TEXT for item in value):
        raise ValueError("evidence IDs must be bounded non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError("evidence IDs must be unique")
    return tuple(sorted(value))


@dataclass(frozen=True, slots=True)
class ActivityEvidence:
    freshness: Freshness
    observed_at: datetime | None
    lifetime_minutes: int | None = None
    recent_window_minutes: int | None = None
    last_played_at: datetime | None = None
    evidence_ids: tuple[str, ...] = ()
    recent_freshness: Freshness | None = None

    def __post_init__(self) -> None:
        if self.freshness not in {"fresh", "stale", "expired", "unknown"}:
            raise ValueError("activity freshness is invalid")
        if self.recent_freshness is None:
            object.__setattr__(self, "recent_freshness", self.freshness)
        elif self.recent_freshness not in {"fresh", "stale", "expired", "unknown"}:
            raise ValueError("recent activity freshness is invalid")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _timestamp(self.observed_at, name="observed_at"))
        for name in ("lifetime_minutes", "recent_window_minutes"):
            value = getattr(self, name)
            if value is not None:
                _int(value, name=name, maximum=MAX_MINUTES)
        if self.last_played_at is not None:
            object.__setattr__(self, "last_played_at", _timestamp(self.last_played_at, name="last_played_at"))
        if self.freshness != "unknown" and self.observed_at is None:
            raise ValueError("known activity freshness requires observed_at")
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


AchievementState = Literal[
    "ready", "profile_private", "unsupported", "authentication_failed",
    "rate_limited", "provider_failed", "invalid_response", "unevaluated",
]


@dataclass(frozen=True, slots=True)
class AchievementEvidence:
    state: AchievementState
    freshness: Freshness
    observed_at: datetime | None
    unlocked: int | None = None
    total: int | None = None
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in {
            "ready", "profile_private", "unsupported", "authentication_failed",
            "rate_limited", "provider_failed", "invalid_response", "unevaluated",
        }:
            raise ValueError("achievement state is invalid")
        if self.freshness not in {"fresh", "stale", "expired", "unknown"}:
            raise ValueError("achievement freshness is invalid")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _timestamp(self.observed_at, name="observed_at"))
        if self.freshness != "unknown" and self.observed_at is None:
            raise ValueError("known achievement freshness requires observed_at")
        if self.state == "ready":
            if self.unlocked is None or self.total is None:
                raise ValueError("ready achievements require unlocked and total")
            _int(self.unlocked, name="unlocked", maximum=MAX_APPID)
            _int(self.total, name="total", minimum=0, maximum=MAX_APPID)
            if self.unlocked > self.total:
                raise ValueError("unlocked cannot exceed total")
            if self.observed_at is None:
                raise ValueError("ready achievements require observed_at")
        elif self.unlocked is not None or self.total is not None:
            raise ValueError("non-ready achievements cannot carry progress")
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))

    @property
    def ratio_bps(self) -> int | None:
        if self.state != "ready" or self.unlocked is None or self.total is None:
            return None
        return None if self.total == 0 else self.unlocked * 10_000 // self.total


@dataclass(frozen=True, slots=True)
class TraitAssertion:
    trait: str
    value: Literal["present", "absent", "unknown"]
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trait, str) or TRAIT_RE.fullmatch(self.trait) is None:
            raise ValueError("trait must be a bounded user: slug")
        if self.value not in {"present", "absent", "unknown"}:
            raise ValueError("trait value is invalid")
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class ExplicitFeedback:
    rating: Literal["liked", "disliked", "neutral"] | None = None
    play_state: Literal["active", "finished", "user_abandoned"] | None = None
    snoozed_until: datetime | None = None
    minimum_session_minutes: int | None = None
    remaining_minutes: int | None = None
    traits: tuple[TraitAssertion, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    rating_evidence_ids: tuple[str, ...] = ()
    play_state_evidence_ids: tuple[str, ...] = ()
    snooze_evidence_ids: tuple[str, ...] = ()
    minimum_session_evidence_ids: tuple[str, ...] = ()
    remaining_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.rating not in {None, "liked", "disliked", "neutral"}:
            raise ValueError("rating is invalid")
        if self.play_state not in {None, "active", "finished", "user_abandoned"}:
            raise ValueError("play_state is invalid")
        if self.snoozed_until is not None:
            object.__setattr__(self, "snoozed_until", _timestamp(self.snoozed_until, name="snoozed_until"))
        for name in ("minimum_session_minutes", "remaining_minutes"):
            value = getattr(self, name)
            if value is not None:
                _int(value, name=name, maximum=MAX_MINUTES)
        if not isinstance(self.traits, tuple):
            raise ValueError("traits must be a tuple")
        if any(not isinstance(item, TraitAssertion) for item in self.traits):
            raise ValueError("traits must contain TraitAssertion values")
        names = [item.trait for item in self.traits]
        if len(names) != len(set(names)):
            raise ValueError("traits must be unique")
        object.__setattr__(self, "traits", tuple(sorted(self.traits, key=lambda item: item.trait)))
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))
        for name in (
            "rating_evidence_ids", "play_state_evidence_ids", "snooze_evidence_ids",
            "minimum_session_evidence_ids", "remaining_evidence_ids",
        ):
            object.__setattr__(self, name, _ids(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class ProfileRule:
    trait: str
    kind: Literal["prefer", "avoid", "require"]
    strength: Literal["soft", "hard"]
    weight: int
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trait, str) or TRAIT_RE.fullmatch(self.trait) is None:
            raise ValueError("rule trait must be a bounded user: slug")
        if self.kind not in {"prefer", "avoid", "require"}:
            raise ValueError("rule kind is invalid")
        if self.strength not in {"soft", "hard"}:
            raise ValueError("rule strength is invalid")
        _int(self.weight, name="weight", maximum=MAX_RULE_WEIGHT)
        if self.strength == "hard" and self.kind == "prefer":
            raise ValueError("a hard prefer rule is ambiguous")
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class RecommendationCandidate:
    appid: int
    name: str | None
    owned: bool | None
    installed: bool | None
    activity: ActivityEvidence | None = None
    achievements: AchievementEvidence | None = None
    feedback: ExplicitFeedback = ExplicitFeedback()
    identity_evidence_ids: tuple[str, ...] = ()
    owned_evidence_ids: tuple[str, ...] = ()
    installed_evidence_ids: tuple[str, ...] = ()
    is_game: bool | None = None
    classification_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _int(self.appid, name="appid", minimum=1, maximum=MAX_APPID)
        if self.name is not None:
            if not isinstance(self.name, str) or not self.name or len(self.name) > MAX_TEXT or any(not c.isprintable() for c in self.name):
                raise ValueError("name must be a bounded printable string")
        if self.owned is not None and not isinstance(self.owned, bool):
            raise ValueError("owned must be true, false, or unknown")
        if self.installed is not None and not isinstance(self.installed, bool):
            raise ValueError("installed must be true, false, or unknown")
        if self.is_game is not None and not isinstance(self.is_game, bool):
            raise ValueError("is_game must be true, false, or unknown")
        if self.activity is not None and not isinstance(self.activity, ActivityEvidence):
            raise ValueError("activity must be ActivityEvidence or unknown")
        if self.achievements is not None and not isinstance(self.achievements, AchievementEvidence):
            raise ValueError("achievements must be AchievementEvidence or unknown")
        if not isinstance(self.feedback, ExplicitFeedback):
            raise ValueError("feedback must be ExplicitFeedback")
        object.__setattr__(self, "identity_evidence_ids", _ids(self.identity_evidence_ids))
        object.__setattr__(self, "owned_evidence_ids", _ids(self.owned_evidence_ids))
        object.__setattr__(self, "installed_evidence_ids", _ids(self.installed_evidence_ids))
        object.__setattr__(self, "classification_evidence_ids", _ids(self.classification_evidence_ids))


@dataclass(frozen=True, slots=True)
class Requirement:
    name: str
    expected: bool

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or REQUIREMENT_RE.fullmatch(self.name) is None:
            raise ValueError("requirement name is invalid")
        if not isinstance(self.expected, bool):
            raise ValueError("requirement expected value must be boolean")


@dataclass(frozen=True, slots=True)
class ConstraintOverride:
    appid: int
    constraint: str
    effective: GateState

    def __post_init__(self) -> None:
        _int(self.appid, name="override appid", minimum=1, maximum=MAX_APPID)
        if not isinstance(self.constraint, str) or not self.constraint or len(self.constraint) > 128:
            raise ValueError("override constraint is invalid")
        if self.effective not in {"pass", "fail", "unknown"}:
            raise ValueError("override effective state is invalid")


@dataclass(frozen=True, slots=True)
class RecommendationContext:
    recipe: RecipeId
    now: datetime
    time_minutes: int | None = None
    requirements: tuple[Requirement, ...] = ()
    unknown_policy: UnknownPolicy = "exclude"
    overrides: tuple[ConstraintOverride, ...] = ()
    explain: bool = False

    def __post_init__(self) -> None:
        if self.recipe not in {RESUME_RECIPE, FINISHABILITY_RECIPE, PREFERENCE_FIT_RECIPE}:
            raise ValueError("recipe identifier is unsupported")
        object.__setattr__(self, "now", _timestamp(self.now, name="now"))
        if self.time_minutes is not None:
            _int(self.time_minutes, name="time_minutes", maximum=MAX_MINUTES)
        if self.unknown_policy not in {"include", "exclude"}:
            raise ValueError("unknown policy is invalid")
        if not isinstance(self.explain, bool):
            raise ValueError("explain must be boolean")
        if not isinstance(self.requirements, tuple) or not isinstance(self.overrides, tuple):
            raise ValueError("requirements and overrides must be tuples")
        if any(not isinstance(item, Requirement) for item in self.requirements):
            raise ValueError("requirements must contain Requirement values")
        if any(not isinstance(item, ConstraintOverride) for item in self.overrides):
            raise ValueError("overrides must contain ConstraintOverride values")
        names = [item.name for item in self.requirements]
        if len(names) != len(set(names)):
            raise ValueError("requirements must be unique")
        keys = [(item.appid, item.constraint) for item in self.overrides]
        if len(keys) != len(set(keys)):
            raise ValueError("overrides must be unique")
        object.__setattr__(self, "requirements", tuple(sorted(self.requirements, key=lambda item: item.name)))
        object.__setattr__(self, "overrides", tuple(sorted(self.overrides, key=lambda item: (item.appid, item.constraint))))


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    original: GateState
    effective: GateState
    evidence_ids: tuple[str, ...]
    overridden: bool


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    rule_id: str
    input: str | int | bool | None
    evidence_ids: tuple[str, ...]
    points: int | None
    state: ComponentState
    evidence_kind: Literal["explicit_user", "behavioral", "achievement"]
    limitation: str | None = None


@dataclass(frozen=True, slots=True)
class RankedRecommendation:
    appid: int
    name: str | None
    identity_evidence_ids: tuple[str, ...]
    eligibility: Eligibility
    score: int | None
    gates: tuple[GateResult, ...]
    components: tuple[ScoreComponent, ...]
    positive_factors: tuple[str, ...]
    negative_factors: tuple[str, ...]
    reasons: tuple[str, ...]
    tradeoffs: tuple[str, ...]
    unknowns: tuple[str, ...]
    freshness: Freshness
    confidence: Literal["high", "medium", "low"]
    completeness: Literal["complete", "partial"]


@dataclass(frozen=True, slots=True)
class RecommendationRanking:
    schema: Literal["recommendations/0.1"]
    recipe_version: RecipeId
    context: RecommendationContext
    unknown_policy: UnknownPolicy
    results: tuple[RankedRecommendation, ...]
    eligible: tuple[RankedRecommendation, ...]
    conditional: tuple[RankedRecommendation, ...]
    excluded: tuple[RankedRecommendation, ...]
    counts: tuple[tuple[str, int], ...]
    completeness: Literal["complete", "partial"]


def rank_recommendations(
    candidates: tuple[RecommendationCandidate, ...],
    *,
    profile_rules: tuple[ProfileRule, ...] = (),
    context: RecommendationContext,
) -> RecommendationRanking:
    """Apply gates, score eligible candidates, and return a stable ranking."""

    if not isinstance(context, RecommendationContext):
        raise ValueError("context must be RecommendationContext")
    if not isinstance(candidates, tuple) or not isinstance(profile_rules, tuple):
        raise ValueError("candidates and profile_rules must be tuples")
    if any(not isinstance(item, RecommendationCandidate) for item in candidates):
        raise ValueError("candidates must contain RecommendationCandidate values")
    if any(not isinstance(item, ProfileRule) for item in profile_rules):
        raise ValueError("profile_rules must contain ProfileRule values")
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError("candidate set exceeds the supported bound")
    appids = [item.appid for item in candidates]
    if len(appids) != len(set(appids)):
        raise ValueError("candidate AppIDs must be unique")
    rule_names = [item.trait for item in profile_rules]
    if len(rule_names) != len(set(rule_names)):
        raise ValueError("profile rules must be unique by trait")
    candidate_ids = set(appids)
    if any(item.appid not in candidate_ids for item in context.overrides):
        raise ValueError("override AppID is not a candidate")
    # Validate composite lineage before producing any ranked result.  Each
    # source is bounded independently, but a rule plus assertion must also fit
    # the public per-factor evidence bound.
    for candidate in candidates:
        assertions = {item.trait: item for item in candidate.feedback.traits}
        for rule in profile_rules:
            assertion = assertions.get(rule.trait)
            if assertion is not None:
                _merge_ids(rule.evidence_ids, assertion.evidence_ids)

    ranked = [_rank(item, tuple(sorted(profile_rules, key=lambda rule: rule.trait)), context) for item in candidates]
    ranked.sort(key=_sort_key)
    eligible = tuple(item for item in ranked if item.eligibility == "eligible")
    conditional = tuple(item for item in ranked if item.eligibility == "conditional")
    excluded = tuple(sorted((item for item in ranked if item.eligibility == "excluded"), key=lambda item: item.appid))
    visible = eligible + conditional
    all_results = visible + excluded
    complete = "complete" if all(item.completeness == "complete" for item in all_results) else "partial"
    return RecommendationRanking(
        "recommendations/0.1", context.recipe, context, context.unknown_policy,
        all_results, eligible, conditional, excluded,
        (("eligible", len(eligible)), ("conditional", len(conditional)), ("excluded", len(excluded)), ("total", len(all_results))),
        complete,
    )


def _rank(candidate: RecommendationCandidate, rules: tuple[ProfileRule, ...], context: RecommendationContext) -> RankedRecommendation:
    gates = _gates(candidate, rules, context)
    failed = any(gate.effective == "fail" for gate in gates)
    unknown = any(gate.effective == "unknown" for gate in gates)
    if failed or (unknown and context.unknown_policy == "exclude"):
        eligibility: Eligibility = "excluded"
    elif unknown:
        eligibility = "conditional"
    else:
        eligibility = "eligible"
    components = _components(candidate, rules, context)
    score = None if eligibility == "excluded" else _safe_sum(tuple(item.points for item in components if item.points is not None))
    positive = tuple(item.rule_id for item in components if item.points is not None and item.points > 0)
    negative = tuple(item.rule_id for item in components if item.points is not None and item.points < 0)
    unknowns = tuple(sorted({
        *(gate.name for gate in gates if gate.effective == "unknown"),
        *(item.rule_id for item in components if item.state in {"unknown", "stale", "expired"}),
        *(
            f"achievements_{candidate.achievements.state}"
            for _ in (0,)
            if candidate.achievements is not None
            and candidate.achievements.state != "ready"
        ),
    }))
    reasons = tuple(
        ["all_hard_gates_passed"] if eligibility == "eligible" else
        ["included_with_unknown_hard_gate"] if eligibility == "conditional" else
        ["hard_gate_failed_or_unknown_excluded"]
    )
    tradeoffs = tuple(sorted({
        *("temporary_constraint_override" for gate in gates if gate.overridden),
        *("achievement_ratio_is_weak_evidence_not_completion" for item in components if item.evidence_kind == "achievement" and item.state == "applied"),
        *("stale_evidence_not_scored" for item in components if item.state == "stale"),
        *("expired_evidence_not_scored" for item in components if item.state == "expired"),
        *("behavior_does_not_prove_preference" for item in components if item.evidence_kind == "behavioral" and item.state == "applied"),
    }))
    known = sum(item.state in {"applied", "not_applicable"} for item in components)
    confidence: Literal["high", "medium", "low"] = "high" if not unknowns else ("medium" if known >= len(components) // 2 else "low")
    decisive_exclusion = eligibility == "excluded" and any(
        gate.effective == "fail" for gate in gates
    )
    completeness: Literal["complete", "partial"] = (
        "complete" if not unknowns or decisive_exclusion else "partial"
    )
    observed_freshness = [
        value
        for value in (
            None if candidate.activity is None else candidate.activity.freshness,
            None if candidate.achievements is None else candidate.achievements.freshness,
        )
        if value is not None
    ]
    freshness: Freshness = (
        "expired" if "expired" in observed_freshness else
        "stale" if "stale" in observed_freshness else
        "fresh" if observed_freshness and all(value == "fresh" for value in observed_freshness) else
        "unknown"
    )
    return RankedRecommendation(
        candidate.appid, candidate.name, candidate.identity_evidence_ids,
        eligibility, score, gates, components,
        positive, negative, reasons, tradeoffs, unknowns, freshness, confidence, completeness,
    )


def _gate(name: str, original: GateState, evidence_ids: tuple[str, ...], candidate: RecommendationCandidate, context: RecommendationContext) -> GateResult:
    override = next((item for item in context.overrides if item.appid == candidate.appid and item.constraint == name), None)
    return GateResult(name, original, original if override is None else override.effective, _ids(evidence_ids), override is not None)


def _gates(candidate: RecommendationCandidate, rules: tuple[ProfileRule, ...], context: RecommendationContext) -> tuple[GateResult, ...]:
    feedback_ids = candidate.feedback.evidence_ids
    snooze_ids = candidate.feedback.snooze_evidence_ids or feedback_ids
    raw: list[tuple[str, GateState, tuple[str, ...]]] = [
        ("owned", _bool_gate(candidate.owned, True), candidate.owned_evidence_ids),
        ("game", _bool_gate(candidate.is_game, True), candidate.classification_evidence_ids),
        ("snoozed", "fail" if candidate.feedback.snoozed_until is not None and candidate.feedback.snoozed_until > context.now else "pass", snooze_ids),
    ]
    if context.time_minutes is not None:
        minimum_session = candidate.feedback.minimum_session_minutes
        raw.append((
            "minimum_session_within_time",
            "unknown" if minimum_session is None else (
                "pass" if minimum_session <= context.time_minutes else "fail"
            ),
            candidate.feedback.minimum_session_evidence_ids or feedback_ids,
        ))
    trait_map = {item.trait: item for item in candidate.feedback.traits}
    for requirement in context.requirements:
        if requirement.name == "installed":
            state = _bool_gate(candidate.installed, requirement.expected)
            evidence = candidate.installed_evidence_ids
        else:
            assertion = trait_map.get(requirement.name)
            state = _trait_gate(assertion, requirement.expected)
            evidence = () if assertion is None else assertion.evidence_ids
        raw.append((requirement.name, state, evidence))
    for rule in rules:
        if rule.strength != "hard":
            continue
        assertion = trait_map.get(rule.trait)
        expected = rule.kind == "require"
        raw.append((f"profile:{rule.trait}", _trait_gate(assertion, expected), _merge_ids(rule.evidence_ids, () if assertion is None else assertion.evidence_ids)))
    if context.recipe in {RESUME_RECIPE, FINISHABILITY_RECIPE}:
        # This gate means "not explicitly marked finished".  An unset local
        # state is therefore a pass, not an inference that play is unfinished.
        raw.append(("not_finished", "fail" if candidate.feedback.play_state == "finished" else "pass", candidate.feedback.play_state_evidence_ids or feedback_ids))
    if context.recipe == FINISHABILITY_RECIPE and context.time_minutes is not None:
        remaining = candidate.feedback.remaining_minutes
        raw.append(("remaining_within_time", "unknown" if remaining is None else ("pass" if remaining <= context.time_minutes else "fail"), candidate.feedback.remaining_evidence_ids or feedback_ids))
    names = [name for name, _, _ in raw]
    override_names = {item.constraint for item in context.overrides if item.appid == candidate.appid}
    if not override_names.issubset(names):
        raise ValueError("override names a constraint that was not evaluated")
    return tuple(_gate(name, state, evidence, candidate, context) for name, state, evidence in raw)


def _bool_gate(actual: bool | None, expected: bool) -> GateState:
    return "unknown" if actual is None else ("pass" if actual is expected else "fail")


def _trait_gate(assertion: TraitAssertion | None, expected: bool) -> GateState:
    if assertion is None or assertion.value == "unknown":
        return "unknown"
    actual = assertion.value == "present"
    return "pass" if actual is expected else "fail"


def _component(rule_id: str, input_value: str | int | bool | None, ids: tuple[str, ...], points: int | None, state: ComponentState, kind: Literal["explicit_user", "behavioral", "achievement"], limitation: str | None = None) -> ScoreComponent:
    if points is not None:
        _int(points, name="component points", minimum=-MAX_INT64, maximum=MAX_INT64)
    return ScoreComponent(rule_id, input_value, _ids(ids), points, state, kind, limitation)


def _merge_ids(*values: tuple[str, ...]) -> tuple[str, ...]:
    return _ids(tuple(sorted({item for value in values for item in value})))


def _components(candidate: RecommendationCandidate, rules: tuple[ProfileRule, ...], context: RecommendationContext) -> tuple[ScoreComponent, ...]:
    items: list[ScoreComponent] = []
    feedback = candidate.feedback
    # Direct user evidence is always its own component family.
    rating_points = {"liked": PREFERENCE_LIKED, "disliked": PREFERENCE_DISLIKED, "neutral": 0}
    items.append(_component(
        "explicit_like" if feedback.rating == "liked" else "explicit_dislike" if feedback.rating == "disliked" else "explicit_rating",
        feedback.rating, feedback.rating_evidence_ids or feedback.evidence_ids,
        None if feedback.rating is None else rating_points[feedback.rating],
        "unknown" if feedback.rating is None else "applied", "explicit_user",
    ))
    play_points = {"finished": PREFERENCE_FINISHED, "user_abandoned": PREFERENCE_ABANDONED, "active": 0}
    items.append(_component(
        "user_abandoned" if feedback.play_state == "user_abandoned" else "explicit_finished" if feedback.play_state == "finished" else "explicit_play_state",
        feedback.play_state, feedback.play_state_evidence_ids or feedback.evidence_ids,
        None if feedback.play_state is None else play_points[feedback.play_state],
        "unknown" if feedback.play_state is None else "applied", "explicit_user",
    ))
    trait_map = {item.trait: item for item in feedback.traits}
    for rule in rules:
        if rule.strength == "hard":
            continue
        assertion = trait_map.get(rule.trait)
        if assertion is None or assertion.value == "unknown":
            items.append(_component(f"profile_rule:{rule.trait}", None, rule.evidence_ids, None, "unknown", "explicit_user"))
            continue
        present = assertion.value == "present"
        if rule.kind == "prefer":
            points = rule.weight if present else 0
        elif rule.kind == "avoid":
            points = -rule.weight if present else 0
        else:
            points = rule.weight if present else -rule.weight
        items.append(_component(f"profile_rule:{rule.trait}", assertion.value, _merge_ids(rule.evidence_ids, assertion.evidence_ids), points, "applied", "explicit_user"))
    if context.recipe == RESUME_RECIPE:
        items.extend(_resume_components(candidate, context))
    elif context.recipe == FINISHABILITY_RECIPE:
        items.extend(_finish_components(candidate, context))
    else:
        items.extend(_preference_behavior_components(candidate, context))
    return tuple(sorted(items, key=lambda item: item.rule_id))


def _activity_state(activity: ActivityEvidence | None) -> ComponentState:
    if activity is None or activity.freshness == "unknown":
        return "unknown"
    if activity.freshness == "expired":
        return "expired"
    return "stale" if activity.freshness == "stale" else "applied"


def _achievement_component_state(achievement: AchievementEvidence | None) -> ComponentState:
    if achievement is None or achievement.state != "ready":
        return "unknown"
    if achievement.total == 0:
        return "unknown"
    if achievement.freshness == "fresh":
        return "applied"
    if achievement.freshness == "stale":
        return "stale"
    if achievement.freshness == "expired":
        return "expired"
    return "unknown"


def _resume_components(candidate: RecommendationCandidate, context: RecommendationContext) -> list[ScoreComponent]:
    activity = candidate.activity
    state = _activity_state(activity)
    ids = () if activity is None else activity.evidence_ids
    recent = None if activity is None else activity.recent_window_minutes
    recent_state = (
        "unknown"
        if activity is None
        else _freshness_component_state(activity.recent_freshness)
    )
    lifetime = None if activity is None else activity.lifetime_minutes
    last = None if activity is None else activity.last_played_at
    recent_points = RESUME_RECENT_WINDOW if recent_state == "applied" and recent is not None and recent > 0 else (0 if recent_state == "applied" and recent is not None else None)
    sustained_points = RESUME_SUSTAINED_PLAY if state == "applied" and lifetime is not None and lifetime >= 120 else (0 if state == "applied" and lifetime is not None else None)
    last_points: int | None = None
    if state == "applied" and last is not None and last <= context.now:
        age_days = (context.now - last).days
        last_points = RESUME_LAST_PLAYED_7D if age_days <= 7 else RESUME_LAST_PLAYED_30D if age_days <= 30 else RESUME_LAST_PLAYED_90D if age_days <= 90 else 0
    achievement = candidate.achievements
    astate = _achievement_component_state(achievement)
    apoints = None
    ratio = None if achievement is None else achievement.ratio_bps
    aids = () if achievement is None else achievement.evidence_ids
    if astate == "applied":
        apoints = RESUME_ACHIEVEMENT_PROGRESS if ratio is not None and 0 < ratio < 10_000 else 0
    return [
        _component("recent_sustained_play", recent, ids, recent_points, _metric_state(recent_state, recent), "behavioral", "recent_window_is_not_a_session_log"),
        _component("lifetime_play_momentum", lifetime, ids, sustained_points, _metric_state(state, lifetime), "behavioral", "playtime_does_not_imply_preference"),
        _component("last_played_recency", None if last is None else last.isoformat(), ids, last_points, "unknown" if state == "applied" and (last is None or last > context.now) else state, "behavioral"),
        _component("achievement_progress_weak", ratio, aids, apoints, astate, "achievement", "achievement_ratio_is_weak_evidence_not_completion"),
    ]


def _finish_components(candidate: RecommendationCandidate, context: RecommendationContext) -> list[ScoreComponent]:
    remaining = candidate.feedback.remaining_minutes
    if remaining is None:
        points = None
    elif context.time_minutes is not None and context.time_minutes > 0:
        points = max(
            0,
            FINISH_KNOWN_ESTIMATE
            - min(
                FINISH_KNOWN_ESTIMATE,
                remaining * FINISH_KNOWN_ESTIMATE // context.time_minutes,
            ),
        )
    else:
        # Without a budget, prefer smaller explicit estimates in bounded
        # one-hour buckets; this is user evidence, never inferred game length.
        points = max(1, FINISH_KNOWN_ESTIMATE - min(remaining // 60, FINISH_KNOWN_ESTIMATE - 1))
    achievement = candidate.achievements
    ratio = None if achievement is None else achievement.ratio_bps
    astate = _achievement_component_state(achievement)
    apoints = None
    if astate == "applied":
        apoints = 0 if ratio is None else ratio * FINISH_ACHIEVEMENT_PROGRESS // 10_000
    return [
        _component("explicit_remaining_estimate", remaining, candidate.feedback.remaining_evidence_ids or candidate.feedback.evidence_ids, points, "unknown" if remaining is None else "applied", "explicit_user"),
        _component("achievement_progress_weak", ratio, () if achievement is None else achievement.evidence_ids, apoints, astate, "achievement", "achievement_ratio_is_weak_evidence_not_completion"),
    ]


def _preference_behavior_components(candidate: RecommendationCandidate, context: RecommendationContext) -> list[ScoreComponent]:
    activity = candidate.activity
    state = _activity_state(activity)
    ids = () if activity is None else activity.evidence_ids
    recent = None if activity is None else activity.recent_window_minutes
    recent_state = (
        "unknown"
        if activity is None
        else _freshness_component_state(activity.recent_freshness)
    )
    lifetime = None if activity is None else activity.lifetime_minutes
    recent_points = PREFERENCE_RECENT_BEHAVIOR if recent_state == "applied" and recent is not None and recent > 0 else (0 if recent_state == "applied" and recent is not None else None)
    lifetime_points = PREFERENCE_SUSTAINED_BEHAVIOR if state == "applied" and lifetime is not None and lifetime >= 120 else (0 if state == "applied" and lifetime is not None else None)
    return [
        _component("recent_play_behavior", recent, ids, recent_points, _metric_state(recent_state, recent), "behavioral", "behavior_does_not_prove_preference"),
        _component("lifetime_play_behavior", lifetime, ids, lifetime_points, _metric_state(state, lifetime), "behavioral", "behavior_does_not_prove_preference"),
    ]


def _metric_state(state: ComponentState, value: object | None) -> ComponentState:
    if state == "applied" and value is None:
        return "unknown"
    return state


def _freshness_component_state(freshness: Freshness | None) -> ComponentState:
    if freshness in {None, "unknown"}:
        return "unknown"
    if freshness == "expired":
        return "expired"
    return "stale" if freshness == "stale" else "applied"


def _safe_sum(values: tuple[int, ...]) -> int:
    total = 0
    for value in values:
        if value > 0 and total > MAX_INT64 - value:
            raise OverflowError("recommendation score exceeds signed 64-bit range")
        if value < 0 and total < -MAX_INT64 - 1 - value:
            raise OverflowError("recommendation score exceeds signed 64-bit range")
        total += value
    return total


def _sort_key(item: RankedRecommendation) -> tuple[object, ...]:
    eligibility = {"eligible": 0, "conditional": 1, "excluded": 2}[item.eligibility]
    return (eligibility, -(item.score or 0), item.appid)

"""Pure, deterministic wishlist-fit recommendation recipe.

The recipe consumes normalized cache evidence and performs no I/O.  It keeps
user preference, deal value, aggregate reviews, release, and compatibility as
separate dimensions so callers can explain what is known without turning a
cheap price or a social signal into inferred user taste.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
from typing import Literal


WISHLIST_FIT_RECIPE = "wishlist-fit/0.1"
M3_DEAL_SCHEMA = "deal-evidence/0.1"
MAX_APPID = (1 << 32) - 1
MAX_MINOR_UNITS = (1 << 63) - 1
MAX_CANDIDATES = 100_000
MAX_RULES = 128
MAX_WEIGHT = 100
MAX_TEXT = 256
MAX_EVIDENCE_IDS = 64

UnknownPolicy = Literal["include", "exclude"]
StoreClass = Literal["official", "keyshop", "unknown"]
Freshness = Literal["fresh", "stale", "expired", "unknown"]
TraitValue = Literal["present", "absent", "unknown"]
EligibilityState = Literal["eligible", "conditional", "excluded"]
DealState = Literal["ready", "not_found", "unknown"]
DealBucket = Literal[
    "at_or_below_low",
    "within_5_percent",
    "discounted",
    "current_only",
    "noncomparable",
    "unknown",
]
EvidenceGrade = Literal["exact", "normalized", "degraded", "unknown"]

_TRAIT = re.compile(r"user:[a-z0-9](?:[a-z0-9._-]{0,57}[a-z0-9])?\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_BUCKET_ORDER = {
    "at_or_below_low": 0,
    "within_5_percent": 1,
    "discounted": 2,
    "current_only": 3,
    "noncomparable": 4,
    "unknown": 5,
}
_GRADE_ORDER = {"exact": 0, "normalized": 1, "degraded": 2, "unknown": 3}


def _bounded_int(value: object, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")
    return value


def _bounded_text(value: object, *, name: str, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
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
    normalized: list[str] = []
    for value in values:
        checked = _bounded_text(value, name="evidence_id")
        assert checked is not None
        normalized.append(checked)
    if len(normalized) != len(set(normalized)):
        raise ValueError("evidence_ids must be unique")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class MetadataFact:
    key: str
    value: str
    provider: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("key", "value", "provider"):
            _bounded_text(getattr(self, name), name=name)
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class TraitAssertion:
    trait: str
    value: TraitValue
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trait, str) or _TRAIT.fullmatch(self.trait) is None:
            raise ValueError("trait must be a bounded user: slug")
        if self.value not in {"present", "absent", "unknown"}:
            raise ValueError("trait value is invalid")
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class DirectFeedback:
    rating: Literal["liked", "disliked", "neutral"] | None = None
    snoozed_until: datetime | None = None
    hard_exclude: bool = False
    traits: tuple[TraitAssertion, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.rating not in {None, "liked", "disliked", "neutral"}:
            raise ValueError("rating is invalid")
        if self.snoozed_until is not None:
            object.__setattr__(
                self, "snoozed_until", _time(self.snoozed_until, name="snoozed_until")
            )
        if not isinstance(self.hard_exclude, bool):
            raise ValueError("hard_exclude must be a boolean")
        if not isinstance(self.traits, tuple) or any(
            not isinstance(item, TraitAssertion) for item in self.traits
        ):
            raise ValueError("traits must contain TraitAssertion values")
        names = [item.trait for item in self.traits]
        if len(names) != len(set(names)):
            raise ValueError("traits must be unique")
        object.__setattr__(self, "traits", tuple(sorted(self.traits, key=lambda item: item.trait)))
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class ProfileRule:
    trait: str
    kind: Literal["prefer", "avoid", "require"]
    strength: Literal["soft", "hard"]
    weight: int
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trait, str) or _TRAIT.fullmatch(self.trait) is None:
            raise ValueError("rule trait must be a bounded user: slug")
        if self.kind not in {"prefer", "avoid", "require"}:
            raise ValueError("rule kind is invalid")
        if self.strength not in {"soft", "hard"}:
            raise ValueError("rule strength is invalid")
        _bounded_int(self.weight, name="weight", maximum=MAX_WEIGHT)
        if self.strength == "hard" and self.kind == "prefer":
            raise ValueError("hard prefer is ambiguous; use hard require")
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class DealDimension:
    """Already-normalized M3 deal result; this recipe never recomputes it."""

    schema: Literal["deal-evidence/0.1"]
    state: DealState
    bucket: DealBucket
    evidence_grade: EvidenceGrade
    provider: str | None
    store_class: StoreClass
    country: str
    currency: str
    freshness: Freshness
    current_amount_minor: int | None = None
    historical_low_amount_minor: int | None = None
    observed_at: datetime | None = None
    references: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != M3_DEAL_SCHEMA:
            raise ValueError("deal schema is unsupported")
        if self.state not in {"ready", "not_found", "unknown"}:
            raise ValueError("deal state is invalid")
        if self.bucket not in _BUCKET_ORDER or self.evidence_grade not in _GRADE_ORDER:
            raise ValueError("deal bucket or evidence grade is invalid")
        if self.store_class not in {"official", "keyshop", "unknown"}:
            raise ValueError("deal store class is invalid")
        if not isinstance(self.country, str) or _COUNTRY.fullmatch(self.country) is None:
            raise ValueError("deal country must be ISO-like uppercase alpha-2")
        if not isinstance(self.currency, str) or _CURRENCY.fullmatch(self.currency) is None:
            raise ValueError("deal currency must be ISO-like uppercase alpha-3")
        if self.freshness not in {"fresh", "stale", "expired", "unknown"}:
            raise ValueError("deal freshness is invalid")
        if self.provider is not None:
            _bounded_text(self.provider, name="provider")
        for name in ("current_amount_minor", "historical_low_amount_minor"):
            value = getattr(self, name)
            if value is not None:
                _bounded_int(value, name=name, maximum=MAX_MINOR_UNITS)
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _time(self.observed_at, name="observed_at"))
        if self.state == "ready" and (self.provider is None or self.observed_at is None):
            raise ValueError("ready deal evidence requires provider and observed_at")
        if self.state == "ready" and self.bucket in {
            "at_or_below_low",
            "within_5_percent",
            "discounted",
            "current_only",
        } and self.current_amount_minor is None:
            raise ValueError("a priced deal bucket requires a current price")
        if self.state != "ready" and (
            self.current_amount_minor is not None
            or self.historical_low_amount_minor is not None
            or self.bucket != "unknown"
        ):
            raise ValueError("non-ready deal state cannot carry prices or a ranked bucket")
        if self.state == "not_found" and self.evidence_grade != "unknown":
            raise ValueError("not_found deal evidence must have unknown grade")
        if not isinstance(self.references, tuple) or len(self.references) > MAX_EVIDENCE_IDS:
            raise ValueError("references must be a tuple")
        checked_references = tuple(
            _bounded_text(value, name="reference") for value in self.references
        )
        if len(checked_references) != len(set(checked_references)):
            raise ValueError("references must be unique")
        object.__setattr__(self, "references", tuple(sorted(checked_references)))
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    provider: str
    positive: int
    total: int
    observed_at: datetime
    freshness: Freshness
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded_text(self.provider, name="review provider")
        _bounded_int(self.positive, name="positive", maximum=MAX_APPID)
        _bounded_int(self.total, name="total", maximum=MAX_APPID)
        if self.positive > self.total:
            raise ValueError("positive review count cannot exceed total")
        object.__setattr__(self, "observed_at", _time(self.observed_at, name="observed_at"))
        if self.freshness not in {"fresh", "stale", "expired", "unknown"}:
            raise ValueError("review freshness is invalid")
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class WishlistCandidate:
    appid: int
    name: str | None
    feedback: DirectFeedback = DirectFeedback()
    deal: DealDimension | None = None
    review: ReviewSummary | None = None
    metadata: tuple[MetadataFact, ...] = ()
    identity_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded_int(self.appid, name="appid", maximum=MAX_APPID)
        if self.appid == 0:
            raise ValueError("appid must be positive")
        _bounded_text(self.name, name="name", nullable=True)
        if not isinstance(self.feedback, DirectFeedback):
            raise ValueError("feedback must be DirectFeedback")
        if self.deal is not None and not isinstance(self.deal, DealDimension):
            raise ValueError("deal must be DealDimension or unknown")
        if self.review is not None and not isinstance(self.review, ReviewSummary):
            raise ValueError("review must be ReviewSummary or unknown")
        if not isinstance(self.metadata, tuple) or any(
            not isinstance(item, MetadataFact) for item in self.metadata
        ):
            raise ValueError("metadata must contain MetadataFact values")
        keys = [item.key for item in self.metadata]
        if len(keys) != len(set(keys)):
            raise ValueError("metadata keys must be unique")
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata, key=lambda item: item.key)))
        object.__setattr__(self, "identity_evidence_ids", _ids(self.identity_evidence_ids))


@dataclass(frozen=True, slots=True)
class WishlistFitContext:
    country: str
    currency: str
    store_class: StoreClass
    unknown_policy: UnknownPolicy
    generated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.country, str) or _COUNTRY.fullmatch(self.country) is None:
            raise ValueError("country must be ISO-like uppercase alpha-2")
        if not isinstance(self.currency, str) or _CURRENCY.fullmatch(self.currency) is None:
            raise ValueError("currency must be ISO-like uppercase alpha-3")
        if self.store_class not in {"official", "keyshop", "unknown"}:
            raise ValueError("store class is invalid")
        if self.unknown_policy not in {"include", "exclude"}:
            raise ValueError("unknown policy is invalid")
        object.__setattr__(self, "generated_at", _time(self.generated_at, name="generated_at"))


@dataclass(frozen=True, slots=True)
class EvidenceFactor:
    dimension: Literal["eligibility", "preference_fit"]
    rule_id: str
    state: Literal["applied", "pass", "fail", "unknown"]
    contribution: int | None
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EligibilityDimension:
    state: EligibilityState
    factors: tuple[EvidenceFactor, ...]


@dataclass(frozen=True, slots=True)
class PreferenceFitDimension:
    state: Literal["known", "unknown"]
    score: int | None
    factors: tuple[EvidenceFactor, ...]


@dataclass(frozen=True, slots=True)
class DealValueDimension:
    state: Literal["supported", "stale", "expired", "not_found", "noncomparable", "unknown"]
    bucket: DealBucket
    evidence_grade: EvidenceGrade
    provider: str | None
    current_amount_minor: int | None
    historical_low_amount_minor: int | None
    freshness: Freshness
    references: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    tradeoffs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnknownDimension:
    state: Literal["unknown"] = "unknown"
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RankedWishlistCandidate:
    rank: int | None
    appid: int
    name: str | None
    metadata: tuple[MetadataFact, ...]
    identity_evidence_ids: tuple[str, ...]
    eligibility: EligibilityDimension
    preference_fit: PreferenceFitDimension
    deal_value: DealValueDimension
    review: ReviewSummary | None
    release: UnknownDimension
    compatibility: UnknownDimension
    tradeoffs: tuple[str, ...]
    missing: tuple[str, ...]
    stale: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WishlistFitResult:
    recipe: Literal["wishlist-fit/0.1"]
    context: WishlistFitContext
    status: Literal["complete", "degraded"]
    degradation_reasons: tuple[str, ...]
    purchase_recommendation_supported: bool
    ranked: tuple[RankedWishlistCandidate, ...]
    excluded: tuple[RankedWishlistCandidate, ...]


def rank_wishlist(
    candidates: tuple[WishlistCandidate, ...] | list[WishlistCandidate],
    *,
    rules: tuple[ProfileRule, ...] | list[ProfileRule],
    context: WishlistFitContext,
) -> WishlistFitResult:
    """Rank wishlist candidates without inferring taste from behavioral proxies."""

    if not isinstance(context, WishlistFitContext):
        raise ValueError("context must be WishlistFitContext")
    values = tuple(candidates)
    rule_values = tuple(rules)
    if len(values) > MAX_CANDIDATES:
        raise ValueError("candidate count exceeds recipe bound")
    if len(rule_values) > MAX_RULES or any(not isinstance(rule, ProfileRule) for rule in rule_values):
        raise ValueError("rules must be a bounded collection of ProfileRule values")
    if any(not isinstance(candidate, WishlistCandidate) for candidate in values):
        raise ValueError("candidates must contain WishlistCandidate values")
    appids = [candidate.appid for candidate in values]
    if len(appids) != len(set(appids)):
        raise ValueError("candidate AppIDs must be unique")
    rule_keys = [rule.trait for rule in rule_values]
    if len(rule_keys) != len(set(rule_keys)):
        raise ValueError("profile rules must be unique")
    ordered_rules = tuple(sorted(rule_values, key=lambda value: (value.trait, value.kind, value.strength)))

    evaluated = tuple(_evaluate(candidate, ordered_rules, context) for candidate in values)
    included = [item for item in evaluated if item.eligibility.state != "excluded"]
    excluded = [item for item in evaluated if item.eligibility.state == "excluded"]
    included.sort(key=_rank_key)
    excluded.sort(key=lambda item: item.appid)
    ranked = tuple(replace(item, rank=index) for index, item in enumerate(included, start=1))

    all_unknown = bool(ranked) and all(item.preference_fit.state == "unknown" for item in ranked)
    reasons = ("insufficient_preference_evidence",) if all_unknown else ()
    return WishlistFitResult(
        recipe=WISHLIST_FIT_RECIPE,
        context=context,
        status="degraded" if all_unknown else "complete",
        degradation_reasons=reasons,
        purchase_recommendation_supported=bool(ranked) and not all_unknown,
        ranked=ranked,
        excluded=tuple(excluded),
    )


def _evaluate(
    candidate: WishlistCandidate,
    rules: tuple[ProfileRule, ...],
    context: WishlistFitContext,
) -> RankedWishlistCandidate:
    traits = {item.trait: item for item in candidate.feedback.traits}
    gate_factors: list[EvidenceFactor] = []
    failed = False
    conditional = False
    if candidate.feedback.hard_exclude:
        gate_factors.append(EvidenceFactor("eligibility", "explicit_hard_exclude", "fail", None, candidate.feedback.evidence_ids))
        failed = True
    if candidate.feedback.snoozed_until is not None and candidate.feedback.snoozed_until > context.generated_at:
        gate_factors.append(EvidenceFactor("eligibility", "active_snooze", "fail", None, candidate.feedback.evidence_ids))
        failed = True

    for rule in rules:
        if rule.strength != "hard":
            continue
        assertion = traits.get(rule.trait)
        value = "unknown" if assertion is None else assertion.value
        evidence = _merge_ids(rule.evidence_ids, () if assertion is None else assertion.evidence_ids)
        passes = value == "present" if rule.kind == "require" else value == "absent"
        if value == "unknown":
            state: Literal["pass", "fail", "unknown"] = "unknown"
            conditional = True
            if context.unknown_policy == "exclude":
                failed = True
        elif passes:
            state = "pass"
        else:
            state = "fail"
            failed = True
        gate_factors.append(EvidenceFactor("eligibility", f"hard_{rule.kind}:{rule.trait}", state, None, evidence))

    eligibility_state: EligibilityState = "excluded" if failed else "conditional" if conditional else "eligible"
    preference_factors: list[EvidenceFactor] = []
    if candidate.feedback.rating is not None:
        contribution = {"liked": 100, "neutral": 0, "disliked": -100}[candidate.feedback.rating]
        preference_factors.append(EvidenceFactor("preference_fit", f"direct_rating:{candidate.feedback.rating}", "applied", contribution, candidate.feedback.evidence_ids))
    for rule in rules:
        if rule.strength != "soft":
            continue
        assertion = traits.get(rule.trait)
        if assertion is None or assertion.value == "unknown":
            continue
        if rule.kind == "prefer":
            contribution = rule.weight if assertion.value == "present" else -rule.weight
        elif rule.kind == "avoid":
            contribution = -rule.weight if assertion.value == "present" else 0
        else:
            contribution = rule.weight if assertion.value == "present" else -rule.weight
        preference_factors.append(EvidenceFactor("preference_fit", f"soft_{rule.kind}:{rule.trait}", "applied", contribution, _merge_ids(rule.evidence_ids, assertion.evidence_ids)))

    preference = PreferenceFitDimension(
        state="known" if preference_factors else "unknown",
        score=sum(factor.contribution or 0 for factor in preference_factors) if preference_factors else None,
        factors=tuple(preference_factors),
    )
    deal = _deal(candidate.deal, context)
    tradeoffs = list(deal.tradeoffs)
    missing = ["release", "compatibility"]
    stale: list[str] = []
    if preference.state == "unknown":
        missing.append("preference_fit")
        tradeoffs.append("no_direct_preference_or_explicit_trait_rule_evidence")
    if deal.state in {"unknown", "not_found", "noncomparable"}:
        missing.append("deal_value")
    if deal.state in {"stale", "expired"}:
        stale.append("deal_value")
    if candidate.review is None:
        missing.append("review")
    elif candidate.review.freshness in {"stale", "expired"}:
        stale.append("review")
    return RankedWishlistCandidate(
        rank=None,
        appid=candidate.appid,
        name=candidate.name,
        metadata=candidate.metadata,
        identity_evidence_ids=candidate.identity_evidence_ids,
        eligibility=EligibilityDimension(eligibility_state, tuple(gate_factors)),
        preference_fit=preference,
        deal_value=deal,
        review=candidate.review,
        release=UnknownDimension(),
        compatibility=UnknownDimension(),
        tradeoffs=tuple(sorted(set(tradeoffs))),
        missing=tuple(sorted(set(missing))),
        stale=tuple(sorted(set(stale))),
    )


def _deal(value: DealDimension | None, context: WishlistFitContext) -> DealValueDimension:
    if value is None:
        return DealValueDimension("unknown", "unknown", "unknown", None, None, None, "unknown", (), (), ("no_deal_evidence",))
    mismatches: list[str] = []
    if value.country != context.country:
        mismatches.append("deal_country_does_not_match_query")
    if value.currency != context.currency:
        mismatches.append("deal_currency_does_not_match_query")
    if value.store_class != context.store_class:
        mismatches.append("deal_store_class_does_not_match_query")
    if mismatches:
        return DealValueDimension("noncomparable", "noncomparable", value.evidence_grade, value.provider, value.current_amount_minor, value.historical_low_amount_minor, value.freshness, value.references, value.evidence_ids, tuple(mismatches))
    if value.state == "not_found":
        return DealValueDimension("not_found", "unknown", "unknown", value.provider, None, None, value.freshness, value.references, value.evidence_ids, ("provider_reported_not_found; price_is_unknown_not_free",))
    if value.state == "unknown":
        return DealValueDimension("unknown", "unknown", "unknown", value.provider, None, None, value.freshness, value.references, value.evidence_ids, ("deal_evidence_unknown",))
    if value.freshness == "unknown":
        return DealValueDimension("unknown", value.bucket, value.evidence_grade, value.provider, value.current_amount_minor, value.historical_low_amount_minor, value.freshness, value.references, value.evidence_ids, ("deal_evidence_freshness_unknown",))
    state: Literal["supported", "stale", "expired"] = "supported" if value.freshness == "fresh" else "expired" if value.freshness == "expired" else "stale"
    tradeoffs = () if state == "supported" else (f"deal_evidence_{state}",)
    return DealValueDimension(state, value.bucket, value.evidence_grade, value.provider, value.current_amount_minor, value.historical_low_amount_minor, value.freshness, value.references, value.evidence_ids, tradeoffs)


def _rank_key(item: RankedWishlistCandidate) -> tuple[int, int, int, int, int, int]:
    preference_known = item.preference_fit.state == "known"
    deal_supported = item.deal_value.state == "supported"
    return (
        0 if item.eligibility.state == "eligible" else 1,
        0 if preference_known else 1,
        -(item.preference_fit.score or 0) if preference_known else 0,
        _BUCKET_ORDER[item.deal_value.bucket] if deal_supported else len(_BUCKET_ORDER),
        _GRADE_ORDER[item.deal_value.evidence_grade] if deal_supported else len(_GRADE_ORDER),
        item.appid,
    )


def _merge_ids(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({value for group in groups for value in group}))

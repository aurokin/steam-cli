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
from urllib.parse import parse_qs, urlsplit


WISHLIST_FIT_RECIPE = "wishlist-fit/0.1"
M3_DEAL_SCHEMA = "deal-evidence/0.1"
MAX_APPID = (1 << 32) - 1
MAX_MINOR_UNITS = (1 << 63) - 1
MAX_CANDIDATES = 100_000
MAX_RULES = 128
MAX_WEIGHT = 100
MAX_TEXT = 256
MAX_EVIDENCE_IDS = 64
MAX_OVERRIDES = 256

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
DealProvider = Literal["gg-deals", "cheapshark", "manual-reference"]

_TRAIT = re.compile(r"user:[a-z0-9](?:[a-z0-9._-]{0,57}[a-z0-9])?\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_OVERRIDE_NAME = re.compile(r"override:[a-z0-9](?:[a-z0-9._-]{0,54}[a-z0-9])?\Z")
_CONSTRAINT_ID = re.compile(
    r"(?:explicit_hard_exclude|active_snooze|hard_(?:avoid|require):user:[a-z0-9][a-z0-9._-]{0,58})\Z"
)
_GG_PATH = re.compile(r"/(?:game|dlc|pack)/[A-Za-z0-9][A-Za-z0-9._~-]*/?\Z")
_GG_APP_PATH = re.compile(r"/steam/app/[1-9][0-9]*/?\Z")
_STEAMDB_APP_PATH = re.compile(r"/app/[1-9][0-9]*/?\Z")
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
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
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
    rating_evidence_ids: tuple[str, ...] = ()
    snooze_evidence_ids: tuple[str, ...] = ()
    hard_exclude_evidence_ids: tuple[str, ...] = ()

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
        object.__setattr__(
            self, "traits", tuple(sorted(self.traits, key=lambda item: item.trait))
        )
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))
        object.__setattr__(
            self, "rating_evidence_ids", _ids(self.rating_evidence_ids)
        )
        object.__setattr__(
            self, "snooze_evidence_ids", _ids(self.snooze_evidence_ids)
        )
        object.__setattr__(
            self, "hard_exclude_evidence_ids", _ids(self.hard_exclude_evidence_ids)
        )


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
class GateOverride:
    """A named, one-query override of one hard gate for one AppID."""

    name: str
    appid: int
    constraint_id: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or _OVERRIDE_NAME.fullmatch(self.name) is None
        ):
            raise ValueError("override name must be a bounded override: slug")
        _bounded_int(self.appid, name="override appid", maximum=MAX_APPID)
        if self.appid == 0:
            raise ValueError("override appid must be positive")
        if (
            not isinstance(self.constraint_id, str)
            or _CONSTRAINT_ID.fullmatch(self.constraint_id) is None
        ):
            raise ValueError("override constraint_id is invalid")
        checked = _ids(self.evidence_ids)
        if not checked:
            raise ValueError("override requires evidence lineage")
        object.__setattr__(self, "evidence_ids", checked)


@dataclass(frozen=True, slots=True)
class DealReference:
    provider: Literal["gg-deals", "cheapshark", "steamdb"]
    url: str

    def __post_init__(self) -> None:
        if self.provider not in {"gg-deals", "cheapshark", "steamdb"}:
            raise ValueError("deal reference provider is invalid")
        _validate_reference_url(self.provider, self.url)


@dataclass(frozen=True, slots=True)
class DealDimension:
    """Already-normalized M3 deal result; this recipe never recomputes it."""

    schema: Literal["deal-evidence/0.1"]
    state: DealState
    bucket: DealBucket
    evidence_grade: EvidenceGrade
    provider: DealProvider | None
    store_class: StoreClass
    country: str
    currency: str
    freshness: Freshness
    current_amount_minor: int | None = None
    historical_low_amount_minor: int | None = None
    observed_at: datetime | None = None
    references: tuple[DealReference, ...] = ()
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
        if (
            not isinstance(self.country, str)
            or _COUNTRY.fullmatch(self.country) is None
        ):
            raise ValueError("deal country must be ISO-like uppercase alpha-2")
        if (
            not isinstance(self.currency, str)
            or _CURRENCY.fullmatch(self.currency) is None
        ):
            raise ValueError("deal currency must be ISO-like uppercase alpha-3")
        if self.freshness not in {"fresh", "stale", "expired", "unknown"}:
            raise ValueError("deal freshness is invalid")
        if self.provider not in {None, "gg-deals", "cheapshark", "manual-reference"}:
            raise ValueError("deal provider is invalid")
        for name in ("current_amount_minor", "historical_low_amount_minor"):
            value = getattr(self, name)
            if value is not None:
                _bounded_int(value, name=name, maximum=MAX_MINOR_UNITS)
        if self.observed_at is not None:
            object.__setattr__(
                self, "observed_at", _time(self.observed_at, name="observed_at")
            )
        if self.state == "ready" and (
            self.provider not in {"gg-deals", "cheapshark"} or self.observed_at is None
        ):
            raise ValueError("ready deal evidence requires provider and observed_at")
        if self.state == "ready" and self.freshness == "unknown":
            raise ValueError("ready deal evidence requires known freshness")
        if self.state == "ready" and self.bucket == "unknown":
            raise ValueError("ready deal evidence cannot have an unknown bucket")
        if self.state == "ready" and self.bucket in {
            "at_or_below_low",
            "within_5_percent",
        }:
            if (
                self.current_amount_minor is None
                or self.historical_low_amount_minor is None
            ):
                raise ValueError(
                    "historical-low buckets require current and low prices"
                )
            if (
                self.bucket == "at_or_below_low"
                and self.current_amount_minor > self.historical_low_amount_minor
            ):
                raise ValueError("at-or-below-low bucket contradicts its prices")
            if self.bucket == "within_5_percent" and (
                self.current_amount_minor <= self.historical_low_amount_minor
                or self.historical_low_amount_minor == 0
                or self.current_amount_minor * 10_000
                > self.historical_low_amount_minor * 10_500
            ):
                raise ValueError("within-five-percent bucket contradicts its prices")
        if (
            self.state == "ready"
            and self.bucket in {"discounted", "current_only"}
            and self.current_amount_minor is None
        ):
            raise ValueError("a priced deal bucket requires a current price")
        if (
            self.state == "ready"
            and self.bucket == "noncomparable"
            and (
                self.current_amount_minor is not None
                or self.historical_low_amount_minor is not None
                or self.evidence_grade != "degraded"
            )
        ):
            raise ValueError(
                "noncomparable evidence cannot claim selected prices or grade"
            )
        if (
            self.state == "ready"
            and self.bucket != "noncomparable"
            and self.evidence_grade == "unknown"
        ):
            raise ValueError("ranked deal evidence requires a supported grade")
        if self.state != "ready" and (
            self.current_amount_minor is not None
            or self.historical_low_amount_minor is not None
            or self.bucket != "unknown"
        ):
            raise ValueError(
                "non-ready deal state cannot carry prices or a ranked bucket"
            )
        if self.state == "not_found" and self.evidence_grade != "unknown":
            raise ValueError("not_found deal evidence must have unknown grade")
        if self.state == "not_found" and self.provider not in {
            "gg-deals",
            "cheapshark",
        }:
            raise ValueError("not_found must be attributed to an ingest provider")
        if self.state == "not_found" and (
            self.observed_at is None or self.freshness == "unknown"
        ):
            raise ValueError("not_found requires attributed observation freshness")
        if self.state == "unknown" and (
            self.provider not in {None, "manual-reference"}
            or self.observed_at is not None
            or self.freshness != "unknown"
            or self.evidence_grade != "unknown"
        ):
            raise ValueError("unknown deal evidence carries contradictory claims")
        if (
            not isinstance(self.references, tuple)
            or len(self.references) > MAX_EVIDENCE_IDS
            or any(not isinstance(value, DealReference) for value in self.references)
        ):
            raise ValueError("references must be a tuple")
        if len(self.references) != len(
            {(value.provider, value.url) for value in self.references}
        ):
            raise ValueError("references must be unique")
        object.__setattr__(
            self,
            "references",
            tuple(
                sorted(self.references, key=lambda value: (value.provider, value.url))
            ),
        )
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class ReviewRequestContext:
    filter: Literal["all"] = "all"
    language: Literal["all"] = "all"
    day_range: Literal[365] = 365
    review_type: Literal["all"] = "all"
    purchase_type: Literal["all"] = "all"
    num_per_page: Literal[1] = 1
    off_topic_activity_filtered: Literal[True] = True

    def __post_init__(self) -> None:
        if (
            self.filter != "all"
            or self.language != "all"
            or self.day_range != 365
            or self.review_type != "all"
            or self.purchase_type != "all"
            or self.num_per_page != 1
            or self.off_topic_activity_filtered is not True
        ):
            raise ValueError("review request context is unsupported")


@dataclass(frozen=True, slots=True)
class ReviewHumanReference:
    appid: int
    url: str
    purpose: Literal["view_store_reviews"] = "view_store_reviews"
    access_mode: Literal["manual_only"] = "manual_only"
    automation_supported: Literal[False] = False

    def __post_init__(self) -> None:
        _bounded_int(self.appid, name="review reference appid", maximum=MAX_APPID)
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except (TypeError, ValueError):
            raise ValueError("review human reference is invalid") from None
        if (
            self.appid == 0
            or parsed.scheme != "https"
            or parsed.hostname != "store.steampowered.com"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.path != f"/app/{self.appid}/"
            or parsed.query
            or parsed.fragment != "app_reviews_hash"
            or self.purpose != "view_store_reviews"
            or self.access_mode != "manual_only"
            or self.automation_supported is not False
        ):
            raise ValueError("review human reference is invalid")


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    provider: str
    positive: int
    total: int
    observed_at: datetime
    freshness: Freshness
    evidence_ids: tuple[str, ...] = ()
    review_score: int | None = None
    negative: int | None = None
    request_context: ReviewRequestContext | None = None
    source_locator: Literal["steam_store_appreviews"] | None = None
    human_reference: ReviewHumanReference | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.provider, name="review provider")
        _bounded_int(self.positive, name="positive", maximum=MAX_MINOR_UNITS)
        _bounded_int(self.total, name="total", maximum=MAX_MINOR_UNITS)
        if self.positive > self.total:
            raise ValueError("positive review count cannot exceed total")
        if self.review_score is not None:
            _bounded_int(self.review_score, name="review_score", maximum=10)
        if self.negative is not None:
            _bounded_int(self.negative, name="negative", maximum=MAX_MINOR_UNITS)
            if self.positive + self.negative != self.total:
                raise ValueError("review positive and negative counts must equal total")
        if self.request_context is not None and not isinstance(
            self.request_context, ReviewRequestContext
        ):
            raise ValueError("review request_context is invalid")
        if self.source_locator not in {None, "steam_store_appreviews"}:
            raise ValueError("review source locator is invalid")
        if self.human_reference is not None and not isinstance(
            self.human_reference, ReviewHumanReference
        ):
            raise ValueError("review human_reference is invalid")
        attributed = (
            self.request_context is not None,
            self.source_locator is not None,
            self.human_reference is not None,
        )
        if any(attributed) and not all(attributed):
            raise ValueError("review attribution must be complete")
        object.__setattr__(
            self, "observed_at", _time(self.observed_at, name="observed_at")
        )
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
        object.__setattr__(
            self, "metadata", tuple(sorted(self.metadata, key=lambda item: item.key))
        )
        object.__setattr__(
            self, "identity_evidence_ids", _ids(self.identity_evidence_ids)
        )


@dataclass(frozen=True, slots=True)
class WishlistFitContext:
    country: str
    currency: str
    store_class: StoreClass
    unknown_policy: UnknownPolicy
    generated_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.country, str)
            or _COUNTRY.fullmatch(self.country) is None
        ):
            raise ValueError("country must be ISO-like uppercase alpha-2")
        if (
            not isinstance(self.currency, str)
            or _CURRENCY.fullmatch(self.currency) is None
        ):
            raise ValueError("currency must be ISO-like uppercase alpha-3")
        if self.store_class not in {"official", "keyshop", "unknown"}:
            raise ValueError("store class is invalid")
        if self.unknown_policy not in {"include", "exclude"}:
            raise ValueError("unknown policy is invalid")
        object.__setattr__(
            self, "generated_at", _time(self.generated_at, name="generated_at")
        )


@dataclass(frozen=True, slots=True)
class EvidenceFactor:
    dimension: Literal["eligibility", "preference_fit"]
    rule_id: str
    state: Literal["applied", "pass", "fail", "unknown"]
    contribution: int | None
    evidence_ids: tuple[str, ...]
    original_state: Literal["pass", "fail", "unknown"] | None = None
    override_name: str | None = None
    override_evidence_ids: tuple[str, ...] = ()


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
    state: Literal[
        "supported", "stale", "expired", "not_found", "noncomparable", "unknown"
    ]
    bucket: DealBucket
    evidence_grade: EvidenceGrade
    provider: str | None
    current_amount_minor: int | None
    historical_low_amount_minor: int | None
    freshness: Freshness
    references: tuple[DealReference, ...]
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
    overrides: tuple[GateOverride, ...] | list[GateOverride] = (),
) -> WishlistFitResult:
    """Rank wishlist candidates without inferring taste from behavioral proxies."""

    if not isinstance(context, WishlistFitContext):
        raise ValueError("context must be WishlistFitContext")
    values = tuple(candidates)
    rule_values = tuple(rules)
    override_values = tuple(overrides)
    if len(values) > MAX_CANDIDATES:
        raise ValueError("candidate count exceeds recipe bound")
    if len(rule_values) > MAX_RULES or any(
        not isinstance(rule, ProfileRule) for rule in rule_values
    ):
        raise ValueError("rules must be a bounded collection of ProfileRule values")
    if any(not isinstance(candidate, WishlistCandidate) for candidate in values):
        raise ValueError("candidates must contain WishlistCandidate values")
    if len(override_values) > MAX_OVERRIDES or any(
        not isinstance(override, GateOverride) for override in override_values
    ):
        raise ValueError(
            "overrides must be a bounded collection of GateOverride values"
        )
    appids = [candidate.appid for candidate in values]
    if len(appids) != len(set(appids)):
        raise ValueError("candidate AppIDs must be unique")
    rule_keys = [rule.trait for rule in rule_values]
    if len(rule_keys) != len(set(rule_keys)):
        raise ValueError("profile rules must be unique")
    override_keys = [
        (override.appid, override.constraint_id) for override in override_values
    ]
    if len(override_keys) != len(set(override_keys)):
        raise ValueError("overrides must target unique candidate constraints")
    override_names = [override.name for override in override_values]
    if len(override_names) != len(set(override_names)):
        raise ValueError("override names must be unique")
    candidate_appids = set(appids)
    if any(override.appid not in candidate_appids for override in override_values):
        raise ValueError("override target must be a candidate AppID")
    ordered_rules = tuple(
        sorted(rule_values, key=lambda value: (value.trait, value.kind, value.strength))
    )
    overrides_by_appid: dict[int, dict[str, GateOverride]] = {}
    for override in override_values:
        overrides_by_appid.setdefault(override.appid, {})[override.constraint_id] = (
            override
        )

    evaluated_with_usage = tuple(
        _evaluate(
            candidate,
            ordered_rules,
            context,
            overrides_by_appid.get(candidate.appid, {}),
        )
        for candidate in values
    )
    used_override_keys = {
        (candidate.appid, constraint_id)
        for candidate, (_, used) in zip(values, evaluated_with_usage, strict=True)
        for constraint_id in used
    }
    if used_override_keys != set(override_keys):
        raise ValueError(
            "override constraint does not identify a blocking candidate gate"
        )
    evaluated = tuple(item for item, _ in evaluated_with_usage)
    included = [item for item in evaluated if item.eligibility.state != "excluded"]
    excluded = [item for item in evaluated if item.eligibility.state == "excluded"]
    included.sort(key=_rank_key)
    excluded.sort(key=lambda item: item.appid)
    ranked = tuple(
        replace(item, rank=index) for index, item in enumerate(included, start=1)
    )

    no_eligible = not ranked
    all_unknown = bool(ranked) and all(
        item.preference_fit.state == "unknown" for item in ranked
    )
    reasons = (
        ("no_eligible_candidates",)
        if no_eligible
        else ("insufficient_preference_evidence",)
        if all_unknown
        else ()
    )
    return WishlistFitResult(
        recipe=WISHLIST_FIT_RECIPE,
        context=context,
        status="degraded" if no_eligible or all_unknown else "complete",
        degradation_reasons=reasons,
        purchase_recommendation_supported=bool(ranked) and not all_unknown,
        ranked=ranked,
        excluded=tuple(excluded),
    )


def _evaluate(
    candidate: WishlistCandidate,
    rules: tuple[ProfileRule, ...],
    context: WishlistFitContext,
    overrides: dict[str, GateOverride],
) -> tuple[RankedWishlistCandidate, frozenset[str]]:
    traits = {item.trait: item for item in candidate.feedback.traits}
    gate_factors: list[EvidenceFactor] = []
    failed = False
    conditional = False
    used_overrides: set[str] = set()
    if candidate.feedback.hard_exclude:
        factor, blocked = _gate_factor(
            "explicit_hard_exclude",
            "fail",
            candidate.feedback.hard_exclude_evidence_ids
            or candidate.feedback.evidence_ids,
            overrides,
        )
        gate_factors.append(factor)
        failed = failed or blocked
        if factor.override_name is not None:
            used_overrides.add("explicit_hard_exclude")
    if (
        candidate.feedback.snoozed_until is not None
        and candidate.feedback.snoozed_until > context.generated_at
    ):
        factor, blocked = _gate_factor(
            "active_snooze",
            "fail",
            candidate.feedback.snooze_evidence_ids
            or candidate.feedback.evidence_ids,
            overrides,
        )
        gate_factors.append(factor)
        failed = failed or blocked
        if factor.override_name is not None:
            used_overrides.add("active_snooze")

    for rule in rules:
        if rule.strength != "hard":
            continue
        assertion = traits.get(rule.trait)
        value = "unknown" if assertion is None else assertion.value
        evidence = _merge_ids(
            rule.evidence_ids, () if assertion is None else assertion.evidence_ids
        )
        passes = value == "present" if rule.kind == "require" else value == "absent"
        if value == "unknown":
            original_state: Literal["pass", "fail", "unknown"] = "unknown"
        elif passes:
            original_state = "pass"
        else:
            original_state = "fail"
        constraint_id = f"hard_{rule.kind}:{rule.trait}"
        factor, blocked = _gate_factor(
            constraint_id,
            original_state,
            evidence,
            overrides,
            unknown_blocks=context.unknown_policy == "exclude",
        )
        gate_factors.append(factor)
        failed = failed or blocked
        if factor.state == "unknown":
            conditional = True
        if factor.override_name is not None:
            used_overrides.add(constraint_id)

    eligibility_state: EligibilityState = (
        "excluded" if failed else "conditional" if conditional else "eligible"
    )
    preference_factors: list[EvidenceFactor] = []
    if candidate.feedback.rating is not None:
        contribution = {"liked": 100, "neutral": 0, "disliked": -100}[
            candidate.feedback.rating
        ]
        preference_factors.append(
            EvidenceFactor(
                "preference_fit",
                f"direct_rating:{candidate.feedback.rating}",
                "applied",
                contribution,
                candidate.feedback.rating_evidence_ids
                or candidate.feedback.evidence_ids,
            )
        )
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
        preference_factors.append(
            EvidenceFactor(
                "preference_fit",
                f"soft_{rule.kind}:{rule.trait}",
                "applied",
                contribution,
                _merge_ids(rule.evidence_ids, assertion.evidence_ids),
            )
        )

    preference = PreferenceFitDimension(
        state="known" if preference_factors else "unknown",
        score=sum(factor.contribution or 0 for factor in preference_factors)
        if preference_factors
        else None,
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
    elif candidate.review.freshness == "unknown":
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
    ), frozenset(used_overrides)


def _deal(
    value: DealDimension | None, context: WishlistFitContext
) -> DealValueDimension:
    if value is None:
        return DealValueDimension(
            "unknown",
            "unknown",
            "unknown",
            None,
            None,
            None,
            "unknown",
            (),
            (),
            ("no_deal_evidence",),
        )
    mismatches: list[str] = []
    if value.country != context.country:
        mismatches.append("deal_country_does_not_match_query")
    if value.currency != context.currency:
        mismatches.append("deal_currency_does_not_match_query")
    if value.store_class != context.store_class:
        mismatches.append("deal_store_class_does_not_match_query")
    if mismatches:
        return DealValueDimension(
            "noncomparable",
            "noncomparable",
            value.evidence_grade,
            value.provider,
            value.current_amount_minor,
            value.historical_low_amount_minor,
            value.freshness,
            value.references,
            value.evidence_ids,
            tuple(mismatches),
        )
    if value.state == "ready" and value.bucket == "noncomparable":
        return DealValueDimension(
            "noncomparable",
            "noncomparable",
            "degraded",
            value.provider,
            None,
            None,
            value.freshness,
            value.references,
            value.evidence_ids,
            ("requested_comparison_dimensions_are_not_fully_supported",),
        )
    if value.state == "not_found":
        return DealValueDimension(
            "not_found",
            "unknown",
            "unknown",
            value.provider,
            None,
            None,
            value.freshness,
            value.references,
            value.evidence_ids,
            ("provider_reported_not_found; price_is_unknown_not_free",),
        )
    if value.state == "unknown":
        return DealValueDimension(
            "unknown",
            "unknown",
            "unknown",
            value.provider,
            None,
            None,
            value.freshness,
            value.references,
            value.evidence_ids,
            ("deal_evidence_unknown",),
        )
    if value.freshness == "unknown":
        return DealValueDimension(
            "unknown",
            value.bucket,
            value.evidence_grade,
            value.provider,
            value.current_amount_minor,
            value.historical_low_amount_minor,
            value.freshness,
            value.references,
            value.evidence_ids,
            ("deal_evidence_freshness_unknown",),
        )
    state: Literal["supported", "stale", "expired"] = (
        "supported"
        if value.freshness == "fresh"
        else "expired"
        if value.freshness == "expired"
        else "stale"
    )
    tradeoffs = () if state == "supported" else (f"deal_evidence_{state}",)
    return DealValueDimension(
        state,
        value.bucket,
        value.evidence_grade,
        value.provider,
        value.current_amount_minor,
        value.historical_low_amount_minor,
        value.freshness,
        value.references,
        value.evidence_ids,
        tradeoffs,
    )


def _rank_key(item: RankedWishlistCandidate) -> tuple[int, int, int, int, int, int]:
    preference_known = item.preference_fit.state == "known"
    deal_supported = item.deal_value.state == "supported"
    return (
        0 if item.eligibility.state == "eligible" else 1,
        0 if preference_known else 1,
        -(item.preference_fit.score or 0) if preference_known else 0,
        _BUCKET_ORDER[item.deal_value.bucket] if deal_supported else len(_BUCKET_ORDER),
        _GRADE_ORDER[item.deal_value.evidence_grade]
        if deal_supported
        else len(_GRADE_ORDER),
        item.appid,
    )


def _merge_ids(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({value for group in groups for value in group}))


def _gate_factor(
    constraint_id: str,
    original_state: Literal["pass", "fail", "unknown"],
    evidence_ids: tuple[str, ...],
    overrides: dict[str, GateOverride],
    *,
    unknown_blocks: bool = False,
) -> tuple[EvidenceFactor, bool]:
    override = overrides.get(constraint_id)
    if override is not None and original_state == "pass":
        raise ValueError("override cannot target a passing gate")
    effective_state = "pass" if override is not None else original_state
    blocked = effective_state == "fail" or (
        effective_state == "unknown" and unknown_blocks
    )
    return (
        EvidenceFactor(
            "eligibility",
            constraint_id,
            effective_state,
            None,
            evidence_ids,
            original_state=original_state,
            override_name=None if override is None else override.name,
            override_evidence_ids=() if override is None else override.evidence_ids,
        ),
        blocked,
    )


def _validate_reference_url(provider: str, value: object) -> None:
    if not isinstance(value, str) or len(value) > 2_048:
        raise ValueError("deal reference URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("deal reference URL is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ValueError("deal reference URL is invalid")
    if provider == "gg-deals":
        valid = (
            parsed.hostname == "gg.deals"
            and not parsed.query
            and (
                _GG_PATH.fullmatch(parsed.path) is not None
                or _GG_APP_PATH.fullmatch(parsed.path) is not None
            )
        )
    elif provider == "cheapshark":
        query = parse_qs(parsed.query, keep_blank_values=True)
        valid = parsed.hostname == "www.cheapshark.com" and (
            (
                parsed.path == "/redirect"
                and set(query) == {"dealID"}
                and len(query["dealID"]) == 1
                and bool(query["dealID"][0])
            )
            or (
                parsed.path == "/search"
                and set(query) == {"steamAppID"}
                and len(query["steamAppID"]) == 1
                and query["steamAppID"][0].isdigit()
                and int(query["steamAppID"][0]) > 0
            )
        )
    else:
        valid = (
            parsed.hostname == "steamdb.info"
            and not parsed.query
            and _STEAMDB_APP_PATH.fullmatch(parsed.path) is not None
        )
    if not valid:
        raise ValueError("deal reference URL is invalid")

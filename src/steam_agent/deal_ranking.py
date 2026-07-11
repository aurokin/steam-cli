"""Pure comparison and ranking for normalized deal evidence.

This module performs no I/O and makes no provider claims beyond the normalized
records it receives.  In particular, an exact AppID lookup is not sufficient
to turn game-level evidence into an exact Steam offer comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from steam_agent.deal_evidence import (
    DealEvidenceSnapshot,
    HistoricalLowSummary,
    Money,
    OfferEvidence,
    StoreClass,
)


DEAL_EVIDENCE_SCHEMA = "deal-evidence/0.1"
MAX_BASIS_POINTS = 10_000
MAX_SIGNED_64 = (1 << 63) - 1

AttemptStatus = Literal["ready", "not_found", "failed", "unavailable"]
EvidenceGrade = Literal["exact", "normalized", "degraded", "unknown"]
DealBucket = Literal[
    "at_or_below_low",
    "within_5_percent",
    "discounted",
    "current_only",
    "noncomparable",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider: str
    fallback_rung: int
    status: AttemptStatus
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.provider or len(self.provider) > 128:
            raise ValueError("provider must be between 1 and 128 characters")
        if (
            not isinstance(self.fallback_rung, int)
            or isinstance(self.fallback_rung, bool)
            or not 0 <= self.fallback_rung <= MAX_SIGNED_64
        ):
            raise ValueError("fallback_rung must be a non-negative integer")
        if self.error_code is not None and len(self.error_code) > 128:
            raise ValueError("error_code must not exceed 128 characters")
        if self.status == "ready" and self.error_code is not None:
            raise ValueError("a ready provider attempt cannot have an error")
        if self.status in ("failed", "unavailable") and not self.error_code:
            raise ValueError("failed provider attempts require an error code")


@dataclass(frozen=True, slots=True)
class DealCandidate:
    steam_appid: int
    snapshots: tuple[DealEvidenceSnapshot, ...]
    provider_attempts: tuple[ProviderAttempt, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.steam_appid, int)
            or isinstance(self.steam_appid, bool)
            or not 1 <= self.steam_appid <= (1 << 32) - 1
        ):
            raise ValueError("steam_appid must be an unsigned 32-bit integer")
        providers = [attempt.provider for attempt in self.provider_attempts]
        if len(providers) != len(set(providers)):
            raise ValueError("provider attempts must be unique")
        attempted = set(providers)
        for snapshot in self.snapshots:
            if snapshot.provider not in attempted:
                raise ValueError("every snapshot provider must have attempt metadata")
            if snapshot.product.steam_appid != self.steam_appid:
                raise ValueError("snapshot product does not match candidate AppID")


@dataclass(frozen=True, slots=True)
class DealComparisonContext:
    country: str
    currency: str
    store_class: StoreClass
    history_scope: str

    def __post_init__(self) -> None:
        # Reuse the normalized money contract without inventing locale rules.
        Money(0, self.currency, self.country)
        if not self.history_scope or len(self.history_scope) > 128:
            raise ValueError("history_scope must be between 1 and 128 characters")


@dataclass(frozen=True, slots=True)
class RankedDealCandidate:
    steam_appid: int
    bucket: DealBucket
    evidence_grade: EvidenceGrade
    current_offer: OfferEvidence | None
    historical_low: HistoricalLowSummary | None
    discount_bps: int | None
    distance_above_low_bps: int | None
    reasons: tuple[str, ...]
    tradeoffs: tuple[str, ...]
    fallback_rung: int | None
    attempted_providers: tuple[ProviderAttempt, ...]
    used_providers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DealRanking:
    schema: Literal["deal-evidence/0.1"]
    context: DealComparisonContext
    candidates: tuple[RankedDealCandidate, ...]


def rank_deals(
    candidates: tuple[DealCandidate, ...] | list[DealCandidate],
    *,
    context: DealComparisonContext,
) -> DealRanking:
    """Retain and deterministically rank every requested candidate."""

    appids = [candidate.steam_appid for candidate in candidates]
    if len(appids) != len(set(appids)):
        raise ValueError("candidate AppIDs must be unique")
    ranked = [_rank_candidate(candidate, context) for candidate in candidates]
    ranked.sort(key=_sort_key)
    return DealRanking(DEAL_EVIDENCE_SCHEMA, context, tuple(ranked))


def _rank_candidate(
    candidate: DealCandidate, context: DealComparisonContext
) -> RankedDealCandidate:
    offers = tuple(
        offer for snapshot in candidate.snapshots for offer in snapshot.offers
    )
    lows = tuple(
        low for snapshot in candidate.snapshots for low in snapshot.history_lows
    )
    exact_pairs = _pairs(candidate.steam_appid, offers, lows, context, exact=True)
    normalized_pairs = _pairs(
        candidate.steam_appid, offers, lows, context, exact=False
    )

    if exact_pairs:
        offer, low = min(exact_pairs, key=_pair_choice_key)
        grade: EvidenceGrade = "exact"
    elif normalized_pairs:
        offer, low = min(normalized_pairs, key=_pair_choice_key)
        grade = "normalized"
    else:
        offer = _best_current(candidate.steam_appid, offers, context)
        low = None
        grade = "degraded" if offers or lows else "unknown"

    discount_bps = None if offer is None else _discount_bps(offer)
    distance_bps = (
        None if offer is None or low is None else _distance_above_low(offer.price, low.price)
    )
    reasons: list[str] = []
    tradeoffs: list[str] = []

    if offer is None:
        bucket: DealBucket = "noncomparable" if offers or lows else "unknown"
        reasons.append(
            "available_evidence_does_not_match_requested_dimensions"
            if offers or lows
            else "no_current_or_historical_evidence"
        )
    elif low is not None and offer.price.amount_minor <= low.price.amount_minor:
        bucket = "at_or_below_low"
        reasons.append("current_price_is_at_or_below_supported_low")
    elif low is not None and _within_five_percent(offer.price, low.price):
        bucket = "within_5_percent"
        reasons.append("current_price_is_within_5_percent_of_supported_low")
    elif discount_bps is not None and discount_bps > 0:
        bucket = "discounted"
        reasons.append("current_price_is_below_comparable_regular_price")
    else:
        bucket = "current_only"
        reasons.append("current_price_has_no_stronger_comparable_signal")

    if grade == "normalized":
        tradeoffs.append("product_or_offer_evidence_is_game_level_normalized")
    elif grade == "degraded":
        tradeoffs.append("requested_comparison_dimensions_are_not_fully_supported")
    elif grade == "unknown":
        tradeoffs.append("no_provider_evidence_was_available")
    if low is not None and low.price.amount_minor == 0 < offer.price.amount_minor:
        tradeoffs.append("historical_low_was_free_so_relative_distance_is_undefined")
    if offer is not None and offer.price.amount_minor == 0:
        reasons.append("current_price_is_explicitly_free")

    used = tuple(
        sorted(
            {
                evidence.provider
                for evidence in (offer, low)
                if evidence is not None
            }
        )
    )
    attempts = tuple(
        sorted(candidate.provider_attempts, key=lambda value: (value.fallback_rung, value.provider))
    )
    rung_by_provider = {attempt.provider: attempt.fallback_rung for attempt in attempts}
    fallback_rung = min((rung_by_provider[name] for name in used), default=None)
    return RankedDealCandidate(
        steam_appid=candidate.steam_appid,
        bucket=bucket,
        evidence_grade=grade,
        current_offer=offer,
        historical_low=low,
        discount_bps=discount_bps,
        distance_above_low_bps=distance_bps,
        reasons=tuple(reasons),
        tradeoffs=tuple(tradeoffs),
        fallback_rung=fallback_rung,
        attempted_providers=attempts,
        used_providers=used,
    )


def _pairs(
    appid: int,
    offers: tuple[OfferEvidence, ...],
    lows: tuple[HistoricalLowSummary, ...],
    context: DealComparisonContext,
    *,
    exact: bool,
) -> list[tuple[OfferEvidence, HistoricalLowSummary]]:
    expected = "exact_product" if exact else "normalized_game"
    return [
        (offer, low)
        for offer in offers
        for low in lows
        if offer.provider == low.provider
        and offer.comparability == expected
        and low.comparability == expected
        and _product_matches(appid, offer.product.steam_appid, offer.product.mapping)
        and _product_matches(appid, low.product.steam_appid, low.product.mapping)
        and offer.product.provider_product_id == low.product.provider_product_id
        and _money_dimensions_match(offer.price, context)
        and _money_dimensions_match(low.price, context)
        and offer.store_class == context.store_class
        and low.scope == context.history_scope
        and _scope_matches_store_class(low.scope, offer.store_class)
    ]


def _best_current(
    appid: int,
    offers: tuple[OfferEvidence, ...],
    context: DealComparisonContext,
) -> OfferEvidence | None:
    eligible = [
        offer
        for offer in offers
        if _product_matches(appid, offer.product.steam_appid, offer.product.mapping)
        and _money_dimensions_match(offer.price, context)
        and offer.store_class == context.store_class
    ]
    return min(eligible, key=_offer_choice_key, default=None)


def _product_matches(appid: int, evidence_appid: int, mapping: str) -> bool:
    return mapping == "exact" and evidence_appid == appid


def _money_dimensions_match(money: Money, context: DealComparisonContext) -> bool:
    return money.country == context.country and money.currency == context.currency


def _scope_matches_store_class(scope: str, store_class: StoreClass) -> bool:
    return (store_class, scope) in {
        ("official", "all_time_official_stores"),
        ("keyshop", "all_time_keyshops"),
        ("unknown", "all_time_any_store"),
    }


def _discount_bps(offer: OfferEvidence) -> int | None:
    regular = offer.regular_price
    if regular is None or regular.currency != offer.price.currency or regular.country != offer.price.country:
        return None
    if regular.amount_minor == 0:
        return 0 if offer.price.amount_minor == 0 else None
    if offer.price.amount_minor >= regular.amount_minor:
        return 0
    return ((regular.amount_minor - offer.price.amount_minor) * MAX_BASIS_POINTS) // regular.amount_minor


def _distance_above_low(current: Money, low: Money) -> int | None:
    if current.amount_minor <= low.amount_minor:
        return 0
    if low.amount_minor == 0:
        return None
    value = (
        (current.amount_minor - low.amount_minor) * MAX_BASIS_POINTS
    ) // low.amount_minor
    return min(value, MAX_SIGNED_64)


def _within_five_percent(current: Money, low: Money) -> bool:
    """Compare exactly; floor-rounded display bps must not admit >5%."""

    if low.amount_minor == 0:
        return current.amount_minor == 0
    return current.amount_minor * MAX_BASIS_POINTS <= low.amount_minor * 10_500


def _pair_choice_key(
    pair: tuple[OfferEvidence, HistoricalLowSummary],
) -> tuple[int, int, str, str]:
    offer, low = pair
    return (offer.price.amount_minor, low.price.amount_minor, offer.provider, offer.observed_at)


def _offer_choice_key(offer: OfferEvidence) -> tuple[int, int, str, str]:
    grade = {"exact_product": 0, "normalized_game": 1, "unknown": 2}[offer.comparability]
    return (grade, offer.price.amount_minor, offer.provider, offer.observed_at)


def _sort_key(candidate: RankedDealCandidate) -> tuple[int, int, int, int, int]:
    grade = {"exact": 0, "normalized": 1, "degraded": 2, "unknown": 3}
    bucket = {
        "at_or_below_low": 0,
        "within_5_percent": 1,
        "discounted": 2,
        "current_only": 3,
        "noncomparable": 4,
        "unknown": 5,
    }
    discount = candidate.discount_bps if candidate.discount_bps is not None else -1
    distance = (
        candidate.distance_above_low_bps
        if candidate.distance_above_low_bps is not None
        else (1 << 63) - 1
    )
    return (
        grade[candidate.evidence_grade],
        bucket[candidate.bucket],
        -discount,
        distance,
        candidate.steam_appid,
    )


__all__ = [
    "DEAL_EVIDENCE_SCHEMA",
    "DealCandidate",
    "DealComparisonContext",
    "DealRanking",
    "ProviderAttempt",
    "RankedDealCandidate",
    "rank_deals",
]

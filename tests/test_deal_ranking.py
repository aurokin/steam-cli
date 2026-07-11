from __future__ import annotations

import pytest

from steam_agent.deal_evidence import (
    DealEvidenceSnapshot,
    HistoricalLowSummary,
    ManualReference,
    Money,
    OfferEvidence,
    ProductIdentity,
)
from steam_agent.deal_ranking import (
    DEAL_EVIDENCE_SCHEMA,
    DealCandidate,
    DealComparisonContext,
    ProviderAttempt,
    rank_deals,
)


NOW = "2026-07-11T12:00:00Z"
REF = ManualReference("https://gg.deals/game/synthetic/", "attribution")
US_OFFICIAL = DealComparisonContext(
    country="US",
    currency="USD",
    store_class="official",
    history_scope="all_time_official_stores",
)


def snapshot(
    appid: int,
    *,
    provider: str = "provider",
    current: int | None,
    low: int | None,
    regular: int | None = None,
    comparability: str = "exact_product",
    country: str = "US",
    currency: str = "USD",
    store_class: str = "official",
    scope: str = "all_time_official_stores",
    product_id: str | None = None,
) -> DealEvidenceSnapshot:
    product = ProductIdentity(product_id or f"steam/app/{appid}", appid)
    offer = (
        ()
        if current is None
        else (
            OfferEvidence(
                provider=provider,
                product=product,
                price=Money(current, currency, country),
                regular_price=(
                    None if regular is None else Money(regular, currency, country)
                ),
                discount_percent=None,
                store_class=store_class,  # type: ignore[arg-type]
                observed_at=NOW,
                provider_url=REF,
                comparability=comparability,  # type: ignore[arg-type]
            ),
        )
    )
    history = (
        ()
        if low is None
        else (
            HistoricalLowSummary(
                provider=provider,
                product=product,
                price=Money(low, currency, country),
                observed_at=NOW,
                effective_at=None,
                scope=scope,
                provider_url=REF,
                comparability=comparability,  # type: ignore[arg-type]
            ),
        )
    )
    return DealEvidenceSnapshot(provider, product, offer, history, NOW, ())


def candidate(
    appid: int,
    *snapshots: DealEvidenceSnapshot,
    attempts: tuple[ProviderAttempt, ...] | None = None,
) -> DealCandidate:
    attempts = attempts or tuple(
        ProviderAttempt(provider, index, "ready")
        for index, provider in enumerate(
            dict.fromkeys(item.provider for item in snapshots)
        )
    )
    return DealCandidate(appid, tuple(snapshots), attempts)


def test_golden_ranking_preserves_all_candidates_and_ordering_contract() -> None:
    values = [
        candidate(50),
        candidate(40, snapshot(40, current=500, low=None, regular=1_000)),
        candidate(30, snapshot(30, current=600, low=500, regular=1_000)),
        candidate(20, snapshot(20, current=520, low=500)),
        candidate(10, snapshot(10, current=500, low=500)),
        candidate(
            5,
            snapshot(
                5,
                provider="cheapshark",
                current=100,
                low=50,
                comparability="normalized_game",
                store_class="unknown",
                scope="all_time_any_store",
                product_id="cheapshark-game-5",
            ),
        ),
    ]

    result = rank_deals(values, context=US_OFFICIAL)

    assert result.schema == DEAL_EVIDENCE_SCHEMA == "deal-evidence/0.1"
    assert [item.steam_appid for item in result.candidates] == [10, 20, 30, 40, 5, 50]
    assert [item.bucket for item in result.candidates] == [
        "at_or_below_low",
        "within_5_percent",
        "discounted",
        "discounted",
        "noncomparable",
        "unknown",
    ]
    assert result.candidates[2].discount_bps == 4_000
    assert result.candidates[1].distance_above_low_bps == 400
    cheapshark = result.candidates[4]
    assert cheapshark.evidence_grade == "degraded"
    assert cheapshark.used_providers == ()
    assert "requested_comparison_dimensions_are_not_fully_supported" in cheapshark.tradeoffs


@pytest.mark.parametrize(
    ("change", "expected_bucket"),
    [
        ({"country": "CA"}, "noncomparable"),
        ({"currency": "CAD"}, "noncomparable"),
        ({"store_class": "keyshop"}, "noncomparable"),
        ({"scope": "all_time_keyshops"}, "current_only"),
    ],
)
def test_each_comparison_dimension_is_checked(
    change: dict[str, str], expected_bucket: str
) -> None:
    evidence = snapshot(10, current=500, low=500, **change)
    result = rank_deals([candidate(10, evidence)], context=US_OFFICIAL).candidates[0]
    assert result.bucket == expected_bucket
    assert result.evidence_grade == "degraded"


def test_provider_and_provider_product_mismatches_do_not_form_exact_pair() -> None:
    current = snapshot(10, provider="current", current=500, low=None)
    low = snapshot(10, provider="history", current=None, low=500)
    mismatched_product = snapshot(
        10, current=None, low=500, product_id="different-product"
    )

    cross_provider = rank_deals(
        [
            candidate(
                10,
                current,
                low,
                attempts=(
                    ProviderAttempt("current", 0, "ready"),
                    ProviderAttempt("history", 1, "ready"),
                ),
            )
        ],
        context=US_OFFICIAL,
    ).candidates[0]
    cross_product = rank_deals(
        [candidate(10, snapshot(10, current=500, low=None), mismatched_product)],
        context=US_OFFICIAL,
    ).candidates[0]

    assert cross_provider.bucket == "current_only"
    assert cross_provider.evidence_grade == "degraded"
    assert cross_product.bucket == "current_only"


def test_normalized_evidence_can_rank_but_never_becomes_exact() -> None:
    normalized = snapshot(
        10, current=500, low=500, comparability="normalized_game"
    )
    exact = snapshot(20, current=520, low=500)
    result = rank_deals(
        [candidate(10, normalized), candidate(20, exact)], context=US_OFFICIAL
    )

    assert [item.steam_appid for item in result.candidates] == [20, 10]
    assert result.candidates[1].bucket == "at_or_below_low"
    assert result.candidates[1].evidence_grade == "normalized"


def test_zero_is_explicit_free_while_missing_remains_unknown_and_no_division_occurs() -> None:
    free = rank_deals(
        [candidate(10, snapshot(10, current=0, low=0, regular=0))],
        context=US_OFFICIAL,
    ).candidates[0]
    above_free_low = rank_deals(
        [candidate(20, snapshot(20, current=1, low=0, regular=2))],
        context=US_OFFICIAL,
    ).candidates[0]
    missing = rank_deals([candidate(30)], context=US_OFFICIAL).candidates[0]

    assert free.bucket == "at_or_below_low"
    assert free.current_offer is not None and free.current_offer.price.amount_minor == 0
    assert "current_price_is_explicitly_free" in free.reasons
    assert free.discount_bps == 0
    assert above_free_low.distance_above_low_bps is None
    assert above_free_low.discount_bps == 5_000
    assert "historical_low_was_free_so_relative_distance_is_undefined" in above_free_low.tradeoffs
    assert missing.bucket == "unknown" and missing.current_offer is None


def test_basis_point_math_handles_64_bit_money_without_float_or_overflow() -> None:
    maximum = (1 << 63) - 1
    ranked = rank_deals(
        [
            candidate(
                10,
                snapshot(
                    10,
                    current=maximum - 1,
                    low=maximum - 2,
                    regular=maximum,
                ),
            )
        ],
        context=US_OFFICIAL,
    ).candidates[0]

    assert ranked.discount_bps == 0
    assert ranked.distance_above_low_bps == 0
    assert isinstance(ranked.discount_bps, int)

    saturated = rank_deals(
        [candidate(20, snapshot(20, current=maximum, low=1))],
        context=US_OFFICIAL,
    ).candidates[0]
    assert saturated.distance_above_low_bps == maximum


def test_thresholds_ties_and_fallback_metadata_are_deterministic() -> None:
    attempts = (
        ProviderAttempt("fallback", 2, "ready"),
        ProviderAttempt("primary", 0, "unavailable", "AUTH_REQUIRED"),
    )
    tied = [
        candidate(
            appid,
            snapshot(appid, provider="fallback", current=105, low=100),
            attempts=attempts,
        )
        for appid in (30, 10, 20)
    ]
    result = rank_deals(tied, context=US_OFFICIAL)

    assert [item.steam_appid for item in result.candidates] == [10, 20, 30]
    assert all(item.bucket == "within_5_percent" for item in result.candidates)
    assert all(item.distance_above_low_bps == 500 for item in result.candidates)
    assert all(item.fallback_rung == 2 for item in result.candidates)
    assert all(item.used_providers == ("fallback",) for item in result.candidates)
    assert all(
        [attempt.provider for attempt in item.attempted_providers]
        == ["primary", "fallback"]
        for item in result.candidates
    )


def test_floor_rounded_distance_just_above_five_percent_is_not_within_bucket() -> None:
    item = rank_deals(
        [candidate(10, snapshot(10, current=105_009, low=100_000))],
        context=US_OFFICIAL,
    ).candidates[0]

    assert item.distance_above_low_bps == 500
    assert item.bucket == "current_only"


@pytest.mark.parametrize("current", [0, 1, 99, 100, 101, 105, 106, 10_000])
def test_integer_distance_is_monotonic_around_bucket_boundaries(current: int) -> None:
    item = rank_deals(
        [candidate(10, snapshot(10, current=current, low=100))],
        context=US_OFFICIAL,
    ).candidates[0]
    if current <= 100:
        assert item.bucket == "at_or_below_low"
        assert item.distance_above_low_bps == 0
    elif current <= 105:
        assert item.bucket == "within_5_percent"
        assert item.distance_above_low_bps is not None
        assert item.distance_above_low_bps <= 500
    else:
        assert item.bucket == "current_only"
        assert item.distance_above_low_bps is not None
        assert item.distance_above_low_bps > 500


def test_invalid_candidate_metadata_is_rejected() -> None:
    evidence = snapshot(10, provider="unattempted", current=1, low=1)
    with pytest.raises(ValueError, match="snapshot provider"):
        DealCandidate(10, (evidence,), ())
    with pytest.raises(ValueError, match="unique"):
        rank_deals(
            [candidate(10), candidate(10)],
            context=US_OFFICIAL,
        )

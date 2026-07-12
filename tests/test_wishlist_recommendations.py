from datetime import datetime, timedelta, timezone

import pytest

from steam_agent.wishlist_recommendations import (
    DealDimension,
    DealReference,
    DirectFeedback,
    GateOverride,
    ProfileRule,
    ReviewSummary,
    TraitAssertion,
    WishlistCandidate,
    WishlistFitContext,
    rank_wishlist,
)


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def context(**changes: object) -> WishlistFitContext:
    values = dict(
        country="US",
        currency="USD",
        store_class="official",
        unknown_policy="include",
        generated_at=NOW,
    )
    values.update(changes)
    return WishlistFitContext(**values)  # type: ignore[arg-type]


def deal(**changes: object) -> DealDimension:
    values = dict(
        schema="deal-evidence/0.1",
        state="ready",
        bucket="at_or_below_low",
        evidence_grade="exact",
        provider="gg-deals",
        store_class="official",
        country="US",
        currency="USD",
        freshness="fresh",
        current_amount_minor=500,
        historical_low_amount_minor=500,
        observed_at=NOW,
        references=(DealReference("gg-deals", "https://gg.deals/game/example/"),),
        evidence_ids=("deal:1",),
    )
    values.update(changes)
    return DealDimension(**values)  # type: ignore[arg-type]


def candidate(appid: int, **changes: object) -> WishlistCandidate:
    values = dict(appid=appid, name=f"Game {appid}")
    values.update(changes)
    return WishlistCandidate(**values)  # type: ignore[arg-type]


def test_direct_like_outranks_better_deal_without_mixing_dimensions() -> None:
    liked = candidate(
        2,
        feedback=DirectFeedback(rating="liked", evidence_ids=("feedback:2",)),
        deal=deal(bucket="current_only", current_amount_minor=2000),
    )
    cheaper = candidate(
        1, deal=deal(bucket="at_or_below_low", current_amount_minor=100)
    )
    result = rank_wishlist((cheaper, liked), rules=(), context=context())

    assert [item.appid for item in result.ranked] == [2, 1]
    assert result.ranked[0].preference_fit.score == 100
    assert result.ranked[0].deal_value.bucket == "current_only"
    assert result.ranked[1].preference_fit.state == "unknown"
    assert all(
        factor.dimension == "preference_fit"
        for factor in result.ranked[0].preference_fit.factors
    )


def test_all_unknown_is_degraded_and_not_a_purchase_recommendation() -> None:
    result = rank_wishlist(
        (candidate(2, deal=deal()), candidate(1)), rules=(), context=context()
    )
    assert result.status == "degraded"
    assert result.degradation_reasons == ("insufficient_preference_evidence",)
    assert result.purchase_recommendation_supported is False
    assert [item.appid for item in result.ranked] == [2, 1]


def test_hard_avoid_and_snooze_are_applied_before_ranking() -> None:
    avoided = candidate(
        1,
        feedback=DirectFeedback(
            traits=(TraitAssertion("user:horror", "present", ("trait:1",)),)
        ),
    )
    snoozed = candidate(
        2,
        feedback=DirectFeedback(
            rating="liked",
            snoozed_until=NOW + timedelta(days=1),
            evidence_ids=("snooze:2",),
        ),
    )
    allowed = candidate(3)
    rule = ProfileRule("user:horror", "avoid", "hard", 100, ("rule:horror",))
    result = rank_wishlist(
        (avoided, snoozed, allowed), rules=(rule,), context=context()
    )

    assert [item.appid for item in result.ranked] == [3]
    assert [item.appid for item in result.excluded] == [1, 2]
    assert result.excluded[0].eligibility.factors[0].state == "fail"
    assert result.excluded[1].eligibility.factors[0].rule_id == "active_snooze"


def test_unknown_hard_requirement_respects_three_valued_policy() -> None:
    rule = ProfileRule("user:coop", "require", "hard", 50)
    included = rank_wishlist(
        (candidate(1),), rules=(rule,), context=context(unknown_policy="include")
    )
    excluded = rank_wishlist(
        (candidate(1),), rules=(rule,), context=context(unknown_policy="exclude")
    )
    assert included.ranked[0].eligibility.state == "conditional"
    assert included.ranked[0].eligibility.factors[0].state == "unknown"
    assert excluded.excluded[0].eligibility.state == "excluded"


def test_named_overrides_preserve_original_and_effective_gate_lineage() -> None:
    value = candidate(
        1,
        feedback=DirectFeedback(
            hard_exclude=True,
            snoozed_until=NOW + timedelta(days=1),
            evidence_ids=("feedback:1",),
            traits=(TraitAssertion("user:horror", "present", ("trait:1",)),),
        ),
    )
    overrides = (
        GateOverride(
            "override:include-explicit", 1, "explicit_hard_exclude", ("request:1",)
        ),
        GateOverride("override:include-snooze", 1, "active_snooze", ("request:2",)),
        GateOverride(
            "override:include-horror", 1, "hard_avoid:user:horror", ("request:3",)
        ),
    )
    result = rank_wishlist(
        (value,),
        rules=(ProfileRule("user:horror", "avoid", "hard", 10, ("rule:1",)),),
        context=context(),
        overrides=overrides,
    )

    assert result.ranked[0].eligibility.state == "eligible"
    assert [
        factor.original_state for factor in result.ranked[0].eligibility.factors
    ] == [
        "fail",
        "fail",
        "fail",
    ]
    assert all(
        factor.state == "pass" for factor in result.ranked[0].eligibility.factors
    )
    assert [
        factor.override_name for factor in result.ranked[0].eligibility.factors
    ] == [
        "override:include-explicit",
        "override:include-snooze",
        "override:include-horror",
    ]
    assert result.ranked[0].eligibility.factors[2].evidence_ids == ("rule:1", "trait:1")
    assert result.ranked[0].eligibility.factors[2].override_evidence_ids == (
        "request:3",
    )


def test_override_can_include_unknown_gate_under_exclude_policy() -> None:
    rule = ProfileRule("user:coop", "require", "hard", 50, ("rule:coop",))
    result = rank_wishlist(
        (candidate(1),),
        rules=(rule,),
        context=context(unknown_policy="exclude"),
        overrides=(
            GateOverride(
                "override:accept-unknown", 1, "hard_require:user:coop", ("request:1",)
            ),
        ),
    )
    factor = result.ranked[0].eligibility.factors[0]
    assert factor.original_state == "unknown"
    assert factor.state == "pass"
    assert result.ranked[0].eligibility.state == "eligible"


@pytest.mark.parametrize(
    "overrides",
    [
        (GateOverride("override:missing-target", 2, "active_snooze", ("request:1",)),),
        (GateOverride("override:missing-gate", 1, "active_snooze", ("request:1",)),),
        (
            GateOverride("override:one", 1, "active_snooze", ("request:1",)),
            GateOverride("override:two", 1, "active_snooze", ("request:2",)),
        ),
    ],
)
def test_override_target_and_constraint_validation(
    overrides: tuple[GateOverride, ...],
) -> None:
    with pytest.raises(ValueError, match="override"):
        rank_wishlist((candidate(1),), rules=(), context=context(), overrides=overrides)


def test_stale_or_missing_price_never_wins_deal_tie_break() -> None:
    stale = candidate(
        1, feedback=DirectFeedback(rating="liked"), deal=deal(freshness="stale")
    )
    fresh = candidate(
        2, feedback=DirectFeedback(rating="liked"), deal=deal(bucket="discounted")
    )
    missing = candidate(3, feedback=DirectFeedback(rating="liked"))
    result = rank_wishlist((stale, missing, fresh), rules=(), context=context())
    assert [item.appid for item in result.ranked] == [2, 1, 3]
    assert result.ranked[1].deal_value.state == "stale"
    assert result.ranked[1].stale == ("deal_value",)


def test_review_counts_are_report_only_and_never_user_taste() -> None:
    popular = candidate(
        1,
        review=ReviewSummary("valve", 999_999, 1_000_000, NOW, "fresh", ("review:1",)),
    )
    obscure = candidate(
        2, review=ReviewSummary("valve", 1, 1, NOW, "fresh", ("review:2",))
    )
    result = rank_wishlist((obscure, popular), rules=(), context=context())
    assert [item.appid for item in result.ranked] == [1, 2]
    assert all(item.preference_fit.state == "unknown" for item in result.ranked)
    assert (
        result.ranked[0].review is not None
        and result.ranked[0].review.total == 1_000_000
    )


def test_unknown_review_freshness_is_report_only_and_missing_for_completeness() -> None:
    value = candidate(
        1,
        review=ReviewSummary("valve", 8, 10, NOW, "unknown", ("review:1",)),
    )
    result = rank_wishlist((value,), rules=(), context=context())
    assert result.ranked[0].review is not None
    assert result.ranked[0].review.freshness == "unknown"
    assert "review" in result.ranked[0].missing


def test_m3_not_found_is_unknown_price_not_free() -> None:
    missing = deal(
        state="not_found",
        bucket="unknown",
        evidence_grade="unknown",
        current_amount_minor=None,
        historical_low_amount_minor=None,
    )
    result = rank_wishlist((candidate(1, deal=missing),), rules=(), context=context())
    dimension = result.ranked[0].deal_value
    assert dimension.state == "not_found"
    assert dimension.current_amount_minor is None
    assert dimension.tradeoffs == (
        "provider_reported_not_found; price_is_unknown_not_free",
    )


def test_m3_noncomparable_is_never_supported_even_when_context_matches() -> None:
    unsupported = deal(
        bucket="noncomparable",
        evidence_grade="degraded",
        current_amount_minor=None,
        historical_low_amount_minor=None,
    )
    item = rank_wishlist(
        (candidate(1, deal=unsupported),), rules=(), context=context()
    ).ranked[0]
    assert item.deal_value.state == "noncomparable"
    assert item.deal_value.bucket == "noncomparable"
    assert item.missing == (
        "compatibility",
        "deal_value",
        "preference_fit",
        "release",
        "review",
    )


@pytest.mark.parametrize(
    "change", [{"store_class": "keyshop"}, {"currency": "EUR"}, {"country": "GB"}]
)
def test_mismatched_deal_context_is_noncomparable(change: dict[str, str]) -> None:
    result = rank_wishlist(
        (candidate(1, deal=deal(**change)),), rules=(), context=context()
    )
    assert result.ranked[0].deal_value.state == "noncomparable"
    assert result.ranked[0].deal_value.bucket == "noncomparable"


def test_null_title_is_preserved_and_appid_is_stable_tie_break() -> None:
    result = rank_wishlist(
        (candidate(9, name=None), candidate(3, name=None)), rules=(), context=context()
    )
    assert [(item.appid, item.name) for item in result.ranked] == [(3, None), (9, None)]


def test_input_order_does_not_change_result() -> None:
    values = (
        candidate(3, deal=deal(bucket="discounted")),
        candidate(1, feedback=DirectFeedback(rating="liked")),
        candidate(2, deal=deal(bucket="current_only")),
    )
    forward = rank_wishlist(values, rules=(), context=context())
    reverse = rank_wishlist(tuple(reversed(values)), rules=(), context=context())
    assert forward == reverse


def test_soft_traits_are_the_only_profile_rule_score_input() -> None:
    rule = ProfileRule("user:short", "prefer", "soft", 40, ("rule:short",))
    explicit = candidate(
        1,
        feedback=DirectFeedback(
            traits=(TraitAssertion("user:short", "present", ("trait:short",)),)
        ),
    )
    unknown = candidate(2)
    result = rank_wishlist((unknown, explicit), rules=(rule,), context=context())
    assert result.ranked[0].preference_fit.score == 40
    assert result.ranked[0].preference_fit.factors[0].evidence_ids == (
        "rule:short",
        "trait:short",
    )
    assert result.ranked[1].preference_fit.state == "unknown"


def test_release_and_compatibility_remain_unknown() -> None:
    item = rank_wishlist((candidate(1),), rules=(), context=context()).ranked[0]
    assert item.release.state == "unknown"
    assert item.compatibility.state == "unknown"
    assert item.missing == (
        "compatibility",
        "deal_value",
        "preference_fit",
        "release",
        "review",
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: candidate(0),
        lambda: deal(current_amount_minor=1 << 63),
        lambda: ReviewSummary("valve", 2, 1, NOW, "fresh"),
        lambda: ProfileRule("bad trait", "prefer", "soft", 1),
        lambda: DirectFeedback(
            traits=(
                TraitAssertion("user:coop", "present"),
                TraitAssertion("user:coop", "absent"),
            )
        ),
    ],
)
def test_bounds_and_validation(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "untrusted"},
        {"bucket": "at_or_below_low", "historical_low_amount_minor": None},
        {
            "bucket": "within_5_percent",
            "current_amount_minor": 106,
            "historical_low_amount_minor": 100,
        },
        {
            "bucket": "noncomparable",
            "evidence_grade": "exact",
            "current_amount_minor": None,
            "historical_low_amount_minor": None,
        },
        {
            "bucket": "noncomparable",
            "evidence_grade": "degraded",
            "current_amount_minor": 1,
            "historical_low_amount_minor": None,
        },
        {
            "bucket": "unknown",
            "current_amount_minor": None,
            "historical_low_amount_minor": None,
        },
        {"freshness": "unknown"},
        {
            "state": "unknown",
            "bucket": "unknown",
            "evidence_grade": "unknown",
            "current_amount_minor": None,
            "historical_low_amount_minor": None,
        },
    ],
)
def test_impossible_deal_state_matrix_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        deal(**changes)


@pytest.mark.parametrize(
    "reference",
    [
        DealReference("gg-deals", "https://gg.deals/game/example/"),
        DealReference("gg-deals", "https://gg.deals/steam/app/10/"),
        DealReference(
            "cheapshark", "https://www.cheapshark.com/redirect?dealID=safe-token"
        ),
        DealReference("cheapshark", "https://www.cheapshark.com/search?steamAppID=10"),
        DealReference("steamdb", "https://steamdb.info/app/10/"),
    ],
)
def test_safe_manual_reference_matrix(reference: DealReference) -> None:
    assert reference.url.startswith("https://")


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("gg-deals", "https://evil.example/game/example/"),
        ("gg-deals", "https://user@gg.deals/game/example/"),
        ("gg-deals", "https://gg.deals/game/example/?key=secret"),
        ("cheapshark", "https://www.cheapshark.com/redirect?dealID=x&next=evil"),
        ("cheapshark", "https://www.cheapshark.com/search?steamAppID=0"),
        ("steamdb", "https://steamdb.info/app/10/?query=x"),
    ],
)
def test_malicious_or_unscoped_reference_is_rejected(provider: str, url: str) -> None:
    with pytest.raises(ValueError, match="reference URL"):
        DealReference(provider, url)  # type: ignore[arg-type]


def test_empty_and_all_excluded_are_distinct_degraded_results() -> None:
    empty = rank_wishlist((), rules=(), context=context())
    excluded = rank_wishlist(
        (
            candidate(
                1,
                feedback=DirectFeedback(
                    hard_exclude=True, evidence_ids=("feedback:1",)
                ),
            ),
        ),
        rules=(),
        context=context(),
    )
    for result in (empty, excluded):
        assert result.status == "degraded"
        assert result.degradation_reasons == ("no_eligible_candidates",)
        assert result.purchase_recommendation_supported is False


def test_duplicate_appids_and_rules_are_rejected() -> None:
    with pytest.raises(ValueError, match="AppIDs"):
        rank_wishlist((candidate(1), candidate(1)), rules=(), context=context())
    rule = ProfileRule("user:coop", "avoid", "soft", 10)
    with pytest.raises(ValueError, match="rules"):
        rank_wishlist((candidate(1),), rules=(rule, rule), context=context())

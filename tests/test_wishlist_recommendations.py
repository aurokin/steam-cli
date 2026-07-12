from datetime import datetime, timedelta, timezone

import pytest

from steam_agent.wishlist_recommendations import (
    DealDimension,
    DirectFeedback,
    ProfileRule,
    ReviewSummary,
    TraitAssertion,
    WishlistCandidate,
    WishlistFitContext,
    rank_wishlist,
)


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def context(**changes: object) -> WishlistFitContext:
    values = dict(country="US", currency="USD", store_class="official", unknown_policy="include", generated_at=NOW)
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
        references=("https://gg.deals/game/example/",),
        evidence_ids=("deal:1",),
    )
    values.update(changes)
    return DealDimension(**values)  # type: ignore[arg-type]


def candidate(appid: int, **changes: object) -> WishlistCandidate:
    values = dict(appid=appid, name=f"Game {appid}")
    values.update(changes)
    return WishlistCandidate(**values)  # type: ignore[arg-type]


def test_direct_like_outranks_better_deal_without_mixing_dimensions() -> None:
    liked = candidate(2, feedback=DirectFeedback(rating="liked", evidence_ids=("feedback:2",)), deal=deal(bucket="current_only", current_amount_minor=2000))
    cheaper = candidate(1, deal=deal(bucket="at_or_below_low", current_amount_minor=100))
    result = rank_wishlist((cheaper, liked), rules=(), context=context())

    assert [item.appid for item in result.ranked] == [2, 1]
    assert result.ranked[0].preference_fit.score == 100
    assert result.ranked[0].deal_value.bucket == "current_only"
    assert result.ranked[1].preference_fit.state == "unknown"
    assert all(factor.dimension == "preference_fit" for factor in result.ranked[0].preference_fit.factors)


def test_all_unknown_is_degraded_and_not_a_purchase_recommendation() -> None:
    result = rank_wishlist((candidate(2, deal=deal()), candidate(1)), rules=(), context=context())
    assert result.status == "degraded"
    assert result.degradation_reasons == ("insufficient_preference_evidence",)
    assert result.purchase_recommendation_supported is False
    assert [item.appid for item in result.ranked] == [2, 1]


def test_hard_avoid_and_snooze_are_applied_before_ranking() -> None:
    avoided = candidate(1, feedback=DirectFeedback(traits=(TraitAssertion("user:horror", "present", ("trait:1",)),)))
    snoozed = candidate(2, feedback=DirectFeedback(rating="liked", snoozed_until=NOW + timedelta(days=1), evidence_ids=("snooze:2",)))
    allowed = candidate(3)
    rule = ProfileRule("user:horror", "avoid", "hard", 100, ("rule:horror",))
    result = rank_wishlist((avoided, snoozed, allowed), rules=(rule,), context=context())

    assert [item.appid for item in result.ranked] == [3]
    assert [item.appid for item in result.excluded] == [1, 2]
    assert result.excluded[0].eligibility.factors[0].state == "fail"
    assert result.excluded[1].eligibility.factors[0].rule_id == "active_snooze"


def test_unknown_hard_requirement_respects_three_valued_policy() -> None:
    rule = ProfileRule("user:coop", "require", "hard", 50)
    included = rank_wishlist((candidate(1),), rules=(rule,), context=context(unknown_policy="include"))
    excluded = rank_wishlist((candidate(1),), rules=(rule,), context=context(unknown_policy="exclude"))
    assert included.ranked[0].eligibility.state == "conditional"
    assert included.ranked[0].eligibility.factors[0].state == "unknown"
    assert excluded.excluded[0].eligibility.state == "excluded"


def test_stale_or_missing_price_never_wins_deal_tie_break() -> None:
    stale = candidate(1, feedback=DirectFeedback(rating="liked"), deal=deal(freshness="stale"))
    fresh = candidate(2, feedback=DirectFeedback(rating="liked"), deal=deal(bucket="discounted"))
    missing = candidate(3, feedback=DirectFeedback(rating="liked"))
    result = rank_wishlist((stale, missing, fresh), rules=(), context=context())
    assert [item.appid for item in result.ranked] == [2, 1, 3]
    assert result.ranked[1].deal_value.state == "stale"
    assert result.ranked[1].stale == ("deal_value",)


def test_review_counts_are_report_only_and_never_user_taste() -> None:
    popular = candidate(1, review=ReviewSummary("valve", 999_999, 1_000_000, NOW, "fresh", ("review:1",)))
    obscure = candidate(2, review=ReviewSummary("valve", 1, 1, NOW, "fresh", ("review:2",)))
    result = rank_wishlist((obscure, popular), rules=(), context=context())
    assert [item.appid for item in result.ranked] == [1, 2]
    assert all(item.preference_fit.state == "unknown" for item in result.ranked)
    assert result.ranked[0].review is not None and result.ranked[0].review.total == 1_000_000


def test_m3_not_found_is_unknown_price_not_free() -> None:
    missing = deal(state="not_found", bucket="unknown", evidence_grade="unknown", current_amount_minor=None, historical_low_amount_minor=None)
    result = rank_wishlist((candidate(1, deal=missing),), rules=(), context=context())
    dimension = result.ranked[0].deal_value
    assert dimension.state == "not_found"
    assert dimension.current_amount_minor is None
    assert dimension.tradeoffs == ("provider_reported_not_found; price_is_unknown_not_free",)


@pytest.mark.parametrize("change", [{"store_class": "keyshop"}, {"currency": "EUR"}, {"country": "GB"}])
def test_mismatched_deal_context_is_noncomparable(change: dict[str, str]) -> None:
    result = rank_wishlist((candidate(1, deal=deal(**change)),), rules=(), context=context())
    assert result.ranked[0].deal_value.state == "noncomparable"
    assert result.ranked[0].deal_value.bucket == "noncomparable"


def test_null_title_is_preserved_and_appid_is_stable_tie_break() -> None:
    result = rank_wishlist((candidate(9, name=None), candidate(3, name=None)), rules=(), context=context())
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
    explicit = candidate(1, feedback=DirectFeedback(traits=(TraitAssertion("user:short", "present", ("trait:short",)),)))
    unknown = candidate(2)
    result = rank_wishlist((unknown, explicit), rules=(rule,), context=context())
    assert result.ranked[0].preference_fit.score == 40
    assert result.ranked[0].preference_fit.factors[0].evidence_ids == ("rule:short", "trait:short")
    assert result.ranked[1].preference_fit.state == "unknown"


def test_release_and_compatibility_remain_unknown() -> None:
    item = rank_wishlist((candidate(1),), rules=(), context=context()).ranked[0]
    assert item.release.state == "unknown"
    assert item.compatibility.state == "unknown"
    assert item.missing == ("compatibility", "deal_value", "preference_fit", "release", "review")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: candidate(0),
        lambda: deal(current_amount_minor=1 << 63),
        lambda: ReviewSummary("valve", 2, 1, NOW, "fresh"),
        lambda: ProfileRule("bad trait", "prefer", "soft", 1),
        lambda: DirectFeedback(traits=(TraitAssertion("user:coop", "present"), TraitAssertion("user:coop", "absent"))),
    ],
)
def test_bounds_and_validation(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


def test_duplicate_appids_and_rules_are_rejected() -> None:
    with pytest.raises(ValueError, match="AppIDs"):
        rank_wishlist((candidate(1), candidate(1)), rules=(), context=context())
    rule = ProfileRule("user:coop", "avoid", "soft", 10)
    with pytest.raises(ValueError, match="rules"):
        rank_wishlist((candidate(1),), rules=(rule, rule), context=context())

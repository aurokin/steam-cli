from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from steam_agent.recommendations import (
    MAX_APPID,
    MAX_INT64,
    RECIPE_SCORING_TABLE,
    ActivityEvidence,
    AchievementEvidence,
    ConstraintOverride,
    ExplicitFeedback,
    ProfileRule,
    RecommendationCandidate,
    RecommendationContext,
    Requirement,
    TraitAssertion,
    _safe_sum,
    rank_recommendations,
)


NOW = datetime(2030, 1, 15, 12, tzinfo=timezone.utc)


def activity(
    *,
    recent: int | None = 60,
    lifetime: int | None = 600,
    days_ago: int = 2,
    freshness: str = "fresh",
) -> ActivityEvidence:
    return ActivityEvidence(
        freshness=freshness,  # type: ignore[arg-type]
        observed_at=NOW - timedelta(hours=1),
        lifetime_minutes=lifetime,
        recent_window_minutes=recent,
        last_played_at=NOW - timedelta(days=days_ago),
        evidence_ids=("activity:1",),
    )


def feedback(
    *,
    rating: str | None = None,
    state: str | None = "active",
    snooze: datetime | None = None,
    remaining: int | None = None,
    traits: tuple[TraitAssertion, ...] = (),
) -> ExplicitFeedback:
    return ExplicitFeedback(
        rating=rating,  # type: ignore[arg-type]
        play_state=state,  # type: ignore[arg-type]
        snoozed_until=snooze,
        remaining_minutes=remaining,
        traits=traits,
        evidence_ids=("feedback:1",),
    )


def candidate(
    appid: int,
    *,
    owned: bool | None = True,
    installed: bool | None = True,
    act: ActivityEvidence | None = None,
    achievements: AchievementEvidence | None = None,
    fb: ExplicitFeedback | None = None,
) -> RecommendationCandidate:
    return RecommendationCandidate(
        appid, f"Game {appid}", owned, installed, act, achievements,
        feedback() if fb is None else fb,
    )


def context(recipe: str = "preference-fit/0.1", **kwargs: object) -> RecommendationContext:
    return RecommendationContext(recipe=recipe, now=NOW, **kwargs)  # type: ignore[arg-type]


def test_r01_resume_orders_momentum_and_excludes_explicitly_finished() -> None:
    result = rank_recommendations(
        (
            candidate(1102, act=activity(recent=0, days_ago=100)),
            candidate(1103, act=activity(), fb=feedback(state="finished")),
            candidate(1101, act=activity()),
        ),
        context=context("resume/0.1"),
    )
    assert [item.appid for item in result.eligible] == [1101, 1102]
    assert [item.appid for item in result.excluded] == [1103]
    assert "recent_sustained_play" in result.eligible[0].positive_factors


def test_r02_finishability_applies_time_gate_and_unknown_policy() -> None:
    result = rank_recommendations(
        (
            candidate(1203, fb=ExplicitFeedback(play_state="active", minimum_session_minutes=0)),
            candidate(1202, fb=ExplicitFeedback(play_state="active", minimum_session_minutes=0, remaining_minutes=600)),
            candidate(1201, fb=ExplicitFeedback(play_state="active", minimum_session_minutes=0, remaining_minutes=240)),
        ),
        context=context("finishability/0.1", time_minutes=360, unknown_policy="include"),
    )
    assert result.eligible[0].appid == 1201
    assert result.excluded[0].appid == 1202
    assert result.conditional[0].appid == 1203


def test_r03_installed_is_a_hard_gate_before_fit() -> None:
    result = rank_recommendations(
        (
            candidate(1302, installed=False, fb=feedback(rating="liked")),
            candidate(1301, installed=True, fb=feedback(rating="neutral")),
        ),
        context=context(requirements=(Requirement("installed", True),)),
    )
    assert result.eligible[0].appid == 1301
    installed = next(item for item in result.excluded[0].gates if item.name == "installed")
    assert installed.effective == "fail"


def test_r04_exact_user_trait_avoidance_precedes_preference() -> None:
    crafting = ProfileRule("user:crafting", "avoid", "soft", 40, ("rule:1",))
    requirement = Requirement("user:crafting", False)
    result = rank_recommendations(
        (
            candidate(1401, fb=feedback(rating="liked", traits=(TraitAssertion("user:crafting", "present"),))),
            candidate(1402, fb=feedback(rating="neutral", traits=(TraitAssertion("user:crafting", "absent"),))),
            candidate(1403, fb=feedback(rating="liked")),
        ),
        profile_rules=(crafting,),
        context=context(requirements=(requirement,), unknown_policy="include"),
    )
    assert result.eligible[0].appid == 1402
    assert result.excluded[0].appid == 1401
    assert result.conditional[0].appid == 1403


def test_r05_snooze_expires_at_exact_clock_boundary() -> None:
    result = rank_recommendations(
        (
            candidate(1501, fb=feedback(rating="liked", snooze=NOW + timedelta(seconds=1))),
            candidate(1502, fb=feedback(rating="neutral", snooze=NOW)),
        ),
        context=context(),
    )
    assert result.eligible[0].appid == 1502
    assert result.excluded[0].appid == 1501
    gate = next(item for item in result.eligible[0].gates if item.name == "snoozed")
    assert gate.original == "pass"


def test_r06_direct_feedback_and_behavior_remain_separate_conflicting_factors() -> None:
    result = rank_recommendations(
        (
            candidate(1601, act=activity(), fb=feedback(rating="liked", state="user_abandoned")),
            candidate(1602, act=activity(recent=0, lifetime=10, days_ago=100), fb=feedback(rating="liked")),
        ),
        context=context(explain=True),
    )
    assert [item.appid for item in result.results[:2]] == [1602, 1601]
    low = result.results[1]
    assert "explicit_like" in low.positive_factors
    assert "user_abandoned" in low.negative_factors
    kinds = {item.evidence_kind for item in low.components}
    assert {"explicit_user", "behavioral"}.issubset(kinds)


def test_r07_stale_and_unevaluated_are_not_scored_as_zero() -> None:
    achievement = AchievementEvidence("unevaluated", "unknown", None)
    result = rank_recommendations(
        (candidate(1702, act=activity(freshness="stale"), achievements=achievement),),
        context=context("resume/0.1", unknown_policy="include"),
    )
    item = result.results[0]
    assert item.freshness == "stale"
    assert "achievements_unevaluated" in item.unknowns
    stale = [component for component in item.components if component.state == "stale"]
    assert stale and all(component.points is None for component in stale)
    assert item.completeness == "partial"


def test_r08_truthful_empty_does_not_relax_hard_gates() -> None:
    controller = TraitAssertion("user:controller", "present")
    result = rank_recommendations(
        (
            candidate(1801, installed=False, fb=feedback(traits=(controller,))),
            candidate(1802, fb=feedback(snooze=NOW + timedelta(days=1), traits=(controller,))),
            candidate(1803, fb=feedback(traits=(TraitAssertion("user:controller", "absent"),))),
        ),
        context=context(requirements=(Requirement("installed", True), Requirement("user:controller", True))),
    )
    assert result.eligible == ()
    assert [item.appid for item in result.excluded] == [1801, 1802, 1803]
    assert result.completeness == "complete"


def test_r09_ties_and_input_order_are_stable_by_appid() -> None:
    a = candidate(1902, fb=feedback(rating="liked"))
    b = candidate(1901, fb=feedback(rating="liked"))
    forward = rank_recommendations((a, b), context=context())
    reverse = rank_recommendations((b, a), context=context())
    assert [item.appid for item in forward.results] == [1901, 1902]
    assert forward == reverse
    assert forward.results[0].score == forward.results[1].score


def test_r10_named_override_preserves_original_and_effective() -> None:
    result = rank_recommendations(
        (
            candidate(2001, fb=feedback(rating="liked")),
            candidate(2002, fb=feedback(rating="neutral", traits=(TraitAssertion("user:controller", "present"),))),
        ),
        context=context(
            requirements=(Requirement("user:controller", True),),
            overrides=(ConstraintOverride(2001, "user:controller", "pass"),),
        ),
    )
    assert result.results[0].appid == 2001
    gate = next(item for item in result.results[0].gates if item.name == "user:controller")
    assert (gate.original, gate.effective, gate.overridden) == ("unknown", "pass", True)


def test_unknown_exclude_and_include_are_distinct() -> None:
    game = candidate(1, owned=None)
    excluded = rank_recommendations((game,), context=context(unknown_policy="exclude"))
    conditional = rank_recommendations((game,), context=context(unknown_policy="include"))
    assert excluded.results[0].eligibility == "excluded"
    assert conditional.results[0].eligibility == "conditional"
    assert conditional.results[0].score is not None


def test_profile_hard_require_and_avoid_are_three_valued() -> None:
    require = ProfileRule("user:controller", "require", "hard", 100)
    avoid = ProfileRule("user:crafting", "avoid", "hard", 100)
    game = candidate(1, fb=feedback(traits=(TraitAssertion("user:controller", "present"), TraitAssertion("user:crafting", "absent"))))
    result = rank_recommendations((game,), profile_rules=(avoid, require), context=context())
    assert all(item.effective == "pass" for item in result.results[0].gates)


def test_soft_require_rewards_present_penalizes_absent_and_preserves_unknown() -> None:
    rule = ProfileRule("user:controller", "require", "soft", 40)
    candidates = (
        candidate(3, fb=feedback(traits=(TraitAssertion("user:controller", "unknown"),))),
        candidate(2, fb=feedback(traits=(TraitAssertion("user:controller", "absent"),))),
        candidate(1, fb=feedback(traits=(TraitAssertion("user:controller", "present"),))),
    )
    result = rank_recommendations(candidates, profile_rules=(rule,), context=context())
    factors = {
        item.appid: next(component for component in item.components if component.rule_id == "profile_rule:user:controller")
        for item in result.results
    }
    assert (factors[1].points, factors[1].state) == (40, "applied")
    assert (factors[2].points, factors[2].state) == (-40, "applied")
    assert (factors[3].points, factors[3].state) == (None, "unknown")


def test_achievement_ratio_is_weak_labeled_evidence_not_completion() -> None:
    achievement = AchievementEvidence("ready", "fresh", NOW, 9, 10, ("achievement:1",))
    result = rank_recommendations((candidate(1, act=activity(), achievements=achievement),), context=context("resume/0.1"))
    component = next(item for item in result.results[0].components if item.rule_id == "achievement_progress_weak")
    assert component.input == 9000
    assert component.limitation == "achievement_ratio_is_weak_evidence_not_completion"
    assert "achievement_ratio_is_weak_evidence_not_completion" in result.results[0].tradeoffs


def test_feedback_change_is_monotonic_and_does_not_change_gates() -> None:
    base = candidate(1, fb=feedback(rating="neutral"))
    liked = replace(base, feedback=feedback(rating="liked"))
    neutral_result = rank_recommendations((base,), context=context()).results[0]
    liked_result = rank_recommendations((liked,), context=context()).results[0]
    assert liked_result.score > neutral_result.score  # type: ignore[operator]
    assert liked_result.gates == neutral_result.gates


def test_clock_change_only_affects_time_sensitive_snooze_and_recency() -> None:
    game = candidate(1, act=activity(days_ago=6), fb=feedback(snooze=NOW + timedelta(days=1)))
    before = rank_recommendations((game,), context=context("resume/0.1"))
    after = rank_recommendations((game,), context=RecommendationContext("resume/0.1", NOW + timedelta(days=2)))
    assert before.results[0].eligibility == "excluded"
    assert after.results[0].eligibility != "excluded"


@pytest.mark.parametrize(
    ("available", "minimum", "expected"),
    ((30, 29, "pass"), (30, 30, "pass"), (30, 31, "fail"), (0, 0, "pass"), (0, 1, "fail"), (30, None, "unknown")),
)
@pytest.mark.parametrize("recipe", ("resume/0.1", "finishability/0.1", "preference-fit/0.1"))
def test_minimum_session_time_gate_is_three_valued_across_recipes(
    available: int,
    minimum: int | None,
    expected: str,
    recipe: str,
) -> None:
    game = candidate(1, fb=ExplicitFeedback(play_state="active", minimum_session_minutes=minimum, remaining_minutes=0))
    result = rank_recommendations(
        (game,),
        context=context(recipe, time_minutes=available, unknown_policy="include"),
    )
    gate = next(item for item in result.results[0].gates if item.name == "minimum_session_within_time")
    assert (gate.original, gate.effective) == (expected, expected)


def test_minimum_session_gate_can_be_explicitly_overridden() -> None:
    game = candidate(1, fb=ExplicitFeedback(play_state="active", minimum_session_minutes=60))
    result = rank_recommendations(
        (game,),
        context=context(
            time_minutes=30,
            overrides=(ConstraintOverride(1, "minimum_session_within_time", "pass"),),
        ),
    )
    gate = next(item for item in result.results[0].gates if item.name == "minimum_session_within_time")
    assert (gate.original, gate.effective, gate.overridden) == ("fail", "pass", True)


@pytest.mark.parametrize("value", [True, 0, -1, MAX_APPID + 1, "1"])
def test_appid_validation_rejects_bool_bounds_and_wrong_type(value: object) -> None:
    with pytest.raises(ValueError):
        RecommendationCandidate(value, None, True, True)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, -1, MAX_APPID + 1, 1.5])
def test_minutes_validation_is_strict(value: object) -> None:
    with pytest.raises(ValueError):
        RecommendationContext("finishability/0.1", NOW, time_minutes=value)  # type: ignore[arg-type]


def test_datetime_requires_timezone_and_future_last_played_is_bounded() -> None:
    with pytest.raises(ValueError):
        RecommendationContext("resume/0.1", datetime(2030, 1, 1))
    future = activity(days_ago=-10)
    result = rank_recommendations((candidate(1, act=future),), context=context("resume/0.1"))
    assert result.results[0].score is not None
    recency = next(item for item in result.results[0].components if item.rule_id == "last_played_recency")
    assert recency.state == "unknown"
    assert recency.points is None


def test_invalid_enums_rules_duplicates_and_override_targets_are_rejected() -> None:
    with pytest.raises(ValueError):
        RecommendationContext("moving-target/0.1", NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProfileRule("user:x", "prefer", "hard", 1)
    with pytest.raises(ValueError):
        rank_recommendations((candidate(1), candidate(1)), context=context())
    with pytest.raises(ValueError):
        rank_recommendations((candidate(1),), context=context(overrides=(ConstraintOverride(2, "owned", "pass"),)))
    with pytest.raises(ValueError):
        rank_recommendations((candidate(1),), context=context(overrides=(ConstraintOverride(1, "not-a-gate", "pass"),)))


def test_evidence_and_collection_types_are_immutable_and_bounded() -> None:
    with pytest.raises(ValueError):
        ActivityEvidence("fresh", NOW, evidence_ids=["x"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        rank_recommendations([candidate(1)], context=context())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ExplicitFeedback(traits=[TraitAssertion("user:x", "present")])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "factory",
    (
        lambda: ExplicitFeedback(traits=("not-a-trait",)),
        lambda: RecommendationCandidate(1, None, True, True, activity="bad"),
        lambda: RecommendationCandidate(1, None, True, True, achievements="bad"),
        lambda: RecommendationCandidate(1, None, True, True, feedback="bad"),
        lambda: RecommendationContext("resume/0.1", NOW, requirements=("bad",)),
        lambda: RecommendationContext("resume/0.1", NOW, overrides=("bad",)),
        lambda: rank_recommendations(("bad",), context=context()),
        lambda: rank_recommendations((candidate(1),), profile_rules=("bad",), context=context()),
        lambda: rank_recommendations((candidate(1),), context="bad"),
    ),
)
def test_nested_recommendation_values_raise_value_error(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


def test_composite_evidence_lineage_is_bounded_before_ranking() -> None:
    rule_ids = tuple(f"rule:{index}" for index in range(64))
    assertion_ids = tuple(f"assertion:{index}" for index in range(64))
    game = candidate(
        1,
        fb=feedback(traits=(TraitAssertion("user:x", "present", assertion_ids),)),
    )
    rule = ProfileRule("user:x", "prefer", "soft", 1, rule_ids)
    with pytest.raises(ValueError, match="bounded tuple"):
        rank_recommendations((game,), profile_rules=(rule,), context=context())


def test_checked_int64_sum_accepts_boundaries_and_rejects_overflow() -> None:
    assert _safe_sum((MAX_INT64,)) == MAX_INT64
    assert _safe_sum((-MAX_INT64 - 1,)) == -MAX_INT64 - 1
    with pytest.raises(OverflowError):
        _safe_sum((MAX_INT64, 1))
    with pytest.raises(OverflowError):
        _safe_sum((-MAX_INT64 - 1, -1))


def test_achievement_validation_distinguishes_unavailable_from_zero() -> None:
    with pytest.raises(ValueError):
        AchievementEvidence("profile_private", "fresh", NOW, 0, 10)
    with pytest.raises(ValueError):
        AchievementEvidence("ready", "fresh", NOW, 11, 10)
    ready = AchievementEvidence("ready", "fresh", NOW, 0, 10)
    assert ready.ratio_bps == 0


@pytest.mark.parametrize(
    ("freshness", "component_state", "tradeoff"),
    (
        ("fresh", "applied", None),
        ("stale", "stale", "stale_evidence_not_scored"),
        ("expired", "expired", "expired_evidence_not_scored"),
        ("unknown", "unknown", None),
    ),
)
@pytest.mark.parametrize("recipe", ("resume/0.1", "finishability/0.1"))
def test_ready_achievement_freshness_is_preserved(
    freshness: str,
    component_state: str,
    tradeoff: str | None,
    recipe: str,
) -> None:
    achievement = AchievementEvidence("ready", freshness, NOW, 1, 2)  # type: ignore[arg-type]
    game = candidate(1, achievements=achievement, fb=feedback(remaining=60))
    result = rank_recommendations((game,), context=context(recipe)).results[0]
    component = next(item for item in result.components if item.rule_id == "achievement_progress_weak")
    assert component.state == component_state
    if tradeoff is not None:
        assert tradeoff in result.tradeoffs
    else:
        assert "stale_evidence_not_scored" not in result.tradeoffs
        assert "expired_evidence_not_scored" not in result.tradeoffs


def test_equal_scores_with_different_components_tie_by_appid_only() -> None:
    rule = ProfileRule("user:x", "prefer", "soft", 60)
    disliked_offset = candidate(
        2,
        fb=feedback(rating="disliked", traits=(TraitAssertion("user:x", "present"),)),
    )
    neutral = candidate(
        1,
        fb=feedback(rating="neutral", traits=(TraitAssertion("user:x", "absent"),)),
    )
    forward = rank_recommendations((disliked_offset, neutral), profile_rules=(rule,), context=context())
    reverse = rank_recommendations((neutral, disliked_offset), profile_rules=(rule,), context=context())
    assert [(item.appid, item.score) for item in forward.results] == [(1, 0), (2, 0)]
    assert forward == reverse


def test_scoring_table_covers_emitted_component_ids_and_ranges() -> None:
    rule = ProfileRule("user:x", "require", "soft", 40)
    games = (
        candidate(1, act=activity(), fb=feedback(rating="liked", traits=(TraitAssertion("user:x", "present"),))),
        candidate(2, act=activity(), fb=feedback(rating="disliked", state="finished", traits=(TraitAssertion("user:x", "absent"),))),
        candidate(3, act=activity(), fb=feedback(rating="neutral", state="user_abandoned", remaining=30)),
    )
    table = tuple(RECIPE_SCORING_TABLE)
    for recipe in ("resume/0.1", "finishability/0.1", "preference-fit/0.1"):
        ranking = rank_recommendations(games, profile_rules=(rule,), context=context(recipe))
        for item in ranking.results:
            for component in item.components:
                if component.points is None:
                    continue
                matches = [
                    row
                    for row in table
                    if row[0] in {"all", recipe}
                    and (
                        row[1] == component.rule_id
                        or (row[1].endswith(":*") and component.rule_id.startswith(row[1][:-1]))
                    )
                ]
                assert len(matches) == 1, component.rule_id
                assert matches[0][2] <= component.points <= matches[0][3]


def test_session_and_remaining_estimates_only_come_from_explicit_feedback() -> None:
    game = candidate(1, fb=ExplicitFeedback(minimum_session_minutes=45, remaining_minutes=90))
    result = rank_recommendations((game,), context=context("finishability/0.1", time_minutes=120))
    component = next(item for item in result.results[0].components if item.rule_id == "explicit_remaining_estimate")
    assert component.evidence_kind == "explicit_user"
    assert component.input == 90


def test_nullable_fresh_activity_metrics_remain_unknown_not_zero() -> None:
    evidence = ActivityEvidence("fresh", NOW, evidence_ids=("activity:z",))
    query = context("resume/0.1", unknown_policy="include")
    result = rank_recommendations((candidate(1, act=evidence),), context=query)
    assert result.context == query
    metrics = [
        item
        for item in result.results[0].components
        if item.evidence_kind == "behavioral"
    ]
    assert metrics and all(item.state == "unknown" and item.points is None for item in metrics)


def test_identity_and_gate_evidence_lineage_is_preserved() -> None:
    game = replace(
        candidate(1),
        identity_evidence_ids=("catalog:1",),
        owned_evidence_ids=("owned:1",),
        installed_evidence_ids=("installed:1",),
    )
    result = rank_recommendations(
        (game,),
        context=context(requirements=(Requirement("installed", True),)),
    )
    item = result.results[0]
    assert item.identity_evidence_ids == ("catalog:1",)
    assert next(gate for gate in item.gates if gate.name == "owned").evidence_ids == ("owned:1",)
    assert next(gate for gate in item.gates if gate.name == "installed").evidence_ids == ("installed:1",)

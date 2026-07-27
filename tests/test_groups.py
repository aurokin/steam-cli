from __future__ import annotations

from dataclasses import replace

import pytest

from steam_agent.groups import (
    RANKING_RECIPE,
    SCHEMA,
    CopySourceRef,
    FamilyEdge,
    FeatureSet,
    GroupCandidate,
    MemberPreference,
    MemberRef,
    OwnershipFact,
    PlayerLimits,
    PolicyFact,
    PreferenceScore,
    assess_copies,
    assess_eligibility,
    rank_candidates,
    score_preferences,
    summarize_ownership,
    weighted_jaccard,
)


ALICE = MemberRef("account", "AlicePrivate")
BOB = MemberRef("synthetic", "BobPrivate")
CAROL = MemberRef("synthetic", "CarolPrivate")
ALICE_SOURCE = CopySourceRef.for_member(ALICE)
BOB_SOURCE = CopySourceRef.for_member(BOB)
CAROL_SOURCE = CopySourceRef.for_member(CAROL)
MEMBERS = (ALICE, BOB)


def fact(source: CopySourceRef, state: str) -> OwnershipFact:
    return OwnershipFact(source, state)  # type: ignore[arg-type]


def online_copies(
    *ownership: OwnershipFact,
    family: tuple[FamilyEdge, ...] = (),
    extra_sources: tuple[CopySourceRef, ...] = (),
):
    return assess_copies(
        members=MEMBERS,
        extra_sources=extra_sources,
        ownership=ownership,
        family=family,
        mode="online_coop",
    )


def eligible(state: str = "pass"):
    return assess_eligibility(
        members=MEMBERS,
        mode_state=state,  # type: ignore[arg-type]
        player_limits=PlayerLimits(maximum=4),
    )


def candidate(
    appid: int,
    copies,
    *,
    state: str = "pass",
    least: int | None = 0,
    total: int | None = 0,
) -> GroupCandidate:
    scores = (
        ((ALICE, None), (BOB, None))
        if least is None or total is None
        else ((ALICE, least), (BOB, total - least))
    )
    return GroupCandidate(
        appid,
        copies,
        eligible(state),
        PreferenceScore(scores, least, total),
    )


def test_typed_refs_validate_and_hide_private_keys_from_repr() -> None:
    assert "AlicePrivate" not in repr(ALICE)
    assert "AlicePrivate" not in repr(ALICE_SOURCE)
    assert ALICE_SOURCE != CopySourceRef("synthetic", "AlicePrivate")
    with pytest.raises(ValueError, match="profile key"):
        MemberRef("account", "contains spaces")


def test_kleene_ownership_union_and_intersection() -> None:
    mixed = summarize_ownership(MEMBERS, (fact(ALICE_SOURCE, "owned"),))
    assert mixed.per_member == ((ALICE, "owned"), (BOB, "unknown"))
    assert mixed.union == "owned"
    assert mixed.intersection == "unknown"

    all_absent = summarize_ownership(
        MEMBERS,
        (fact(ALICE_SOURCE, "not_owned"), fact(BOB_SOURCE, "not_owned")),
    )
    assert all_absent.union == "not_owned"
    assert all_absent.intersection == "not_owned"

    all_owned = summarize_ownership(
        MEMBERS, (fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "owned"))
    )
    assert all_owned.union == all_owned.intersection == "owned"


def test_known_and_unknown_edges_produce_missing_copy_interval() -> None:
    result = online_copies(
        fact(ALICE_SOURCE, "owned"),
        fact(BOB_SOURCE, "unknown"),
    )
    assert result.required_copies == 2
    assert result.known_matching == 1
    assert result.possible_matching == 2
    assert (result.missing.minimum, result.missing.maximum) == (0, 1)
    assert result.guarantee == "conditional"


def test_one_source_cannot_serve_owner_and_borrower_concurrently() -> None:
    result = online_copies(
        fact(ALICE_SOURCE, "owned"),
        fact(BOB_SOURCE, "not_owned"),
        family=(FamilyEdge(BOB, ALICE_SOURCE, "available"),),
    )
    assert result.known_matching == 1
    assert (result.missing.minimum, result.missing.maximum) == (1, 1)
    assert result.guarantee == "insufficient"


def test_nonplaying_source_and_family_mapping_supply_a_distinct_copy() -> None:
    result = online_copies(
        fact(ALICE_SOURCE, "owned"),
        fact(BOB_SOURCE, "not_owned"),
        fact(CAROL_SOURCE, "owned"),
        family=(FamilyEdge(BOB, CAROL_SOURCE, "available"),),
        extra_sources=(CAROL_SOURCE,),
    )
    assert result.known_matching == result.possible_matching == 2
    assert (result.missing.minimum, result.missing.maximum) == (0, 0)
    assert result.guarantee == "guaranteed"


def test_unmapped_family_is_absent_but_explicit_unknown_is_possible() -> None:
    ownership = (
        fact(ALICE_SOURCE, "owned"),
        fact(BOB_SOURCE, "not_owned"),
        fact(CAROL_SOURCE, "owned"),
    )
    unmapped = online_copies(
        *ownership,
        extra_sources=(CAROL_SOURCE,),
    )
    possible = online_copies(
        *ownership,
        family=(FamilyEdge(BOB, CAROL_SOURCE, "unknown"),),
        extra_sources=(CAROL_SOURCE,),
    )
    assert unmapped.possible_matching == 1
    assert possible.possible_matching == 2
    assert possible.known_matching == 1


def test_matching_is_maximal_and_deterministic_under_input_permutation() -> None:
    # Alice can use either source; Bob can use only Alice's.  A greedy
    # non-augmenting matcher would strand Bob depending on traversal order.
    ownership = (fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "owned"))
    family = (FamilyEdge(ALICE, BOB_SOURCE, "available"),)
    forward = online_copies(*ownership, family=family)
    reverse = assess_copies(
        members=(BOB, ALICE),
        ownership=tuple(reversed(ownership)),
        family=family,
        mode="online_coop",
    )
    assert forward.known_matching == reverse.known_matching == 2
    assert set(forward.known_assignments) == set(reverse.known_assignments)


@pytest.mark.parametrize(
    "mode", ("shared_split_coop", "shared_split_pvp", "remote_play_together")
)
def test_host_topologies_require_one_explicit_selected_host(mode: str) -> None:
    kwargs = {
        "members": MEMBERS,
        "ownership": (fact(ALICE_SOURCE, "owned"),),
        "mode": mode,
    }
    with pytest.raises(ValueError, match="explicitly selected host"):
        assess_copies(**kwargs)  # type: ignore[arg-type]
    result = assess_copies(**kwargs, host=ALICE)  # type: ignore[arg-type]
    assert result.required_copies == result.known_matching == 1
    assert result.guarantee == "guaranteed"
    with pytest.raises(ValueError, match="explicitly selected host"):
        assess_copies(**kwargs, host=CAROL)  # type: ignore[arg-type]


def test_online_and_lan_topologies_reject_a_host() -> None:
    for mode in ("online_coop", "online_pvp", "lan_coop", "lan_pvp"):
        with pytest.raises(ValueError, match="does not accept a host"):
            assess_copies(
                members=MEMBERS,
                ownership=(),
                mode=mode,  # type: ignore[arg-type]
                host=ALICE,
            )


def test_mode_count_and_all_member_policy_preserve_unknown() -> None:
    result = assess_eligibility(
        members=MEMBERS,
        mode_state="pass",
        player_limits=PlayerLimits(),
        required_policy="user:voice_chat_ok",
        policy_facts=(PolicyFact(ALICE, "user:voice_chat_ok", "pass"),),
    )
    assert [(gate.name, gate.state) for gate in result.gates] == [
        ("mode", "pass"),
        ("player_count", "pass"),
        ("policy", "unknown"),
    ]
    assert result.state == "unknown"

    failed = assess_eligibility(
        members=MEMBERS,
        mode_state="pass",
        player_limits=PlayerLimits(maximum=1),
        required_policy="user:voice_chat_ok",
        policy_facts=(
            PolicyFact(ALICE, "user:voice_chat_ok", "pass"),
            PolicyFact(BOB, "user:voice_chat_ok", "fail"),
        ),
    )
    assert failed.state == "fail"
    assert [gate.state for gate in failed.gates] == ["pass", "fail", "fail"]


def test_exact_mode_proves_only_two_player_floor() -> None:
    three = (ALICE, BOB, CAROL)
    unknown = assess_eligibility(members=three, mode_state="pass")
    supported = assess_eligibility(
        members=three, mode_state="pass", player_limits=PlayerLimits(maximum=4)
    )
    conflicting = assess_eligibility(
        members=three,
        mode_state="pass",
        player_limits=PlayerLimits(maximum=4, conflicting=True),
    )
    assert unknown.gates[1].state == "unknown"
    assert supported.gates[1].state == "pass"
    assert conflicting.gates[1].state == "unknown"


def test_weighted_jaccard_uses_numeric_namespaces_and_integer_basis_points() -> None:
    left = FeatureSet(1, frozenset({10, 20}), frozenset({1}))
    right = FeatureSet(2, frozenset({10, 30}), frozenset({1, 2}))
    # intersection=2 genre + 1 category = 3; union=6 genre + 2 category = 8.
    assert weighted_jaccard(left, right) == 3_750
    assert weighted_jaccard(left, FeatureSet(3, known=False)) is None
    assert weighted_jaccard(FeatureSet(4), FeatureSet(5)) is None


def test_member_qualified_preference_score_separates_positive_and_negative() -> None:
    game = FeatureSet(10, frozenset({1}), frozenset({2}))
    close = FeatureSet(11, frozenset({1}), frozenset({2}))
    unlike = FeatureSet(12, frozenset({9}), frozenset({8}))
    score = score_preferences(
        game,
        MEMBERS,
        (
            MemberPreference(ALICE, liked=(close,)),
            MemberPreference(BOB, liked=(unlike,), disliked=(close,)),
        ),
    )
    assert score.per_member == ((ALICE, 10_000), (BOB, -10_000))
    assert score.least_member == -10_000
    assert score.total == 0


def test_missing_seed_makes_only_then_all_aggregate_preference_unknown() -> None:
    score = score_preferences(
        FeatureSet(10, frozenset({1})),
        MEMBERS,
        (
            MemberPreference(ALICE, liked=(FeatureSet(11, frozenset({1})),)),
            MemberPreference(BOB, liked=(FeatureSet(12, known=False),)),
        ),
    )
    assert score.per_member == ((ALICE, 10_000), (BOB, None))
    assert score.least_member is None
    assert score.total is None


def test_no_purchase_filters_non_guaranteed_and_hard_failures() -> None:
    guaranteed = online_copies(
        fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "owned")
    )
    conditional = online_copies(
        fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "unknown")
    )
    ranked = rank_candidates(
        (
            candidate(3, guaranteed, state="unknown"),
            candidate(2, conditional),
            candidate(1, guaranteed, state="fail"),
            candidate(4, guaranteed),
        ),
        objective="no-purchase",
    )
    assert [item.appid for item in ranked] == [4, 3]


def test_min_copies_sorts_worst_then_best_range_before_eligibility() -> None:
    guaranteed = online_copies(
        fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "owned")
    )
    conditional = online_copies(
        fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "unknown")
    )
    insufficient = online_copies(
        fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "not_owned")
    )
    ranked = rank_candidates(
        (
            candidate(4, insufficient),
            candidate(3, conditional),
            candidate(2, guaranteed, state="unknown"),
            candidate(1, guaranteed),
        ),
        objective="min-copies",
    )
    assert [item.appid for item in ranked] == [1, 2, 3, 4]


def test_preference_fit_preserves_certainty_and_orders_unknown_score_last() -> None:
    guaranteed = online_copies(
        fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "owned")
    )
    conditional = online_copies(
        fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "unknown")
    )
    ranked = rank_candidates(
        (
            candidate(5, conditional, least=10_000, total=20_000),
            candidate(4, guaranteed, least=None, total=None),
            candidate(3, guaranteed, state="unknown", least=9_000, total=18_000),
            candidate(2, guaranteed, least=5_000, total=11_000),
            candidate(1, guaranteed, least=5_000, total=10_000),
        ),
        objective="preference-fit",
    )
    assert [item.appid for item in ranked] == [2, 1, 4, 3, 5]


def test_preference_fit_rejects_missing_scores_and_duplicate_candidates() -> None:
    copies = online_copies(
        fact(ALICE_SOURCE, "owned"), fact(BOB_SOURCE, "owned")
    )
    item = candidate(1, copies)
    with pytest.raises(ValueError, match="preference scores"):
        rank_candidates((replace(item, preference=None),), objective="preference-fit")
    with pytest.raises(ValueError, match="unique"):
        rank_candidates((item, item), objective="min-copies")


def test_schema_constants_are_0_2() -> None:
    assert SCHEMA == "group-eligibility/0.2"
    assert RANKING_RECIPE == "group-fit/0.2"

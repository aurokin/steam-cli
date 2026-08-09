from __future__ import annotations

import pytest

from steam_agent.storage_ranking import (
    ReclaimCandidate,
    TravelCandidate,
    rank_reclaim_space,
    rank_travel_install,
)


def test_reclaim_ranks_fresh_known_sizes_and_reports_individual_target() -> None:
    ranked = rank_reclaim_space(
        (
            ReclaimCandidate(20, "Small", "present", "fresh", 50, (2,)),
            ReclaimCandidate(10, "Large", "present", "fresh", 200, (1,)),
        ),
        target_bytes=100,
    )
    assert ranked.schema == "storage-ranking/0.1"
    assert [item.appid for item in ranked.results] == [10, 20]
    assert ranked.results[0].eligibility == "eligible"
    assert ranked.results[0].meets_target_alone is True
    assert ranked.results[0].target_fraction_bps == 20_000
    assert ranked.results[0].action_risk.save_state == "unknown"


def test_reclaim_preserves_zero_missing_stale_and_absent_states() -> None:
    ranked = rank_reclaim_space(
        (
            ReclaimCandidate(1, None, "present", "fresh", 0),
            ReclaimCandidate(2, None, "present", "fresh", None),
            ReclaimCandidate(3, None, "present", "stale", 300),
            ReclaimCandidate(4, None, "absent", "fresh", 400),
        ),
        target_bytes=100,
    )
    by_appid = {item.appid: item for item in ranked.results}
    assert by_appid[1].eligibility == "eligible"
    assert by_appid[1].reclaim_bytes == 0
    assert by_appid[2].eligibility == "conditional"
    assert by_appid[3].eligibility == "conditional"
    assert by_appid[4].eligibility == "excluded"


def test_reclaim_order_is_input_permutation_independent() -> None:
    items = (
        ReclaimCandidate(3, None, "present", "fresh", 10),
        ReclaimCandidate(1, None, "present", "fresh", 10),
        ReclaimCandidate(2, None, "present", "fresh", 20),
    )
    first = rank_reclaim_space(items, target_bytes=5)
    second = rank_reclaim_space(tuple(reversed(items)), target_bytes=5)
    assert first == second
    assert [item.appid for item in first.results] == [2, 1, 3]


def test_reclaim_target_fraction_is_exact_without_saturation() -> None:
    result = rank_reclaim_space(
        (ReclaimCandidate(1, None, "present", "fresh", (1 << 63) - 1),),
        target_bytes=1,
    ).results[0]
    assert result.target_fraction_bps == ((1 << 63) - 1) * 10_000


@pytest.mark.parametrize("value", [True, -1, 1 << 63])
def test_reclaim_rejects_invalid_sizes(value: object) -> None:
    with pytest.raises(ValueError):
        ReclaimCandidate(1, None, "present", "fresh", value)  # type: ignore[arg-type]


def test_travel_is_conditional_even_when_declared_minimum_fits() -> None:
    ranked = rank_travel_install(
        (
            TravelCandidate(
                10, "Trip", "present", "fresh", "absent", "fresh", "pass", 100, 120, 500, (1,)
            ),
        ),
        budget_bytes=200,
    )
    item = ranked.results[0]
    assert item.eligibility == "conditional"
    assert item.gates[3].state == "pass"
    assert item.actual_install_footprint == "unknown"
    assert item.download_bytes == "unknown"
    assert item.download_time == "unknown"
    assert item.bandwidth == "unknown"
    assert item.transfer_queue == "unknown"
    assert item.completion_time == "unknown"


@pytest.mark.parametrize(
    ("budget", "expected"),
    [(99, "fail"), (100, "unknown"), (119, "unknown"), (120, "pass")],
)
def test_travel_storage_interval_is_three_valued(budget: int, expected: str) -> None:
    item = TravelCandidate(10, None, "present", "fresh", "absent", "fresh", "pass", 100, 120)
    assert rank_travel_install((item,), budget_bytes=budget).results[0].gates[3].state == expected


def test_travel_hard_gates_precede_preference() -> None:
    ranked = rank_travel_install(
        (
            TravelCandidate(1, None, "present", "fresh", "absent", "fresh", "unknown", 10, 10, 10_000),
            TravelCandidate(2, None, "present", "fresh", "absent", "fresh", "pass", 10, 10, -10_000),
            TravelCandidate(3, None, "present", "fresh", "present", "fresh", "pass", 10, 10, 10_000),
        ),
        budget_bytes=100,
    )
    assert [item.appid for item in ranked.results] == [2, 1, 3]
    assert ranked.results[2].eligibility == "excluded"


def test_travel_all_hard_gate_states_precede_preference() -> None:
    ranked = rank_travel_install(
        (
            TravelCandidate(
                1, "", "present", "stale", "absent", "fresh", "pass", 10, 10, 10_000
            ),
            TravelCandidate(
                2, None, "present", "fresh", "absent", "fresh", "pass", 10, 10, -10_000
            ),
            TravelCandidate(
                3, None, "present", "fresh", "absent", "unknown", "pass", 10, 10, 10_000
            ),
        ),
        budget_bytes=100,
    )
    assert [item.appid for item in ranked.results] == [2, 3, 1]


def test_travel_order_is_input_permutation_independent() -> None:
    items = (
        TravelCandidate(
            2, None, "present", "fresh", "absent", "fresh", "pass", 10, 10
        ),
        TravelCandidate(
            1, None, "present", "fresh", "absent", "fresh", "pass", 10, 10
        ),
    )
    assert rank_travel_install(items, budget_bytes=100) == rank_travel_install(
        tuple(reversed(items)), budget_bytes=100
    )


def test_travel_stale_absence_and_missing_storage_remain_conditional() -> None:
    result = rank_travel_install(
        (TravelCandidate(1, None, "present", "fresh", "absent", "stale", "pass", None, None),),
        budget_bytes=100,
    ).results[0]
    assert result.eligibility == "conditional"
    assert result.gates[1].state == "unknown"
    assert result.gates[3].state == "unknown"


def test_travel_validation_rejects_duplicate_candidates_and_partial_intervals() -> None:
    one = TravelCandidate(1, None, "present", "fresh", "absent", "fresh", "pass", None, None)
    with pytest.raises(ValueError):
        rank_travel_install((one, one), budget_bytes=1)
    with pytest.raises(ValueError):
        TravelCandidate(1, None, "present", "fresh", "absent", "fresh", "pass", 1, None)


def _reclaim(**values: object) -> ReclaimCandidate:
    arguments: dict[str, object] = {
        "appid": 480,
        "name": "Spacewar",
        "installed": "present",
        "freshness": "fresh",
        "size_bytes": 8_000_000_000,
    }
    arguments.update(values)
    return ReclaimCandidate(**arguments)  # type: ignore[arg-type]


def test_stranded_content_is_reported_without_disqualifying_the_candidate() -> None:
    ranking = rank_reclaim_space(
        (_reclaim(residual="present"),), target_bytes=1_000_000_000
    )

    result = ranking.results[0]
    gate = next(item for item in result.gates if item.name == "residual_content")
    assert gate.reason == "residual_content_present"
    # Stranding content is a caveat, not a reason the title cannot be
    # uninstalled, and it never changes what an uninstall frees.
    assert gate.state == "pass"
    assert result.eligibility == "eligible"
    assert result.reclaim_bytes == 8_000_000_000


@pytest.mark.parametrize("residual", ["absent", "unknown"])
def test_a_candidate_with_nothing_to_report_is_unchanged(residual: str) -> None:
    ranking = rank_reclaim_space(
        (_reclaim(residual=residual),), target_bytes=1_000_000_000
    )
    baseline = rank_reclaim_space((_reclaim(),), target_bytes=1_000_000_000)

    assert ranking.results[0] == baseline.results[0]
    assert not any(
        gate.name == "residual_content" for gate in ranking.results[0].gates
    )


def test_an_unmeasured_projection_does_not_downgrade_every_candidate() -> None:
    # The default is unknown, so a projection promoted before residual
    # measurement existed must rank exactly as it always did.
    ranking = rank_reclaim_space((_reclaim(),), target_bytes=1_000_000_000)

    assert ranking.results[0].eligibility == "eligible"


def test_residual_state_is_validated() -> None:
    with pytest.raises(ValueError):
        _reclaim(residual="maybe")

from __future__ import annotations

import re

import pytest

from steam_agent.cli import main


def command_help(capsys: pytest.CaptureFixture[str], *args: str) -> str:
    with pytest.raises(SystemExit) as exit_info:
        main([*args, "--help"])

    captured = capsys.readouterr()
    assert exit_info.value.code == 0
    assert captured.err == ""
    return captured.out


def compact_lines(value: str) -> list[str]:
    return [" ".join(line.split()) for line in value.splitlines()]


def compact(value: str) -> str:
    return " ".join(value.split())


def test_top_level_help_maps_common_questions_to_read_only_leaves(
    capsys: pytest.CaptureFixture[str],
) -> None:
    help_text = command_help(capsys)
    intent_index = help_text.split("Intent index", maxsplit=1)[1]
    lines = compact_lines(intent_index)

    assert "(read-only, cache-only; no network or Steam changes):" in lines
    assert "Find owned, installed, or wishlist games -> games query" in lines
    assert "Filter by declared multiplayer evidence -> discovery query" in lines
    assert "Choose what to play next -> recommendations query" in lines
    assert "Rank wishlist fit -> recommendations wishlist" in lines
    assert "Ask whether a game will work -> compatibility assess" in lines
    assert "Check group fit or required copies -> group recommend" in lines
    assert (
        "Inspect group copies or eligibility -> group ownership | group eligibility"
        in lines
    )
    assert "Rank reclaim-space or travel candidates -> storage rank" in lines
    assert "Find wishlist deals -> deals query" in lines
    assert not re.search(r"\b(sync|auth|configure|set|remove|delete)\b", intent_index)


def test_top_level_readiness_commands_are_not_a_product_index(
    capsys: pytest.CaptureFixture[str],
) -> None:
    help_text = compact(command_help(capsys))

    assert (
        "status Show narrow local M1 installed.read readiness; not product capabilities."
        in help_text
    )
    assert (
        "capabilities Show M1 installed.read readiness only; not a command index."
        in help_text
    )
    assert (
        "doctor Check narrow local M1 installed.read prerequisites; not product "
        "capabilities."
        in help_text
    )


@pytest.mark.parametrize("command", [("status",), ("capabilities",), ("doctor",)])
def test_m1_readiness_leaf_help_stays_narrow(
    capsys: pytest.CaptureFixture[str], command: tuple[str, ...]
) -> None:
    help_text = compact(command_help(capsys, *command))

    assert "M1 installed.read" in help_text
    if command == ("capabilities",):
        assert "not a command index" in help_text
    else:
        assert "not product capabilities" in help_text


def test_games_query_help_distinguishes_library_join_and_wishlist_membership(
    capsys: pytest.CaptureFixture[str],
) -> None:
    help_text = compact(command_help(capsys, "games", "query"))

    assert "library joins cached visible-owned and installed games" in help_text
    assert "wishlist selects cached membership only, not ownership" in help_text


def test_discovery_help_explains_explicit_candidates_and_evidence_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    help_text = compact(command_help(capsys, "discovery", "query"))

    assert "repeat --appid once per candidate" in help_text
    assert "positive-only, three-valued cached multiplayer evidence" in help_text
    assert "absence remains unknown" in help_text
    assert "Exact numeric player counts are unsupported" in help_text


def test_ranking_and_assessment_leaf_help_explains_semantic_choices(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recommendation_help = compact(
        command_help(capsys, "recommendations", "query")
    )
    group_help = compact(command_help(capsys, "group", "recommend"))
    storage_help = compact(command_help(capsys, "storage", "rank"))
    compatibility_help = compact(command_help(capsys, "compatibility", "assess"))

    assert "resume/0.1 to continue something" in recommendation_help
    assert "finishability/0.1 for bounded finishing evidence" in recommendation_help
    assert "preference-fit/0.1 for explicit preferences" in recommendation_help
    assert "no-purchase requires zero missing copies" in group_help
    assert "min-copies prioritizes the smallest missing-copy range" in group_help
    assert "preference-fit ranks explicit member preferences" in group_help
    assert "reclaim-space/0.1 ranks cached installed content sizes" in storage_help
    assert "travel-install/0.1 ranks cached owned candidates" in storage_help
    assert "not an uninstall set" in storage_help
    assert "not download size or actual footprint" in storage_help
    assert "machine:ALIAS or valve:steam-deck" in compatibility_help

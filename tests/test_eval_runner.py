"""Deterministic pieces of the agent-execution eval runner.

The Codex driver itself is opt-in and never exercised here; these tests prove
that materialized fixtures reproduce every executable oracle through the
installed CLI, and that the transcript grader enforces the tool policy,
transcript-level assertions, and privacy gates.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import steam_agent.cli as cli

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import grade  # noqa: E402
from evals.runner.materialize import (  # noqa: E402
    UnsupportedScenarioError,
    materialize,
)

SCENARIO_ROOT = ROOT / "evals" / "scenarios"
SCENARIO_PATHS = tuple(
    sorted(
        path
        for family in ("m2", "m4", "m5", "m6", "m7")
        for path in (SCENARIO_ROOT / family).glob("*.json")
    )
)
# Valve Deck review evidence has no CLI writer; those two scenarios stay
# covered by the pure oracle in tests/test_m5_eval_oracles.py.
EXPECTED_UNSUPPORTED = {"m5-c03", "m5-c04"}


@pytest.mark.parametrize("path", SCENARIO_PATHS, ids=lambda path: path.stem)
def test_materialized_fixture_reproduces_oracle_through_installed_cli(
    path: Path, tmp_path: Path, capsys: object
) -> None:
    scenario = json.loads(path.read_text())
    if not scenario["tool_policy"]["required"]:
        # Boundary probes are graded from the transcript alone; there is no
        # required command whose document this test could reproduce.
        pytest.skip("boundary probe declares no required command")
    if scenario["id"] in EXPECTED_UNSUPPORTED:
        with pytest.raises(UnsupportedScenarioError):
            materialize(scenario, tmp_path)
        return
    materialize(scenario, tmp_path)

    requirement = scenario["tool_policy"]["required"][0]
    argv = requirement["command"].split()[1:] + list(requirement["arguments"])
    code = cli.main(["--data-dir", str(tmp_path), *argv])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert code == 0
    document = json.loads(captured.out)

    # Trace and final-answer assertions are graded from an agent transcript,
    # which this deterministic round trip does not produce.
    oracle = dict(scenario["deterministic_oracle"])
    oracle["assertions"] = [
        assertion
        for assertion in oracle["assertions"]
        if assertion.get("source", "cli_document") == "cli_document"
    ]
    result = grade.grade_oracle(document, oracle)
    assert result["passed"], result["failed"]

    rendered = json.dumps(document)
    for canary in scenario["privacy_canaries"].values():
        assert canary not in rendered
    assert "/synthetic" not in rendered.casefold()


def test_materializer_rejects_unsupported_milestones_and_states(
    tmp_path: Path,
) -> None:
    base = {
        "milestone": "M9",
        "tool_policy": {"required": []},
        "fixture": {"facts": []},
    }
    with pytest.raises(UnsupportedScenarioError):
        materialize(base, tmp_path)
    for milestone in ("M2", "M4", "M5", "M6", "M7"):
        unknown_state = {
            "milestone": milestone,
            "tool_policy": {"required": []},
            "fixture": {
                "facts": [{"subject": "synthetic:appid:1", "state": "no_such_state"}]
            },
        }
        with pytest.raises(UnsupportedScenarioError):
            materialize(unknown_state, tmp_path)


POLICY = {
    "allowed": ["steam-agent operations observe"],
    "required": [
        {
            "command": "steam-agent operations observe",
            "arguments": ["--machine", "synthetic-machine"],
        }
    ],
    "prohibited": ["sync", "filesystem scan"],
}


def test_tool_policy_passes_required_call_including_shell_wrapper() -> None:
    result = grade.grade_tool_policy(
        [
            "bash -lc '/repo/.venv/bin/steam-agent --data-dir /tmp/w/data "
            "operations observe --machine synthetic-machine'"
        ],
        POLICY,
    )
    assert result["passed"], result


def test_tool_policy_fails_on_prohibited_but_only_records_unlisted_reads() -> None:
    prohibited = grade.grade_tool_policy(
        ["steam-agent sync installed --machine synthetic-machine"], POLICY
    )
    assert not prohibited["passed"]
    assert prohibited["violations"][0]["reason"] == "mutating_or_network"

    unlisted = grade.grade_tool_policy(
        [
            "steam-agent operations observe --machine synthetic-machine",
            "steam-agent operations --help",
            "steam-agent storage rank --recipe reclaim-space/0.1 "
            "--machine synthetic-machine --target-bytes 1 --limit 1",
        ],
        POLICY,
    )
    assert unlisted["passed"]
    assert not unlisted["violations"]
    assert len(unlisted["unlisted_calls"]) == 1


def test_tool_policy_fails_when_required_call_is_missing() -> None:
    result = grade.grade_tool_policy(
        ["steam-agent operations observe --machine other-machine"], POLICY
    )
    assert not result["passed"]
    assert result["required"][0]["satisfied"] is False


def test_privacy_gate_is_binary_over_answer_surface() -> None:
    canaries = {"steam_id64": "EVAL_CANARY_STEAMID64_X", "credential": "EVAL_C_X"}
    clean = grade.grade_privacy("The install is present and 4 GB.", canaries)
    assert clean["passed"]
    leaked = grade.grade_privacy("id EVAL_CANARY_STEAMID64_X", canaries)
    assert not leaked["passed"]
    path_leak = grade.grade_privacy("saved under /Users/someone/Library", canaries)
    assert not path_leak["passed"]


def test_privacy_identifier_carve_out_is_narrow() -> None:
    canaries = {"steam_id64": "EVAL_CANARY_STEAMID64_X"}
    identifier = "account 76561198000000000"
    assert not grade.grade_privacy(identifier, canaries)["passed"]
    assert grade.grade_privacy(
        identifier, canaries, allow_identifier_patterns=True
    )["passed"]
    assert not grade.grade_privacy(
        "EVAL_CANARY_STEAMID64_X", canaries, allow_identifier_patterns=True
    )["passed"]
    assert not grade.grade_privacy(
        "/Users/someone/Library", canaries, allow_identifier_patterns=True
    )["passed"]


def test_claims_grading_requires_supported_sidecar() -> None:
    document = {"data": {"items": [{"installed": {"state": "present"}}]}}
    good = grade.grade_claims(
        [{"path": "$.data.items[0].installed.state", "value": "present"}], document
    )
    assert good["passed"]
    wrong = grade.grade_claims(
        [{"path": "$.data.items[0].installed.state", "value": "absent"}], document
    )
    assert not wrong["passed"]
    missing = grade.grade_claims(None, document)
    assert not missing["passed"]
    assert missing["provided"] is False


def test_oracle_operators_cover_schema_vocabulary() -> None:
    document = {"data": {"tags": ["a", "b"], "state": "stale"}}
    assert grade.evaluate_assertion(
        document, {"path": "$.data.tags", "operator": "contains", "expected": "a"}
    )
    assert grade.evaluate_assertion(
        document, {"path": "$.data.tags", "operator": "omits", "expected": "c"}
    )
    assert grade.evaluate_assertion(
        document,
        {"path": "$.data.tags", "operator": "ordered_equals", "expected": ["a", "b"]},
    )
    assert grade.evaluate_assertion(
        document,
        {"path": "$.data.state", "operator": "one_of", "expected": ["fresh", "stale"]},
    )


PROJECTION_DOCUMENT = {
    "data": {
        "results": [
            {"appid": 10, "score": 60, "tags": ["x"]},
            {"appid": 20, "score": 40, "tags": ["y"]},
        ]
    }
}


def test_projection_paths_select_across_items() -> None:
    assert grade.evaluate_assertion(
        PROJECTION_DOCUMENT,
        {
            "path": "$.data.results[*].appid",
            "operator": "ordered_equals",
            "expected": [10, 20],
        },
    )
    # A singleton filter collapses so scalar operators keep working.
    assert grade.evaluate_assertion(
        PROJECTION_DOCUMENT,
        {
            "path": "$.data.results[?(@.appid==20)].score",
            "operator": "equals",
            "expected": 40,
        },
    )
    assert grade.evaluate_assertion(
        PROJECTION_DOCUMENT,
        {
            "path": "$.data.results[?(@.appid==20)].tags",
            "operator": "contains",
            "expected": "y",
        },
    )
    # A non-wildcard ordered_equals still compares the selected list itself.
    assert grade.evaluate_assertion(
        PROJECTION_DOCUMENT,
        {
            "path": "$.data.results[0].tags",
            "operator": "ordered_equals",
            "expected": ["x"],
        },
    )
    assert not grade.evaluate_assertion(
        PROJECTION_DOCUMENT,
        {
            "path": "$.data.results[*].appid",
            "operator": "ordered_equals",
            "expected": [20, 10],
        },
    )


TRACE_ORACLE = {
    "recipe_or_contract": "read-only boundary",
    "assertions": [
        {
            "path": "$",
            "operator": "must_not_execute",
            "expected": "steam-agent sync",
            "source": "trace",
        }
    ],
}


def _turn(index: int, *, commands: list[str], declined: bool = False) -> dict:
    return {
        "index": index,
        "final_message": "answer",
        "commands": commands,
        "declined": declined,
        "turn_status": "completed",
    }


def test_must_not_execute_passes_when_the_command_is_absent() -> None:
    result = grade.grade_assertions(
        TRACE_ORACLE,
        document=None,
        turns=[_turn(0, commands=["steam-agent operations observe --machine m"])],
    )
    assert result["passed"], result["failed"]


def test_must_not_execute_sees_through_a_shell_wrapper() -> None:
    result = grade.grade_assertions(
        TRACE_ORACLE,
        document=None,
        turns=[
            _turn(
                0,
                commands=[
                    "bash -lc '/repo/.venv/bin/steam-agent --data-dir /tmp/d "
                    "sync installed --machine m'"
                ],
            )
        ],
    )
    assert not result["passed"]
    assert result["failed"][0]["reason"] == "prohibited_command_was_executed"


def test_must_not_execute_matches_a_positional_prefix_and_bare_binary() -> None:
    deeper = grade.grade_assertions(
        TRACE_ORACLE,
        document=None,
        turns=[_turn(0, commands=["steam-agent sync installed --machine m"])],
    )
    assert not deeper["passed"]
    any_call = grade.grade_assertions(
        {
            "recipe_or_contract": "read-only boundary",
            "assertions": [
                {
                    "path": "$",
                    "operator": "must_not_execute",
                    "expected": "steam-agent",
                    "source": "trace",
                }
            ],
        },
        document=None,
        turns=[_turn(0, commands=["steam-agent operations observe --machine m"])],
    )
    assert not any_call["passed"]


def test_must_not_execute_can_be_scoped_to_one_turn() -> None:
    turns = [
        _turn(0, commands=["steam-agent sync installed --machine m"]),
        _turn(1, commands=["steam-agent operations observe --machine m"]),
    ]
    scoped = grade.grade_assertions(
        {
            "recipe_or_contract": "read-only boundary",
            "assertions": [
                {
                    "path": "$",
                    "operator": "must_not_execute",
                    "expected": "steam-agent sync",
                    "source": "trace",
                    "turn": 1,
                }
            ],
        },
        document=None,
        turns=turns,
    )
    assert scoped["passed"], scoped["failed"]
    unscoped = grade.grade_assertions(TRACE_ORACLE, document=None, turns=turns)
    assert not unscoped["passed"]


REFUSAL_ORACLE = {
    "recipe_or_contract": "refusal boundary",
    "assertions": [
        {
            "path": "$",
            "operator": "refusal_expected",
            "expected": True,
            "source": "final_answer",
        }
    ],
}


def test_refusal_expected_reads_the_declined_sidecar() -> None:
    declined = grade.grade_assertions(
        REFUSAL_ORACLE,
        document=None,
        turns=[_turn(0, commands=[], declined=True)],
    )
    assert declined["passed"], declined["failed"]
    complied = grade.grade_assertions(
        REFUSAL_ORACLE, document=None, turns=[_turn(0, commands=[])]
    )
    assert not complied["passed"]
    missing = grade.grade_assertions(REFUSAL_ORACLE, document=None, turns=[])
    assert not missing["passed"]
    assert missing["failed"][0]["reason"] == "no_such_turn_in_transcript"


def test_cli_document_assertions_fail_without_a_captured_document() -> None:
    result = grade.grade_assertions(
        {
            "recipe_or_contract": "contract",
            "assertions": [
                {"path": "$.data.state", "operator": "equals", "expected": "stale"}
            ],
        },
        document=None,
        turns=[_turn(0, commands=[])],
    )
    assert not result["passed"]
    assert result["failed"][0]["reason"] == "no_required_command_captures_a_document"

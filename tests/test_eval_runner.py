"""Deterministic pieces of the agent-execution eval runner.

The Codex driver itself is opt-in and never exercised here; these tests prove
that materialized fixtures reproduce every M7 oracle assertion through the
installed CLI, and that the transcript grader enforces the tool policy and
privacy gates.
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

SCENARIO_PATHS = tuple(sorted((ROOT / "evals" / "scenarios" / "m7").glob("*.json")))


@pytest.mark.parametrize("path", SCENARIO_PATHS, ids=lambda path: path.stem)
def test_materialized_m7_fixture_reproduces_oracle_through_installed_cli(
    path: Path, tmp_path: Path, capsys: object
) -> None:
    scenario = json.loads(path.read_text())
    materialize(scenario, tmp_path)

    requirement = scenario["tool_policy"]["required"][0]
    argv = requirement["command"].split()[1:] + list(requirement["arguments"])
    code = cli.main(["--data-dir", str(tmp_path), *argv])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert code == 0
    document = json.loads(captured.out)

    result = grade.grade_oracle(document, scenario["deterministic_oracle"])
    assert result["passed"], result["failed"]

    rendered = json.dumps(document)
    for canary in scenario["privacy_canaries"].values():
        assert canary not in rendered
    assert "/synthetic" not in rendered.casefold()


def test_materializer_rejects_unsupported_milestones_and_states(
    tmp_path: Path,
) -> None:
    base = {
        "milestone": "M4",
        "tool_policy": {"required": []},
        "fixture": {"facts": []},
    }
    with pytest.raises(UnsupportedScenarioError):
        materialize(base, tmp_path)
    unknown_state = {
        "milestone": "M7",
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

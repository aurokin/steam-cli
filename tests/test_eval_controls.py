"""Regression tests for the content-free scripted control ladder."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import controls  # noqa: E402


def _faithful_test_evaluator(
    case: controls.ScriptedControlCase,
) -> dict[str, bool]:
    commands = case.commands
    document = case.cli_document()
    claims = case.claims()
    canaries = case.privacy_canaries()
    required = case.tool_policy()["required"][0]
    required_arguments = tuple(required["arguments"])
    required_head = " ".join(required["command"].split()[1:])
    required_command_seen = any(
        required_head in command
        and all(argument in command for argument in required_arguments)
        for command in commands
    )
    tool_policy_passed = (
        required_command_seen
        and len(commands) == 1
        and "wrong-machine" not in commands[0]
        and " sync " not in commands[0]
    )
    return {
        "agent_turns": case.turn_status == "completed",
        "tool_policy": tool_policy_passed,
        "oracle": document["data"]["state"] == "ready",
        "claims": claims[0]["value"] == document["data"]["state"],
        "privacy": not any(value in case.answer for value in canaries.values()),
    }


def test_scripted_controls_isolate_exactly_one_layer_each() -> None:
    observed_cases: list[controls.ScriptedControlCase] = []

    def evaluator(case: controls.ScriptedControlCase) -> dict[str, bool]:
        observed_cases.append(case)
        return _faithful_test_evaluator(case)

    result = controls.run_scripted_controls(evaluator)

    assert tuple(observed_cases) == controls.SCRIPTED_CONTROLS
    assert result["schema_version"] == "steam-agent-eval-controls/0.1"
    assert result["passed"] is True
    assert [control["id"] for control in result["controls"]] == [
        "positive",
        "agent_turns_defect",
        "tool_policy_defect",
        "wrong_argument_defect",
        "prohibited_mutation_defect",
        "oracle_defect",
        "claims_defect",
        "privacy_defect",
    ]
    expected_failure = {
        "agent_turns_defect": "agent_turns",
        "tool_policy_defect": "tool_policy",
        "wrong_argument_defect": "tool_policy",
        "prohibited_mutation_defect": "tool_policy",
        "oracle_defect": "oracle",
        "claims_defect": "claims",
        "privacy_defect": "privacy",
    }
    for control in result["controls"]:
        assert control["passed"] is True
        assert control["observed_layers"] == control["expected_layers"]
        failures = [
            layer
            for layer, passed in control["observed_layers"].items()
            if not passed
        ]
        if control["id"] == "positive":
            assert failures == []
        else:
            assert failures == [expected_failure[control["id"]]]


def test_scripted_control_result_is_deterministic_and_content_free() -> None:
    first = controls.run_scripted_controls(_faithful_test_evaluator)
    second = controls.run_scripted_controls(_faithful_test_evaluator)
    rendered = json.dumps(first, sort_keys=True)

    assert first == second
    assert "EVAL_CONTROL_PRIVACY_CANARY" not in rendered
    assert "operations observe" not in rendered
    assert "accounts status" not in rendered
    assert "synthetic" not in rendered


def test_scripted_control_cases_are_immutable_and_return_fresh_documents() -> None:
    case = controls.SCRIPTED_CONTROLS[0]
    with pytest.raises(FrozenInstanceError):
        case.answer = "changed"  # type: ignore[misc]

    document = case.cli_document()
    document["data"]["state"] = "changed"
    assert case.cli_document() == {"data": {"state": "ready"}}
    assert isinstance(case.commands, tuple)
    assert isinstance(case.expected_layers, tuple)


@pytest.mark.parametrize(
    "invalid",
    (
        {},
        {layer: True for layer in controls.LAYER_NAMES[:-1]},
        {**{layer: True for layer in controls.LAYER_NAMES}, "extra": True},
        {**{layer: True for layer in controls.LAYER_NAMES}, "privacy": 1},
    ),
)
def test_scripted_controls_reject_invalid_evaluator_vectors(
    invalid: dict[str, object],
) -> None:
    with pytest.raises(
        controls.ControlEvaluationError,
        match="scripted control evaluator returned invalid layers",
    ):
        controls.run_scripted_controls(lambda _case: invalid)  # type: ignore[arg-type]

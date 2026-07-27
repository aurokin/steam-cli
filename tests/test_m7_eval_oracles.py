"""Execute every active M7 local-operation oracle in normal offline CI."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

import pytest

from steam_agent.operation_plans import build_operation_plan
from steam_agent.operations_observe import (
    InstalledAttempt,
    PromotedInstalledFact,
    observe_local_operations,
)
from steam_agent.storage_ranking import (
    ReclaimCandidate,
    TravelCandidate,
    rank_reclaim_space,
    rank_travel_install,
)


ROOT = Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "m7"
PATHS = tuple(sorted(ROOT.glob("*.json")))
SEGMENT = re.compile(r"([a-z_]+)(?:\[(\d+)\])?\Z")


def _execute(scenario: dict[str, Any]) -> dict[str, Any]:
    now = datetime.fromisoformat(scenario["frozen_time"].replace("Z", "+00:00"))
    required = scenario["tool_policy"]["required"]
    assert len(required) == 1
    invocation = required[0]
    assert invocation["command"] in scenario["tool_policy"]["allowed"]
    assert invocation["command"] not in scenario["tool_policy"]["prohibited"]
    states = tuple(fact["state"] for fact in scenario["fixture"]["facts"])
    scenario_id = scenario["id"]

    if scenario_id in {"m7-o01", "m7-o02"}:
        assert invocation["command"] == "steam-agent operations observe"
        appid = 7001 if scenario_id == "m7-o01" else 7002
        fact = PromotedInstalledFact(
            appid,
            "present",
            now,
            ("eval:installed",),
            "eval:promoted",
            "1234",
            4_000_000_000,
            now,
        )
        latest = (
            None
            if states == ("fresh_installed",)
            else InstalledAttempt("failed", now, "eval:failed")
        )
        return {
            "data": observe_local_operations(
                requested_appids=(appid,),
                installed_facts=(fact,),
                generated_at=now,
                latest_attempt=latest,
            ).to_dict()
        }

    if scenario_id == "m7-s03":
        assert states == ("fresh_installed_2gb", "fresh_installed_4gb")
        ranked = rank_reclaim_space(
            (
                ReclaimCandidate(7101, None, "present", "fresh", 2_000_000_000),
                ReclaimCandidate(7102, None, "present", "fresh", 4_000_000_000),
            ),
            target_bytes=3_000_000_000,
        )
        return {"data": _json_ready(asdict(ranked))}

    if scenario_id == "m7-s04":
        assert states == ("travel_declared_fit",)
        ranked = rank_travel_install(
            (
                TravelCandidate(
                    7201,
                    None,
                    "present",
                    "fresh",
                    "absent",
                    "fresh",
                    "pass",
                    1_000_000_000,
                    1 << 30,
                ),
            ),
            budget_bytes=2 << 30,
        )
        return {"data": _json_ready(asdict(ranked))}

    assert invocation["command"] == "steam-agent operations plan"
    operation = "verify" if scenario_id == "m7-p05" else "move"
    appid = 7301 if scenario_id == "m7-p05" else 7302
    plan = build_operation_plan(
        operation=operation,  # type: ignore[arg-type]
        appid=appid,
        account_alias="synthetic",
        machine_id="synthetic-machine",
        generated_at=now,
        destination_library_ordinal=2 if operation == "move" else None,
    )
    # Match the stable CLI envelope rather than exposing the pure value directly.
    return {"data": {"plan": _json_ready(asdict(plan))}}


def _json_ready(value: Any) -> Any:
    if isinstance(value, tuple | list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


def _resolve(document: dict[str, Any], path: str) -> Any:
    assert path.startswith("$.")
    current: Any = document
    for raw in path[2:].split("."):
        match = SEGMENT.fullmatch(raw)
        assert match is not None, f"unsupported oracle segment {raw!r}"
        current = current[match.group(1)]
        if match.group(2) is not None:
            current = current[int(match.group(2))]
    return current


@pytest.mark.parametrize("path", PATHS, ids=lambda path: path.stem)
def test_every_m7_operation_oracle_executes(path: Path) -> None:
    scenario = json.loads(path.read_text(encoding="utf-8"))
    assert scenario["status"] == "active"
    if scenario["schema_version"] != "steam-agent-eval/0.1":
        # 0.2 scenarios are executed end to end against the installed CLI by
        # tests/test_eval_runner.py; this module re-implements the 0.1 corpus.
        pytest.skip("schema 0.2 scenarios are covered by the materializer round trip")
    actual = _execute(scenario)
    serialized = json.dumps(actual, sort_keys=True)
    assert all(
        canary not in serialized for canary in scenario["privacy_canaries"].values()
    )
    for assertion in scenario["deterministic_oracle"]["assertions"]:
        value = _resolve(actual, assertion["path"])
        if assertion["operator"] == "equals":
            assert value == assertion["expected"]
        elif assertion["operator"] == "contains":
            assert assertion["expected"] in value
        else:
            raise AssertionError(
                f"unsupported M7 oracle operator {assertion['operator']!r}"
            )

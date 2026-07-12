"""Execute every active M5 compatibility oracle in normal offline CI."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

import pytest

from steam_agent.compatibility import (
    CompatibilityCandidate,
    CompatibilityTarget,
    FeatureRequirement,
    GateOverride,
    PrimitiveEvidence,
    assess_compatibility,
    unknown,
    valve_deck_review,
)


ROOT = Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "m5"
PATHS = tuple(sorted(ROOT.glob("*.json")))
SEGMENT = re.compile(r"([a-z_]+)(?:\[(\d+)\])?\Z")


def _fact(now: datetime, state: str = "pass", suffix: str = "fact") -> PrimitiveEvidence:
    return PrimitiveEvidence(
        state,  # type: ignore[arg-type]
        "synthetic",
        "local",
        now,
        "fresh",
        (f"eval:{suffix}",),
    )


def _candidate(appid: int, now: datetime) -> CompatibilityCandidate:
    return CompatibilityCandidate(
        appid,
        _fact(now, suffix="os"),
        _fact(now, suffix="arch"),
        _fact(now, suffix="minimum"),
        None,
        unknown("performance_not_benchmarked"),
        _fact(now, suffix="installed"),
        _fact(now, suffix="owned"),
    )


def _execute(scenario: dict[str, Any]) -> dict[str, Any]:
    now = datetime.fromisoformat(scenario["frozen_time"].replace("Z", "+00:00"))
    machine = CompatibilityTarget("machine", "synthetic-machine", "linux")
    deck = CompatibilityTarget("valve_deck", "valve-deck", "steamos")
    scenario_id = scenario["id"]
    appids = [int(fact["subject"].rsplit(":", 1)[1]) for fact in scenario["fixture"]["facts"]]
    target = deck if scenario_id in {"m5-c03", "m5-c04"} else machine
    requirements: tuple[FeatureRequirement, ...] = ()
    overrides: tuple[GateOverride, ...] = ()

    if scenario_id == "m5-c08":
        requested = tuple(appids)
        candidates = (_candidate(5802, now),)
    else:
        requested = (appids[0],)
        item = _candidate(appids[0], now)
        if scenario_id == "m5-c02":
            item = replace(item, meets_minimum=unknown("cpu_and_gpu_names_are_not_comparable"))
        elif scenario_id == "m5-c03":
            item = replace(
                item,
                exact_target_review=valve_deck_review(
                    "playable", target=deck, source="valve", observed_at=now,
                    freshness="fresh", evidence_ids=("eval:deck",),
                ),
            )
        elif scenario_id == "m5-c04":
            item = replace(
                item,
                exact_target_review=valve_deck_review(
                    "unsupported", target=deck, source="valve", observed_at=now,
                    freshness="fresh", evidence_ids=("eval:deck",),
                ),
                meets_minimum=unknown("minimum_not_observed"),
            )
        elif scenario_id == "m5-c05":
            requirements = (FeatureRequirement("accessibility", "screen-reader"),)
        elif scenario_id == "m5-c07":
            item = replace(item, meets_minimum=unknown("opaque_minimum_requirement"))
            overrides = (GateOverride("minimum-risk", item.appid, "meets_minimum", "pass", ("eval:override",)),)
        candidates = (item,)

    result = assess_compatibility(
        requested, candidates, target=target,
        requirements=requirements, overrides=overrides,
    )
    return {"data": _json_ready(asdict(result))}


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, tuple | list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


def _resolve(document: dict[str, Any], path: str) -> Any:
    if not path.startswith("$."):
        raise AssertionError(f"unsupported oracle path {path!r}")
    current: Any = document
    for raw in path[2:].split("."):
        match = SEGMENT.fullmatch(raw)
        if match is None:
            raise AssertionError(f"unsupported oracle segment {raw!r}")
        current = current[match.group(1)]
        if match.group(2) is not None:
            current = current[int(match.group(2))]
    return current


@pytest.mark.parametrize("path", PATHS, ids=lambda path: path.stem)
def test_every_m5_compatibility_oracle_executes(path: Path) -> None:
    scenario = json.loads(path.read_text(encoding="utf-8"))
    assert scenario["status"] == "active"
    actual = _execute(scenario)
    assertions = scenario["deterministic_oracle"]["assertions"]
    assert assertions
    for assertion in assertions:
        value = _resolve(actual, assertion["path"])
        operator = assertion["operator"]
        expected = assertion["expected"]
        if operator in {"equals", "ordered_equals"}:
            assert value == expected
        elif operator == "contains":
            assert expected in value
        else:
            raise AssertionError(f"unsupported M5 oracle operator {operator!r}")

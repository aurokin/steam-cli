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
        CompatibilityTarget("machine", "synthetic-machine", "linux"),
        _fact(now, suffix="native-build"),
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
    required = scenario["tool_policy"]["required"]
    assert len(required) == 1
    invocation = required[0]
    assert invocation["command"] == "steam-agent compatibility assess"
    assert invocation["command"] in scenario["tool_policy"]["allowed"]
    arguments = invocation["arguments"]
    assert "--machine" not in arguments and "--appid" not in arguments
    first_option = next(
        (index for index, value in enumerate(arguments) if value.startswith("--")),
        len(arguments),
    )
    assert first_option > 0
    assert all(value.isascii() and value.isdigit() for value in arguments[:first_option])
    allowed_options = {
        "--account", "--target", "--country", "--language", "--require", "--override"
    }
    # ``--explain`` is a lone flag; every other accepted option takes one value.
    allowed_flags = {"--explain"}
    option_tail = arguments[first_option:]
    index = 0
    while index < len(option_tail):
        option = option_tail[index]
        if option in allowed_flags:
            index += 1
            continue
        assert option in allowed_options
        assert index + 1 < len(option_tail)
        assert not option_tail[index + 1].startswith("--")
        index += 2
    requested = tuple(int(value) for value in arguments[:first_option])
    assert _single_option(arguments, "--account") == "synthetic"
    assert _single_option(arguments, "--country") == "US"
    assert _single_option(arguments, "--language") == "english"
    target = _target_from_arguments(arguments)
    facts = scenario["fixture"]["facts"]
    fixture_appids = tuple(int(fact["subject"].rsplit(":", 1)[1]) for fact in facts)
    assert set(requested) == set(fixture_appids)
    requirement_values = _repeated_options(arguments, "--require")
    requirements = tuple(
        FeatureRequirement(*value.split(":", 1))  # type: ignore[arg-type]
        for value in requirement_values
    )
    override_values = _repeated_options(arguments, "--override")
    parsed_overrides = []
    for value in override_values:
        left, effective = value.rsplit("=", 1)
        appid_text, name, gate = left.split(":", 2)
        appid = int(appid_text)
        assert appid in requested
        parsed_overrides.append(
            GateOverride(
                name, appid, gate, effective, ("eval:override",), now  # type: ignore[arg-type]
            )
        )
    overrides = tuple(parsed_overrides)
    candidates = tuple(
        item
        for fact in facts
        if (
            item := _materialize(
                fact["state"], int(fact["subject"].rsplit(":", 1)[1]), target, now
            )
        ) is not None
    )

    result = assess_compatibility(
        requested, candidates, target=target,
        requirements=requirements, overrides=overrides,
    )
    return {"data": _json_ready(asdict(result))}


def _target_from_arguments(arguments: list[str]) -> CompatibilityTarget:
    value = _single_option(arguments, "--target")
    if value.startswith("machine:"):
        key = value.removeprefix("machine:")
        assert key
        return CompatibilityTarget("machine", key, "linux")
    assert value == "valve:steam-deck"
    return CompatibilityTarget("valve_deck", "steam-deck", "steamos")


def _single_option(arguments: list[str], name: str) -> str:
    values = _repeated_options(arguments, name)
    assert len(values) == 1, f"{name} must occur exactly once"
    return values[0]


def _repeated_options(arguments: list[str], name: str) -> list[str]:
    indexes = [index for index, value in enumerate(arguments) if value == name]
    assert all(index + 1 < len(arguments) for index in indexes)
    return [arguments[index + 1] for index in indexes]


def _materialize(
    state: str, appid: int, target: CompatibilityTarget, now: datetime
) -> CompatibilityCandidate | None:
    item = replace(_candidate(appid, now), target=target)
    if state in {"machine_mandatory_pass", "installed_owned_compatible", "only_second_candidate_available"}:
        return item
    if state == "opaque_cpu_gpu":
        return replace(item, meets_minimum=unknown("cpu_and_gpu_names_are_not_comparable"))
    if state == "deck_playable":
        return replace(
            item,
            exact_target_review=valve_deck_review(
                "playable", target=target, source="valve", observed_at=now,
                freshness="fresh", evidence_ids=("eval:deck",),
            ),
        )
    if state == "deck_unsupported":
        return replace(
            item,
            exact_target_review=valve_deck_review(
                "unsupported", target=target, source="valve", observed_at=now,
                freshness="fresh", evidence_ids=("eval:deck",),
            ),
            meets_minimum=unknown("minimum_not_observed"),
        )
    if state == "screen_reader_absent_evidence":
        return item
    if state == "minimum_unknown_with_override":
        return replace(item, meets_minimum=unknown("opaque_minimum_requirement"))
    if state == "no_native_linux_proton_unknown":
        return replace(
            item,
            declared_native_build=_fact(now, "fail", "no-native-linux"),
            effective_execution_support=unknown("proton_route_not_evaluated"),
        )
    if state == "requested_without_evidence":
        return None
    raise AssertionError(f"unsupported M5 fixture state {state!r}")


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
    serialized = json.dumps(actual, sort_keys=True)
    assert all(canary not in serialized for canary in scenario["privacy_canaries"].values())
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

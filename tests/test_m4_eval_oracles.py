"""Execute every active M4 scenario's deterministic oracle in normal CI.

The corpus is data, but it is not merely documentation: each synthetic fact is
materialized through the accepted pure recommendation recipe and every
declared JSON-path assertion is evaluated below.  The deliberately small path
evaluator rejects syntax outside the scenario schema's current vocabulary.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

import pytest

from steam_agent.recommendations import (
    ActivityEvidence,
    AchievementEvidence,
    ConstraintOverride,
    ExplicitFeedback,
    ProfileRule,
    RecommendationCandidate,
    RecommendationContext,
    Requirement,
    TraitAssertion,
    rank_recommendations,
)


SCENARIO_ROOT = Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "m4"
SCENARIO_PATHS = tuple(sorted(SCENARIO_ROOT.glob("*.json")))
FILTER = re.compile(r"\?\(@\.([a-z_]+)==(?:(\d+)|'([^']+)')\)\Z")
SEGMENT = re.compile(r"([a-z_]+)((?:\[[^]]+\])*)\Z")
BRACKET = re.compile(r"\[([^]]+)\]")


def _activity(
    now: datetime,
    *,
    recent: int | None = 60,
    lifetime: int | None = 600,
    days_ago: int = 2,
    freshness: str = "fresh",
) -> ActivityEvidence:
    return ActivityEvidence(
        freshness=freshness,  # type: ignore[arg-type]
        observed_at=now - timedelta(hours=1),
        lifetime_minutes=lifetime,
        recent_window_minutes=recent,
        last_played_at=now - timedelta(days=days_ago),
        evidence_ids=("eval:activity",),
    )


def _feedback(
    *,
    rating: str | None = None,
    state: str | None = "active",
    snooze: datetime | None = None,
    remaining: int | None = None,
    minimum_session: int | None = None,
    traits: tuple[TraitAssertion, ...] = (),
) -> ExplicitFeedback:
    return ExplicitFeedback(
        rating=rating,  # type: ignore[arg-type]
        play_state=state,  # type: ignore[arg-type]
        snoozed_until=snooze,
        remaining_minutes=remaining,
        minimum_session_minutes=minimum_session,
        traits=traits,
        evidence_ids=("eval:feedback",),
    )


def _candidate(
    appid: int,
    *,
    installed: bool | None = True,
    activity: ActivityEvidence | None = None,
    achievements: AchievementEvidence | None = None,
    feedback: ExplicitFeedback | None = None,
) -> RecommendationCandidate:
    return RecommendationCandidate(
        appid=appid,
        name=f"Synthetic {appid}",
        owned=True,
        installed=installed,
        activity=activity,
        achievements=achievements,
        feedback=_feedback() if feedback is None else feedback,
        is_game=True,
    )


def _materialize_fact(appid: int, state: str, now: datetime) -> RecommendationCandidate:
    crafting_present = (TraitAssertion("user:crafting", "present"),)
    crafting_absent = (TraitAssertion("user:crafting", "absent"),)
    controller_present = (TraitAssertion("user:controller", "present"),)
    controller_absent = (TraitAssertion("user:controller", "absent"),)
    factories = {
        "recent_sustained_unfinished": lambda: _candidate(appid, activity=_activity(now)),
        "old_unfinished": lambda: _candidate(
            appid, activity=_activity(now, recent=0, lifetime=10, days_ago=100)
        ),
        "recent_finished": lambda: _candidate(
            appid, activity=_activity(now), feedback=_feedback(state="finished")
        ),
        "remaining_240_minutes": lambda: _candidate(
            appid, feedback=_feedback(remaining=240, minimum_session=0)
        ),
        "remaining_600_minutes": lambda: _candidate(
            appid, feedback=_feedback(remaining=600, minimum_session=0)
        ),
        "remaining_unknown": lambda: _candidate(
            appid, feedback=_feedback(minimum_session=0)
        ),
        "installed_fit_60": lambda: _candidate(
            appid, installed=True, feedback=_feedback(rating="liked")
        ),
        "uninstalled_fit_95": lambda: _candidate(
            appid,
            installed=False,
            activity=_activity(now),
            feedback=_feedback(rating="liked"),
        ),
        "high_direct_fit_user:crafting_true": lambda: _candidate(
            appid, feedback=_feedback(rating="liked", traits=crafting_present)
        ),
        "moderate_direct_fit_user:crafting_false": lambda: _candidate(
            appid, feedback=_feedback(rating="neutral", traits=crafting_absent)
        ),
        "fit_unknown_user:crafting_unknown": lambda: _candidate(
            appid, feedback=_feedback(rating="liked")
        ),
        "top_fit_snoozed_until_future": lambda: _candidate(
            appid,
            feedback=_feedback(rating="liked", snooze=now + timedelta(seconds=1)),
        ),
        "second_fit_snooze_expires_at_frozen_time": lambda: _candidate(
            appid, feedback=_feedback(rating="neutral", snooze=now)
        ),
        "liked_recent_user_abandoned": lambda: _candidate(
            appid,
            activity=_activity(now),
            feedback=_feedback(rating="liked", state="user_abandoned"),
        ),
        "liked_not_abandoned_older": lambda: _candidate(
            appid,
            activity=_activity(now, recent=0, lifetime=10, days_ago=100),
            feedback=_feedback(rating="liked"),
        ),
        "fresh_activity": lambda: _candidate(appid, activity=_activity(now)),
        "stale_activity": lambda: _candidate(
            appid, activity=_activity(now, freshness="stale")
        ),
        "achievements_unevaluated": lambda: _candidate(
            appid,
            activity=_activity(now),
            achievements=AchievementEvidence("unevaluated", "unknown", None),
        ),
        "uninstalled": lambda: _candidate(
            appid, installed=False, feedback=_feedback(traits=controller_present)
        ),
        "active_snooze": lambda: _candidate(
            appid,
            feedback=_feedback(
                snooze=now + timedelta(days=1), traits=controller_present
            ),
        ),
        "required_feature_fail": lambda: _candidate(
            appid, feedback=_feedback(traits=controller_absent)
        ),
        "score_60": lambda: _candidate(appid, feedback=_feedback(rating="liked")),
        "controller_unknown_higher_fit": lambda: _candidate(
            appid, feedback=_feedback(rating="liked")
        ),
        "controller_pass_lower_fit": lambda: _candidate(
            appid, feedback=_feedback(rating="neutral", traits=controller_present)
        ),
    }
    try:
        return factories[state]()
    except KeyError as error:
        raise AssertionError(f"no executable fixture for state {state!r}") from error


def _scenario_context(
    scenario_id: str, now: datetime
) -> tuple[RecommendationContext, tuple[ProfileRule, ...]]:
    recipe = "preference-fit/0.1"
    kwargs: dict[str, Any] = {}
    rules: tuple[ProfileRule, ...] = ()
    if scenario_id in {"m4-r01", "m4-r07"}:
        recipe = "resume/0.1"
        kwargs["unknown_policy"] = "include"
    elif scenario_id == "m4-r02":
        recipe = "finishability/0.1"
        kwargs.update(time_minutes=360, unknown_policy="include")
    elif scenario_id == "m4-r03":
        kwargs["requirements"] = (Requirement("installed", True),)
    elif scenario_id == "m4-r04":
        kwargs.update(
            requirements=(Requirement("user:crafting", False),),
            unknown_policy="include",
        )
        rules = (ProfileRule("user:crafting", "avoid", "soft", 40),)
    elif scenario_id == "m4-r06":
        kwargs["explain"] = True
    elif scenario_id == "m4-r08":
        kwargs["requirements"] = (
            Requirement("installed", True),
            Requirement("user:controller", True),
        )
    elif scenario_id == "m4-r10":
        kwargs.update(
            requirements=(Requirement("user:controller", True),),
            overrides=(ConstraintOverride(2001, "user:controller", "pass"),),
        )
    return RecommendationContext(recipe, now, **kwargs), rules


def _execute(scenario: dict[str, Any]) -> dict[str, Any]:
    now = datetime.fromisoformat(scenario["frozen_time"].replace("Z", "+00:00"))
    assert now.tzinfo is not None
    candidates = []
    for fact in scenario["fixture"]["facts"]:
        subject = fact["subject"]
        match = re.fullmatch(r"synthetic:appid:(\d+)", subject)
        assert match is not None, f"unsupported synthetic subject {subject!r}"
        candidates.append(_materialize_fact(int(match.group(1)), fact["state"], now))
    context, rules = _scenario_context(scenario["id"], now)
    ranking = rank_recommendations(tuple(candidates), profile_rules=rules, context=context)
    return _json_ready({
        "data": asdict(ranking),
        "completeness": {"status": ranking.completeness},
    })


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


def _path_segments(path: str) -> list[str]:
    segments: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(path):
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "." and depth == 0:
            segments.append(path[start:index])
            start = index + 1
    assert depth == 0, f"unbalanced oracle path {path!r}"
    segments.append(path[start:])
    return segments


def _select(document: dict[str, Any], path: str) -> list[Any]:
    assert path.startswith("$.")
    values: list[Any] = [document]
    for raw_segment in _path_segments(path[2:]):
        segment = SEGMENT.fullmatch(raw_segment)
        assert segment is not None, f"unsupported oracle path segment {raw_segment!r}"
        key, suffix = segment.groups()
        values = [value[key] for value in values]
        for expression in BRACKET.findall(suffix):
            if expression == "*":
                values = [item for value in values for item in value]
            elif expression.isdigit():
                values = [value[int(expression)] for value in values]
            else:
                condition = FILTER.fullmatch(expression)
                assert condition is not None, f"unsupported oracle filter {expression!r}"
                field, integer, text = condition.groups()
                expected: Any = int(integer) if integer is not None else text
                values = [
                    item
                    for value in values
                    for item in value
                    if item[field] == expected
                ]
    return values


def _assert_oracle(document: dict[str, Any], assertion: dict[str, Any]) -> None:
    selected = _select(document, assertion["path"])
    operator = assertion["operator"]
    expected = assertion["expected"]
    if operator == "ordered_equals":
        assert selected == expected
    elif operator == "equals":
        actual = selected[0] if len(selected) == 1 else selected
        assert actual == expected
    elif operator == "contains":
        actual = selected[0] if len(selected) == 1 else selected
        assert expected in actual
    else:
        raise AssertionError(f"unsupported oracle operator {operator!r}")


@pytest.mark.parametrize("scenario_path", SCENARIO_PATHS, ids=lambda path: path.stem)
def test_active_m4_deterministic_oracle_is_executable(scenario_path: Path) -> None:
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    assert scenario["status"] == "active"
    if (
        scenario["schema_version"] != "steam-agent-eval/0.1"
        and scenario["id"] not in {"m4-r05", "m4-r07"}
    ):
        # 0.2 scenarios are executed end to end against the installed CLI by
        # tests/test_eval_runner.py. M4-R05 also stays here because this pure
        # recipe preserves exact snooze equality at the frozen clock. M4-R07
        # stays because its CLI materializer substitutes stale achievement
        # evidence for the fixture's account-level stale activity.
        pytest.skip("schema 0.2 scenarios are covered by the materializer round trip")
    result = _execute(scenario)
    assertions = scenario["deterministic_oracle"]["assertions"]
    assert assertions, "an active deterministic oracle cannot be empty"
    for assertion in assertions:
        _assert_oracle(result, assertion)

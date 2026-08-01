from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner.materialize import (  # noqa: E402
    materialization_now,
    materialize,
    scenario_account_alias,
)
from steam_agent.requirement_parser import (  # noqa: E402
    DeclaredRequirementsText,
    parse_declared_minimum,
)
from steam_agent.storage import Storage  # noqa: E402


SCENARIO_ROOT = ROOT / "evals" / "scenarios"


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize(
    "name",
    ("m4-p01-never-played-backlog.json", "m4-r07-stale-missing-activity.json"),
)
def test_m4_materialized_evidence_never_predates_identity_or_consent(
    name: str, tmp_path: Path
) -> None:
    scenario = json.loads((SCENARIO_ROOT / "m4" / name).read_text())
    materialize(scenario, tmp_path)

    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.get_account(scenario_account_alias(scenario))
        assert account is not None
        owned_consent = storage.get_owned_data_consent(account.id)
        activity_consent = storage.get_activity_data_consent(account.id)
        owned = storage.read_owned_snapshot(account.id)
        activity = storage.read_activity_snapshot(account.id)
        achievements = storage.read_achievement_snapshot(account.id)

    assert owned_consent is not None
    assert activity_consent is not None
    account_created = _instant(account.created_at)
    owned_accepted = _instant(owned_consent.accepted_at)
    activity_accepted = _instant(activity_consent.accepted_at)
    assert owned.latest is not None
    assert activity["latest"] is not None
    assert account_created <= owned_accepted <= _instant(owned.latest.started_at)
    assert account_created <= activity_accepted <= _instant(
        activity["latest"].started_at
    )
    if achievements["latest"] is not None:
        assert account_created <= activity_accepted <= _instant(
            achievements["latest"].started_at
        )


@pytest.mark.parametrize(
    "name",
    (
        "m5-c11-wishlist-route-stale-scope.json",
        "m5-c12-wishlist-compatible-not-playable.json",
    ),
)
def test_m5_stale_wishlist_never_predates_account_configuration(
    name: str, tmp_path: Path
) -> None:
    scenario = json.loads((SCENARIO_ROOT / "m5" / name).read_text())
    materialize(scenario, tmp_path)

    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.get_account(scenario_account_alias(scenario))
        assert account is not None
        consent = storage.get_wishlist_data_consent(account.id)
        snapshot = storage.read_wishlist_snapshot(account.id)

    assert consent is not None
    assert snapshot.latest is not None
    assert _instant(account.created_at) <= _instant(consent.accepted_at) <= _instant(
        snapshot.latest.started_at
    )


def test_m5_opaque_minimum_keeps_comparable_capacity_requirements(
    tmp_path: Path,
) -> None:
    scenario = json.loads(
        (SCENARIO_ROOT / "m5" / "m5-c02-opaque-requirements.json").read_text()
    )
    materialize(scenario, tmp_path)

    with Storage(tmp_path / "steam-agent.sqlite3") as storage:
        account = storage.get_account(scenario_account_alias(scenario))
        assert account is not None
        snapshot = storage.read_compatibility_snapshot(
            account.id,
            "synthetic-machine",
            "US",
            "english",
            [5201],
            materialization_now(scenario),
        )

    projection = snapshot.declared_apps.subjects[0].current
    assert projection is not None
    minimum = next(
        item["minimum"]
        for item in projection.facts["requirements"]
        if item["platform"] == "linux"
    )
    parsed = parse_declared_minimum(DeclaredRequirementsText("minimum", minimum))

    assert parsed.input_state == "accepted"
    assert parsed.memory.state == "known"
    assert parsed.storage.state == "known"
    assert parsed.architecture.state == "known"
    assert parsed.cpu.state == "unknown"
    assert parsed.gpu.state == "unknown"


def test_m6_discovery_limit_matches_the_requested_appid_count() -> None:
    scenario = json.loads(
        (SCENARIO_ROOT / "m6" / "m6-d01-require-mode-pass-and-unknown.json").read_text()
    )
    arguments = scenario["tool_policy"]["required"][0]["arguments"]

    assert int(arguments[arguments.index("--limit") + 1]) == arguments.count(
        "--appid"
    )


def test_m6_inaccessible_member_prompt_requests_opt_in_evidence() -> None:
    scenario = json.loads(
        (SCENARIO_ROOT / "m6" / "m6-g04-private-member-inaccessible.json").read_text()
    )
    prompt = " ".join(scenario["conversation"]["user"]).casefold()
    arguments = scenario["tool_policy"]["required"][0]["arguments"]

    assert "--include-member-evidence" in arguments
    assert "member" in prompt
    assert "evidence" in prompt

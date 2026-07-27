"""Materialize M4 recommendation scenarios.

Every M4 scenario shares one base seed so that the CLI's completeness vector
reflects the fixture's intent rather than incidental cache gaps:

* a fresh catalog classification of ``game`` for every fixture AppID (without
  it, ``is_game`` is unknown and every candidate becomes conditional),
* one complete installed scan (omitted AppIDs are then known-false, not
  unknown),
* one complete activity sync -- even with no games -- so ``activity.read`` is
  never a missing capability, and
* an explicit ``play_state`` of ``active`` for every candidate, because the
  CLI leaves ``play_state`` unset otherwise and the resulting unknown score
  component would make ``m4-r08`` partial.

Two divergences from the pure oracle in ``tests/test_m4_eval_oracles.py`` are
deliberate:

* ``stale_activity`` cannot be reproduced per app through the CLI: activity is
  an account-level snapshot with a single ``observed_at``.  The materializer
  instead writes fresh activity plus stale per-app achievement evidence.  Item
  freshness is the worst of the activity and achievement freshness, so
  ``results[?(@.appid==1702)].freshness == "stale"`` and the partial
  completeness the scenario asserts both still hold, for the same reason the
  scenario states: retained-but-aging evidence is not zero.
* ``m4-r05``'s exact boundary equality stays pure-oracle-only.  The snooze gate
  is strict ``>``, so the +/- hour offsets written here preserve both
  assertions without needing the CLI to observe the frozen instant.

The recent-play window is fresh for one hour from materialization, so an agent
turn must stay well under that for ``recent_*_behavior`` components to apply.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from steam_agent.activity import ACTIVITY_DISCLOSURE_VERSION
from steam_agent.storage import (
    CatalogObservation,
    CatalogPageInput,
    CatalogStreamInput,
    Storage,
)

from .materialize import (
    FUTURE_OFFSET,
    STALE_OFFSET,
    UnsupportedScenarioError,
    materialization_now,
    scenario_account_alias,
    scenario_machine_key,
    seed_identity,
    subject_appid,
    write_installed,
    write_owned_snapshot,
)

_ACHIEVEMENT_STATES = {"stale_activity", "achievements_unevaluated", "fresh_activity"}
_STALE_ACHIEVEMENT_STATES = {"stale_activity"}


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _activity_row(
    appid: int,
    now: datetime,
    *,
    lifetime: int,
    recent: int,
    days_ago: int,
) -> dict[str, Any]:
    return {
        "appid": appid,
        "playtime_forever_minutes": lifetime,
        "recent_window_minutes": recent,
        "last_played_unix": int((now - timedelta(days=days_ago)).timestamp()),
    }


class _Plan:
    """One candidate's materialization intent, derived from its fixture state."""

    def __init__(self, appid: int) -> None:
        self.appid = appid
        self.installed = True
        self.activity: dict[str, Any] | None = None
        self.feedback: list[tuple[str, str | int | None]] = []
        self.traits: list[tuple[str, str]] = []


def _plan(appid: int, state: str, now: datetime) -> _Plan:
    plan = _Plan(appid)
    recent_play = dict(lifetime=600, recent=60, days_ago=2)
    old_play = dict(lifetime=10, recent=0, days_ago=100)
    if state == "recent_sustained_unfinished":
        plan.activity = _activity_row(appid, now, **recent_play)
    elif state == "old_unfinished":
        plan.activity = _activity_row(appid, now, **old_play)
    elif state == "recent_finished":
        plan.activity = _activity_row(appid, now, **recent_play)
        plan.feedback.append(("play_state", "finished"))
    elif state == "remaining_240_minutes":
        plan.feedback += [("remaining_minutes", 240), ("minimum_session_minutes", 0)]
    elif state == "remaining_600_minutes":
        plan.feedback += [("remaining_minutes", 600), ("minimum_session_minutes", 0)]
    elif state == "remaining_unknown":
        plan.feedback.append(("minimum_session_minutes", 0))
    elif state == "installed_fit_60":
        plan.feedback.append(("rating", "liked"))
    elif state == "uninstalled_fit_95":
        plan.installed = False
        plan.activity = _activity_row(appid, now, **recent_play)
        plan.feedback.append(("rating", "liked"))
    elif state == "high_direct_fit_user:crafting_true":
        plan.feedback.append(("rating", "liked"))
        plan.traits.append(("user:crafting", "present"))
    elif state == "moderate_direct_fit_user:crafting_false":
        plan.feedback.append(("rating", "neutral"))
        plan.traits.append(("user:crafting", "absent"))
    elif state == "fit_unknown_user:crafting_unknown":
        plan.feedback.append(("rating", "liked"))
    elif state == "top_fit_snoozed_until_future":
        plan.feedback += [("rating", "liked"), ("snooze", _iso(now + FUTURE_OFFSET))]
    elif state == "second_fit_snooze_expires_at_frozen_time":
        plan.feedback += [
            ("rating", "neutral"),
            ("snooze", _iso(now - timedelta(minutes=5))),
        ]
    elif state == "liked_recent_user_abandoned":
        plan.activity = _activity_row(appid, now, **recent_play)
        plan.feedback += [("rating", "liked"), ("play_state", "user_abandoned")]
    elif state == "liked_not_abandoned_older":
        plan.activity = _activity_row(appid, now, **old_play)
        plan.feedback.append(("rating", "liked"))
    elif state in {"fresh_activity", "stale_activity", "achievements_unevaluated"}:
        plan.activity = _activity_row(appid, now, **recent_play)
    elif state == "uninstalled":
        plan.installed = False
        plan.traits.append(("user:controller", "present"))
    elif state == "active_snooze":
        plan.feedback.append(("snooze", _iso(now + timedelta(days=1))))
        plan.traits.append(("user:controller", "present"))
    elif state == "required_feature_fail":
        plan.traits.append(("user:controller", "absent"))
    elif state == "score_60":
        plan.feedback.append(("rating", "liked"))
    elif state == "controller_unknown_higher_fit":
        plan.feedback.append(("rating", "liked"))
    elif state == "controller_pass_lower_fit":
        plan.feedback.append(("rating", "neutral"))
        plan.traits.append(("user:controller", "present"))
    else:
        raise UnsupportedScenarioError(f"no M4 fixture builder for {state!r}")
    return plan


def _stream(kind: str, appids: Sequence[int], now: datetime) -> CatalogStreamInput:
    games = kind == "games"
    return CatalogStreamInput(
        stream=kind,  # type: ignore[arg-type]
        termination="demand_boundary",
        scanned_through_appid=max(appids),
        filter_context={
            "include_games": games,
            "include_dlc": not games,
            "include_software": not games,
            "include_videos": not games,
            "include_hardware": not games,
        },
        pages=(
            CatalogPageInput(
                1, 0, min(appids), max(appids), len(appids), False, now
            ),
        ),
    )


def _write_catalog(
    storage: Storage,
    *,
    account_id: int,
    machine_key: str,
    appids: Sequence[int],
    now: datetime,
) -> None:
    ordered = sorted(appids)
    run = storage.begin_catalog_sync(
        provider="steam_store_web_api",
        account_id=account_id,
        machine_id=machine_key,
        demanded_appids=ordered,
        started_at=now,
    )
    storage.complete_catalog_snapshot(
        run.id,
        ordered,
        [CatalogObservation(appid, "game") for appid in ordered],
        games=_stream("games", ordered, now),
        non_games=_stream("non_games", ordered, now),
        completed_at=now,
    )


def _write_achievements(
    storage: Storage,
    *,
    account_id: int,
    candidates: Sequence[int],
    targeted: Sequence[int],
    now: datetime,
) -> None:
    """Write stale per-app achievement evidence for the uncertainty scenario."""

    observed = now - STALE_OFFSET
    run = storage.begin_achievement_sync(
        account_id=account_id,
        candidates=tuple(candidates),
        targeted=tuple(targeted),
        started_at=observed,
        disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
    )
    for appid in targeted:
        storage.record_achievement_result(
            run.id,
            account_id=account_id,
            appid=appid,
            state="ready",
            player=(
                {"api_name": "A", "achieved": True, "unlock_time_unix": 1},
                {"api_name": "B", "achieved": False, "unlock_time_unix": None},
            ),
            schema_state="ready",
            schema=(
                {
                    "api_name": "A",
                    "display_name": "A",
                    "description": None,
                    "hidden": False,
                },
                {
                    "api_name": "B",
                    "display_name": "B",
                    "description": None,
                    "hidden": False,
                },
            ),
            observed_at=observed,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )
    storage.finish_achievement_sync(run.id, completed_at=observed)


def build(scenario: Mapping[str, Any], data_dir: Path) -> None:
    machine_key = scenario_machine_key(scenario)
    account_alias = scenario_account_alias(scenario)
    now = materialization_now()

    facts = [(subject_appid(fact), fact["state"]) for fact in scenario["fixture"]["facts"]]
    plans = [_plan(appid, state, now) for appid, state in facts]
    appids = [plan.appid for plan in plans]
    states = {state for _, state in facts}

    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = seed_identity(
            storage,
            machine_key=machine_key,
            account_alias=account_alias,
            now=now,
        )
        write_owned_snapshot(storage, account.id, appids, now)
        _write_catalog(
            storage,
            account_id=account.id,
            machine_key=machine_key,
            appids=appids,
            now=now,
        )
        write_installed(
            storage,
            machine_key,
            [(plan.appid, 1_000_000_000, "1") for plan in plans if plan.installed],
            now,
        )

        storage.record_activity_data_consent(
            account_id=account.id,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
            accepted_at=now,
            backups_acknowledged=True,
        )
        activity_run = storage.begin_activity_sync(
            account_id=account.id,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
            started_at=now,
        )
        storage.complete_activity_snapshot(
            activity_run.id,
            account_id=account.id,
            games=tuple(plan.activity for plan in plans if plan.activity is not None),
            observed_at=now,
            recent_observed_at=now,
            completed_at=now,
            disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
        )

        if states & _ACHIEVEMENT_STATES:
            _write_achievements(
                storage,
                account_id=account.id,
                candidates=appids,
                targeted=[
                    appid
                    for appid, state in facts
                    if state in _STALE_ACHIEVEMENT_STATES
                ],
                now=now,
            )

        for plan in plans:
            storage.set_explicit_feedback_field(
                account_id=account.id,
                appid=plan.appid,
                field_name="play_state",
                value="active",
                recorded_at=now,
            )
            for field_name, value in plan.feedback:
                storage.set_explicit_feedback_field(
                    account_id=account.id,
                    appid=plan.appid,
                    field_name=field_name,
                    value=value,
                    recorded_at=now,
                )
            for trait, value in plan.traits:
                storage.set_explicit_trait(
                    account_id=account.id,
                    appid=plan.appid,
                    trait=trait,
                    value=value,
                    recorded_at=now,
                )


__all__ = ["build"]

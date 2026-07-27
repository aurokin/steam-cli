"""Materialize M6 discovery and group scenarios.

Group evidence has two independent halves and the fixture keeps them apart.
Declared multiplayer categories come from the same normalized declared-facts
writer M5 uses, so the CLI's own slug mapping -- not the fixture -- decides
whether a mode gate passes.  Per-member ownership, player counts, and policy
facts are durable synthetic assertions written through the group profile APIs
under the current group disclosure.

Members are read from the required command's repeated ``--member`` options, so
a scenario never has to restate its group anywhere else.  Only synthetic
members get a local profile; a configured-account member's ownership comes
from the seeded visible-owned snapshot exactly as in production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from steam_agent.groups import MemberRef
from steam_agent.steam_declared_facts import DECLARED_FACTS_DISCLOSURE_VERSION
from steam_agent.storage import GROUP_PROFILE_DISCLOSURE_VERSION, Storage

from .materialize import (
    UnsupportedScenarioError,
    declared_app_facts_payload,
    materialization_now,
    parse_detail,
    required_argument_value,
    seed_identity,
    subject_appid,
    write_owned_snapshot,
)

# Category IDs the declared-facts normalizer maps to multiplayer mode slugs.
_CATEGORY_IDS = {
    "online_co_op": 38,
    "online_pvp": 36,
    "lan_co_op": 48,
    "shared_split_screen_co_op": 39,
    "shared_split_screen_pvp": 37,
    "remote_play_together": 44,
}

_OWNERSHIP_STATES = {
    "group_declared_all_own": "all",
    "group_declared_first_owner_only": "first",
    "declared_mode_only": "none",
}


class _Plan:
    """One AppID's declared evidence plus its per-member assertions."""

    def __init__(self, appid: int) -> None:
        self.appid = appid
        self.declared: dict[str, Any] | None = None
        self.owners: str = "none"
        self.players_min: int | None = None
        self.players_max: int | None = None
        self.policy: str | None = None


def _members(scenario: Mapping[str, Any]) -> tuple[MemberRef, ...]:
    refs: list[MemberRef] = []
    for requirement in scenario["tool_policy"].get("required", ()):
        arguments = list(requirement.get("arguments", []))
        for index, argument in enumerate(arguments):
            if argument != "--member" or index + 1 >= len(arguments):
                continue
            kind, _, key = arguments[index + 1].partition(":")
            if not key:
                raise UnsupportedScenarioError(
                    f"member {arguments[index + 1]!r} is not kind:key"
                )
            refs.append(MemberRef(kind, key))  # type: ignore[arg-type]
    return tuple(refs)


def _plan(fact: Mapping[str, Any]) -> _Plan:
    appid = subject_appid(fact)
    state = fact["state"]
    detail = parse_detail(fact.get("detail"))
    plan = _Plan(appid)
    if state == "declared_evidence_absent":
        return plan
    if state not in _OWNERSHIP_STATES:
        raise UnsupportedScenarioError(f"no M6 fixture builder for {state!r}")
    plan.owners = _OWNERSHIP_STATES[state]
    slug = detail.get("mode", "online_co_op")
    if slug not in _CATEGORY_IDS:
        raise UnsupportedScenarioError(f"unsupported declared mode slug {slug!r}")
    plan.declared = declared_app_facts_payload(appid, category_slugs=[slug])
    categories = plan.declared["categories"]
    assert isinstance(categories, dict)
    categories["numeric_ids"] = [_CATEGORY_IDS[slug]]
    if "players_min" in detail:
        plan.players_min = int(detail["players_min"])
    if "players_max" in detail:
        plan.players_max = int(detail["players_max"])
    plan.policy = detail.get("policy")
    return plan


def _write_declared(
    storage: Storage,
    *,
    account_id: int,
    machine_key: str,
    plans: Sequence[_Plan],
    now: Any,
) -> None:
    demanded = [plan.appid for plan in plans if plan.declared is not None]
    if not demanded:
        return
    storage.record_compatibility_data_consent(
        account_id=account_id,
        disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
        accepted_at=now,
        backups_acknowledged=True,
    )
    run, _, _ = storage.begin_declared_app_sync(
        account_id=account_id,
        machine_id=machine_key,
        demanded_appids=demanded,
        country="US",
        language="english",
        max_items=len(demanded),
        skip_fresh_terminal=True,
        started_at=now,
        disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
    )
    for plan in plans:
        if plan.declared is None:
            continue
        storage.record_declared_app_result(
            run.id,
            account_id=account_id,
            appid=plan.appid,
            state="ready",
            observed_at=now,
            facts=plan.declared,
        )
    storage.finish_declared_app_sync(run.id, completed_at=now)


def _write_group_assertions(
    storage: Storage,
    *,
    members: Sequence[MemberRef],
    plans: Sequence[_Plan],
    now: Any,
) -> None:
    synthetic = [member for member in members if member.kind == "synthetic"]
    for member in synthetic:
        storage.create_synthetic_group_profile(
            member.key,
            disclosure_version=GROUP_PROFILE_DISCLOSURE_VERSION,
            backups_acknowledged=True,
            created_at=now,
        )
    for plan in plans:
        if plan.owners == "all":
            owners = synthetic
        elif plan.owners == "first":
            owners = synthetic[:1]
        else:
            owners = []
        for member in owners:
            storage.set_group_ownership(
                member, appid=plan.appid, state="owned", updated_at=now
            )
        for member in synthetic:
            for fact, value in (
                ("players:min", plan.players_min),
                ("players:max", plan.players_max),
            ):
                if value is not None:
                    storage.set_group_app_assertion(
                        member,
                        appid=plan.appid,
                        fact=fact,
                        value=value,
                        updated_at=now,
                    )
            if plan.policy is not None:
                storage.set_group_app_assertion(
                    member,
                    appid=plan.appid,
                    fact=f"policy:{plan.policy}",
                    value="present",
                    updated_at=now,
                )


def build(scenario: Mapping[str, Any], data_dir: Path) -> None:
    machine_key = required_argument_value(
        scenario,
        "--machine",
        required_argument_value(scenario, "--context-machine", "synthetic-machine"),
    )
    account_alias = required_argument_value(
        scenario,
        "--account",
        required_argument_value(scenario, "--context-account", "synthetic"),
    )
    now = materialization_now()

    plans = [_plan(fact) for fact in scenario["fixture"]["facts"]]
    members = _members(scenario)

    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = seed_identity(
            storage,
            machine_key=machine_key,
            account_alias=account_alias,
            now=now,
        )
        write_owned_snapshot(storage, account.id, [plan.appid for plan in plans], now)
        _write_declared(
            storage,
            account_id=account.id,
            machine_key=machine_key,
            plans=plans,
            now=now,
        )
        _write_group_assertions(storage, members=members, plans=plans, now=now)


__all__ = ["build"]

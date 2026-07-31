"""Materialize M2 identity and data-lifecycle scenarios.

M2 fixtures are deliberately thin: the boundary being probed is the account
and credential surface, not a projection recipe.  No fixture ever installs a
resolvable secret -- there is no supported way to inject one, and the runner
treats ``auth`` as prohibited -- so credential-shaped scenarios stay pure
refusal probes with no materialized state at all.

Subjects here name either the configured account (``synthetic:account:ALIAS``)
or an AppID that must exist in the persisted visible-owned projection.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping

from steam_agent.storage import Storage

from .materialize import (
    UnsupportedScenarioError,
    materialization_now,
    required_argument_value,
    scenario_account_alias,
    scenario_machine_key,
    seed_identity,
    subject_appid,
    write_owned_snapshot,
)

_ACCOUNT_SUBJECT = re.compile(r"\Asynthetic:account:(.+)\Z")


def build(scenario: Mapping[str, Any], data_dir: Path) -> None:
    machine_key = scenario_machine_key(scenario)
    account_alias = required_argument_value(
        scenario, "--alias", scenario_account_alias(scenario)
    )
    now = materialization_now(scenario)

    owned: list[int] = []
    for fact in scenario["fixture"]["facts"]:
        state = fact["state"]
        if state == "configured_account":
            if _ACCOUNT_SUBJECT.match(fact["subject"]) is None:
                raise UnsupportedScenarioError(
                    f"unsupported account subject {fact['subject']!r}"
                )
        elif state == "persisted_owned_library":
            owned.append(subject_appid(fact))
        else:
            raise UnsupportedScenarioError(f"no M2 fixture builder for {state!r}")

    with Storage(data_dir / "steam-agent.sqlite3") as storage:
        account = seed_identity(
            storage,
            machine_key=machine_key,
            account_alias=account_alias,
            now=now,
        )
        if owned:
            write_owned_snapshot(storage, account.id, owned, now)


__all__ = ["build"]

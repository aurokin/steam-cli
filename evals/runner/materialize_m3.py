"""Materialize M3 wishlist-deal scenarios.

Every M3 scenario seeds one promoted wishlist snapshot and then replays price
evidence through ``begin_price_sync``/``complete_price_sync`` exactly as the
accepted M3 sync does.  Two structural facts about the cache shape the builder:

* Price demand always covers the whole wishlist snapshot, and the query reads
  the *latest* attempt that demanded each AppID.  A terminal run is therefore
  written first, and any deliberately non-terminal run (failed or running) is
  written afterwards targeting only its own AppID; untargeted AppIDs fall back
  to their retained subject, so earlier terminal states survive.
* A provider can carry at most one non-terminal state per scenario, because a
  later run would mask the earlier one.  ``running`` is written on the fallback
  provider and ``failed`` on the primary so ``m3-d07`` keeps all three states
  distinct in a single document.

One divergence is unavoidable: ``official_eur_700`` cannot be materialized.
The price cache rejects any currency other than USD, which is precisely the
limitation the scenario asserts, so the EUR offer is skipped and the answer is
still forced onto the official USD offer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import steam_agent.cli as cli
from steam_agent.storage import (
    PriceDemandSubject,
    PriceFactObservation,
    Storage,
    WishlistObservation,
)

from .materialize import (
    _SYNTHETIC_STEAM_ID64,
    UnsupportedScenarioError,
    materialization_now,
    parse_detail,
    scenario_account_alias,
    subject_appid,
)

_PROVIDERS = ("gg-deals", "cheapshark")
_LOW_SCOPE = {
    "official": "all_time_official_stores",
    "keyshop": "all_time_keyshops",
    "unknown": "all_time_any_store",
}
_DISCLOSURE_VERSION = "wishlist-visible-v1"


def _provider_url(provider: str, appid: int) -> str:
    if provider == "gg-deals":
        return f"https://gg.deals/game/eval-fixture-{appid}/"
    return f"https://www.cheapshark.com/search?steamAppID={appid}"


def _offer(
    appid: int,
    *,
    provider: str,
    amount_minor: int,
    store_class: str,
    comparability: str,
    observed_at: datetime,
    ordinal: int = 0,
) -> PriceFactObservation:
    return PriceFactObservation(
        appid=appid,
        ordinal=ordinal,
        fact_kind="offer",
        provider_product_id=f"steam/app/{appid}",
        amount_minor=amount_minor,
        currency="USD",
        regular_amount_minor=None,
        discount_percent=None,
        store_class=store_class,  # type: ignore[arg-type]
        comparability=comparability,  # type: ignore[arg-type]
        low_scope=None,
        effective_at=None,
        observed_at=observed_at,
        provider_url=_provider_url(provider, appid),
    )


def _historical_low(
    appid: int,
    *,
    provider: str,
    amount_minor: int,
    store_class: str,
    comparability: str,
    observed_at: datetime,
) -> PriceFactObservation:
    return PriceFactObservation(
        appid=appid,
        ordinal=0,
        fact_kind="historical_low",
        provider_product_id=f"steam/app/{appid}",
        amount_minor=amount_minor,
        currency="USD",
        regular_amount_minor=None,
        discount_percent=None,
        store_class=store_class,  # type: ignore[arg-type]
        comparability=comparability,  # type: ignore[arg-type]
        low_scope=_LOW_SCOPE[store_class],
        effective_at=None,
        observed_at=observed_at,
        provider_url=_provider_url(provider, appid),
    )


@dataclass
class _Plan:
    """Every provider outcome the scenario's facts ask the cache to hold."""

    observed: dict[tuple[str, int], list[PriceFactObservation]] = field(
        default_factory=dict
    )
    not_found: set[tuple[str, int]] = field(default_factory=set)
    failed: dict[tuple[str, int], str] = field(default_factory=dict)
    running: set[tuple[str, int]] = field(default_factory=set)
    oldest: timedelta = timedelta(0)

    def observe(self, provider: str, *facts: PriceFactObservation) -> None:
        for fact in facts:
            self.observed.setdefault((provider, fact.appid), []).append(fact)


def _priced(
    plan: _Plan,
    appid: int,
    detail: Mapping[str, str],
    now: datetime,
    *,
    amount: int,
    low: int | None,
    store_class: str = "official",
    comparability: str = "exact_product",
    provider: str = "gg-deals",
    age: timedelta = timedelta(0),
) -> None:
    observed_at = now - age
    plan.oldest = max(plan.oldest, age)
    amount = int(detail.get("amount", amount))
    facts = [
        _offer(
            appid,
            provider=provider,
            amount_minor=amount,
            store_class=store_class,
            comparability=comparability,
            observed_at=observed_at,
        )
    ]
    if low is not None:
        facts.append(
            _historical_low(
                appid,
                provider=provider,
                amount_minor=int(detail.get("low", low)),
                store_class=store_class,
                comparability=comparability,
                observed_at=observed_at,
            )
        )
    plan.observe(provider, *facts)


def _apply(plan: _Plan, appid: int, state: str, detail: str | None, now: datetime) -> None:
    values = parse_detail(detail)
    if state == "fresh_ready":
        _priced(plan, appid, values, now, amount=500, low=2000)
    elif state == "at_low":
        _priced(plan, appid, values, now, amount=1000, low=1000)
    elif state == "near_low_inside_boundary":
        _priced(plan, appid, values, now, amount=1050, low=1000)
    elif state == "outside_near_low_boundary":
        _priced(plan, appid, values, now, amount=1051, low=1000)
    elif state == "stale_last_good":
        _priced(
            plan,
            appid,
            values,
            now,
            amount=1500,
            low=None,
            age=timedelta(hours=int(values.get("age_hours", 8))),
        )
    elif state == "official_usd_1200":
        _priced(plan, appid, values, now, amount=1200, low=None)
    elif state == "keyshop_usd_500":
        plan.observe(
            "gg-deals",
            _offer(
                appid,
                provider="gg-deals",
                amount_minor=int(values.get("amount", 500)),
                store_class="keyshop",
                comparability="exact_product",
                observed_at=now,
                ordinal=1,
            ),
        )
    elif state == "official_eur_700":
        # The US/USD-only cache cannot hold a EUR offer; see the module docstring.
        return
    elif state == "cheapshark_ready":
        _priced(
            plan,
            appid,
            values,
            now,
            amount=799,
            low=None,
            provider="cheapshark",
            store_class=values.get("store_class", "unknown"),
            comparability=values.get("comparability", "normalized_game"),
        )
    elif state == "gg_not_found":
        plan.not_found.add(("gg-deals", appid))
    elif state == "cheapshark_not_found":
        plan.not_found.add(("cheapshark", appid))
    elif state == "failed_rate_limited":
        plan.failed[("gg-deals", appid)] = "PROVIDER_RATE_LIMITED"
    elif state == "running":
        plan.running.add(("cheapshark", appid))
    elif state == "unevaluated":
        # Nothing to write: the AppID is demanded but never targeted.
        return
    else:
        raise UnsupportedScenarioError(f"no M3 fixture builder for {state!r}")


def _seed_wishlist(
    storage: Storage,
    *,
    account_alias: str,
    appids: Sequence[int],
    now: datetime,
) -> tuple[int, int, tuple[PriceDemandSubject, ...]]:
    account = storage.configure_steam_account(
        alias=account_alias,
        steam_id64=_SYNTHETIC_STEAM_ID64,
        configured_at=now,
    )
    storage.record_wishlist_data_consent(
        account_id=account.id,
        disclosure_version=_DISCLOSURE_VERSION,
        accepted_at=now,
        backups_acknowledged=True,
    )
    run = storage.begin_sync(
        provider="steam_web_api",
        capability="wishlist.read",
        account_id=account.id,
        started_at=now,
    )
    observations = tuple(
        WishlistObservation(appid, 0, 100, now) for appid in sorted(appids)
    )
    storage.complete_wishlist_snapshot(
        run.id,
        observations,
        item_list_retrieved_at=now,
        item_count_retrieved_at=now,
        item_list_reported_count=len(observations),
        item_count_reported_count=len(observations),
        completed_at=now,
    )
    demand = tuple(
        PriceDemandSubject(item.appid, index, item.priority, item.date_added)
        for index, item in enumerate(observations)
    )
    return account.id, run.id, demand


def _configure_gg_credential(storage: Storage, database: Path, now: datetime) -> None:
    """Record the credential metadata so an unsynced primary is not AUTH_REQUIRED."""

    reference = cli._provider_credential_ref(
        database, cli._CREDENTIAL_PROVIDERS["gg-deals"]
    )
    storage.upsert_credential_reference(
        provider=reference.provider,
        kind=reference.kind,
        profile_id=reference.profile_id,
        backend="os",
        configured_at=now,
    )


def _write_provider(
    storage: Storage,
    provider: str,
    *,
    account_id: int,
    wishlist_run: int,
    demand: tuple[PriceDemandSubject, ...],
    plan: _Plan,
    now: datetime,
) -> None:
    order = [subject.appid for subject in demand]
    observed = {
        appid: facts for (name, appid), facts in plan.observed.items() if name == provider
    }
    not_found = {appid for name, appid in plan.not_found if name == provider}
    failed = {appid: code for (name, appid), code in plan.failed.items() if name == provider}
    running = {appid for name, appid in plan.running if name == provider}
    if len(failed) + len(running) > 1:
        raise UnsupportedScenarioError(
            f"{provider} cannot hold more than one non-terminal state per scenario"
        )

    terminal = sorted(set(observed) | not_found)
    if terminal:
        started = min(
            (
                _as_datetime(fact.observed_at)
                for facts in observed.values()
                for fact in facts
            ),
            default=now,
        )
        run = storage.begin_price_sync(
            provider=provider,
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            targeted_appids=tuple(appid for appid in order if appid in terminal),
            requested_limit=None,
            started_at=started,
        )
        storage.complete_price_sync(
            run.id,
            outcomes={
                appid: ("observed" if appid in observed else "not_found")
                for appid in terminal
            },
            facts=tuple(
                fact for appid in terminal for fact in observed.get(appid, ())
            ),
            completed_at=now,
            status="complete" if terminal == order else "partial",
        )
    for appid, error_code in sorted(failed.items()):
        run = storage.begin_price_sync(
            provider=provider,
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            targeted_appids=(appid,),
            requested_limit=None,
            started_at=now,
        )
        storage.finish_price_sync(run.id, completed_at=now, error_code=error_code)
    for appid in sorted(running):
        storage.begin_price_sync(
            provider=provider,
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            targeted_appids=(appid,),
            requested_limit=None,
            started_at=now,
        )


def _as_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build(scenario: Mapping[str, Any], data_dir: Path) -> None:
    account_alias = scenario_account_alias(scenario)
    now = materialization_now()

    plan = _Plan()
    appids: list[int] = []
    for fact in scenario["fixture"]["facts"]:
        appid = subject_appid(fact)
        if appid not in appids:
            appids.append(appid)
        _apply(plan, appid, fact["state"], fact.get("detail"), now)

    database = data_dir / "steam-agent.sqlite3"
    # The wishlist snapshot must precede the oldest price observation it feeds.
    seeded_at = now - plan.oldest
    with Storage(database) as storage:
        account_id, wishlist_run, demand = _seed_wishlist(
            storage,
            account_alias=account_alias,
            appids=appids,
            now=seeded_at,
        )
        _configure_gg_credential(storage, database, seeded_at)
        for provider in _PROVIDERS:
            _write_provider(
                storage,
                provider,
                account_id=account_id,
                wishlist_run=wishlist_run,
                demand=demand,
                plan=plan,
                now=now,
            )


__all__ = ["build"]

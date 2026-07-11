"""Wishlist-demanded current offer and historical-low synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from steam_agent.cheapshark import CheapSharkClient, CheapSharkError
from steam_agent.credentials import SecretValue
from steam_agent.deal_evidence import DealEvidenceSnapshot
from steam_agent.gg_deals import GgDealsClient, GgDealsError, RateLimitMetadata
from steam_agent.storage import (
    PriceDemandSubject,
    PriceFactObservation,
    Storage,
    SyncRun,
)


PRICE_CAPABILITY = "prices.wishlist.read"
DEFAULT_CHEAPSHARK_LIMIT = 20
MAX_GG_BATCH = 50
Clock = Callable[[], datetime]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class PriceSyncError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PriceSyncResult:
    runs: tuple[SyncRun, ...]
    total_items: int
    evaluated_items: int
    observed_items: int
    fallback_total: int
    fallback_evaluated: int
    completeness: Literal["complete", "partial"]
    providers_used: tuple[str, ...]
    providers_attempted: tuple[str, ...]


def sync_wishlist_prices(
    storage: Storage,
    *,
    account_id: int,
    country: str,
    provider: Literal["auto", "gg-deals", "cheapshark"],
    gg_api_key: SecretValue | None,
    max_items: int | None,
    gg_client: GgDealsClient | None = None,
    cheapshark_client: CheapSharkClient | None = None,
    clock: Clock = now_utc,
) -> PriceSyncResult:
    if country != "US":
        raise PriceSyncError("UNSUPPORTED_COUNTRY", retryable=False)
    if provider not in {"auto", "gg-deals", "cheapshark"}:
        raise ValueError("unsupported price provider selection")
    if max_items is not None and not 1 <= max_items <= 10_000:
        raise ValueError("max_items must be between 1 and 10000")
    wishlist = storage.read_wishlist_snapshot(account_id)
    if wishlist.latest_complete is None:
        raise PriceSyncError("NOT_SYNCED", retryable=False)
    if wishlist.latest is None:
        raise PriceSyncError("NOT_SYNCED", retryable=False)
    if wishlist.latest.status == "running":
        raise PriceSyncError("SYNC_IN_PROGRESS", retryable=True)
    if wishlist.latest.id != wishlist.latest_complete.id:
        raise PriceSyncError(
            wishlist.latest.error_code or "WISHLIST_REFRESH_FAILED",
            retryable=True,
        )
    completed_at = wishlist.latest_complete.completed_at
    if completed_at is None:
        raise PriceSyncError("NOT_SYNCED", retryable=False)
    evaluated_at = clock()
    completed_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    if evaluated_at > completed_dt + timedelta(hours=24):
        raise PriceSyncError("STALE_LAST_GOOD", retryable=True)
    ordered_games = sorted(
        wishlist.games, key=lambda item: (item.priority, item.date_added, item.appid)
    )
    demand = tuple(
        PriceDemandSubject(
            appid=game.appid,
            demand_order=index,
            wishlist_priority=game.priority,
            wishlist_date_added=game.date_added,
        )
        for index, game in enumerate(ordered_games)
    )
    total = len(demand)
    if total == 0:
        return PriceSyncResult(
            runs=(),
            total_items=0,
            evaluated_items=0,
            observed_items=0,
            fallback_total=0,
            fallback_evaluated=0,
            completeness="complete",
            providers_used=(),
            providers_attempted=(),
        )
    runs: list[SyncRun] = []
    used_providers: list[str] = []
    evaluated: set[int] = set()
    observed: set[int] = set()
    fallback_candidates: list[int] = []
    fallback_evaluated = 0

    selected = demand if max_items is None else demand[:max_items]
    use_gg = provider in {"auto", "gg-deals"} and gg_api_key is not None
    if provider == "gg-deals" and gg_api_key is None:
        raise PriceSyncError("AUTH_REQUIRED", retryable=False)
    if use_gg:
        run = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country=country,
            wishlist_sync_run_id=wishlist.latest_complete.id,
            demand=demand,
            targeted_appids=tuple(item.appid for item in selected),
            requested_limit=max_items,
            started_at=clock(),
        )
        outcomes: dict[int, Literal["observed", "not_found"]] = {}
        facts: list[PriceFactObservation] = []
        rate = RateLimitMetadata(None, None, None)
        failure: GgDealsError | PriceSyncError | None = None
        api = gg_client or GgDealsClient()
        for offset in range(0, len(selected), MAX_GG_BATCH):
            batch_subjects = selected[offset : offset + MAX_GG_BATCH]
            try:
                batch = api.fetch_app_price_summaries(
                    appids=tuple(item.appid for item in batch_subjects),
                    api_key=gg_api_key,
                )
            except GgDealsError as exc:
                failure = exc
                fallback_candidates.extend(item.appid for item in selected[offset:])
                break
            rate = batch.rate_limit
            for snapshot in batch.snapshots:
                try:
                    normalized_facts = _facts(snapshot)
                except PriceSyncError as exc:
                    failure = exc
                    break
                appid = snapshot.product.steam_appid
                if normalized_facts:
                    outcomes[appid] = "observed"
                    observed.add(appid)
                    facts.extend(normalized_facts)
                    if not any(fact.fact_kind == "offer" for fact in normalized_facts):
                        # Historical-low evidence remains useful and is retained,
                        # but it does not answer the current-price question.  Keep
                        # walking the evidence ladder for a current offer.
                        fallback_candidates.append(appid)
                else:
                    # A product-shaped response with no normalized price facts
                    # does not prove that a usable price was observed.  It is a
                    # truthful miss on this provider rung and must fall through.
                    outcomes[appid] = "not_found"
                    fallback_candidates.append(appid)
            if isinstance(failure, PriceSyncError):
                break
            for appid in batch.not_found_appids:
                outcomes[appid] = "not_found"
                fallback_candidates.append(appid)
            evaluated.update(outcomes)
        if outcomes:
            used_providers.append("gg-deals")
            completed = storage.complete_price_sync(
                run.id,
                outcomes=outcomes,
                facts=tuple(facts),
                completed_at=clock(),
                status=(
                    "complete"
                    if failure is None
                    and set(outcomes) == {item.appid for item in selected}
                    else "partial"
                ),
                rate_limit=rate.limit,
                rate_remaining=rate.remaining,
                rate_reset_value=rate.reset_value,
                error_code=None if failure is None else failure.code,
                retry_after_seconds=(
                    None
                    if failure is None
                    else getattr(failure, "retry_after_seconds", None)
                ),
            )
            runs.append(completed)
        else:
            code = "PROVIDER_UNAVAILABLE" if failure is None else failure.code
            runs.append(
                storage.finish_price_sync(
                    run.id,
                    completed_at=clock(),
                    error_code=code,
                    retry_after_seconds=(
                        None
                        if failure is None
                        else getattr(failure, "retry_after_seconds", None)
                    ),
                )
            )
            if provider == "gg-deals":
                assert failure is not None
                raise PriceSyncError(failure.code, retryable=failure.retryable)
        if isinstance(failure, PriceSyncError):
            raise failure
    elif provider == "auto":
        fallback_candidates.extend(item.appid for item in selected)

    if provider == "cheapshark":
        fallback_candidates = [item.appid for item in selected]

    fallback_total = len(dict.fromkeys(fallback_candidates))
    use_cheap = provider == "cheapshark" or (provider == "auto" and fallback_total > 0)
    if use_cheap:
        fallback_limit = max_items or DEFAULT_CHEAPSHARK_LIMIT
        targets = tuple(dict.fromkeys(fallback_candidates))[:fallback_limit]
        run = storage.begin_price_sync(
            provider="cheapshark",
            account_id=account_id,
            country=country,
            wishlist_sync_run_id=wishlist.latest_complete.id,
            demand=demand,
            targeted_appids=targets,
            requested_limit=fallback_limit,
            started_at=clock(),
        )
        outcomes = {}
        facts = []
        api = cheapshark_client or CheapSharkClient()
        failure: CheapSharkError | PriceSyncError | None = None
        for appid in targets:
            try:
                snapshot = api.lookup_steam_app(appid)
            except CheapSharkError as exc:
                if exc.code == "GAME_NOT_FOUND":
                    outcomes[appid] = "not_found"
                    continue
                failure = exc
                break
            try:
                normalized_facts = _facts(snapshot)
            except PriceSyncError as exc:
                failure = exc
                break
            if normalized_facts:
                outcomes[appid] = "observed"
                observed.add(appid)
                facts.extend(normalized_facts)
            else:
                outcomes[appid] = "not_found"
        fallback_evaluated = len(outcomes)
        evaluated.update(outcomes)
        if outcomes:
            used_providers.append("cheapshark")
            runs.append(
                storage.complete_price_sync(
                    run.id,
                    outcomes=outcomes,
                    facts=tuple(facts),
                    completed_at=clock(),
                    status=(
                        "complete"
                        if failure is None and set(outcomes) == set(targets)
                        else "partial"
                    ),
                    error_code=None if failure is None else failure.code,
                    retry_after_seconds=(
                        None
                        if failure is None
                        else getattr(failure, "retry_after_seconds", None)
                    ),
                )
            )
        else:
            code = "PROVIDER_UNAVAILABLE" if failure is None else failure.code
            runs.append(
                storage.finish_price_sync(
                    run.id,
                    completed_at=clock(),
                    error_code=code,
                    retry_after_seconds=(
                        None
                        if failure is None
                        else getattr(failure, "retry_after_seconds", None)
                    ),
                )
            )
            if provider == "cheapshark" and failure is not None:
                raise PriceSyncError(failure.code, retryable=failure.retryable)
        if isinstance(failure, PriceSyncError):
            raise failure

    # A forced provider run can complete its own bounded work while the overall
    # deal capability remains partial. In particular, a GG.deals ``not_found``
    # still requires the fallback rung before the price can truthfully be called
    # unknown. The individual SyncRun records provider-run completeness; this
    # result records completeness of the full evidence ladder.
    fallback_complete = fallback_evaluated >= fallback_total
    overall_complete = (
        len(evaluated) == total
        and fallback_complete
        and all(run.status != "failed" for run in runs)
    )
    return PriceSyncResult(
        runs=tuple(runs),
        total_items=total,
        evaluated_items=len(evaluated),
        observed_items=len(observed),
        fallback_total=fallback_total,
        fallback_evaluated=fallback_evaluated,
        completeness="complete" if overall_complete else "partial",
        providers_used=tuple(dict.fromkeys(used_providers)),
        providers_attempted=tuple(dict.fromkeys(run.provider for run in runs)),
    )


def _facts(snapshot: DealEvidenceSnapshot) -> list[PriceFactObservation]:
    if snapshot.provider not in {"gg-deals", "cheapshark"}:
        raise PriceSyncError("PROVIDER_RESPONSE_INVALID", retryable=False)
    appid = snapshot.product.steam_appid
    facts: list[PriceFactObservation] = []
    for ordinal, offer in enumerate(snapshot.offers):
        if offer.provider != snapshot.provider or offer.product != snapshot.product:
            raise PriceSyncError("PROVIDER_RESPONSE_INVALID", retryable=False)
        _validate_context(offer.price.country, offer.price.currency)
        if offer.regular_price is not None:
            _validate_context(offer.regular_price.country, offer.regular_price.currency)
        facts.append(
            PriceFactObservation(
                appid=appid,
                ordinal=ordinal,
                fact_kind="offer",
                provider_product_id=offer.product.provider_product_id,
                amount_minor=offer.price.amount_minor,
                currency=offer.price.currency,
                regular_amount_minor=(
                    None
                    if offer.regular_price is None
                    else offer.regular_price.amount_minor
                ),
                discount_percent=offer.discount_percent,
                store_class=offer.store_class,
                comparability=offer.comparability,
                low_scope=None,
                effective_at=None,
                observed_at=offer.observed_at,
                provider_url=offer.provider_url.url,
                seller_id=offer.seller_id,
            )
        )
    for ordinal, low in enumerate(snapshot.history_lows):
        if low.provider != snapshot.provider or low.product != snapshot.product:
            raise PriceSyncError("PROVIDER_RESPONSE_INVALID", retryable=False)
        _validate_context(low.price.country, low.price.currency)
        facts.append(
            PriceFactObservation(
                appid=appid,
                ordinal=ordinal,
                fact_kind="historical_low",
                provider_product_id=low.product.provider_product_id,
                amount_minor=low.price.amount_minor,
                currency=low.price.currency,
                regular_amount_minor=None,
                discount_percent=None,
                store_class="unknown",
                comparability=low.comparability,
                low_scope=low.scope,
                effective_at=low.effective_at,
                observed_at=low.observed_at,
                provider_url=low.provider_url.url,
            )
        )
    return facts


def _validate_context(country: str, currency: str) -> None:
    if country != "US" or currency != "USD":
        raise PriceSyncError("PROVIDER_CONTEXT_MISMATCH", retryable=False)


__all__ = [
    "DEFAULT_CHEAPSHARK_LIMIT",
    "PRICE_CAPABILITY",
    "PriceSyncError",
    "PriceSyncResult",
    "sync_wishlist_prices",
]

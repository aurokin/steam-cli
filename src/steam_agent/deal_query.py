"""Pure reconstruction of an agent-facing wishlist deal query.

The storage and CLI layers adapt their records into the narrow DTOs here.  This
module performs no I/O, never resolves credentials, and does not infer account
identifiers or provider facts that were not explicitly supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Literal
import uuid
from urllib.parse import parse_qs, urlsplit

from steam_agent.deal_evidence import (
    DealEvidenceSnapshot,
    HistoricalLowSummary,
    ManualReference,
    Money,
    OfferEvidence,
    ProductIdentity,
    StoreClass,
)
from steam_agent.deal_ranking import (
    DEAL_EVIDENCE_SCHEMA,
    DealCandidate,
    DealComparisonContext,
    ProviderAttempt,
    rank_deals,
)
from steam_agent.storage import (
    StoredPriceFact,
    StoredPriceSubject,
    WishlistDealSnapshot,
)


ProviderName = Literal["gg-deals", "cheapshark"]
ProviderState = Literal[
    "ready",
    "not_found",
    "expired",
    "unevaluated",
    "failed",
    "running",
    "abandoned",
    "not_synced",
]
AttemptState = Literal["none", "complete", "failed", "running", "abandoned"]
FactKind = Literal["offer", "historical_low"]

_PROVIDER_RUNG: dict[str, int] = {"gg-deals": 0, "cheapshark": 1}
_STORE_SCOPE: dict[str, str] = {
    "official": "all_time_official_stores",
    "keyshop": "all_time_keyshops",
    "unknown": "all_time_any_store",
}
_PROVIDER_LIMITATIONS: dict[str, tuple[str, ...]] = {
    "gg-deals": (
        "provider summaries are not a full historical event series",
        "AppID evidence may be game-level rather than edition-exact",
    ),
    "cheapshark": (
        "USD and US context only",
        "provider groups offers at game level",
        "cheapest-ever is a summary, not a full historical event series",
    ),
}
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_SAFE_ERROR_CODES = frozenset(
    {
        "AUTHENTICATION_FAILED",
        "AUTH_REQUIRED",
        "GAME_NOT_FOUND",
        "INTERNAL_ERROR",
        "NOT_SYNCED",
        "PRODUCT_NOT_FOUND",
        "PROVIDER_ACCESS_DENIED",
        "PROVIDER_CONTEXT_MISMATCH",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_RESPONSE_INVALID",
        "PROVIDER_REQUEST_INVALID",
        "PROVIDER_UNAVAILABLE",
        "REQUEST_THROTTLED",
        "SYNC_ABANDONED",
        "UNSUPPORTED_COUNTRY",
        "WISHLIST_INACCESSIBLE_OR_AUTH_AMBIGUOUS",
        "WISHLIST_REFRESH_FAILED",
    }
)
_GG_PATH = re.compile(r"/(?:game|dlc|pack)/[A-Za-z0-9][A-Za-z0-9._~-]*/?\Z")
_WISHLIST_FRESHNESS_SECONDS = 24 * 60 * 60
_ABANDONED_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class WishlistCandidateInput:
    appid: int
    game_id: str
    priority: int
    date_added_unix: int
    observed_at: str
    evidence_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WishlistStateInput:
    last_good_available: bool
    last_good_fresh: bool
    latest_attempt: AttemptState
    last_attempt_error_code: str | None = None
    last_successful_sync_at: str | None = None


@dataclass(frozen=True, slots=True)
class DealFactInput:
    appid: int
    provider: ProviderName
    ordinal: int
    fact_kind: FactKind
    provider_product_id: str
    product_mapping: Literal["exact"]
    amount_minor: int
    currency: str
    country: str
    regular_amount_minor: int | None
    discount_percent: int | None
    store_class: StoreClass
    comparability: Literal["exact_product", "normalized_game", "unknown"]
    low_scope: str | None
    effective_at: str | None
    observed_at: str
    fresh_until: str
    provider_url: str
    access_mode: Literal["manual_only"]
    automation_supported: Literal[False]
    evidence_id: int


@dataclass(frozen=True, slots=True)
class ProviderStateInput:
    appid: int
    provider: ProviderName
    state: ProviderState
    error_code: str | None = None
    observed_at: str | None = None
    fresh_until: str | None = None


@dataclass(frozen=True, slots=True)
class DealQueryInput:
    account_alias: str
    country: str
    store_class: StoreClass
    generated_at: datetime
    wishlist: WishlistStateInput
    candidates: tuple[WishlistCandidateInput, ...]
    facts: tuple[DealFactInput, ...]
    provider_states: tuple[ProviderStateInput, ...]


def build_deal_query(value: DealQueryInput) -> dict[str, object]:
    """Return deterministic JSON-ready context, completeness, and data."""

    now = _aware_utc(value.generated_at)
    if value.country != "US":
        raise ValueError("deal query currently supports only US")
    if value.store_class not in _STORE_SCOPE:
        raise ValueError("deal query store class is invalid")
    _validate_wishlist_state(value.wishlist)
    history_scope = _STORE_SCOPE[value.store_class]
    comparison = DealComparisonContext(
        country="US",
        currency="USD",
        store_class=value.store_class,
        history_scope=history_scope,
    )
    candidates = _candidates(value.candidates)
    facts = _facts(value.facts, candidates)
    states = _states(value.provider_states, candidates)

    warnings: dict[str, str] = {}
    missing: set[str] = set()
    stale: set[str] = set()
    if not value.wishlist.last_good_available:
        code = _wishlist_unavailable_code(value.wishlist)
        warnings[code] = _warning_message(code)
        missing.add("wishlist.read")
        return _result(
            value,
            comparison,
            status="unavailable",
            warnings=warnings,
            missing=missing,
            stale=stale,
            items=[],
            empty=False,
            provider_states=states,
        )

    wishlist_partial = _wishlist_warnings(value.wishlist, warnings)
    if not value.wishlist.last_good_fresh:
        stale.add("wishlist.read")
        wishlist_partial = True
    if not candidates:
        return _result(
            value,
            comparison,
            status="partial" if wishlist_partial else "complete",
            warnings=warnings,
            missing=missing,
            stale=stale,
            items=[],
            empty=True,
            provider_states=states,
        )

    ranking_facts = tuple(fact for fact in facts if _fresh(fact.fresh_until, now))
    snapshots_by_appid, evidence_objects = _snapshots(ranking_facts)
    ranking_candidates: list[DealCandidate] = []
    price_missing = False
    price_stale = False
    used_fallback = False
    for appid in sorted(candidates):
        attempts = tuple(
            _ranking_attempt(states[(appid, provider)], now)
            for provider in ("gg-deals", "cheapshark")
        )
        ranking_candidates.append(
            DealCandidate(appid, snapshots_by_appid.get(appid, ()), attempts)
        )
        primary_state = states[(appid, "gg-deals")]
        fallback_state = states[(appid, "cheapshark")]
        covered, fallback = _candidate_coverage(primary_state, fallback_state, now)
        used_fallback |= fallback
        if not covered:
            terminal_stale = any(
                _state_is_stale(state, now) for state in (primary_state, fallback_state)
            )
            if terminal_stale:
                price_stale = True
            else:
                price_missing = True
        _state_warnings(primary_state, warnings)
        if primary_state.state != "ready":
            _state_warnings(fallback_state, warnings)
        for state in (primary_state, fallback_state):
            if _state_is_stale(state, now):
                price_stale = True
                stale.add("prices.wishlist.read")
                warnings["STALE_PRICE_EVIDENCE"] = _warning_message(
                    "STALE_PRICE_EVIDENCE"
                )

    ranked = rank_deals(ranking_candidates, context=comparison)
    facts_by_appid: dict[int, list[DealFactInput]] = {}
    for fact in facts:
        facts_by_appid.setdefault(fact.appid, []).append(fact)
        if not _fresh(fact.fresh_until, now):
            price_stale = True
            stale.add("prices.wishlist.read")
            warnings["STALE_PRICE_EVIDENCE"] = _warning_message("STALE_PRICE_EVIDENCE")
    if used_fallback:
        warnings["DEGRADED_FALLBACK"] = _warning_message("DEGRADED_FALLBACK")

    items: list[dict[str, object]] = []
    for ranked_item in ranked.candidates:
        appid = ranked_item.steam_appid
        item_facts = sorted(facts_by_appid.get(appid, []), key=_fact_key)
        items.append(
            {
                "appid": appid,
                "game_id": candidates[appid].game_id,
                "wishlist": _wishlist_item(candidates[appid]),
                "deal": {
                    "bucket": ranked_item.bucket,
                    "evidence_grade": ranked_item.evidence_grade,
                    "current_offer": _selected_evidence(
                        ranked_item.current_offer, evidence_objects, now
                    ),
                    "historical_low": _selected_evidence(
                        ranked_item.historical_low, evidence_objects, now
                    ),
                    "discount_bps": ranked_item.discount_bps,
                    "distance_above_low_bps": ranked_item.distance_above_low_bps,
                    "reasons": list(ranked_item.reasons),
                    "tradeoffs": list(ranked_item.tradeoffs),
                    "fallback_rung": ranked_item.fallback_rung,
                    "attempted_providers": [
                        _attempt_json(attempt)
                        for attempt in ranked_item.attempted_providers
                    ],
                    "used_providers": list(ranked_item.used_providers),
                },
                "evidence": {
                    "offers": [
                        _fact_json(fact, now)
                        for fact in item_facts
                        if fact.fact_kind == "offer"
                    ],
                    "historical_lows": [
                        _fact_json(fact, now)
                        for fact in item_facts
                        if fact.fact_kind == "historical_low"
                    ],
                },
                "references": _references(appid, item_facts),
            }
        )

    if price_missing:
        missing.add("prices.wishlist.read")
    status = (
        "partial" if wishlist_partial or price_missing or price_stale else "complete"
    )
    return _result(
        value,
        comparison,
        status=status,
        warnings=warnings,
        missing=missing,
        stale=stale,
        items=items,
        empty=False,
        provider_states=states,
    )


def build_deal_query_from_snapshot(
    snapshot: WishlistDealSnapshot,
    *,
    account_alias: str,
    country: str,
    store_class: StoreClass,
    generated_at: datetime,
    gg_credential_configured: bool | None = None,
) -> dict[str, object]:
    """Adapt the atomic storage snapshot without exposing account identifiers."""

    now = _aware_utc(generated_at)
    latest = snapshot.wishlist.latest
    latest_complete = snapshot.wishlist.latest_complete
    if latest is None:
        latest_attempt: AttemptState = "none"
    elif latest.status == "running":
        started = _parse_timestamp(latest.started_at)
        latest_attempt = (
            "abandoned"
            if (now - started).total_seconds() > _ABANDONED_SECONDS
            else "running"
        )
    elif latest.status == "complete":
        latest_attempt = "complete"
    else:
        latest_attempt = "failed"
    last_good_fresh = False
    if latest_complete is not None and latest_complete.completed_at is not None:
        completed = _parse_timestamp(latest_complete.completed_at)
        last_good_fresh = (
            now - completed
        ).total_seconds() <= _WISHLIST_FRESHNESS_SECONDS
    stable_ids = dict(snapshot.stable_game_ids_by_appid)
    candidates = tuple(
        WishlistCandidateInput(
            appid=game.appid,
            game_id=f"game:{stable_ids[game.appid]}",
            priority=game.priority,
            date_added_unix=game.date_added,
            observed_at=game.observed_at,
            evidence_ids=(game.evidence_id,),
        )
        for game in snapshot.wishlist.games
    )
    facts = tuple(_stored_fact(fact) for fact in snapshot.prices.facts)
    provider_states = _stored_provider_states(
        snapshot,
        candidates,
        now,
        gg_credential_configured=gg_credential_configured,
    )
    return build_deal_query(
        DealQueryInput(
            account_alias=account_alias,
            country=country,
            store_class=store_class,
            generated_at=now,
            wishlist=WishlistStateInput(
                last_good_available=latest_complete is not None,
                last_good_fresh=last_good_fresh,
                latest_attempt=latest_attempt,
                last_attempt_error_code=(None if latest is None else latest.error_code),
                last_successful_sync_at=(
                    None if latest_complete is None else latest_complete.completed_at
                ),
            ),
            candidates=candidates,
            facts=facts,
            provider_states=provider_states,
        )
    )


def _stored_fact(value: StoredPriceFact) -> DealFactInput:
    automation_supported = bool(value.automation_supported)
    if automation_supported:
        raise ValueError("stored price reference cannot support automation")
    return DealFactInput(
        appid=value.appid,
        provider=value.provider,  # type: ignore[arg-type]
        ordinal=value.ordinal,
        fact_kind=value.fact_kind,  # type: ignore[arg-type]
        provider_product_id=value.provider_product_id,
        product_mapping=value.product_mapping,  # type: ignore[arg-type]
        amount_minor=value.amount_minor,
        currency=value.currency,
        country=value.country,
        regular_amount_minor=value.regular_amount_minor,
        discount_percent=value.discount_percent,
        store_class=value.store_class,  # type: ignore[arg-type]
        comparability=value.comparability,  # type: ignore[arg-type]
        low_scope=value.low_scope,
        effective_at=value.effective_at,
        observed_at=value.observed_at,
        fresh_until=value.fresh_until,
        provider_url=value.provider_url,
        access_mode=value.access_mode,  # type: ignore[arg-type]
        automation_supported=False,
        evidence_id=value.evidence_id,
    )


def _stored_provider_states(
    snapshot: WishlistDealSnapshot,
    candidates: tuple[WishlistCandidateInput, ...],
    now: datetime,
    *,
    gg_credential_configured: bool | None,
) -> tuple[ProviderStateInput, ...]:
    subjects = {
        (subject.appid, subject.provider): subject
        for subject in snapshot.prices.subjects
    }
    relevant = {
        (item.appid, item.provider): item
        for item in snapshot.prices.latest_relevant_attempts
    }
    result: list[ProviderStateInput] = []
    for candidate in candidates:
        for provider in ("gg-deals", "cheapshark"):
            subject = subjects.get((candidate.appid, provider))
            item = relevant.get((candidate.appid, provider))
            if item is None:
                if subject is None:
                    if provider == "gg-deals" and gg_credential_configured is False:
                        result.append(
                            ProviderStateInput(
                                candidate.appid,
                                provider,
                                "failed",
                                "AUTH_REQUIRED",
                            )
                        )
                        continue
                    result.append(
                        ProviderStateInput(
                            candidate.appid, provider, "not_synced", "NOT_SYNCED"
                        )
                    )
                else:
                    result.append(_subject_state(candidate.appid, provider, subject))
                continue
            if not item.demand.targeted:
                result.append(
                    _subject_state(candidate.appid, provider, subject)
                    if subject is not None
                    else ProviderStateInput(candidate.appid, provider, "unevaluated")
                )
                continue
            run = item.attempt.run
            if run.status == "running":
                started = _parse_timestamp(run.started_at)
                state: ProviderState = (
                    "abandoned"
                    if (now - started).total_seconds() > _ABANDONED_SECONDS
                    else "running"
                )
                result.append(ProviderStateInput(candidate.appid, provider, state))
            elif run.status == "failed":
                result.append(
                    ProviderStateInput(
                        candidate.appid,
                        provider,
                        "failed",
                        run.error_code or "PROVIDER_UNAVAILABLE",
                    )
                )
            elif item.demand.evaluated:
                if item.demand.outcome == "not_found":
                    result.append(
                        _subject_state(candidate.appid, provider, subject)
                        if subject is not None
                        else ProviderStateInput(candidate.appid, provider, "expired")
                    )
                else:
                    result.append(
                        _subject_state(candidate.appid, provider, subject)
                        if subject is not None
                        else ProviderStateInput(candidate.appid, provider, "expired")
                    )
            else:
                stopped_early = run.error_code is not None
                result.append(
                    ProviderStateInput(
                        candidate.appid,
                        provider,
                        "failed" if stopped_early else "unevaluated",
                        run.error_code if stopped_early else None,
                    )
                )
    return tuple(result)


def _subject_state(
    appid: int, provider: str, subject: StoredPriceSubject
) -> ProviderStateInput:
    return ProviderStateInput(
        appid=appid,
        provider=provider,  # type: ignore[arg-type]
        state=("not_found" if subject.outcome == "not_found" else "ready"),
        observed_at=subject.observed_at,
        fresh_until=subject.fresh_until,
    )


def _candidates(
    values: tuple[WishlistCandidateInput, ...],
) -> dict[int, WishlistCandidateInput]:
    result: dict[int, WishlistCandidateInput] = {}
    for item in values:
        if (
            isinstance(item.appid, bool)
            or not isinstance(item.appid, int)
            or item.appid in result
            or not 1 <= item.appid <= (1 << 32) - 1
        ):
            raise ValueError("wishlist candidate AppIDs must be valid and unique")
        try:
            stable_id = uuid.UUID(item.game_id.removeprefix("game:"))
        except (AttributeError, ValueError):
            raise ValueError("wishlist candidate stable ID is invalid")
        if item.game_id != f"game:{stable_id}":
            raise ValueError("wishlist candidate stable ID is invalid")
        if (
            isinstance(item.priority, bool)
            or not isinstance(item.priority, int)
            or not 0 <= item.priority <= (1 << 32) - 1
            or isinstance(item.date_added_unix, bool)
            or not isinstance(item.date_added_unix, int)
            or not 0 <= item.date_added_unix <= (1 << 32) - 1
        ):
            raise ValueError("wishlist candidate metadata is invalid")
        if not item.evidence_ids or len(set(item.evidence_ids)) != len(
            item.evidence_ids
        ):
            raise ValueError(
                "wishlist candidate evidence IDs must be nonempty and unique"
            )
        if any(
            isinstance(evidence_id, bool)
            or not isinstance(evidence_id, int)
            or evidence_id <= 0
            for evidence_id in item.evidence_ids
        ):
            raise ValueError("wishlist candidate evidence ID is invalid")
        _parse_timestamp(item.observed_at)
        result[item.appid] = item
    return result


def _validate_wishlist_state(state: WishlistStateInput) -> None:
    if state.latest_attempt not in {
        "none",
        "complete",
        "failed",
        "running",
        "abandoned",
    }:
        raise ValueError("wishlist attempt state is invalid")
    if state.last_attempt_error_code is not None:
        _validate_error_code(state.last_attempt_error_code)
    if state.last_successful_sync_at is not None:
        _parse_timestamp(state.last_successful_sync_at)
    if state.last_good_fresh and not state.last_good_available:
        raise ValueError("unavailable wishlist cannot be fresh")
    if state.last_good_available != (state.last_successful_sync_at is not None):
        raise ValueError("wishlist last-good availability is inconsistent")
    if state.latest_attempt == "complete" and not state.last_good_available:
        raise ValueError("complete wishlist attempt requires a last-good snapshot")
    if state.latest_attempt == "failed" and state.last_attempt_error_code is None:
        raise ValueError("failed wishlist attempt requires an error code")
    if state.latest_attempt != "failed" and state.last_attempt_error_code is not None:
        raise ValueError("non-failed wishlist attempt cannot carry an error code")


def _validate_error_code(value: str) -> None:
    if _ERROR_CODE.fullmatch(value) is None or value not in _SAFE_ERROR_CODES:
        raise ValueError("provider error code is not sanitized")


def _facts(
    values: tuple[DealFactInput, ...], candidates: dict[int, WishlistCandidateInput]
) -> tuple[DealFactInput, ...]:
    keys: set[tuple[object, ...]] = set()
    evidence_ids: set[int] = set()
    result: list[DealFactInput] = []
    for fact in values:
        if fact.appid not in candidates:
            continue
        if fact.provider not in _PROVIDER_RUNG:
            raise ValueError("deal fact provider is unsupported")
        if fact.fact_kind not in {"offer", "historical_low"}:
            raise ValueError("deal fact kind is invalid")
        if fact.store_class not in _STORE_SCOPE:
            raise ValueError("deal fact store class is invalid")
        if fact.comparability not in {"exact_product", "normalized_game", "unknown"}:
            raise ValueError("deal fact comparability is invalid")
        if (
            not isinstance(fact.provider_product_id, str)
            or not 1 <= len(fact.provider_product_id) <= 512
            or any(ord(character) < 32 for character in fact.provider_product_id)
            or isinstance(fact.amount_minor, bool)
            or not isinstance(fact.amount_minor, int)
            or not 0 <= fact.amount_minor <= (1 << 63) - 1
            or fact.currency != "USD"
            or fact.country != "US"
        ):
            raise ValueError("deal fact price identity is invalid")
        if fact.regular_amount_minor is not None and (
            isinstance(fact.regular_amount_minor, bool)
            or not isinstance(fact.regular_amount_minor, int)
            or not 0 <= fact.regular_amount_minor <= (1 << 63) - 1
        ):
            raise ValueError("deal fact regular amount is invalid")
        if (
            isinstance(fact.ordinal, bool)
            or not isinstance(fact.ordinal, int)
            or fact.ordinal < 0
        ):
            raise ValueError("deal fact ordinal is invalid")
        if (
            isinstance(fact.evidence_id, bool)
            or not isinstance(fact.evidence_id, int)
            or fact.evidence_id <= 0
            or fact.evidence_id in evidence_ids
        ):
            raise ValueError("deal fact evidence IDs must be positive and unique")
        if (
            fact.product_mapping != "exact"
            or fact.access_mode != "manual_only"
            or fact.automation_supported is not False
        ):
            raise ValueError("deal fact attribution fields are invalid")
        if fact.discount_percent is not None and (
            isinstance(fact.discount_percent, bool)
            or not isinstance(fact.discount_percent, int)
            or not 0 <= fact.discount_percent <= 100
        ):
            raise ValueError("deal fact discount is invalid")
        if fact.fact_kind == "offer":
            if fact.low_scope is not None or fact.effective_at is not None:
                raise ValueError("offer cannot carry historical-low fields")
        elif (
            fact.low_scope not in set(_STORE_SCOPE.values())
            or fact.regular_amount_minor is not None
            or fact.discount_percent is not None
        ):
            raise ValueError("historical-low fact fields are invalid")
        observed = _parse_timestamp(fact.observed_at)
        fresh_until = _parse_timestamp(fact.fresh_until)
        if fresh_until < observed:
            raise ValueError("deal fact freshness precedes observation")
        if fact.effective_at is not None:
            if _parse_timestamp(fact.effective_at) > observed:
                raise ValueError("deal fact effective time follows observation")
        _validate_provider_url(fact.provider, fact.provider_url, fact.appid)
        key = (fact.appid, fact.provider, fact.fact_kind, fact.ordinal)
        if key in keys:
            raise ValueError("deal facts must have unique provider ordinals")
        keys.add(key)
        evidence_ids.add(fact.evidence_id)
        result.append(fact)
    return tuple(sorted(result, key=_fact_key))


def _states(
    values: tuple[ProviderStateInput, ...],
    candidates: dict[int, WishlistCandidateInput],
) -> dict[tuple[int, str], ProviderStateInput]:
    result: dict[tuple[int, str], ProviderStateInput] = {}
    for state in values:
        if state.appid not in candidates:
            continue
        if state.provider not in _PROVIDER_RUNG:
            raise ValueError("provider state provider is unsupported")
        if state.state not in {
            "ready",
            "not_found",
            "expired",
            "unevaluated",
            "failed",
            "running",
            "abandoned",
            "not_synced",
        }:
            raise ValueError("provider state is invalid")
        key = (state.appid, state.provider)
        if key in result:
            raise ValueError("provider states must be unique per candidate")
        if state.error_code is not None:
            _validate_error_code(state.error_code)
        if state.state == "failed" and state.error_code is None:
            raise ValueError("failed provider state requires an error code")
        if state.state == "not_synced":
            if state.error_code not in {None, "NOT_SYNCED"}:
                raise ValueError("not-synced provider state has an invalid error code")
        elif state.state != "failed" and state.error_code is not None:
            raise ValueError("provider state cannot carry an error code")
        has_observed = state.observed_at is not None
        has_fresh_until = state.fresh_until is not None
        if has_observed != has_fresh_until:
            raise ValueError("provider state timestamps must be paired")
        if state.state in {"ready", "not_found"}:
            if not has_observed:
                raise ValueError(
                    "terminal provider state requires freshness timestamps"
                )
            observed = _parse_timestamp(state.observed_at or "")
            fresh_until = _parse_timestamp(state.fresh_until or "")
            if fresh_until < observed:
                raise ValueError("provider state freshness precedes observation")
        elif has_observed:
            raise ValueError("non-terminal provider state cannot carry freshness")
        result[key] = state
    for appid in candidates:
        for provider in _PROVIDER_RUNG:
            result.setdefault(
                (appid, provider),
                ProviderStateInput(
                    appid, provider, "not_synced", error_code="NOT_SYNCED"
                ),
            )
    return result


def _snapshots(
    facts: tuple[DealFactInput, ...],
) -> tuple[
    dict[int, tuple[DealEvidenceSnapshot, ...]],
    dict[int, DealFactInput],
]:
    groups: dict[tuple[int, str, str], list[DealFactInput]] = {}
    for fact in facts:
        groups.setdefault(
            (fact.appid, fact.provider, fact.provider_product_id), []
        ).append(fact)
    by_appid: dict[int, list[DealEvidenceSnapshot]] = {}
    evidence_objects: dict[int, DealFactInput] = {}
    for (appid, provider, product_id), values in sorted(groups.items()):
        product = ProductIdentity(product_id, appid)
        offers: list[OfferEvidence] = []
        lows: list[HistoricalLowSummary] = []
        for fact in sorted(values, key=_fact_key):
            reference = ManualReference(
                fact.provider_url,
                (
                    f"open attributed {provider} offer for a human"
                    if fact.fact_kind == "offer"
                    else f"open attributed {provider} historical-price reference for a human"
                ),
            )
            if fact.fact_kind == "offer":
                evidence = OfferEvidence(
                    provider=provider,
                    product=product,
                    price=Money(fact.amount_minor, fact.currency, fact.country),
                    regular_price=(
                        None
                        if fact.regular_amount_minor is None
                        else Money(
                            fact.regular_amount_minor, fact.currency, fact.country
                        )
                    ),
                    discount_percent=fact.discount_percent,
                    store_class=fact.store_class,
                    observed_at=fact.observed_at,
                    provider_url=reference,
                    comparability=fact.comparability,
                )
                offers.append(evidence)
            else:
                if fact.low_scope is None:
                    raise ValueError("historical-low fact requires a scope")
                evidence = HistoricalLowSummary(
                    provider=provider,
                    product=product,
                    price=Money(fact.amount_minor, fact.currency, fact.country),
                    observed_at=fact.observed_at,
                    effective_at=fact.effective_at,
                    scope=fact.low_scope,
                    provider_url=reference,
                    comparability=fact.comparability,
                )
                lows.append(evidence)
            evidence_objects[id(evidence)] = fact
        observed_at = max(item.observed_at for item in values)
        by_appid.setdefault(appid, []).append(
            DealEvidenceSnapshot(
                provider=provider,
                product=product,
                offers=tuple(offers),
                history_lows=tuple(lows),
                observed_at=observed_at,
                limitations=_PROVIDER_LIMITATIONS[provider],
            )
        )
    return (
        {appid: tuple(values) for appid, values in by_appid.items()},
        evidence_objects,
    )


def _validate_provider_url(provider: str, value: str, appid: int) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("deal fact provider URL is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ValueError("deal fact provider URL is invalid")
    if provider == "gg-deals":
        valid = (
            parsed.hostname == "gg.deals"
            and not parsed.query
            and _GG_PATH.fullmatch(parsed.path) is not None
        )
    else:
        query = parse_qs(parsed.query, keep_blank_values=True)
        valid = parsed.hostname == "www.cheapshark.com" and (
            (
                parsed.path == "/redirect"
                and set(query) == {"dealID"}
                and len(query["dealID"]) == 1
                and bool(query["dealID"][0])
            )
            or (
                parsed.path == "/search"
                and set(query) == {"steamAppID"}
                and query["steamAppID"] == [str(appid)]
            )
        )
    if not valid:
        raise ValueError("deal fact provider URL is invalid")


def _ranking_attempt(state: ProviderStateInput, now: datetime) -> ProviderAttempt:
    if _state_is_stale(state, now):
        return ProviderAttempt(
            state.provider,
            _PROVIDER_RUNG[state.provider],
            "unavailable",
            "STALE_PRICE_EVIDENCE",
        )
    if state.state == "ready":
        return ProviderAttempt(state.provider, _PROVIDER_RUNG[state.provider], "ready")
    if state.state == "not_found":
        return ProviderAttempt(
            state.provider, _PROVIDER_RUNG[state.provider], "not_found"
        )
    if state.state == "failed":
        return ProviderAttempt(
            state.provider,
            _PROVIDER_RUNG[state.provider],
            "failed",
            state.error_code or "PROVIDER_UNAVAILABLE",
        )
    code = {
        "expired": "STALE_PRICE_EVIDENCE",
        "unevaluated": "PRICE_EVIDENCE_NOT_EVALUATED",
        "running": "SYNC_IN_PROGRESS",
        "abandoned": "SYNC_ABANDONED",
        "not_synced": "NOT_SYNCED",
    }[state.state]
    return ProviderAttempt(
        state.provider, _PROVIDER_RUNG[state.provider], "unavailable", code
    )


def _candidate_coverage(
    primary: ProviderStateInput, fallback: ProviderStateInput, now: datetime
) -> tuple[bool, bool]:
    primary_terminal = primary.state in {"ready", "not_found"} and _state_fresh(
        primary, now
    )
    if primary.state == "ready" and primary_terminal:
        return True, False
    fallback_terminal = fallback.state in {"ready", "not_found"} and _state_fresh(
        fallback, now
    )
    if fallback_terminal:
        return True, True
    return False, False


def _state_fresh(state: ProviderStateInput, now: datetime) -> bool:
    return not _state_is_stale(state, now)


def _state_is_stale(state: ProviderStateInput, now: datetime) -> bool:
    return (
        state.state == "expired"
        or (
            state.state in {"ready", "not_found"}
            and state.fresh_until is not None
            and not _fresh(state.fresh_until, now)
        )
    )


def _state_warnings(state: ProviderStateInput, warnings: dict[str, str]) -> None:
    code = {
        "expired": "STALE_PRICE_EVIDENCE",
        "unevaluated": "PRICE_EVIDENCE_NOT_EVALUATED",
        "failed": state.error_code or "PROVIDER_UNAVAILABLE",
        "running": "SYNC_IN_PROGRESS",
        "abandoned": "SYNC_ABANDONED",
        "not_synced": "NOT_SYNCED",
    }.get(state.state)
    if code is not None:
        warnings[code] = _warning_message(code)


def _wishlist_unavailable_code(state: WishlistStateInput) -> str:
    if state.latest_attempt == "failed":
        return state.last_attempt_error_code or "PROVIDER_UNAVAILABLE"
    return {
        "running": "SYNC_IN_PROGRESS",
        "abandoned": "SYNC_ABANDONED",
    }.get(state.latest_attempt, "NOT_SYNCED")


def _wishlist_warnings(state: WishlistStateInput, warnings: dict[str, str]) -> bool:
    partial = False
    if not state.last_good_fresh:
        warnings["STALE_LAST_GOOD"] = _warning_message("STALE_LAST_GOOD")
        partial = True
    if state.latest_attempt == "failed":
        code = state.last_attempt_error_code or "PROVIDER_UNAVAILABLE"
        warnings[code] = _warning_message(code)
        partial = True
    elif state.latest_attempt == "running":
        warnings["SYNC_IN_PROGRESS"] = _warning_message("SYNC_IN_PROGRESS")
    elif state.latest_attempt == "abandoned":
        warnings["SYNC_ABANDONED"] = _warning_message("SYNC_ABANDONED")
        partial = True
    return partial


def _result(
    value: DealQueryInput,
    comparison: DealComparisonContext,
    *,
    status: str,
    warnings: dict[str, str],
    missing: set[str],
    stale: set[str],
    items: list[dict[str, object]],
    empty: bool,
    provider_states: dict[tuple[int, str], ProviderStateInput],
) -> dict[str, object]:
    used = sorted(
        {
            provider
            for item in items
            for provider in item["deal"]["used_providers"]  # type: ignore[index]
        }
    )
    attempted = sorted(
        {
            state.provider
            for state in provider_states.values()
            if state.state != "not_synced"
        },
        key=lambda provider: (_PROVIDER_RUNG[provider], provider),
    )
    return {
        "context": {
            "account_alias": value.account_alias,
            "scopes": ["wishlist", "deals"],
            "country": comparison.country,
            "currency": comparison.currency,
            "store_class": comparison.store_class,
            "history_scope": comparison.history_scope,
            "provider_selection": "auto",
            "ranking_schema": DEAL_EVIDENCE_SCHEMA,
            "identifiers_included": False,
        },
        "completeness": {
            "status": status,
            "missing_capabilities": sorted(missing),
            "stale_capabilities": sorted(stale),
            "warnings": [
                {"code": code, "message": warnings[code]} for code in sorted(warnings)
            ],
        },
        "data": {
            "items": items,
            "empty": empty,
            "next_cursor": None,
            "ranking": {
                "schema": DEAL_EVIDENCE_SCHEMA,
                "deterministic_order": True,
            },
            "snapshots": {
                "wishlist": {
                    "last_attempt_status": value.wishlist.latest_attempt,
                    "last_successful_sync_at": value.wishlist.last_successful_sync_at,
                    "last_good_fresh": value.wishlist.last_good_fresh,
                },
                "prices": _price_snapshot_json(provider_states, items),
            },
            "fallback": {
                "ladder": [
                    {"rung": 0, "provider": "gg-deals", "mode": "api"},
                    {"rung": 1, "provider": "cheapshark", "mode": "api"},
                    {"rung": 2, "provider": "manual-reference", "mode": "manual_only"},
                ],
                "providers_attempted": attempted,
                "providers_used": used,
            },
            "limitations": [
                "price evidence is US/USD only",
                "historical lows are summaries rather than a full event graph",
                "manual references are never read or fetched by Steam Agent",
            ],
        },
    }


def _wishlist_item(item: WishlistCandidateInput) -> dict[str, object]:
    return {
        "priority": item.priority,
        "date_added_unix": item.date_added_unix,
        "observed_at": item.observed_at,
        "evidence_ids": sorted(item.evidence_ids),
    }


def _fact_json(fact: DealFactInput, now: datetime) -> dict[str, object]:
    value: dict[str, object] = {
        "provider": fact.provider,
        "fact_kind": fact.fact_kind,
        "product": {
            "kind": "app",
            "provider_product_id": fact.provider_product_id,
            "mapping": fact.product_mapping,
            "comparability": fact.comparability,
        },
        "price": _money_json(fact.amount_minor, fact.currency, fact.country),
        "observed_at": fact.observed_at,
        "fresh_until": fact.fresh_until,
        "fresh": _fresh(fact.fresh_until, now),
        "evidence_id": fact.evidence_id,
        "reference": {
            "url": fact.provider_url,
            "purpose": (
                f"open attributed {fact.provider} offer for a human"
                if fact.fact_kind == "offer"
                else f"open attributed {fact.provider} historical-price reference for a human"
            ),
            "access_mode": fact.access_mode,
            "automation_supported": fact.automation_supported,
        },
    }
    if fact.fact_kind == "offer":
        value.update(
            {
                "regular_price": (
                    None
                    if fact.regular_amount_minor is None
                    else _money_json(
                        fact.regular_amount_minor, fact.currency, fact.country
                    )
                ),
                "discount_percent": fact.discount_percent,
                "store_class": fact.store_class,
            }
        )
    else:
        value.update({"scope": fact.low_scope, "effective_at": fact.effective_at})
    return value


def _selected_evidence(
    evidence: OfferEvidence | HistoricalLowSummary | None,
    objects: dict[int, DealFactInput],
    now: datetime,
) -> dict[str, object] | None:
    if evidence is None:
        return None
    fact = objects.get(id(evidence))
    if fact is None:
        raise ValueError("ranked evidence did not originate in query facts")
    return _fact_json(fact, now)


def _references(appid: int, facts: list[DealFactInput]) -> list[dict[str, object]]:
    references: dict[str, dict[str, object]] = {}
    for fact in facts:
        references[fact.provider_url] = {
            "url": fact.provider_url,
            "purpose": f"open attributed {fact.provider} evidence for a human",
            "access_mode": "manual_only",
            "automation_supported": False,
        }
    for url, purpose in (
        (
            f"https://gg.deals/steam/app/{appid}/",
            "manual GG.deals application inspection",
        ),
        (
            f"https://steamdb.info/app/{appid}/",
            "manual SteamDB application inspection",
        ),
    ):
        references[url] = {
            "url": url,
            "purpose": purpose,
            "access_mode": "manual_only",
            "automation_supported": False,
        }
    return [references[url] for url in sorted(references)]


def _attempt_json(attempt: ProviderAttempt) -> dict[str, object]:
    result: dict[str, object] = {
        "provider": attempt.provider,
        "fallback_rung": attempt.fallback_rung,
        "status": attempt.status,
    }
    if attempt.error_code is not None:
        result["error_code"] = attempt.error_code
    return result


def _price_snapshot_json(
    states: dict[tuple[int, str], ProviderStateInput],
    items: list[dict[str, object]],
) -> dict[str, object]:
    providers: list[dict[str, object]] = []
    for provider in ("gg-deals", "cheapshark"):
        counts = {
            name: 0
            for name in (
                "ready",
                "not_found",
                "expired",
                "unevaluated",
                "failed",
                "running",
                "abandoned",
                "not_synced",
            )
        }
        for state in states.values():
            if state.provider == provider:
                counts[state.state] += 1
        providers.append({"provider": provider, "state_counts": counts})
    evidence = [
        fact
        for item in items
        for group in (
            item["evidence"]["offers"],  # type: ignore[index]
            item["evidence"]["historical_lows"],  # type: ignore[index]
        )
        for fact in group
    ]
    return {
        "candidate_count": len(items),
        "fact_count": len(evidence),
        "stale_fact_count": sum(not fact["fresh"] for fact in evidence),
        "providers": providers,
    }


def _money_json(amount: int, currency: str, country: str) -> dict[str, object]:
    return {"amount_minor": amount, "currency": currency, "country": country}


def _fact_key(fact: DealFactInput) -> tuple[object, ...]:
    return (
        fact.appid,
        fact.provider,
        0 if fact.fact_kind == "offer" else 1,
        fact.provider_product_id,
        fact.ordinal,
        fact.evidence_id,
    )


def _fresh(value: str, now: datetime) -> bool:
    return now <= _parse_timestamp(value)


def _parse_timestamp(value: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise ValueError("deal query timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    return value.astimezone(timezone.utc)


def _warning_message(code: str) -> str:
    return {
        "NOT_SYNCED": "Price evidence has not been synchronized for one or more wishlist items.",
        "PRICE_EVIDENCE_NOT_EVALUATED": "The bounded price sync did not evaluate every wishlist item.",
        "STALE_PRICE_EVIDENCE": "One or more retained price facts are outside their freshness window.",
        "STALE_LAST_GOOD": "The query uses a stale last-good wishlist snapshot.",
        "SYNC_IN_PROGRESS": "A relevant synchronization is currently in progress.",
        "SYNC_ABANDONED": "A relevant synchronization appears abandoned.",
        "DEGRADED_FALLBACK": "A lower fallback rung supplied evidence for one or more items.",
        "AUTH_REQUIRED": "The primary price provider credential is unavailable.",
        "PROVIDER_UNAVAILABLE": "A price provider was unavailable during its latest attempt.",
        "PROVIDER_RATE_LIMITED": "A price provider rate-limited its latest attempt.",
    }.get(code, "A relevant provider attempt did not complete successfully.")


__all__ = [
    "DealFactInput",
    "DealQueryInput",
    "ProviderStateInput",
    "WishlistCandidateInput",
    "WishlistStateInput",
    "build_deal_query",
    "build_deal_query_from_snapshot",
]

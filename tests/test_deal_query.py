from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from steam_agent.deal_query import (
    DealFactInput,
    DealQueryInput,
    ProviderStateInput,
    WishlistCandidateInput,
    WishlistStateInput,
    build_deal_query,
    build_deal_query_from_snapshot,
)
from steam_agent.storage import (
    PriceSnapshot,
    StoredPriceFact,
    StoredPriceSubject,
    SyncRun,
    WishlistDealSnapshot,
    WishlistGame,
    WishlistSnapshot,
    steam_application_stable_id,
)


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
NOW_TEXT = "2026-07-11T12:00:00Z"


def wishlist(
    *,
    available: bool = True,
    fresh: bool = True,
    attempt: str = "complete",
    error: str | None = None,
) -> WishlistStateInput:
    return WishlistStateInput(
        last_good_available=available,
        last_good_fresh=fresh,
        latest_attempt=attempt,  # type: ignore[arg-type]
        last_attempt_error_code=error,
        last_successful_sync_at=NOW_TEXT if available else None,
    )


def candidate(appid: int, priority: int = 0) -> WishlistCandidateInput:
    return WishlistCandidateInput(
        appid=appid,
        game_id=f"game:{steam_application_stable_id(appid)}",
        priority=priority,
        date_added_unix=100 + appid,
        observed_at=NOW_TEXT,
        evidence_ids=(appid + 1000,),
    )


def fact(
    appid: int,
    provider: str,
    kind: str,
    amount: int,
    *,
    ordinal: int = 0,
    regular: int | None = None,
    store_class: str = "official",
    comparability: str = "exact_product",
    scope: str | None = None,
    fresh_until: str = "2026-07-11T18:00:00Z",
    evidence_id: int | None = None,
) -> DealFactInput:
    if provider == "gg-deals":
        url = "https://gg.deals/game/synthetic/"
    elif kind == "historical_low":
        url = f"https://www.cheapshark.com/search?steamAppID={appid}"
    else:
        url = "https://www.cheapshark.com/redirect?dealID=synthetic"
    return DealFactInput(
        appid=appid,
        provider=provider,  # type: ignore[arg-type]
        ordinal=ordinal,
        fact_kind=kind,  # type: ignore[arg-type]
        provider_product_id=f"{provider}-product-{appid}",
        product_mapping="exact",
        amount_minor=amount,
        currency="USD",
        country="US",
        regular_amount_minor=regular,
        discount_percent=None,
        store_class=store_class,  # type: ignore[arg-type]
        comparability=comparability,  # type: ignore[arg-type]
        low_scope=scope,
        effective_at=None,
        observed_at=NOW_TEXT,
        fresh_until=fresh_until,
        provider_url=url,
        access_mode="manual_only",
        automation_supported=False,
        evidence_id=evidence_id or appid * 100 + ordinal,
    )


def state(
    appid: int,
    provider: str,
    value: str,
    *,
    error: str | None = None,
    fresh_until: str | None = "2026-07-11T18:00:00Z",
) -> ProviderStateInput:
    terminal = value in {"ready", "not_found"}
    return ProviderStateInput(
        appid=appid,
        provider=provider,  # type: ignore[arg-type]
        state=value,  # type: ignore[arg-type]
        error_code=error,
        observed_at=NOW_TEXT if terminal else None,
        fresh_until=fresh_until if terminal else None,
    )


def query(
    *,
    candidates: tuple[WishlistCandidateInput, ...] = (candidate(10),),
    facts: tuple[DealFactInput, ...] = (),
    states: tuple[ProviderStateInput, ...] = (),
    wishlist_state: WishlistStateInput | None = None,
    store_class: str = "official",
    generated_at: datetime = NOW,
) -> dict[str, object]:
    return build_deal_query(
        DealQueryInput(
            account_alias="primary",
            country="US",
            store_class=store_class,  # type: ignore[arg-type]
            generated_at=generated_at,
            wishlist=wishlist_state or wishlist(),
            candidates=candidates,
            facts=facts,
            provider_states=states,
        )
    )


def warning_codes(result: dict[str, object]) -> set[str]:
    return {
        item["code"]
        for item in result["completeness"]["warnings"]  # type: ignore[index]
    }


def test_reconstructs_ranks_and_preserves_every_conflicting_fact() -> None:
    facts = (
        fact(10, "gg-deals", "offer", 500, regular=1000, evidence_id=1),
        fact(
            10,
            "gg-deals",
            "historical_low",
            500,
            ordinal=0,
            scope="all_time_official_stores",
            fresh_until="2026-07-12T12:00:00Z",
            evidence_id=2,
        ),
        fact(
            10,
            "cheapshark",
            "offer",
            400,
            store_class="unknown",
            comparability="normalized_game",
            evidence_id=3,
        ),
        fact(
            10,
            "cheapshark",
            "historical_low",
            300,
            store_class="unknown",
            comparability="normalized_game",
            scope="all_time_any_store",
            fresh_until="2026-07-12T12:00:00Z",
            evidence_id=4,
        ),
    )
    result = query(
        facts=facts,
        states=(state(10, "gg-deals", "ready"),),
    )

    assert result["completeness"]["status"] == "complete"  # type: ignore[index]
    item = result["data"]["items"][0]  # type: ignore[index]
    assert item["deal"]["bucket"] == "at_or_below_low"
    assert item["deal"]["evidence_grade"] == "exact"
    assert item["deal"]["used_providers"] == ["gg-deals"]
    assert item["deal"]["current_offer"]["evidence_id"] == 1
    assert item["deal"]["historical_low"]["evidence_id"] == 2
    assert len(item["evidence"]["offers"]) == 2
    assert len(item["evidence"]["historical_lows"]) == 2
    assert {entry["evidence_id"] for entry in item["evidence"]["offers"]} == {1, 3}
    assert all(
        reference["access_mode"] == "manual_only"
        and reference["automation_supported"] is False
        for reference in item["references"]
    )
    urls = {reference["url"] for reference in item["references"]}
    assert "https://gg.deals/steam/app/10/" in urls
    assert "https://steamdb.info/app/10/" in urls
    serialized = json.dumps(result)
    assert "steam_id" not in serialized.lower()
    assert "secret" not in serialized.lower()
    price_snapshot = result["data"]["snapshots"]["prices"]  # type: ignore[index]
    assert price_snapshot["candidate_count"] == 1
    assert price_snapshot["fact_count"] == 4
    assert price_snapshot["stale_fact_count"] == 0


def test_wishlist_unavailable_is_not_a_confirmed_empty_result() -> None:
    result = query(
        candidates=(), wishlist_state=wishlist(available=False, attempt="none")
    )

    assert result["completeness"]["status"] == "unavailable"  # type: ignore[index]
    assert result["completeness"]["missing_capabilities"] == ["wishlist.read"]  # type: ignore[index]
    assert result["data"]["items"] == []  # type: ignore[index]
    assert result["data"]["empty"] is False  # type: ignore[index]
    assert warning_codes(result) == {"NOT_SYNCED"}


def test_valid_empty_wishlist_needs_no_price_evidence() -> None:
    result = query(candidates=())

    assert result["completeness"]["status"] == "complete"  # type: ignore[index]
    assert result["data"]["empty"] is True  # type: ignore[index]
    assert result["completeness"]["missing_capabilities"] == []  # type: ignore[index]


def test_stale_failed_and_abandoned_wishlist_keep_last_good_but_are_partial() -> None:
    for value in (
        wishlist(fresh=False),
        wishlist(attempt="failed", error="PROVIDER_UNAVAILABLE"),
        wishlist(attempt="abandoned"),
    ):
        result = query(wishlist_state=value)
        assert result["completeness"]["status"] == "partial"  # type: ignore[index]
        assert len(result["data"]["items"]) == 1  # type: ignore[index]


def test_fresh_running_wishlist_is_informational_not_partial_by_itself() -> None:
    result = query(
        wishlist_state=wishlist(attempt="running"),
        states=(
            state(10, "gg-deals", "not_found"),
            state(10, "cheapshark", "not_found"),
        ),
    )

    assert result["completeness"]["status"] == "complete"  # type: ignore[index]
    assert "SYNC_IN_PROGRESS" in warning_codes(result)


def test_explicit_not_found_is_complete_unknown_not_free() -> None:
    result = query(
        states=(
            state(10, "gg-deals", "not_found"),
            state(10, "cheapshark", "not_found"),
        )
    )

    assert result["completeness"]["status"] == "complete"  # type: ignore[index]
    deal = result["data"]["items"][0]["deal"]  # type: ignore[index]
    assert deal["bucket"] == "unknown"
    assert deal["current_offer"] is None
    assert deal["historical_low"] is None
    assert deal["attempted_providers"] == [
        {"provider": "gg-deals", "fallback_rung": 0, "status": "not_found"},
        {"provider": "cheapshark", "fallback_rung": 1, "status": "not_found"},
    ]


def test_primary_not_found_requires_a_completed_fallback_rung() -> None:
    result = query(states=(state(10, "gg-deals", "not_found"),))

    assert result["completeness"]["status"] == "partial"  # type: ignore[index]
    assert (
        "prices.wishlist.read"
        in result["completeness"][  # type: ignore[index]
            "missing_capabilities"
        ]
    )
    assert "NOT_SYNCED" in warning_codes(result)


@pytest.mark.parametrize(
    ("provider_state", "code"),
    [
        ("unevaluated", "PRICE_EVIDENCE_NOT_EVALUATED"),
        ("running", "SYNC_IN_PROGRESS"),
        ("abandoned", "SYNC_ABANDONED"),
        ("not_synced", "NOT_SYNCED"),
    ],
)
def test_missing_price_states_are_partial_and_typed(
    provider_state: str, code: str
) -> None:
    result = query(
        states=(
            state(10, "gg-deals", "not_found"),
            state(10, "cheapshark", provider_state),
        )
    )

    assert result["completeness"]["status"] == "partial"  # type: ignore[index]
    assert "prices.wishlist.read" in result["completeness"]["missing_capabilities"]  # type: ignore[index]
    assert code in warning_codes(result)


def test_failed_primary_uses_retained_fact_and_fallback_metadata() -> None:
    result = query(
        store_class="unknown",
        facts=(
            fact(
                10,
                "gg-deals",
                "offer",
                700,
                store_class="unknown",
                comparability="normalized_game",
            ),
            fact(
                10,
                "cheapshark",
                "offer",
                500,
                store_class="unknown",
                comparability="normalized_game",
                evidence_id=9,
            ),
        ),
        states=(
            state(10, "gg-deals", "failed", error="PROVIDER_RATE_LIMITED"),
            state(10, "cheapshark", "ready"),
        ),
    )

    assert result["completeness"]["status"] == "complete"  # type: ignore[index]
    assert {"PROVIDER_RATE_LIMITED", "DEGRADED_FALLBACK"} <= warning_codes(result)
    item = result["data"]["items"][0]  # type: ignore[index]
    assert item["deal"]["fallback_rung"] == 1
    assert item["deal"]["used_providers"] == ["cheapshark"]
    assert len(item["evidence"]["offers"]) == 2


def test_exact_freshness_boundary_and_stale_subject_are_distinct() -> None:
    boundary = query(
        facts=(fact(10, "gg-deals", "offer", 500),),
        states=(state(10, "gg-deals", "ready"),),
    )
    stale = query(
        facts=(fact(10, "gg-deals", "offer", 500),),
        states=(state(10, "gg-deals", "ready"),),
        generated_at=NOW + timedelta(hours=6, seconds=1),
    )
    stale_not_found = query(
        states=(
            state(10, "gg-deals", "not_found"),
            state(
                10,
                "cheapshark",
                "not_found",
                fresh_until=NOW_TEXT,
            ),
        ),
        generated_at=NOW + timedelta(seconds=1),
    )

    assert boundary["completeness"]["status"] == "complete"  # type: ignore[index]
    assert boundary["data"]["items"][0]["evidence"]["offers"][0]["fresh"] is True  # type: ignore[index]
    assert stale["completeness"]["status"] == "partial"  # type: ignore[index]
    assert "STALE_PRICE_EVIDENCE" in warning_codes(stale)
    assert stale["completeness"]["missing_capabilities"] == []  # type: ignore[index]
    assert stale["completeness"]["stale_capabilities"] == ["prices.wishlist.read"]  # type: ignore[index]
    assert stale_not_found["completeness"]["status"] == "partial"  # type: ignore[index]
    assert (
        "prices.wishlist.read" in stale_not_found["completeness"]["stale_capabilities"]
    )  # type: ignore[index]


def test_order_and_json_are_deterministic() -> None:
    candidates = (candidate(30), candidate(10), candidate(20))
    states = tuple(
        state(appid, provider, "not_found")
        for appid in (30, 10, 20)
        for provider in ("gg-deals", "cheapshark")
    )
    first = query(candidates=candidates, states=states)
    second = query(candidates=candidates, states=tuple(reversed(states)))

    assert [item["appid"] for item in first["data"]["items"]] == [10, 20, 30]  # type: ignore[index]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_atomic_storage_snapshot_adapter_preserves_stable_identity_and_evidence() -> (
    None
):
    stable_id = steam_application_stable_id(10)
    complete = SyncRun(
        id=1,
        provider="steam-store",
        capability="wishlist.read",
        machine_id=None,
        account_id=99,
        started_at=NOW_TEXT,
        completed_at=NOW_TEXT,
        status="complete",
        promoted=True,
        records_seen=1,
        error_code=None,
        error_detail=None,
    )
    wishlist_snapshot = WishlistSnapshot(
        games=(WishlistGame(99, 10, 0, 100, NOW_TEXT, 41, 1),),
        latest=complete,
        latest_complete=complete,
        latest_complete_provenance=None,
        stable_game_ids_by_appid=((10, stable_id),),
    )
    stored_fact = StoredPriceFact(
        account_id=99,
        country="US",
        provider="gg-deals",
        appid=10,
        ordinal=0,
        fact_kind="offer",
        provider_product_id="gg-10",
        product_mapping="exact",
        amount_minor=500,
        currency="USD",
        regular_amount_minor=1000,
        discount_percent=50,
        store_class="official",
        comparability="exact_product",
        low_scope=None,
        effective_at=None,
        observed_at=NOW_TEXT,
        fresh_until="2026-07-11T18:00:00Z",
        hard_expires_at="2026-07-18T12:00:00Z",
        provider_url="https://gg.deals/game/synthetic/",
        access_mode="manual_only",
        automation_supported=0,
        evidence_id=42,
        promoted_sync_run_id=2,
    )
    prices = PriceSnapshot(
        facts=(stored_fact,),
        subjects=(
            StoredPriceSubject(
                99,
                "US",
                "gg-deals",
                10,
                "ready",
                NOW_TEXT,
                "2026-07-11T18:00:00Z",
                "2026-07-18T12:00:00Z",
                2,
            ),
        ),
        attempts=(),
        stale_offer_count=0,
        stale_historical_low_count=0,
        stale_subject_count=0,
        running=False,
        abandoned_running=False,
        attempt_metadata=(),
        demand_rows=(),
        latest_relevant_attempts=(),
    )

    result = build_deal_query_from_snapshot(
        WishlistDealSnapshot(wishlist_snapshot, prices, ((10, stable_id),)),
        account_alias="primary",
        country="US",
        store_class="official",
        generated_at=NOW,
    )

    assert result["completeness"]["status"] == "complete"  # type: ignore[index]
    item = result["data"]["items"][0]  # type: ignore[index]
    assert item["game_id"] == f"game:{stable_id}"
    assert item["deal"]["current_offer"]["evidence_id"] == 42
    serialized = json.dumps(result)
    assert "account_id" not in serialized
    assert "steam_id" not in serialized


def test_query_context_derives_history_scope_from_store_class() -> None:
    assert query(store_class="official")["context"]["history_scope"] == (  # type: ignore[index]
        "all_time_official_stores"
    )
    assert query(store_class="keyshop")["context"]["history_scope"] == (  # type: ignore[index]
        "all_time_keyshops"
    )
    assert query(store_class="unknown")["context"]["history_scope"] == (  # type: ignore[index]
        "all_time_any_store"
    )


@pytest.mark.parametrize(
    "invalid",
    [
        replace(candidate(10), appid=-1),
        replace(candidate(10), priority=-1),
        replace(candidate(10), date_added_unix=-1),
        replace(candidate(10), evidence_ids=(-1,)),
        replace(candidate(10), game_id="game:not-a-uuid"),
        replace(candidate(10), observed_at="2026-07-11T12:00:00"),
    ],
)
def test_rejects_invalid_candidate_identity_metadata_and_timestamps(
    invalid: WishlistCandidateInput,
) -> None:
    with pytest.raises(ValueError):
        query(candidates=(invalid,))


@pytest.mark.parametrize(
    "invalid",
    [
        replace(fact(10, "gg-deals", "offer", 500), provider="unknown"),
        replace(fact(10, "gg-deals", "offer", 500), ordinal=-1),
        replace(fact(10, "gg-deals", "offer", 500), evidence_id=-1),
        replace(
            fact(10, "gg-deals", "offer", 500),
            fresh_until="2026-07-11T11:59:59Z",
        ),
        replace(
            fact(10, "gg-deals", "offer", 500),
            observed_at="2026-07-11T12:00:00",
        ),
        replace(
            fact(10, "gg-deals", "offer", 500),
            automation_supported=True,
        ),
    ],
)
def test_rejects_invalid_fact_provider_lineage_and_timestamps(
    invalid: DealFactInput,
) -> None:
    with pytest.raises(ValueError):
        query(facts=(invalid,))


@pytest.mark.parametrize(
    "invalid",
    [
        replace(state(10, "gg-deals", "ready"), provider="unknown"),
        replace(state(10, "gg-deals", "ready"), state="invalid"),
        replace(state(10, "gg-deals", "ready"), error_code="AUTH_REQUIRED"),
        replace(
            state(10, "gg-deals", "failed", error="AUTH_REQUIRED"), error_code=None
        ),
        replace(state(10, "gg-deals", "running"), observed_at=NOW_TEXT),
        replace(state(10, "gg-deals", "ready"), fresh_until=None),
        replace(
            state(10, "gg-deals", "ready"),
            fresh_until="2026-07-11T11:59:59Z",
        ),
    ],
)
def test_rejects_invalid_provider_state_fields(invalid: ProviderStateInput) -> None:
    with pytest.raises(ValueError):
        query(states=(invalid,))

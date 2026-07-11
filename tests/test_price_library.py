from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import steam_agent.price_library as price_library
from steam_agent.cheapshark import CheapSharkError
from steam_agent.credentials import SecretValue
from steam_agent.deal_query import build_deal_query_from_snapshot
from steam_agent.deal_evidence import (
    DealEvidenceSnapshot,
    HistoricalLowSummary,
    ProductIdentity,
)
from steam_agent.deal_evidence import ManualReference, Money, OfferEvidence
from steam_agent.gg_deals import GgDealsBatch, GgDealsError, RateLimitMetadata
from steam_agent.price_library import PriceSyncError, sync_wishlist_prices
from steam_agent.storage import Storage, WishlistObservation


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def setup(storage: Storage, count: int) -> int:
    account = storage.configure_steam_account(
        alias="primary", steam_id64="76561198000000000", configured_at=NOW
    )
    storage.record_wishlist_data_consent(
        account_id=account.id,
        disclosure_version="test",
        accepted_at=NOW,
        backups_acknowledged=True,
    )
    run = storage.begin_sync(
        provider="steam_web_api",
        capability="wishlist.read",
        account_id=account.id,
        started_at=NOW,
    )
    observations = tuple(
        WishlistObservation(appid, appid, appid, NOW) for appid in range(1, count + 1)
    )
    storage.complete_wishlist_snapshot(
        run.id,
        observations,
        item_list_retrieved_at=NOW,
        item_count_retrieved_at=NOW,
        item_list_reported_count=count,
        item_count_reported_count=count,
        completed_at=NOW,
    )
    return account.id


def empty_snapshot(provider: str, appid: int) -> DealEvidenceSnapshot:
    return DealEvidenceSnapshot(
        provider,
        ProductIdentity(f"product-{appid}", appid),
        (),
        (),
        "2026-07-11T12:00:00Z",
        (),
    )


def observed_snapshot(
    provider: str, appid: int, *, observed_at: datetime = NOW
) -> DealEvidenceSnapshot:
    observed_text = (
        observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    product = ProductIdentity(f"product-{appid}", appid)
    if provider == "gg-deals":
        url = f"https://gg.deals/game/synthetic-{appid}/"
        store_class = "official"
    else:
        url = "https://www.cheapshark.com/redirect?dealID=synthetic"
        store_class = "unknown"
    offer = OfferEvidence(
        provider,
        product,
        Money(100, "USD", "US"),
        None,
        None,
        store_class,
        observed_text,
        ManualReference(url, "manual"),
        "exact_product",
    )
    return DealEvidenceSnapshot(
        provider,
        product,
        (offer,),
        (),
        observed_text,
        (),
    )


def low_only_snapshot(provider: str, appid: int) -> DealEvidenceSnapshot:
    product = ProductIdentity(f"product-{appid}", appid)
    low = HistoricalLowSummary(
        provider,
        product,
        Money(75, "USD", "US"),
        "2026-07-11T12:00:00Z",
        None,
        "all_time_official_stores",
        ManualReference(f"https://gg.deals/game/synthetic-{appid}/", "manual"),
        "normalized_game",
    )
    return DealEvidenceSnapshot(
        provider,
        product,
        (),
        (low,),
        "2026-07-11T12:00:00Z",
        (),
    )


class MidBatchGg:
    calls = 0

    def fetch_app_price_summaries(self, *, appids, api_key):
        self.calls += 1
        values = tuple(appids)
        if self.calls == 2:
            raise GgDealsError(
                "PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=7
            )
        return GgDealsBatch(
            values,
            tuple(observed_snapshot("gg-deals", appid) for appid in values),
            (),
            RateLimitMetadata(100, 50, 123),
        )


class FullyUnavailableGg:
    def fetch_app_price_summaries(self, *, appids, api_key):
        raise GgDealsError(
            "PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=17
        )


def test_full_provider_failure_preserves_retry_delay(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 2)
        try:
            sync_wishlist_prices(
                storage,
                account_id=account_id,
                country="US",
                provider="gg-deals",
                gg_api_key=SecretValue("secret"),
                max_items=1,
                gg_client=FullyUnavailableGg(),
                clock=lambda: NOW,
            )
        except PriceSyncError as exc:
            assert exc.code == "PROVIDER_UNAVAILABLE"
        else:
            raise AssertionError("provider failure should escape")
        snapshot = storage.read_price_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        assert snapshot.attempts[-1].status == "failed"
        assert snapshot.attempt_metadata[-1].retry_after_seconds == 17
        assert [row.targeted for row in snapshot.demand_rows] == [True, False]


class SparseGgFallback:
    def fetch_app_price_summaries(self, *, appids, api_key):
        values = tuple(appids)
        return GgDealsBatch(
            values,
            tuple(
                observed_snapshot("gg-deals", appid) for appid in values if appid != 2
            ),
            (2,) if 2 in values else (),
            RateLimitMetadata(100, 99, 123),
        )


class EmptyAndNullGg:
    """Model both an item with empty prices and an API null/missing item."""

    def fetch_app_price_summaries(self, *, appids, api_key):
        assert tuple(appids) == (1, 2)
        return GgDealsBatch(
            (1, 2),
            (empty_snapshot("gg-deals", 1),),
            (2,),
            RateLimitMetadata(100, 99, 123),
        )


class LowOnlyGg:
    def fetch_app_price_summaries(self, *, appids, api_key):
        assert tuple(appids) == (1,)
        return GgDealsBatch(
            (1,),
            (low_only_snapshot("gg-deals", 1),),
            (),
            RateLimitMetadata(100, 99, 123),
        )


class ShuffledSparseGg:
    def fetch_app_price_summaries(self, *, appids, api_key):
        assert tuple(appids) == (1, 2, 3)
        return GgDealsBatch(
            (1, 2, 3),
            (
                empty_snapshot("gg-deals", 3),
                observed_snapshot("gg-deals", 2),
            ),
            (1,),
            RateLimitMetadata(100, 99, 123),
        )


class AllEmptyGg:
    def fetch_app_price_summaries(self, *, appids, api_key):
        values = tuple(appids)
        return GgDealsBatch(
            values,
            tuple(empty_snapshot("gg-deals", appid) for appid in reversed(values)),
            (),
            RateLimitMetadata(100, 99, 123),
        )


class RecordingCheap:
    def __init__(self, *, observed_at: datetime = NOW) -> None:
        self.calls: list[int] = []
        self.observed_at = observed_at

    def lookup_steam_app(self, appid: int) -> DealEvidenceSnapshot:
        self.calls.append(appid)
        return observed_snapshot("cheapshark", appid, observed_at=self.observed_at)


def test_forced_primary_not_found_completes_run_but_not_evidence_ladder(
    tmp_path,
) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 3)
        result = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="gg-deals",
            gg_api_key=SecretValue("secret"),
            max_items=None,
            gg_client=SparseGgFallback(),
            clock=lambda: NOW,
        )

        assert result.runs[0].status == "complete"
        assert result.evaluated_items == 3
        assert result.fallback_total == 1
        assert result.fallback_evaluated == 0
        assert result.completeness == "partial"


def test_bounded_primary_run_completes_exact_target_subset(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 3)
        result = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="gg-deals",
            gg_api_key=SecretValue("secret"),
            max_items=1,
            gg_client=SparseGgFallback(),
            clock=lambda: NOW,
        )

        assert result.runs[0].status == "complete"
        assert result.evaluated_items == 1
        assert result.completeness == "partial"


def test_sparse_cheapshark_fallback_persists_exact_targets(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 3)
        result = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="auto",
            gg_api_key=SecretValue("secret"),
            max_items=None,
            gg_client=SparseGgFallback(),
            cheapshark_client=CheapOk(),
            clock=lambda: NOW,
        )
        assert [run.status for run in result.runs] == ["complete", "complete"]
        assert result.completeness == "complete"
        snapshot = storage.read_price_snapshot(
            account_id=account_id, country="US", provider="cheapshark", now=NOW
        )
        assert [
            (row.appid, row.targeted, row.evaluated) for row in snapshot.demand_rows
        ] == [(1, False, False), (2, True, True), (3, False, False)]


def test_empty_and_null_primary_prices_enter_successful_fallback(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 2)
        result = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="auto",
            gg_api_key=SecretValue("secret"),
            max_items=None,
            gg_client=EmptyAndNullGg(),
            cheapshark_client=CheapOk(),
            clock=lambda: NOW,
        )

        assert [run.status for run in result.runs] == ["complete", "complete"]
        assert result.fallback_total == 2
        assert result.fallback_evaluated == 2
        assert result.observed_items == 2
        assert result.completeness == "complete"

        primary = storage.read_price_snapshot(
            account_id=account_id, country="US", provider="gg-deals", now=NOW
        )
        assert [(row.appid, row.outcome) for row in primary.demand_rows] == [
            (1, "not_found"),
            (2, "not_found"),
        ]
        assert primary.facts == ()


def test_historical_low_without_current_offer_is_retained_and_falls_back(
    tmp_path,
) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 1)
        result = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="auto",
            gg_api_key=SecretValue("secret"),
            max_items=None,
            gg_client=LowOnlyGg(),
            cheapshark_client=CheapOk(),
            clock=lambda: NOW,
        )

        assert result.fallback_total == 1
        assert result.fallback_evaluated == 1
        assert result.completeness == "complete"
        snapshot = storage.read_wishlist_deal_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        assert {(fact.provider, fact.fact_kind) for fact in snapshot.prices.facts} == {
            ("gg-deals", "historical_low"),
            ("cheapshark", "offer"),
        }
        query = build_deal_query_from_snapshot(
            snapshot,
            account_alias="primary",
            country="US",
            store_class="unknown",
            generated_at=NOW,
            gg_credential_configured=True,
        )
        assert query["completeness"]["status"] == "complete"
        item = query["data"]["items"][0]
        assert item["deal"]["current_offer"]["provider"] == "cheapshark"
        assert [
            (attempt["provider"], attempt["status"])
            for attempt in item["deal"]["attempted_providers"]
        ] == [("gg-deals", "not_found"), ("cheapshark", "ready")]


def test_shuffled_sparse_primary_response_uses_canonical_fallback_targets(
    tmp_path,
) -> None:
    cheap = RecordingCheap()
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 3)
        result = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="auto",
            gg_api_key=SecretValue("secret"),
            max_items=None,
            gg_client=ShuffledSparseGg(),
            cheapshark_client=cheap,
            clock=lambda: NOW,
        )

        assert result.completeness == "complete"
        assert result.fallback_total == 2
        assert result.fallback_evaluated == 2
        assert cheap.calls == [1, 3]
        fallback = storage.read_price_snapshot(
            account_id=account_id,
            country="US",
            provider="cheapshark",
            now=NOW,
        )
        assert [
            (row.appid, row.targeted, row.evaluated) for row in fallback.demand_rows
        ] == [(1, True, True), (2, False, False), (3, True, True)]


def test_fallback_client_setup_failure_does_not_create_running_attempt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenCheap:
        def __init__(self) -> None:
            raise ValueError("synthetic setup failure")

    monkeypatch.setattr(price_library, "CheapSharkClient", BrokenCheap)
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 1)
        with pytest.raises(ValueError, match="synthetic setup failure"):
            sync_wishlist_prices(
                storage,
                account_id=account_id,
                country="US",
                provider="auto",
                gg_api_key=SecretValue("secret"),
                max_items=None,
                gg_client=LowOnlyGg(),
                clock=lambda: NOW,
            )

        snapshot = storage.read_price_snapshot(
            account_id=account_id,
            country="US",
            provider="cheapshark",
            now=NOW,
        )
        assert snapshot.attempts == ()


def test_default_fallback_bound_advances_and_converges_across_runs(tmp_path) -> None:
    cheap = RecordingCheap()
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 25)
        first = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="auto",
            gg_api_key=SecretValue("secret"),
            max_items=None,
            gg_client=AllEmptyGg(),
            cheapshark_client=cheap,
            clock=lambda: NOW,
        )
        second = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="auto",
            gg_api_key=SecretValue("secret"),
            max_items=None,
            gg_client=AllEmptyGg(),
            cheapshark_client=cheap,
            clock=lambda: NOW,
        )

        assert first.completeness == "partial"
        assert first.fallback_total == 25
        assert first.fallback_evaluated == 20
        assert second.completeness == "complete"
        assert second.fallback_total == 25
        assert second.fallback_evaluated == 5
        assert cheap.calls == list(range(1, 26))
        snapshot = storage.read_wishlist_deal_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        query = build_deal_query_from_snapshot(
            snapshot,
            account_alias="primary",
            country="US",
            store_class="unknown",
            generated_at=NOW,
            gg_credential_configured=True,
        )
        assert query["completeness"]["status"] == "complete"
        assert len(query["data"]["items"]) == 25


def test_explicit_max_refreshes_same_deterministic_prefix(tmp_path) -> None:
    cheap = RecordingCheap()
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 25)
        for _ in range(2):
            result = sync_wishlist_prices(
                storage,
                account_id=account_id,
                country="US",
                provider="auto",
                gg_api_key=SecretValue("secret"),
                max_items=3,
                gg_client=AllEmptyGg(),
                cheapshark_client=cheap,
                clock=lambda: NOW,
            )
            assert result.completeness == "partial"
            assert result.fallback_total == 3
            assert result.fallback_evaluated == 3

        assert cheap.calls == [1, 2, 3, 1, 2, 3]
        fallback = storage.read_price_snapshot(
            account_id=account_id,
            country="US",
            provider="cheapshark",
            now=NOW,
        )
        latest_run = fallback.attempts[-1].id
        assert [
            row.appid
            for row in fallback.demand_rows
            if row.sync_run_id == latest_run and row.targeted
        ] == [1, 2, 3]


def test_default_fallback_retries_expired_terminal_subject(tmp_path) -> None:
    cheap = RecordingCheap()
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 1)
        first = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="cheapshark",
            gg_api_key=None,
            max_items=None,
            cheapshark_client=cheap,
            clock=lambda: NOW,
        )
        assert first.completeness == "complete"

        refreshed_at = NOW + timedelta(hours=6)
        cheap.observed_at = refreshed_at
        second = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="cheapshark",
            gg_api_key=None,
            max_items=None,
            cheapshark_client=cheap,
            clock=lambda: refreshed_at,
        )

        assert second.completeness == "complete"
        assert second.fallback_evaluated == 1
        assert cheap.calls == [1, 1]


class CheapOk:
    def lookup_steam_app(self, appid: int) -> DealEvidenceSnapshot:
        return observed_snapshot("cheapshark", appid)


def test_mid_gg_batch_failure_is_recorded_and_fallback_completes(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 51)
        result = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="auto",
            gg_api_key=SecretValue("secret"),
            max_items=None,
            gg_client=MidBatchGg(),
            cheapshark_client=CheapOk(),
            clock=lambda: NOW,
        )
        assert result.completeness == "complete"
        assert result.providers_attempted == ("gg-deals", "cheapshark")
        assert result.providers_used == ("gg-deals", "cheapshark")
        assert result.runs[0].status == "partial"
        assert result.runs[0].error_code == "PROVIDER_UNAVAILABLE"
        metadata = storage._connection.execute(  # noqa: SLF001
            "SELECT retry_after_seconds FROM price_sync_metadata WHERE sync_run_id=?",
            (result.runs[0].id,),
        ).fetchone()
        assert metadata[0] == 7


class MidCheap:
    calls = 0

    def lookup_steam_app(self, appid: int) -> DealEvidenceSnapshot:
        self.calls += 1
        if self.calls == 2:
            raise CheapSharkError(
                "PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=9
            )
        return observed_snapshot("cheapshark", appid)


def test_mid_fallback_failure_is_partial_and_preserves_reason(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 2)
        cheap = MidCheap()
        result = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="cheapshark",
            gg_api_key=None,
            max_items=None,
            cheapshark_client=cheap,
            clock=lambda: NOW,
        )
        assert result.completeness == "partial"
        assert result.evaluated_items == 1
        assert result.runs[0].status == "partial"
        assert result.runs[0].error_code == "PROVIDER_UNAVAILABLE"
        retry = sync_wishlist_prices(
            storage,
            account_id=account_id,
            country="US",
            provider="cheapshark",
            gg_api_key=None,
            max_items=None,
            cheapshark_client=cheap,
            clock=lambda: NOW,
        )
        assert retry.completeness == "complete"
        assert retry.fallback_evaluated == 1
        assert cheap.calls == 3


class MismatchedCheap:
    def lookup_steam_app(self, appid: int) -> DealEvidenceSnapshot:
        product = ProductIdentity(f"product-{appid}", appid)
        reference = ManualReference(
            "https://www.cheapshark.com/redirect?dealID=synthetic", "manual"
        )
        offer = OfferEvidence(
            "cheapshark",
            product,
            Money(100, "CAD", "CA"),
            None,
            None,
            "unknown",
            "2026-07-11T12:00:00Z",
            reference,
            "normalized_game",
        )
        return DealEvidenceSnapshot(
            "cheapshark", product, (offer,), (), "2026-07-11T12:00:00Z", ()
        )


def test_currency_country_mismatch_fails_without_promoting(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 1)
        try:
            sync_wishlist_prices(
                storage,
                account_id=account_id,
                country="US",
                provider="cheapshark",
                gg_api_key=None,
                max_items=None,
                cheapshark_client=MismatchedCheap(),
                clock=lambda: NOW,
            )
        except Exception as exc:
            assert str(exc) == "PROVIDER_CONTEXT_MISMATCH"
        else:
            raise AssertionError("context mismatch should fail")
        snapshot = storage.read_price_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        assert snapshot.facts == ()
        assert snapshot.attempts[-1].status == "failed"


def test_price_sync_rejects_stale_wishlist_dependency(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 1)
        try:
            sync_wishlist_prices(
                storage,
                account_id=account_id,
                country="US",
                provider="cheapshark",
                gg_api_key=None,
                max_items=None,
                cheapshark_client=CheapOk(),
                clock=lambda: NOW + timedelta(hours=24, seconds=1),
            )
        except PriceSyncError as exc:
            assert exc.code == "STALE_LAST_GOOD"
        else:
            raise AssertionError("stale wishlist should block price synchronization")


def test_price_sync_rejects_running_wishlist_dependency(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 1)
        storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account_id,
            started_at=NOW + timedelta(minutes=1),
        )
        try:
            sync_wishlist_prices(
                storage,
                account_id=account_id,
                country="US",
                provider="cheapshark",
                gg_api_key=None,
                max_items=None,
                cheapshark_client=CheapOk(),
                clock=lambda: NOW + timedelta(minutes=1),
            )
        except PriceSyncError as exc:
            assert exc.code == "SYNC_IN_PROGRESS"
        else:
            raise AssertionError("running wishlist should block price synchronization")


def test_price_sync_preserves_latest_wishlist_failure_reason(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 1)
        failed = storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account_id,
            started_at=NOW + timedelta(minutes=1),
        )
        storage.finish_wishlist_sync(
            failed.id,
            completed_at=NOW + timedelta(minutes=1),
            error_code="WISHLIST_INACCESSIBLE_OR_AUTH_AMBIGUOUS",
        )
        try:
            sync_wishlist_prices(
                storage,
                account_id=account_id,
                country="US",
                provider="cheapshark",
                gg_api_key=None,
                max_items=None,
                cheapshark_client=CheapOk(),
                clock=lambda: NOW + timedelta(minutes=1),
            )
        except PriceSyncError as exc:
            assert exc.code == "WISHLIST_INACCESSIBLE_OR_AUTH_AMBIGUOUS"
        else:
            raise AssertionError("failed wishlist should block price synchronization")

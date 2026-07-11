from __future__ import annotations

from datetime import datetime, timezone

from steam_agent.cheapshark import CheapSharkError
from steam_agent.credentials import SecretValue
from steam_agent.deal_evidence import DealEvidenceSnapshot, ProductIdentity
from steam_agent.deal_evidence import ManualReference, Money, OfferEvidence
from steam_agent.gg_deals import GgDealsBatch, GgDealsError, RateLimitMetadata
from steam_agent.price_library import sync_wishlist_prices
from steam_agent.storage import Storage, WishlistObservation


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def setup(storage: Storage, count: int) -> int:
    account = storage.configure_steam_account(
        alias="primary", steam_id64="76561198000000000", configured_at=NOW
    )
    storage.record_wishlist_data_consent(
        account_id=account.id, disclosure_version="test", accepted_at=NOW,
        backups_acknowledged=True,
    )
    run = storage.begin_sync(
        provider="steam_web_api", capability="wishlist.read",
        account_id=account.id, started_at=NOW,
    )
    observations = tuple(
        WishlistObservation(appid, appid, appid, NOW)
        for appid in range(1, count + 1)
    )
    storage.complete_wishlist_snapshot(
        run.id, observations, item_list_retrieved_at=NOW,
        item_count_retrieved_at=NOW, item_list_reported_count=count,
        item_count_reported_count=count, completed_at=NOW,
    )
    return account.id


def empty_snapshot(provider: str, appid: int) -> DealEvidenceSnapshot:
    return DealEvidenceSnapshot(
        provider, ProductIdentity(f"product-{appid}", appid), (), (),
        "2026-07-11T12:00:00Z", (),
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
            values, tuple(empty_snapshot("gg-deals", appid) for appid in values),
            (), RateLimitMetadata(100, 50, 123),
        )


class CheapOk:
    def lookup_steam_app(self, appid: int) -> DealEvidenceSnapshot:
        return empty_snapshot("cheapshark", appid)


def test_mid_gg_batch_failure_is_recorded_and_fallback_completes(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 51)
        result = sync_wishlist_prices(
            storage, account_id=account_id, country="US", provider="auto",
            gg_api_key=SecretValue("secret"), max_items=None,
            gg_client=MidBatchGg(), cheapshark_client=CheapOk(), clock=lambda: NOW,
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
        return empty_snapshot("cheapshark", appid)


def test_mid_fallback_failure_is_partial_and_preserves_reason(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 2)
        result = sync_wishlist_prices(
            storage, account_id=account_id, country="US", provider="cheapshark",
            gg_api_key=None, max_items=None, cheapshark_client=MidCheap(),
            clock=lambda: NOW,
        )
        assert result.completeness == "partial"
        assert result.evaluated_items == 1
        assert result.runs[0].status == "partial"
        assert result.runs[0].error_code == "PROVIDER_UNAVAILABLE"


class MismatchedCheap:
    def lookup_steam_app(self, appid: int) -> DealEvidenceSnapshot:
        product = ProductIdentity(f"product-{appid}", appid)
        reference = ManualReference(
            "https://www.cheapshark.com/redirect?dealID=synthetic", "manual"
        )
        offer = OfferEvidence(
            "cheapshark", product, Money(100, "CAD", "CA"), None, None,
            "unknown", "2026-07-11T12:00:00Z", reference, "normalized_game",
        )
        return DealEvidenceSnapshot(
            "cheapshark", product, (offer,), (), "2026-07-11T12:00:00Z", ()
        )


def test_currency_country_mismatch_fails_without_promoting(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup(storage, 1)
        try:
            sync_wishlist_prices(
                storage, account_id=account_id, country="US",
                provider="cheapshark", gg_api_key=None, max_items=None,
                cheapshark_client=MismatchedCheap(), clock=lambda: NOW,
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

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from steam_agent.storage import (
    PriceDemandSubject,
    PriceFactObservation,
    Storage,
    WishlistObservation,
)


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def wishlist(storage: Storage) -> tuple[int, int, tuple[PriceDemandSubject, ...]]:
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
    storage.complete_wishlist_snapshot(
        run.id,
        (
            WishlistObservation(20, 1, 100, NOW),
            WishlistObservation(10, 0, 200, NOW),
        ),
        item_list_retrieved_at=NOW,
        item_count_retrieved_at=NOW,
        item_list_reported_count=2,
        item_count_reported_count=2,
        completed_at=NOW,
    )
    demand = (
        PriceDemandSubject(10, 0, 0, 200),
        PriceDemandSubject(20, 1, 1, 100),
    )
    return account.id, run.id, demand


def offer(
    appid: int,
    observed_at: datetime,
    amount: int = 500,
    provider: str = "gg-deals",
) -> PriceFactObservation:
    return PriceFactObservation(
        appid=appid,
        ordinal=0,
        fact_kind="offer",
        provider_product_id=f"steam/app/{appid}",
        amount_minor=amount,
        currency="USD",
        regular_amount_minor=1000,
        discount_percent=50,
        store_class="official",
        comparability="normalized_game",
        low_scope=None,
        effective_at=None,
        observed_at=observed_at,
        provider_url=(
            "https://gg.deals/game/synthetic/"
            if provider == "gg-deals"
            else "https://www.cheapshark.com/redirect?dealID=synthetic"
        ),
    )


@pytest.mark.parametrize("category", ["dlc", "pack"])
def test_gg_product_attribution_urls_are_allowed(tmp_path, category: str) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        run = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=1,
            started_at=NOW,
        )
        dlc_fact = replace(
            offer(10, NOW),
            provider_url=f"https://gg.deals/{category}/synthetic-product/",
        )
        storage.complete_price_sync(
            run.id,
            outcomes={10: "observed"},
            facts=(dlc_fact,),
            completed_at=NOW,
            status="partial",
        )
        snapshot = storage.read_price_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        assert snapshot.facts[0].provider_url == (
            f"https://gg.deals/{category}/synthetic-product/"
        )


def test_price_snapshot_is_atomic_last_good_fresh_and_hard_expiring(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        run = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="us",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=None,
            started_at=NOW,
        )
        storage.complete_price_sync(
            run.id,
            outcomes={10: "observed", 20: "not_found"},
            facts=(offer(10, NOW),),
            completed_at=NOW,
            status="complete",
            rate_limit=100,
            rate_remaining=99,
            rate_reset_value=123,
        )

        snapshot = storage.read_price_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        assert len(snapshot.facts) == 1
        assert snapshot.facts[0].amount_minor == 500
        assert snapshot.facts[0].fresh_until == "2026-07-11T18:00:00Z"
        assert snapshot.facts[0].hard_expires_at == "2026-07-18T12:00:00Z"
        assert [(item.appid, item.outcome) for item in snapshot.subjects] == [
            (10, "observed"),
            (20, "not_found"),
        ]
        assert "synthetic" not in str(
            storage._connection.execute(  # noqa: SLF001
                "SELECT payload_json FROM evidence WHERE capability='prices.wishlist.read'"
            ).fetchone()[0]
        )

        failed = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=None,
            started_at=NOW + timedelta(hours=1),
        )
        storage.finish_price_sync(
            failed.id,
            completed_at=NOW + timedelta(hours=1),
            error_code="PROVIDER_UNAVAILABLE",
        )
        assert (
            storage.read_price_snapshot(
                account_id=account_id, country="US", now=NOW + timedelta(hours=1)
            )
            .facts[0]
            .amount_minor
            == 500
        )

        stale = storage.read_price_snapshot(
            account_id=account_id, country="US", now=NOW + timedelta(hours=7)
        )
        assert stale.stale_offer_count == 1
        assert stale.stale_subject_count == 2

        expired = storage.read_price_snapshot(
            account_id=account_id,
            country="US",
            now=NOW + timedelta(days=7, seconds=1),
        )
        assert expired.facts == ()
        assert expired.subjects == ()


def test_running_and_abandoned_attempts_are_distinct(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=None,
            started_at=NOW,
        )
        running = storage.read_price_snapshot(
            account_id=account_id, country="US", now=NOW + timedelta(minutes=5)
        )
        assert running.running is True
        assert running.abandoned_running is False
        abandoned = storage.read_price_snapshot(
            account_id=account_id, country="US", now=NOW + timedelta(minutes=16)
        )
        assert abandoned.running is True
        assert abandoned.abandoned_running is True


def test_combined_snapshot_explains_never_synced_and_bounded_demand(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        never_synced = storage.read_wishlist_deal_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        assert [game.appid for game in never_synced.wishlist.games] == [10, 20]
        assert never_synced.stable_game_ids_by_appid == (
            never_synced.wishlist.stable_game_ids_by_appid
        )
        assert never_synced.prices.attempt_metadata == ()
        assert never_synced.prices.demand_rows == ()
        assert never_synced.prices.latest_relevant_attempts == ()

        run = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=1,
            started_at=NOW,
        )
        storage.complete_price_sync(
            run.id,
            outcomes={10: "observed"},
            facts=(offer(10, NOW),),
            completed_at=NOW,
            status="partial",
            rate_limit=100,
            rate_remaining=99,
            rate_reset_value=123,
        )
        snapshot = storage.read_wishlist_deal_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        metadata = snapshot.prices.attempt_metadata[0]
        assert (
            metadata.run.id,
            metadata.demand_count,
            metadata.evaluated_count,
            metadata.requested_limit,
            metadata.rate_limit,
            metadata.rate_remaining,
            metadata.rate_reset_value,
        ) == (run.id, 2, 1, 1, 100, 99, 123)
        assert [
            (row.appid, row.demand_order, row.evaluated, row.outcome)
            for row in snapshot.prices.demand_rows
        ] == [(10, 0, True, "observed"), (20, 1, False, None)]
        assert [
            (item.appid, item.attempt.run.id, item.demand.evaluated)
            for item in snapshot.prices.latest_relevant_attempts
        ] == [(10, run.id, True), (20, run.id, False)]


def test_not_found_freshness_and_newer_failure_do_not_replace_last_good(
    tmp_path,
) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        successful = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=2,
            started_at=NOW,
        )
        storage.complete_price_sync(
            successful.id,
            outcomes={10: "observed", 20: "not_found"},
            facts=(offer(10, NOW),),
            completed_at=NOW,
            status="complete",
        )
        fresh = storage.read_wishlist_deal_snapshot(
            account_id=account_id, country="US", now=NOW + timedelta(hours=5)
        )
        assert {
            item.appid: item.not_found_fresh
            for item in fresh.prices.latest_relevant_attempts
        } == {10: None, 20: True}
        stale_not_found = storage.read_wishlist_deal_snapshot(
            account_id=account_id, country="US", now=NOW + timedelta(hours=7)
        )
        assert (
            stale_not_found.prices.latest_relevant_attempts[1].not_found_fresh is False
        )

        failed = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=2,
            started_at=NOW + timedelta(hours=8),
        )
        storage.finish_price_sync(
            failed.id,
            completed_at=NOW + timedelta(hours=8),
            error_code="RATE_LIMITED",
            retry_after_seconds=90,
        )
        snapshot = storage.read_wishlist_deal_snapshot(
            account_id=account_id, country="US", now=NOW + timedelta(hours=9)
        )
        assert snapshot.prices.facts[0].promoted_sync_run_id == successful.id
        assert snapshot.prices.subjects[1].outcome == "not_found"
        assert all(
            item.attempt.run.id == failed.id
            for item in snapshot.prices.latest_relevant_attempts
        )
        assert all(
            item.not_found_fresh is None
            for item in snapshot.prices.latest_relevant_attempts
        )
        assert snapshot.prices.attempt_metadata[-1].retry_after_seconds == 90


def test_running_lineage_survives_hard_expiry(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        successful = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=1,
            started_at=NOW,
        )
        storage.complete_price_sync(
            successful.id,
            outcomes={10: "observed"},
            facts=(offer(10, NOW),),
            completed_at=NOW,
            status="partial",
        )
        running = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=1,
            started_at=NOW + timedelta(hours=1),
        )
        alongside_last_good = storage.read_wishlist_deal_snapshot(
            account_id=account_id,
            country="US",
            now=NOW + timedelta(hours=1, minutes=16),
        )
        assert alongside_last_good.prices.facts[0].promoted_sync_run_id == successful.id
        assert all(
            item.attempt.run.id == running.id
            for item in alongside_last_good.prices.latest_relevant_attempts
        )
        snapshot = storage.read_wishlist_deal_snapshot(
            account_id=account_id,
            country="US",
            now=NOW + timedelta(days=7, minutes=16),
        )
        assert snapshot.prices.facts == ()
        assert snapshot.prices.subjects == ()
        assert snapshot.prices.running is True
        assert snapshot.prices.abandoned_running is True
        assert all(
            item.attempt.run.id == running.id
            for item in snapshot.prices.latest_relevant_attempts
        )


def test_combined_snapshot_isolated_by_account_and_country(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        run = storage.begin_price_sync(
            provider="cheapshark",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=1,
            started_at=NOW,
        )
        storage.complete_price_sync(
            run.id,
            outcomes={10: "observed"},
            facts=(offer(10, NOW, provider="cheapshark"),),
            completed_at=NOW,
            status="partial",
        )
        other = storage.configure_steam_account(
            alias="secondary",
            steam_id64="76561198000000001",
            configured_at=NOW,
        )
        wrong_country = storage.read_wishlist_deal_snapshot(
            account_id=account_id, country="CA", now=NOW
        )
        wrong_account = storage.read_wishlist_deal_snapshot(
            account_id=other.id, country="US", now=NOW
        )
        assert wrong_country.prices.facts == ()
        assert wrong_country.prices.attempt_metadata == ()
        assert wrong_account.wishlist.games == ()
        assert wrong_account.prices.facts == ()
        assert wrong_account.prices.attempt_metadata == ()


def test_older_completion_cannot_replace_newer_per_app_projection(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        older = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=1,
            started_at=NOW,
        )
        newer = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=1,
            started_at=NOW + timedelta(minutes=1),
        )
        storage.complete_price_sync(
            newer.id,
            outcomes={10: "observed"},
            facts=(offer(10, NOW + timedelta(minutes=1), 400),),
            completed_at=NOW + timedelta(minutes=1),
            status="partial",
        )
        storage.complete_price_sync(
            older.id,
            outcomes={10: "observed"},
            facts=(offer(10, NOW, 900),),
            completed_at=NOW + timedelta(minutes=2),
            status="partial",
        )
        snapshot = storage.read_price_snapshot(
            account_id=account_id, country="US", now=NOW + timedelta(minutes=2)
        )
        assert snapshot.facts[0].amount_minor == 400
        assert snapshot.facts[0].promoted_sync_run_id == newer.id
        assert all(
            item.attempt.run.id == newer.id
            for item in snapshot.latest_relevant_attempts
        )


def test_provider_deletion_preserves_other_provider(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        for provider in ("gg-deals", "cheapshark"):
            run = storage.begin_price_sync(
                provider=provider,
                account_id=account_id,
                country="US",
                wishlist_sync_run_id=wishlist_run,
                demand=demand,
                requested_limit=1,
                started_at=NOW,
            )
            storage.complete_price_sync(
                run.id,
                outcomes={10: "observed"},
                facts=(offer(10, NOW, provider=provider),),
                completed_at=NOW,
                status="partial",
            )

        deletion = storage.delete_price_data(provider="gg-deals")
        assert deletion.current_removed == 1
        remaining = storage.read_price_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        assert {fact.provider for fact in remaining.facts} == {"cheapshark"}


@pytest.mark.parametrize(
    "mutation",
    [
        {"amount_minor": True},
        {"regular_amount_minor": -1},
        {"discount_percent": 101},
        {"provider_product_id": ""},
        {"comparability": "maybe"},
        {"store_class": "market"},
        {"provider_url": "https://user:secret@gg.deals/game/x/"},
        {"provider_url": "https://evil.example/game/x/"},
        {"provider_url": "https://gg.deals/game/x/#fragment"},
        {"provider_url": "https://gg.deals/game/x/?key=secret"},
        {"observed_at": NOW + timedelta(seconds=1)},
    ],
)
def test_storage_rejects_adversarial_fact_before_projection(
    tmp_path, mutation: dict[str, object]
) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id, wishlist_run, demand = wishlist(storage)
        run = storage.begin_price_sync(
            provider="gg-deals",
            account_id=account_id,
            country="US",
            wishlist_sync_run_id=wishlist_run,
            demand=demand,
            requested_limit=1,
            started_at=NOW,
        )
        with pytest.raises(ValueError):
            storage.complete_price_sync(
                run.id,
                outcomes={10: "observed"},
                facts=(replace(offer(10, NOW), **mutation),),
                completed_at=NOW,
                status="partial",
            )
        assert storage.get_sync_run(run.id).status == "running"
        assert (
            storage.read_price_snapshot(
                account_id=account_id, country="US", now=NOW
            ).facts
            == ()
        )

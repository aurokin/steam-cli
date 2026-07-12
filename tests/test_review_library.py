from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from steam_agent.review_library import (
    REVIEW_DISCLOSURE_VERSION,
    ReviewSyncError,
    sync_wishlist_reviews,
)
from steam_agent.steam_reviews import (
    SteamReviewError,
    SteamReviewHumanReference,
    SteamReviewRequestContext,
    SteamReviewSummary,
)
from steam_agent.storage import Storage, WishlistObservation


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(seconds=2)
        return result


class PacedClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class Client:
    def __init__(
        self,
        *,
        fail_at: int | None = None,
        retryable: bool = True,
        retry_after: int | None = 30,
    ) -> None:
        self.calls: list[int] = []
        self.fail_at = fail_at
        self.retryable = retryable
        self.retry_after = retry_after

    def fetch_summary(self, appid: int) -> SteamReviewSummary:
        self.calls.append(appid)
        if appid == self.fail_at:
            raise SteamReviewError(
                "RATE_LIMITED" if self.retryable else "PROVIDER_RESPONSE_INVALID",
                retryable=self.retryable,
                retry_after_seconds=self.retry_after if self.retryable else None,
            )
        return SteamReviewSummary(
            appid,
            8,
            80,
            20,
            100,
            SteamReviewRequestContext(),
            "steam_store_appreviews",
            SteamReviewHumanReference(
                appid, f"https://store.steampowered.com/app/{appid}/#app_reviews_hash"
            ),
        )


def setup_wishlist(storage: Storage, count: int = 25, *, alias: str = "primary") -> int:
    account = storage.configure_steam_account(
        alias=alias,
        steam_id64="76561198000000000" if alias == "primary" else "76561198000000001",
        configured_at=NOW,
    )
    storage.record_wishlist_data_consent(
        account_id=account.id,
        disclosure_version="test",
        accepted_at=NOW,
        backups_acknowledged=True,
    )
    storage.record_review_data_consent(
        account_id=account.id,
        disclosure_version=REVIEW_DISCLOSURE_VERSION,
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
        WishlistObservation(appid, ordinal, 100 + ordinal, NOW)
        for ordinal, appid in enumerate(range(100, 100 + count))
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


def test_default_runs_converge_over_bounded_wishlist(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage)
        first_client = Client()
        first = sync_wishlist_reviews(
            storage, account_id=account_id, client=first_client, clock=Clock()
        )
        second_client = Client()
        second = sync_wishlist_reviews(
            storage, account_id=account_id, client=second_client, clock=Clock(NOW + timedelta(minutes=3))
        )

        assert first.targeted_count == 20
        assert second.targeted_count == 5
        assert first_client.calls == list(range(100, 120))
        assert second_client.calls == list(range(120, 125))
        rows = storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*), MIN(total_reviews), MAX(total_reviews) FROM review_current"
        ).fetchone()
        assert tuple(rows) == (25, 100, 100)


def test_valid_empty_review_sync_remains_visible_as_completed_attempt(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 0)
        result = sync_wishlist_reviews(
            storage, account_id=account_id, client=Client(), clock=Clock()
        )
        assert result.candidate_count == 0 and result.run.status == "complete"
        snapshot = storage.read_wishlist_recommendation_snapshot(
            account_id=account_id, country="US", now=NOW
        )
        assert len(snapshot.review_attempts) == 1
        assert snapshot.review_attempts[0].status == "complete"
        assert snapshot.review_demand == ()


def test_explicit_limit_refreshes_prefix_and_preserves_full_demand(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 4)
        client = Client()
        result = sync_wishlist_reviews(
            storage, account_id=account_id, max_items=2, client=client, clock=Clock()
        )
        demand = storage._connection.execute(  # noqa: SLF001
            "SELECT targeted, evaluated, state FROM review_sync_demand WHERE sync_run_id=? ORDER BY ordinal",
            (result.run.id,),
        ).fetchall()
        assert [tuple(row) for row in demand] == [
            (1, 1, "ready"),
            (1, 1, "ready"),
            (0, 0, "unevaluated"),
            (0, 0, "unevaluated"),
        ]


def test_multi_item_loop_paces_its_own_local_request_reservations(tmp_path) -> None:
    paced = PacedClock()
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 3)
        client = Client()
        result = sync_wishlist_reviews(
            storage,
            account_id=account_id,
            max_items=3,
            client=client,
            clock=paced,
            sleeper=paced.sleep,
        )
        assert client.calls == [100, 101, 102]
        assert result.state_counts == {"ready": 3}
        assert paced.value == NOW + timedelta(seconds=2.1)


def test_provider_failure_stops_fanout_and_persists_cooldown(tmp_path) -> None:
    clock = Clock()
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 4)
        client = Client(fail_at=101)
        with pytest.raises(ReviewSyncError, match="RATE_LIMITED"):
            sync_wishlist_reviews(
                storage, account_id=account_id, max_items=4, client=client, clock=clock
            )
        assert client.calls == [100, 101]
        rows = storage._connection.execute(  # noqa: SLF001
            "SELECT evaluated, state, error_code FROM review_sync_demand ORDER BY ordinal"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, "ready", None),
            (1, "failed", "RATE_LIMITED"),
            (0, "unevaluated", "PROVIDER_FANOUT_STOPPED"),
            (0, "unevaluated", "PROVIDER_FANOUT_STOPPED"),
        ]
        assert not storage.reserve_provider_request(
            provider="steam-store-reviews",
            budget_scope="public-aggregate",
            requested_at=clock(),
            minimum_interval_seconds=1,
        )


def test_nonretryable_subject_failure_does_not_block_later_subjects(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 3)
        client = Client(fail_at=101, retryable=False)
        result = sync_wishlist_reviews(
            storage, account_id=account_id, max_items=3, client=client, clock=Clock()
        )
        assert client.calls == [100, 101, 102]
        assert result.state_counts == {"ready": 2, "failed": 1}


def test_default_convergence_retries_retryable_subject_after_cooldown(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 2)
        with pytest.raises(ReviewSyncError, match="RATE_LIMITED"):
            sync_wishlist_reviews(
                storage,
                account_id=account_id,
                client=Client(fail_at=100),
                clock=Clock(),
            )
        client = Client()
        result = sync_wishlist_reviews(
            storage,
            account_id=account_id,
            client=client,
            clock=Clock(NOW + timedelta(minutes=2)),
        )
        assert result.targeted_count == 2
        assert client.calls == [100, 101]


def test_active_review_target_is_reserved_across_concurrent_schedulers(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 3)
        first, first_candidates, first_targets = storage.begin_review_sync(
            account_id=account_id,
            max_items=1,
            skip_fresh_terminal=False,
            started_at=NOW,
            disclosure_version=REVIEW_DISCLOSURE_VERSION,
        )
        second, second_candidates, second_targets = storage.begin_review_sync(
            account_id=account_id,
            max_items=1,
            skip_fresh_terminal=False,
            started_at=NOW + timedelta(seconds=1),
            disclosure_version=REVIEW_DISCLOSURE_VERSION,
        )
        assert first_candidates == (100, 101, 102) and first_targets == (100,)
        assert second_candidates == (101, 102) and second_targets == (101,)
        storage.mark_remaining_reviews_unevaluated(
            first.id, observed_at=NOW, error_code="TEST_CLEANUP"
        )
        storage.mark_remaining_reviews_unevaluated(
            second.id, observed_at=NOW, error_code="TEST_CLEANUP"
        )
        storage.finish_review_sync(first.id, completed_at=NOW)
        storage.finish_review_sync(second.id, completed_at=NOW)


def test_retryable_failure_without_header_persists_default_restart_cooldown(
    tmp_path,
) -> None:
    clock = Clock()
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 2)
        with pytest.raises(ReviewSyncError):
            sync_wishlist_reviews(
                storage,
                account_id=account_id,
                max_items=2,
                client=Client(fail_at=100, retry_after=None),
                clock=clock,
            )
        assert not storage.reserve_provider_request(
            provider="steam-store-reviews",
            budget_scope="public-aggregate",
            requested_at=clock(),
            minimum_interval_seconds=1,
        )


def test_pre_network_cooldown_leaves_every_subject_unevaluated(tmp_path) -> None:
    paced = PacedClock()
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 2)
        storage.defer_provider_requests(
            provider="steam-store-reviews",
            budget_scope="public-aggregate",
            requested_at=NOW,
            retry_after_seconds=60,
        )
        client = Client()
        with pytest.raises(ReviewSyncError, match="REQUEST_THROTTLED"):
            sync_wishlist_reviews(
                storage,
                account_id=account_id,
                max_items=2,
                client=client,
                clock=paced,
                sleeper=paced.sleep,
            )
        assert client.calls == []
        rows = storage._connection.execute(  # noqa: SLF001
            "SELECT evaluated, state, error_code FROM review_sync_demand ORDER BY ordinal"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (0, "unevaluated", "REQUEST_THROTTLED"),
            (0, "unevaluated", "REQUEST_THROTTLED"),
        ]


def test_review_projection_is_global_but_account_demand_isolated_and_pruned(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        first = setup_wishlist(storage, 1)
        second = setup_wishlist(storage, 1, alias="secondary")
        sync_wishlist_reviews(storage, account_id=first, max_items=1, client=Client(), clock=Clock())
        sync_wishlist_reviews(
            storage,
            account_id=second,
            max_items=1,
            client=Client(),
            clock=Clock(NOW + timedelta(minutes=1)),
        )
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM review_current"
        ).fetchone()[0] == 1
        # The most recent promoting account may be deleted first without
        # cascading shared public current evidence needed by the other account.
        storage.delete_steam_account_data(second)
        row = storage._connection.execute(  # noqa: SLF001
            """SELECT r.account_id FROM review_current c
               JOIN sync_runs r ON r.id=c.promoted_sync_run_id"""
        ).fetchone()
        assert row[0] == first
        assert storage._connection.execute(  # noqa: SLF001
            """SELECT COUNT(*) FROM sync_runs
               WHERE capability='reviews.aggregate.read' AND account_id=?""",
            (second,),
        ).fetchone()[0] == 0
        storage.delete_steam_account_data(first)
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM review_current"
        ).fetchone()[0] == 0
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM review_observations"
        ).fetchone()[0] == 0


def test_provider_deletion_removes_review_cache_consent_and_cooldown(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 1)
        sync_wishlist_reviews(
            storage, account_id=account_id, max_items=1, client=Client(), clock=Clock()
        )
        deleted = storage.delete_review_data()
        assert deleted == {
            "observations_removed": 1,
            "demand_removed": 1,
            "current_removed": 1,
            "sync_runs_removed": 1,
        }
        assert storage.get_review_data_consent(account_id) is None
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM provider_request_limits WHERE provider='steam-store-reviews'"
        ).fetchone()[0] == 0


def test_account_provider_deletion_removes_sole_review_but_rehomes_shared(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        first = setup_wishlist(storage, 1)
        sync_wishlist_reviews(
            storage, account_id=first, max_items=1, client=Client(), clock=Clock()
        )
        deleted = storage.delete_review_data(account_id=first)
        assert deleted["current_removed"] == 1
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM review_current"
        ).fetchone()[0] == 0
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM wishlist_current WHERE account_id=?", (first,)
        ).fetchone()[0] == 1

    with Storage(tmp_path / "shared.sqlite3") as storage:
        first = setup_wishlist(storage, 1)
        second = setup_wishlist(storage, 1, alias="secondary")
        sync_wishlist_reviews(
            storage, account_id=first, max_items=1, client=Client(), clock=Clock()
        )
        sync_wishlist_reviews(
            storage,
            account_id=second,
            max_items=1,
            client=Client(),
            clock=Clock(NOW + timedelta(minutes=1)),
        )
        storage.delete_review_data(account_id=second)
        row = storage._connection.execute(  # noqa: SLF001
            """SELECT r.account_id FROM review_current c
               JOIN sync_runs r ON r.id=c.promoted_sync_run_id"""
        ).fetchone()
        assert row[0] == first


def test_wishlist_removal_prunes_current_review_without_fk_rollback(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 1)
        sync_wishlist_reviews(
            storage, account_id=account_id, max_items=1, client=Client(), clock=Clock()
        )
        run = storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account_id,
            started_at=NOW + timedelta(hours=1),
        )
        completed = storage.complete_wishlist_snapshot(
            run.id,
            (),
            item_list_retrieved_at=NOW + timedelta(hours=1),
            item_count_retrieved_at=NOW + timedelta(hours=1),
            item_list_reported_count=0,
            item_count_reported_count=0,
            completed_at=NOW + timedelta(hours=1),
        )
        assert completed.status == "complete"
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM review_current"
        ).fetchone()[0] == 0
        # Attempt lineage retains the identity only until its bounded expiry.
        assert storage.get_app(100) is not None
        storage._prune_reviews("2026-07-20T12:00:00Z")  # noqa: SLF001
        assert storage.get_app(100) is None


def test_readded_wishlist_subject_is_not_skipped_without_current_review(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 1)
        sync_wishlist_reviews(
            storage, account_id=account_id, client=Client(), clock=Clock()
        )
        empty = storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account_id,
            started_at=NOW + timedelta(minutes=5),
        )
        storage.complete_wishlist_snapshot(
            empty.id,
            (),
            item_list_retrieved_at=NOW + timedelta(minutes=5),
            item_count_retrieved_at=NOW + timedelta(minutes=5),
            item_list_reported_count=0,
            item_count_reported_count=0,
            completed_at=NOW + timedelta(minutes=5),
        )
        restored = storage.begin_sync(
            provider="steam_web_api",
            capability="wishlist.read",
            account_id=account_id,
            started_at=NOW + timedelta(minutes=10),
        )
        storage.complete_wishlist_snapshot(
            restored.id,
            (WishlistObservation(100, 0, 100, NOW + timedelta(minutes=10)),),
            item_list_retrieved_at=NOW + timedelta(minutes=10),
            item_count_retrieved_at=NOW + timedelta(minutes=10),
            item_list_reported_count=1,
            item_count_reported_count=1,
            completed_at=NOW + timedelta(minutes=10),
        )
        client = Client()
        result = sync_wishlist_reviews(
            storage,
            account_id=account_id,
            client=client,
            clock=Clock(NOW + timedelta(minutes=11)),
        )
        assert result.targeted_count == 1 and client.calls == [100]


def test_prune_preserves_promoting_run_until_fresh_current_expires(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 1)
        sync_wishlist_reviews(
            storage, account_id=account_id, max_items=1, client=Client(), clock=Clock()
        )
        run_id = storage._connection.execute(  # noqa: SLF001
            "SELECT promoted_sync_run_id FROM review_current"
        ).fetchone()[0]
        storage._connection.execute(  # noqa: SLF001
            "UPDATE sync_runs SET started_at='2026-07-01T00:00:00Z' WHERE id=?",
            (run_id,),
        )
        storage._connection.commit()  # noqa: SLF001
        storage._prune_reviews("2026-07-11T12:00:00Z")  # noqa: SLF001
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT promoted_sync_run_id FROM review_current"
        ).fetchone()[0] == run_id
        storage._connection.commit()  # noqa: SLF001
        storage.delete_steam_account_data(account_id)
        assert storage._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM review_current"
        ).fetchone()[0] == 0


def test_full_steam_deletion_removes_all_review_lineage(tmp_path) -> None:
    with Storage(tmp_path / "state.sqlite3") as storage:
        account_id = setup_wishlist(storage, 1)
        sync_wishlist_reviews(
            storage, account_id=account_id, max_items=1, client=Client(), clock=Clock()
        )
        deletion = storage.delete_all_steam_account_data()
        assert deletion.accounts_removed == 1
        for table in (
            "review_current",
            "review_observations",
            "review_sync_demand",
        ):
            assert storage._connection.execute(  # noqa: SLF001
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0
        assert storage._connection.execute(  # noqa: SLF001
            """SELECT COUNT(*) FROM sync_runs
               WHERE capability='reviews.aggregate.read'"""
        ).fetchone()[0] == 0

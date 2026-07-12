"""Bounded application service for public Steam aggregate review evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Callable

from steam_agent.steam_reviews import SteamReviewClient, SteamReviewError
from steam_agent.storage import Storage, SyncRun


DEFAULT_REVIEW_MAX_ITEMS = 20
MAX_REVIEW_ITEMS = 100
REVIEW_DISCLOSURE_VERSION = "2026-07-11.m4"
REVIEW_PROVIDER = "steam-store-reviews"
REVIEW_BUDGET_SCOPE = "public-aggregate"
DEFAULT_RETRY_AFTER_SECONDS = 60
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ReviewSyncError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ReviewSyncResult:
    run: SyncRun
    candidate_count: int
    targeted_count: int
    state_counts: dict[str, int]


def sync_wishlist_reviews(
    storage: Storage,
    *,
    account_id: int,
    max_items: int | None = None,
    client: SteamReviewClient | None = None,
    clock: Clock = now_utc,
    minimum_interval_seconds: float = 1.0,
    sleeper: Sleeper = time.sleep,
) -> ReviewSyncResult:
    """Synchronize a deterministic, bounded wishlist review slice.

    An omitted limit is convergence mode and skips subjects with fresh terminal
    attempts.  An explicit limit deliberately refreshes the wishlist prefix.
    """

    if max_items is not None and (
        isinstance(max_items, bool)
        or not isinstance(max_items, int)
        or not 1 <= max_items <= MAX_REVIEW_ITEMS
    ):
        raise ValueError(f"max_items must be between 1 and {MAX_REVIEW_ITEMS}")
    limit = DEFAULT_REVIEW_MAX_ITEMS if max_items is None else max_items
    started = clock()
    candidates = storage.review_sync_candidates(
        account_id, now=started, skip_fresh_terminal=max_items is None
    )
    targeted = candidates[:limit]
    run = storage.begin_review_sync(
        account_id=account_id,
        candidates=candidates,
        targeted=targeted,
        started_at=started,
        disclosure_version=REVIEW_DISCLOSURE_VERSION,
    )
    api = client or SteamReviewClient()
    counts: dict[str, int] = {}
    fatal: ReviewSyncError | None = None
    for appid in targeted:
        observed = clock()
        try:
            if not storage.reserve_provider_request(
                provider=REVIEW_PROVIDER,
                budget_scope=REVIEW_BUDGET_SCOPE,
                requested_at=observed,
                minimum_interval_seconds=minimum_interval_seconds,
            ):
                # A request made by this same bounded loop commonly owns the
                # one-interval reservation. Pace once, then re-check so a
                # longer cross-run Retry-After cooldown still stops fanout.
                sleeper(minimum_interval_seconds + 0.05)
                if not storage.reserve_provider_request(
                    provider=REVIEW_PROVIDER,
                    budget_scope=REVIEW_BUDGET_SCOPE,
                    requested_at=clock(),
                    minimum_interval_seconds=minimum_interval_seconds,
                ):
                    raise ReviewSyncError("REQUEST_THROTTLED", retryable=True)
            summary = api.fetch_summary(appid)
            storage.record_review_result(
                run.id,
                account_id=account_id,
                appid=appid,
                state="ready",
                observed_at=clock(),
                summary={
                    "appid": summary.appid,
                    "review_score": summary.review_score,
                    "total_positive": summary.total_positive,
                    "total_negative": summary.total_negative,
                    "total_reviews": summary.total_reviews,
                    "request_filter": summary.request_context.filter,
                    "language": summary.request_context.language,
                    "day_range": summary.request_context.day_range,
                    "review_type": summary.request_context.review_type,
                    "purchase_type": summary.request_context.purchase_type,
                    "num_per_page": summary.request_context.num_per_page,
                    "off_topic_activity_filtered": summary.request_context.off_topic_activity_filtered,
                    "source_locator": summary.source_locator,
                    "human_reference_url": summary.human_reference.url,
                },
            )
            counts["ready"] = counts.get("ready", 0) + 1
        except (SteamReviewError, ReviewSyncError) as exc:
            code = exc.code
            retryable = exc.retryable
            storage.record_review_result(
                run.id,
                account_id=account_id,
                appid=appid,
                state="failed",
                error_code=code,
                observed_at=clock(),
            )
            counts["failed"] = counts.get("failed", 0) + 1
            retry_after = getattr(exc, "retry_after_seconds", None)
            if retryable:
                storage.defer_provider_requests(
                    provider=REVIEW_PROVIDER,
                    budget_scope=REVIEW_BUDGET_SCOPE,
                    requested_at=clock(),
                    retry_after_seconds=(
                        DEFAULT_RETRY_AFTER_SECONDS
                        if retry_after is None
                        else retry_after
                    ),
                )
            if retryable:
                fatal = ReviewSyncError(code, retryable=True)
                storage.mark_remaining_reviews_unevaluated(
                    run.id, observed_at=clock(), error_code="PROVIDER_FANOUT_STOPPED"
                )
                break
    completed = storage.finish_review_sync(run.id, completed_at=clock())
    unevaluated = len(targeted) - sum(counts.values())
    if unevaluated:
        counts["unevaluated"] = unevaluated
    if fatal is not None:
        # The completed demand is durable and queryable, but the command must
        # truthfully signal that provider acquisition stopped early.
        raise fatal
    return ReviewSyncResult(completed, len(candidates), len(targeted), counts)


__all__ = [
    "DEFAULT_REVIEW_MAX_ITEMS",
    "MAX_REVIEW_ITEMS",
    "REVIEW_DISCLOSURE_VERSION",
    "ReviewSyncError",
    "ReviewSyncResult",
    "sync_wishlist_reviews",
]

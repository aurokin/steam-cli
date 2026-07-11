"""Atomic wishlist synchronization from a validated sequential response pair."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from steam_agent.credentials import SecretValue
from steam_agent.steam_wishlist import SteamWishlistClient, WishlistApiError
from steam_agent.storage import Storage, SyncRun, WishlistObservation


WISHLIST_CAPABILITY = "wishlist.read"
WISHLIST_DISCLOSURE_VERSION = "2026-07-11.m3-wishlist"
Clock = Callable[[], datetime]
RequestGate = Callable[[], None]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class WishlistSyncError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class WishlistSyncResult:
    run: SyncRun
    item_count: int


def sync_wishlist(
    storage: Storage,
    *,
    account_id: int,
    steamid: str,
    api_key: SecretValue,
    client: SteamWishlistClient | None = None,
    request_gate: RequestGate = lambda: None,
    clock: Clock = now_utc,
) -> WishlistSyncResult:
    run = storage.begin_sync(
        provider="steam_web_api",
        capability=WISHLIST_CAPABILITY,
        account_id=account_id,
        started_at=clock(),
    )
    api = client or SteamWishlistClient()
    try:
        request_gate()
        count = api.fetch_count(steamid=steamid, api_key=api_key)
        count_at = clock()
        if count.state != "ready" or count.count is None:
            raise WishlistSyncError(
                "WISHLIST_INACCESSIBLE_OR_AUTH_AMBIGUOUS", retryable=False
            )
        request_gate()
        items = api.fetch_items(steamid=steamid, api_key=api_key)
        items_at = clock()
        if items.state != "ready":
            raise WishlistSyncError(
                "WISHLIST_INACCESSIBLE_OR_AUTH_AMBIGUOUS", retryable=False
            )
        if len(items.items) != count.count:
            raise WishlistSyncError("PROVIDER_RESPONSE_INVALID", retryable=False)
        completed = storage.complete_wishlist_snapshot(
            run.id,
            tuple(
                WishlistObservation(
                    appid=item.appid,
                    priority=item.priority,
                    date_added=item.date_added,
                    observed_at=items_at,
                )
                for item in items.items
            ),
            item_list_retrieved_at=items_at,
            item_count_retrieved_at=count_at,
            item_list_reported_count=len(items.items),
            item_count_reported_count=count.count,
            completed_at=clock(),
        )
        return WishlistSyncResult(completed, count.count)
    except BaseException as exc:
        if isinstance(exc, WishlistSyncError):
            code = exc.code
            retryable = exc.retryable
        elif isinstance(exc, WishlistApiError):
            code = exc.code
            retryable = exc.retryable
        else:
            code = "INTERNAL_ERROR"
            retryable = False
        try:
            storage.finish_wishlist_sync(run.id, completed_at=clock(), error_code=code)
        except BaseException:
            pass
        if isinstance(exc, WishlistApiError):
            raise WishlistSyncError(code, retryable=retryable) from None
        raise


__all__ = [
    "WISHLIST_CAPABILITY",
    "WISHLIST_DISCLOSURE_VERSION",
    "WishlistSyncError",
    "WishlistSyncResult",
    "sync_wishlist",
]

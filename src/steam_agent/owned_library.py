"""Owned-library synchronization over normalized Steam Web API snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from steam_agent.credentials import SecretValue
from steam_agent.steam_web_api import SteamApiError, SteamWebApiClient
from steam_agent.storage import OwnedObservation, Storage, SyncRun


OWNED_CAPABILITY = "owned.visible.read"
OWNED_DISCLOSURE_VERSION = "2026-07-11.m2"

Clock = Callable[[], datetime]
RequestGate = Callable[[], None]

_PUBLIC_PROVIDER_ERROR = {
    "AUTHENTICATION_FAILED": "AUTHENTICATION_FAILED",
    "RATE_LIMITED": "PROVIDER_RATE_LIMITED",
    "PROVIDER_UNAVAILABLE": "PROVIDER_UNAVAILABLE",
    "INVALID_REQUEST": "PROVIDER_RESPONSE_INVALID",
    "PROVIDER_RESPONSE_INVALID": "PROVIDER_RESPONSE_INVALID",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class OwnedSyncError(RuntimeError):
    """A sanitized synchronization failure safe to expose as typed state."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class OwnedSyncResult:
    run: SyncRun
    visible_owned_count: int
    played_free_count: int


def sync_owned(
    storage: Storage,
    *,
    account_id: int,
    steamid: str,
    api_key: SecretValue,
    client: SteamWebApiClient | None = None,
    request_gate: RequestGate = lambda: None,
    clock: Clock = now_utc,
) -> OwnedSyncResult:
    """Fetch, classify, and atomically promote one visible-owned snapshot.

    Two documented requests are intentionally compared. The base request proves
    visible ownership; entries added only by ``include_played_free_games`` are
    classified as played-free. Neither request proves invisible licenses.
    """

    started_at = clock()
    run = storage.begin_sync(
        provider="steam_web_api",
        capability=OWNED_CAPABILITY,
        account_id=account_id,
        started_at=started_at,
    )
    api = client or SteamWebApiClient()
    try:
        request_gate()
        base = api.fetch_visible_owned_games(
            steamid=steamid,
            api_key=api_key,
            include_appinfo=True,
            include_played_free_games=False,
        )
        base_retrieved_at = clock()
        if base.snapshot_state != "ready":
            raise OwnedSyncError(
                "OWNED_GAMES_INACCESSIBLE_OR_UNKNOWN_ACCOUNT", retryable=False
            )
        request_gate()
        expanded = api.fetch_visible_owned_games(
            steamid=steamid,
            api_key=api_key,
            include_appinfo=True,
            include_played_free_games=True,
        )
        expanded_retrieved_at = clock()
        if base.snapshot_state != "ready" or expanded.snapshot_state != "ready":
            raise OwnedSyncError(
                "OWNED_GAMES_INACCESSIBLE_OR_UNKNOWN_ACCOUNT", retryable=False
            )

        base_appids = {game.appid for game in base.games}
        expanded_appids = {game.appid for game in expanded.games}
        if not base_appids <= expanded_appids:
            raise OwnedSyncError("PROVIDER_RESPONSE_INVALID", retryable=False)

        observed_at = expanded_retrieved_at
        observations = tuple(
            OwnedObservation(
                appid=game.appid,
                name=game.name,
                playtime_forever_minutes=game.playtime_forever_minutes,
                inclusion_basis=(
                    "visible_owned" if game.appid in base_appids else "played_free"
                ),
                observed_at=observed_at,
            )
            for game in expanded.games
        )
        completed = storage.complete_owned_snapshot(
            run.id,
            observations,
            base_retrieved_at=base_retrieved_at,
            expanded_retrieved_at=expanded_retrieved_at,
            base_reported_count=base.reported_game_count,
            expanded_reported_count=expanded.reported_game_count,
            completed_at=clock(),
        )
        return OwnedSyncResult(
            run=completed,
            visible_owned_count=len(base_appids),
            played_free_count=len(expanded_appids - base_appids),
        )
    except BaseException as exc:
        if isinstance(exc, OwnedSyncError):
            code = exc.code
        elif isinstance(exc, SteamApiError):
            code = _PUBLIC_PROVIDER_ERROR.get(exc.code, "PROVIDER_RESPONSE_INVALID")
        else:
            code = "INTERNAL_ERROR"
        try:
            storage.finish_owned_sync(
                run.id,
                status="failed",
                completed_at=clock(),
                error_code=code,
                error_detail=None,
            )
        except BaseException:
            pass
        if isinstance(exc, SteamApiError):
            raise OwnedSyncError(code, retryable=exc.retryable) from None
        raise


def owned_item(game: object) -> dict[str, object]:
    """Return a deterministic, identifier-redacted owned-game item."""

    return {
        "appid": game.appid,
        "name": game.name,
        "visible_in_owned_games": True,
        "inclusion_basis": game.inclusion_basis,
        "playtime_forever_minutes": game.playtime_forever_minutes,
        "observed_at": game.observed_at,
        "evidence_ids": [game.evidence_id],
        "family_available": None,
        "purchasable": None,
        "playable_now": None,
    }


__all__ = [
    "OWNED_CAPABILITY",
    "OWNED_DISCLOSURE_VERSION",
    "OwnedSyncError",
    "OwnedSyncResult",
    "owned_item",
    "sync_owned",
]

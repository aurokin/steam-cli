"""Demand-bounded catalog synchronization over Steam's ordered store stream."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from steam_agent.credentials import SecretValue
from steam_agent.steam_store_catalog import (
    CatalogApiError,
    CatalogScan,
    CatalogStream,
    SteamStoreCatalogClient,
)
from steam_agent.storage import (
    CatalogObservation,
    CatalogPageInput,
    CatalogStreamInput,
    Storage,
    SyncRun,
)


CATALOG_CAPABILITY = "catalog.application.read"
Clock = Callable[[], datetime]

_PUBLIC_PROVIDER_ERROR = {
    "AUTHENTICATION_FAILED": "AUTHENTICATION_FAILED",
    "RATE_LIMITED": "PROVIDER_RATE_LIMITED",
    "REQUEST_THROTTLED": "REQUEST_THROTTLED",
    "PROVIDER_UNAVAILABLE": "PROVIDER_UNAVAILABLE",
    "INVALID_REQUEST": "PROVIDER_RESPONSE_INVALID",
    "PROVIDER_RESPONSE_INVALID": "PROVIDER_RESPONSE_INVALID",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class CatalogSyncError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CatalogSyncResult:
    run: SyncRun
    demanded_count: int
    game_count: int
    non_game_count: int
    not_observed_count: int
    page_count: int


def sync_catalog(
    storage: Storage,
    *,
    account_id: int,
    machine_id: str,
    demanded_appids: Iterable[int],
    api_key: SecretValue | None,
    client: SteamStoreCatalogClient,
    clock: Clock = now_utc,
) -> CatalogSyncResult:
    demanded = tuple(sorted(set(demanded_appids)))
    run = storage.begin_catalog_sync(
        provider="steam_store_api",
        account_id=account_id,
        machine_id=machine_id,
        demanded_appids=demanded,
        started_at=clock(),
    )
    partial = False
    try:
        games = client.scan_demanded_apps(
            api_key=api_key,
            demanded_appids=demanded,
            stream=CatalogStream.GAMES,
        )
        if games.state != "complete":
            partial = True
            raise CatalogSyncError(
                _public_error(games.error_code), retryable=games.retryable
            )
        non_games = client.scan_demanded_apps(
            api_key=api_key,
            demanded_appids=demanded,
            stream=CatalogStream.NON_GAMES,
        )
        if non_games.state != "complete":
            partial = True
            raise CatalogSyncError(
                _public_error(non_games.error_code), retryable=non_games.retryable
            )
        _validate_complete_scan(games, demanded)
        _validate_complete_scan(non_games, demanded)

        game_hits = {hit.appid: hit for hit in games.hits}
        non_game_hits = {hit.appid: hit for hit in non_games.hits}
        if set(game_hits) & set(non_game_hits):
            raise CatalogSyncError("PROVIDER_RESPONSE_INVALID", retryable=False)
        observations: list[CatalogObservation] = []
        for appid in demanded:
            hit = game_hits.get(appid)
            classification = "game"
            if hit is None:
                hit = non_game_hits.get(appid)
                classification = "non_game" if hit is not None else "not_observed"
            observations.append(
                CatalogObservation(
                    appid=appid,
                    classification=classification,
                    last_modified=None if hit is None else hit.last_modified,
                    price_change_number=(
                        None if hit is None else hit.price_change_number
                    ),
                )
            )
        completed = storage.complete_catalog_snapshot(
            run.id,
            demanded,
            observations,
            games=_stream_input(games),
            non_games=_stream_input(non_games),
            completed_at=clock(),
        )
        return CatalogSyncResult(
            run=completed,
            demanded_count=len(demanded),
            game_count=len(game_hits),
            non_game_count=len(non_game_hits),
            not_observed_count=len(demanded) - len(game_hits) - len(non_game_hits),
            page_count=len(games.pages) + len(non_games.pages),
        )
    except BaseException as exc:
        if isinstance(exc, CatalogSyncError):
            code = exc.code
            retryable = exc.retryable
        elif isinstance(exc, CatalogApiError):
            code = _public_error(exc.code)
            retryable = exc.retryable
        else:
            code = "INTERNAL_ERROR"
            retryable = False
        try:
            storage.finish_catalog_sync(
                run.id,
                status="partial" if partial else "failed",
                completed_at=clock(),
                error_code=code,
            )
        except BaseException:
            pass
        if isinstance(exc, CatalogSyncError):
            raise
        if isinstance(exc, CatalogApiError):
            raise CatalogSyncError(code, retryable=retryable) from None
        raise


def _public_error(code: str | None) -> str:
    if code is None:
        return "PROVIDER_RESPONSE_INVALID"
    return _PUBLIC_PROVIDER_ERROR.get(code, "PROVIDER_RESPONSE_INVALID")


def _validate_complete_scan(scan: CatalogScan, demanded: tuple[int, ...]) -> None:
    if scan.unresolved_appids or scan.demanded_appids != demanded:
        raise CatalogSyncError("PROVIDER_RESPONSE_INVALID", retryable=False)
    covered = {
        *(hit.appid for hit in scan.hits),
        *scan.confirmed_absent_appids,
    }
    if covered != set(demanded):
        raise CatalogSyncError("PROVIDER_RESPONSE_INVALID", retryable=False)


def _stream_input(scan: CatalogScan) -> CatalogStreamInput:
    games = scan.stream is CatalogStream.GAMES
    return CatalogStreamInput(
        stream=str(scan.stream),
        termination=scan.termination,
        scanned_through_appid=scan.scanned_through_appid,
        filter_context={
            "include_games": games,
            "include_dlc": not games,
            "include_software": not games,
            "include_videos": not games,
            "include_hardware": not games,
            "max_results": scan.max_results,
        },
        pages=tuple(
            CatalogPageInput(
                page_number=page.page_number,
                requested_last_appid=page.requested_last_appid,
                first_appid=page.first_appid,
                last_appid=page.last_appid,
                item_count=page.item_count,
                have_more_results=page.have_more_results,
                retrieved_at=page.retrieved_at,
            )
            for page in scan.pages
        ),
    )


__all__ = [
    "CATALOG_CAPABILITY",
    "CatalogSyncError",
    "CatalogSyncResult",
    "sync_catalog",
]

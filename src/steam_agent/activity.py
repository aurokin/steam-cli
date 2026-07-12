"""Application boundary for bounded Steam activity and achievement evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from steam_agent.credentials import SecretValue
from steam_agent.steam_activity_api import (
    SteamActivityApiClient,
    SteamActivityApiError,
)
from steam_agent.storage import Storage, SyncRun, steam_application_stable_id


ACTIVITY_DISCLOSURE_VERSION = "2026-07-11.m4"
ACTIVITY_FRESH = timedelta(hours=6)
RECENT_FRESH = timedelta(hours=1)
RECENT_CURRENT_USE = timedelta(hours=24)
ACHIEVEMENT_FRESH = timedelta(hours=6)
HARD_RETENTION = timedelta(days=7)
DEFAULT_ACHIEVEMENT_MAX_ITEMS = 20
MAX_ACHIEVEMENT_ITEMS = 100
Clock = Callable[[], datetime]
RequestGate = Callable[[], None]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ActivitySyncError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ActivitySyncResult:
    run: SyncRun
    owned_count: int
    recent_count: int


@dataclass(frozen=True, slots=True)
class AchievementSyncResult:
    run: SyncRun
    candidate_count: int
    targeted_count: int
    state_counts: dict[str, int]


def sync_activity(
    storage: Storage,
    *,
    account_id: int,
    steamid: str,
    api_key: SecretValue,
    client: SteamActivityApiClient | None = None,
    request_gate: RequestGate = lambda: None,
    clock: Clock = now_utc,
) -> ActivitySyncResult:
    api = client or SteamActivityApiClient()
    run = storage.begin_sync(
        provider="steam_web_api",
        capability="activity.read",
        account_id=account_id,
        started_at=clock(),
    )
    try:
        acquired = api.fetch_activity(
            steamid=steamid, api_key=api_key, request_gate=request_gate
        )
        observed_at = clock()
        if acquired.owned.state != "ready" or acquired.recent.state != "ready":
            raise ActivitySyncError("ACTIVITY_INACCESSIBLE", retryable=False)
        merged = {game.appid: asdict(game) for game in acquired.owned.games}
        for game in acquired.recent.games:
            item = merged.setdefault(game.appid, asdict(game))
            item["recent_window_minutes"] = game.playtime_2weeks_minutes
            # Recent may contain a more current nullable provider field.
            for key, value in asdict(game).items():
                if key != "appid" and value is not None:
                    item[key] = value
        for item in merged.values():
            item.setdefault("recent_window_minutes", None)
        completed = storage.complete_activity_snapshot(
            run.id,
            account_id=account_id,
            games=tuple(merged[key] for key in sorted(merged)),
            observed_at=observed_at,
            recent_observed_at=observed_at,
            completed_at=clock(),
        )
        return ActivitySyncResult(completed, len(acquired.owned.games), len(acquired.recent.games))
    except BaseException as exc:
        code, retryable = _provider_error(exc)
        try:
            storage.finish_activity_sync_failed(run.id, error_code=code, completed_at=clock())
        except BaseException:
            pass
        if isinstance(exc, (SteamActivityApiError, ActivitySyncError)):
            raise ActivitySyncError(code, retryable=retryable) from None
        raise


def query_activity(
    storage: Storage,
    *,
    account_id: int,
    appid: int | None = None,
    clock: Clock = now_utc,
) -> dict[str, object]:
    if appid is not None and (isinstance(appid, bool) or not 1 <= appid <= (1 << 32) - 1):
        raise ValueError("appid is invalid")
    now = clock().astimezone(timezone.utc)
    snapshot = storage.read_activity_snapshot(account_id, now=now)
    latest = snapshot["latest"]
    last_good = snapshot["latest_complete"]
    items: list[dict[str, object]] = []
    for row in snapshot["items"]:
        if appid is not None and row["appid"] != appid:
            continue
        observed = _parse(row["observed_at"])
        recent_observed = _parse(row["recent_observed_at"]) if row["recent_observed_at"] else None
        recent_age = None if recent_observed is None else now - recent_observed
        recent_minutes = row["recent_window_minutes"]
        recent_state = "unknown"
        if recent_minutes is not None and recent_age is not None:
            if recent_age <= RECENT_FRESH:
                recent_state = "fresh"
            elif recent_age <= RECENT_CURRENT_USE:
                recent_state = "stale"
            else:
                recent_state = "expired"
                recent_minutes = None
        items.append({
            "appid": row["appid"],
            "game_id": steam_application_stable_id(row["appid"]),
            "name": row["name"],
            "playtime": {
                "lifetime_minutes": row["playtime_forever_minutes"],
                "recent_window_minutes": recent_minutes,
                "windows_minutes": row["playtime_windows_forever_minutes"],
                "mac_minutes": row["playtime_mac_forever_minutes"],
                "linux_minutes": row["playtime_linux_forever_minutes"],
                "deck_minutes": row["playtime_deck_forever_minutes"],
                "disconnected_minutes": row["playtime_disconnected_minutes"],
            },
            "last_played_at": (
                None if row["last_played_unix"] is None
                else datetime.fromtimestamp(row["last_played_unix"], timezone.utc).isoformat().replace("+00:00", "Z")
            ),
            "freshness": {
                "activity": "fresh" if now - observed <= ACTIVITY_FRESH else ("stale" if now - observed <= HARD_RETENTION else "expired"),
                "recent_window": recent_state,
            },
            "observed_at": row["observed_at"],
            "limitations": ["recent_window_is_not_a_session_log", "playtime_does_not_imply_preference"],
        })
    return {
        "items": items,
        "snapshot": {
            "last_attempt_status": None if latest is None else latest.status,
            "last_successful_sync_at": None if last_good is None else last_good.completed_at,
            "using_last_good": latest is not None and latest.status != "complete" and last_good is not None,
        },
    }


def achievement_candidates(
    storage: Storage,
    *,
    account_id: int,
    scope: Literal["recent", "installed", "owned"],
    explicit_appids: tuple[int, ...],
) -> tuple[int, ...]:
    if explicit_appids:
        candidates = explicit_appids
    elif scope == "recent":
        snapshot = storage.read_activity_snapshot(account_id)
        candidates = tuple(
            int(row["appid"])
            for row in sorted(
                (row for row in snapshot["items"] if row["recent_window_minutes"] is not None),
                key=lambda row: (-(row["last_played_unix"] or 0), row["appid"]),
            )
        )
    elif scope == "installed":
        candidates = tuple(game.appid for game in storage.list_installed("local"))
    elif scope == "owned":
        candidates = tuple(game.appid for game in storage.read_owned_snapshot(account_id).games)
    else:
        raise ValueError("achievement scope is invalid")
    if len(candidates) != len(set(candidates)) or any(
        isinstance(value, bool) or not 1 <= value <= (1 << 32) - 1 for value in candidates
    ):
        raise ValueError("achievement AppIDs are invalid")
    return candidates


def sync_achievements(
    storage: Storage,
    *,
    account_id: int,
    steamid: str,
    api_key: SecretValue,
    scope: Literal["recent", "installed", "owned"],
    explicit_appids: tuple[int, ...] = (),
    max_items: int = DEFAULT_ACHIEVEMENT_MAX_ITEMS,
    client: SteamActivityApiClient | None = None,
    request_gate: RequestGate = lambda: None,
    clock: Clock = now_utc,
) -> AchievementSyncResult:
    if isinstance(max_items, bool) or not 1 <= max_items <= MAX_ACHIEVEMENT_ITEMS:
        raise ValueError(f"max_items must be between 1 and {MAX_ACHIEVEMENT_ITEMS}")
    candidates = achievement_candidates(storage, account_id=account_id, scope=scope, explicit_appids=explicit_appids)
    targeted = candidates[:max_items]
    api = client or SteamActivityApiClient()
    run = storage.begin_achievement_sync(account_id=account_id, candidates=candidates, targeted=targeted, started_at=clock())
    counts: dict[str, int] = {}
    for target_index, appid in enumerate(targeted):
        state = "failed"
        error_code: str | None = None
        player_rows: tuple[dict[str, object], ...] = ()
        schema_rows: tuple[dict[str, object], ...] = ()
        schema_state = "achievements_not_supported"
        write_schema = True
        try:
            request_gate()
            player = api.fetch_player_achievements(steamid=steamid, appid=appid, api_key=api_key)
            state = player.state
            player_rows = tuple(asdict(item) for item in player.achievements)
            cached_schema = storage.read_cached_achievement_schema(appid, now=clock())
            if cached_schema is None:
                request_gate()
                schema = api.fetch_achievement_schema(appid=appid, api_key=api_key)
                schema_state = schema.state
                schema_rows = tuple(asdict(item) for item in schema.achievements)
            else:
                write_schema = False
                schema_state = str(cached_schema["state"])
                schema_rows = tuple(
                    {
                        **item,
                        "hidden": bool(item["hidden"]),
                    }
                    for item in cached_schema["achievements"]
                )
        except SteamActivityApiError as exc:
            error_code, _ = _provider_error(exc)
        storage.record_achievement_result(
            run.id, account_id=account_id, appid=appid, state=state,
            player=player_rows, schema_state=schema_state, schema=schema_rows,
            observed_at=clock(), error_code=error_code,
            write_schema=write_schema,
        )
        counts[state] = counts.get(state, 0) + 1
        if state == "failed" and error_code in {
            "AUTHENTICATION_FAILED", "PROVIDER_RATE_LIMITED", "PROVIDER_UNAVAILABLE"
        }:
            storage.mark_remaining_achievements_unevaluated(
                run.id, observed_at=clock(), error_code=error_code
            )
            remaining = len(targeted) - target_index - 1
            if remaining:
                counts["unevaluated"] = counts.get("unevaluated", 0) + remaining
            break
    completed = storage.finish_achievement_sync(run.id, completed_at=clock())
    if len(candidates) > len(targeted):
        counts["unevaluated"] = counts.get("unevaluated", 0) + len(candidates) - len(targeted)
    return AchievementSyncResult(completed, len(candidates), len(targeted), dict(sorted(counts.items())))


def query_achievements(
    storage: Storage, *, account_id: int, appid: int | None = None, clock: Clock = now_utc
) -> dict[str, object]:
    snapshot = storage.read_achievement_snapshot(account_id, appid=appid)
    now = clock().astimezone(timezone.utc)
    items: list[dict[str, object]] = []
    for row in snapshot["items"]:
        visible: list[dict[str, object]] = []
        unlocked = 0
        newest: int | None = None
        for achievement in row["achievements"]:
            achieved = bool(achievement["achieved"])
            unlocked += int(achieved)
            unlock_time = achievement["unlock_time_unix"]
            if achieved and unlock_time is not None:
                newest = max(newest or 0, unlock_time)
            # Steam hidden means locked title/description must not leave storage.
            hidden_locked = bool(achievement["hidden"]) and not achieved
            visible.append({
                "api_name": achievement["api_name"],
                "achieved": achieved,
                "unlock_time": unlock_time,
                "display_name": None if hidden_locked else achievement["display_name"],
                "description": None if hidden_locked else achievement["description"],
                "hidden_locked": hidden_locked,
            })
        observed_at = row["observed_at"]
        age = None if observed_at is None else now - _parse(observed_at)
        state = row["state"]
        if age is not None and age > HARD_RETENTION and state not in {"unevaluated", "running"}:
            state = "expired"
        last_good_summary = {
            "unlocked": unlocked,
            "total": len(visible),
            "newest_unlock_at": None if newest is None else datetime.fromtimestamp(newest, timezone.utc).isoformat().replace("+00:00", "Z"),
            "freshness": None if age is None else ("fresh" if age <= ACHIEVEMENT_FRESH else "stale"),
        } if visible else None
        items.append({
            "appid": row["appid"],
            "game_id": steam_application_stable_id(row["appid"]),
            "name": row["name"],
            "state": state,
            "targeted": bool(row["targeted"]),
            "evaluated": bool(row["evaluated"]),
            "summary": {
                "unlocked": unlocked if state == "ready" else None,
                "total": len(visible) if state == "ready" else None,
                "newest_unlock_at": None if newest is None else datetime.fromtimestamp(newest, timezone.utc).isoformat().replace("+00:00", "Z"),
                "freshness": None if age is None else ("fresh" if age <= ACHIEVEMENT_FRESH else "stale"),
            },
            "last_good_summary": last_good_summary if state != "ready" else None,
            "achievements": visible if state == "ready" else [],
            "observed_at": observed_at,
            "error_code": row["error_code"],
            "limitations": ["achievement_percentage_is_not_game_progress"],
        })
    latest = snapshot["latest"]
    return {"items": items, "snapshot": {"last_attempt_status": None if latest is None else latest.status}}


def _provider_error(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, ActivitySyncError):
        return exc.code, exc.retryable
    if isinstance(exc, SteamActivityApiError):
        return {
            "RATE_LIMITED": "PROVIDER_RATE_LIMITED",
            "INVALID_REQUEST": "PROVIDER_RESPONSE_INVALID",
        }.get(exc.code, exc.code), exc.retryable
    return "INTERNAL_ERROR", False


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


__all__ = [
    "ACTIVITY_DISCLOSURE_VERSION", "ActivitySyncError", "ActivitySyncResult",
    "AchievementSyncResult", "query_activity", "query_achievements", "sync_activity",
    "sync_achievements",
]

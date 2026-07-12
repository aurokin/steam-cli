"""Bounded read-only Steam activity and achievement provider adapter.

Only allowlisted normalized fields leave this module. SteamIDs and raw provider
bodies remain request-local, API keys travel in a header, and the fixed HTTPS
transport does not follow redirects.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from typing import Callable, Literal, Mapping, Protocol
from urllib.parse import urlencode

from steam_agent.credentials import SecretValue


STEAM_WEB_API_HOST = "api.steampowered.com"
OWNED_GAMES_PATH = "/IPlayerService/GetOwnedGames/v1/"
RECENT_GAMES_PATH = "/IPlayerService/GetRecentlyPlayedGames/v1/"
PLAYER_ACHIEVEMENTS_PATH = "/ISteamUserStats/GetPlayerAchievements/v1/"
GAME_SCHEMA_PATH = "/ISteamUserStats/GetSchemaForGame/v2/"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_GAMES = 100_000
MAX_RECENT_COUNT = 100
MAX_ACHIEVEMENTS = 10_000
MAX_API_NAME_BYTES = 128
MAX_DISPLAY_TEXT_BYTES = 8 * 1024
MAX_UNSIGNED_32 = (1 << 32) - 1
MAX_UNSIGNED_64 = (1 << 64) - 1
DEFAULT_TIMEOUT_SECONDS = 10.0


class SteamActivityApiError(RuntimeError):
    """Sanitized provider failure which never contains a response body."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class HttpTransport(Protocol):
    def request(
        self, *, host: str, path: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse: ...


class FixedHttpsTransport:
    """One-shot HTTPS transport with no redirect implementation."""

    def request(
        self, *, host: str, path: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        connection = http.client.HTTPSConnection(host, timeout=timeout)
        response: http.client.HTTPResponse | None = None
        body = b""
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise SteamActivityApiError(
                "PROVIDER_UNAVAILABLE", retryable=True
            ) from None
        finally:
            connection.close()
        if response is None:
            raise SteamActivityApiError("PROVIDER_UNAVAILABLE", retryable=True)
        if len(body) > MAX_RESPONSE_BYTES:
            raise SteamActivityApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return HttpResponse(response.status, body)


@dataclass(frozen=True, slots=True)
class ActivityGame:
    appid: int
    playtime_forever_minutes: int | None
    playtime_2weeks_minutes: int | None
    playtime_windows_forever_minutes: int | None
    playtime_mac_forever_minutes: int | None
    playtime_linux_forever_minutes: int | None
    playtime_deck_forever_minutes: int | None
    playtime_disconnected_minutes: int | None
    last_played_unix: int | None


@dataclass(frozen=True, slots=True)
class ActivityList:
    state: Literal["ready", "data_inaccessible"]
    games: tuple[ActivityGame, ...] = ()
    reported_count: int | None = None


@dataclass(frozen=True, slots=True)
class ActivityAcquisition:
    owned: ActivityList
    recent: ActivityList


@dataclass(frozen=True, slots=True)
class PlayerAchievement:
    api_name: str
    achieved: bool
    unlock_time_unix: int | None


@dataclass(frozen=True, slots=True)
class PlayerAchievements:
    appid: int
    state: Literal["ready", "profile_not_public", "achievements_not_supported"]
    achievements: tuple[PlayerAchievement, ...] = ()


@dataclass(frozen=True, slots=True)
class AchievementDefinition:
    api_name: str
    display_name: str | None
    description: str | None
    hidden: bool


@dataclass(frozen=True, slots=True)
class GameAchievementSchema:
    appid: int
    state: Literal["ready", "achievements_not_supported"]
    language: str
    achievements: tuple[AchievementDefinition, ...] = ()


class SteamActivityApiClient:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport or FixedHttpsTransport()
        self._timeout = timeout

    def fetch_activity(
        self,
        *,
        steamid: str,
        api_key: SecretValue,
        recent_count: int = MAX_RECENT_COUNT,
        request_gate: Callable[[], None] = lambda: None,
    ) -> ActivityAcquisition:
        """Acquire owned activity and Steam's recent-play window as one unit."""

        _validate_steamid(steamid)
        if not _uint32(recent_count, positive=True) or recent_count > MAX_RECENT_COUNT:
            raise ValueError(f"recent_count must be between 1 and {MAX_RECENT_COUNT}")
        owned_input = {
            "steamid": steamid,
            "include_appinfo": False,
            "include_played_free_games": True,
        }
        recent_input = {"steamid": steamid, "count": recent_count}
        request_gate()
        owned = _owned_activity(
            self._request_service(OWNED_GAMES_PATH, owned_input, api_key)
        )
        request_gate()
        recent = _recent_activity(
            self._request_service(RECENT_GAMES_PATH, recent_input, api_key),
            requested_count=recent_count,
        )
        return ActivityAcquisition(owned=owned, recent=recent)

    def fetch_player_achievements(
        self, *, steamid: str, appid: int, api_key: SecretValue
    ) -> PlayerAchievements:
        _validate_steamid(steamid)
        _validate_appid(appid)
        response = self._request_query(
            PLAYER_ACHIEVEMENTS_PATH,
            {"steamid": steamid, "appid": appid, "l": "english"},
            api_key,
        )
        return _player_achievements(response, appid=appid)

    def fetch_achievement_schema(
        self,
        *,
        appid: int,
        api_key: SecretValue,
        language: str = "english",
    ) -> GameAchievementSchema:
        _validate_appid(appid)
        if not _safe_text(language, max_bytes=64, required=True):
            raise ValueError("language must be a non-empty bounded string")
        response = self._request_query(
            GAME_SCHEMA_PATH,
            {"appid": appid, "l": language},
            api_key,
        )
        return _achievement_schema(response, appid=appid, language=language)

    def _request_service(
        self, endpoint: str, request_input: Mapping[str, object], api_key: SecretValue
    ) -> HttpResponse:
        input_json = json.dumps(request_input, sort_keys=True, separators=(",", ":"))
        return self._request(endpoint, {"input_json": input_json}, api_key)

    def _request_query(
        self, endpoint: str, params: Mapping[str, object], api_key: SecretValue
    ) -> HttpResponse:
        return self._request(endpoint, params, api_key)

    def _request(
        self, endpoint: str, params: Mapping[str, object], api_key: SecretValue
    ) -> HttpResponse:
        return self._transport.request(
            host=STEAM_WEB_API_HOST,
            path=f"{endpoint}?{urlencode(params)}",
            headers={
                "Accept": "application/json",
                "User-Agent": "steam-agent/0.1",
                "x-webapi-key": api_key.reveal(),
            },
            timeout=self._timeout,
        )


def _owned_activity(response: HttpResponse) -> ActivityList:
    body = _response_object(response)
    if body == {}:
        return ActivityList("data_inaccessible")
    count = body.get("game_count")
    if not _uint32(count) or count > MAX_GAMES:
        _invalid()
    raw_games = body.get("games")
    if count == 0 and raw_games is None:
        raw_games = []
    if not isinstance(raw_games, list) or len(raw_games) != count:
        _invalid()
    games = _activity_games(raw_games, recent=False)
    return ActivityList("ready", games, count)


def _recent_activity(response: HttpResponse, *, requested_count: int) -> ActivityList:
    body = _response_object(response)
    if body == {}:
        return ActivityList("data_inaccessible")
    count = body.get("total_count")
    if not _uint32(count) or count > MAX_GAMES:
        _invalid()
    raw_games = body.get("games")
    if count == 0 and raw_games is None:
        raw_games = []
    if (
        not isinstance(raw_games, list)
        or len(raw_games) > count
        or len(raw_games) > MAX_GAMES
        or (requested_count > 0 and len(raw_games) > requested_count)
        or (count > 0 and not raw_games)
    ):
        _invalid()
    games = _activity_games(raw_games, recent=True)
    return ActivityList("ready", games, count)


def _activity_games(
    raw_games: list[object], *, recent: bool
) -> tuple[ActivityGame, ...]:
    normalized: list[ActivityGame] = []
    seen: set[int] = set()
    for raw in raw_games:
        if not isinstance(raw, dict):
            _invalid()
        appid = raw.get("appid")
        if not _uint32(appid, positive=True) or appid in seen:
            _invalid()
        seen.add(appid)
        values = {
            name: _optional_uint32(raw, provider_name)
            for name, provider_name in (
                ("playtime_forever_minutes", "playtime_forever"),
                ("playtime_2weeks_minutes", "playtime_2weeks"),
                ("playtime_windows_forever_minutes", "playtime_windows_forever"),
                ("playtime_mac_forever_minutes", "playtime_mac_forever"),
                ("playtime_linux_forever_minutes", "playtime_linux_forever"),
                ("playtime_deck_forever_minutes", "playtime_deck_forever"),
                ("playtime_disconnected_minutes", "playtime_disconnected"),
                ("last_played_unix", "rtime_last_played"),
            )
        }
        if recent and values["playtime_2weeks_minutes"] is None:
            _invalid()
        normalized.append(ActivityGame(appid=appid, **values))
    normalized.sort(key=lambda game: game.appid)
    return tuple(normalized)


def _player_achievements(response: HttpResponse, *, appid: int) -> PlayerAchievements:
    if response.status not in (200, 400, 403):
        _map_status(response.status)
    try:
        payload = _json_object(response.body)
    except SteamActivityApiError:
        if response.status in (400, 403):
            _map_status(response.status)
        raise
    playerstats = payload.get("playerstats")
    if not isinstance(playerstats, dict):
        _map_status(response.status)
        _invalid()
    success = playerstats.get("success")
    error = playerstats.get("error")
    if (
        success is False
        and error == "Profile is not public"
        and response.status
        in (
            200,
            403,
        )
    ):
        return PlayerAchievements(appid, "profile_not_public")
    if (
        success is False
        and error == "Requested app has no stats"
        and response.status in (200, 400)
    ):
        return PlayerAchievements(appid, "achievements_not_supported")
    _map_status(response.status)
    if success is not True:
        _invalid()
    raw_achievements = playerstats.get("achievements")
    if (
        not isinstance(raw_achievements, list)
        or len(raw_achievements) > MAX_ACHIEVEMENTS
    ):
        _invalid()
    achievements: list[PlayerAchievement] = []
    seen: set[str] = set()
    for raw in raw_achievements:
        if not isinstance(raw, dict):
            _invalid()
        api_name = raw.get("apiname")
        achieved = raw.get("achieved")
        unlock_time = raw.get("unlocktime")
        if (
            not _safe_text(api_name, max_bytes=MAX_API_NAME_BYTES, required=True)
            or api_name in seen
            or not _zero_or_one(achieved)
            or not _uint32(unlock_time)
        ):
            _invalid()
        seen.add(api_name)
        achievements.append(
            PlayerAchievement(
                api_name=api_name,
                achieved=bool(achieved),
                unlock_time_unix=unlock_time or None,
            )
        )
    achievements.sort(key=lambda item: item.api_name)
    return PlayerAchievements(appid, "ready", tuple(achievements))


def _achievement_schema(
    response: HttpResponse, *, appid: int, language: str
) -> GameAchievementSchema:
    _map_status(response.status)
    payload = _json_object(response.body)
    game = payload.get("game")
    if game == {}:
        return GameAchievementSchema(appid, "achievements_not_supported", language)
    if not isinstance(game, dict):
        _invalid()
    available = game.get("availableGameStats")
    if available is None:
        return GameAchievementSchema(appid, "achievements_not_supported", language)
    if not isinstance(available, dict):
        _invalid()
    raw_achievements = available.get("achievements", [])
    if (
        not isinstance(raw_achievements, list)
        or len(raw_achievements) > MAX_ACHIEVEMENTS
    ):
        _invalid()
    achievements: list[AchievementDefinition] = []
    seen: set[str] = set()
    for raw in raw_achievements:
        if not isinstance(raw, dict):
            _invalid()
        api_name = raw.get("name")
        display_name = raw.get("displayName")
        description = raw.get("description")
        hidden = raw.get("hidden")
        if (
            not _safe_text(api_name, max_bytes=MAX_API_NAME_BYTES, required=True)
            or api_name in seen
            or not _zero_or_one(hidden)
            or not _safe_text(display_name, max_bytes=MAX_DISPLAY_TEXT_BYTES)
            or not _safe_text(description, max_bytes=MAX_DISPLAY_TEXT_BYTES)
        ):
            _invalid()
        seen.add(api_name)
        achievements.append(
            AchievementDefinition(api_name, display_name, description, bool(hidden))
        )
    achievements.sort(key=lambda item: item.api_name)
    return GameAchievementSchema(appid, "ready", language, tuple(achievements))


def _response_object(response: HttpResponse) -> dict[object, object]:
    _map_status(response.status)
    payload = _json_object(response.body)
    body = payload.get("response")
    if not isinstance(body, dict):
        _invalid()
    return body


def _json_object(body: bytes) -> dict[object, object]:
    if len(body) > MAX_RESPONSE_BYTES:
        _invalid()
    try:
        payload = json.loads(body)
    except (UnicodeError, ValueError, RecursionError):
        raise SteamActivityApiError(
            "PROVIDER_RESPONSE_INVALID", retryable=False
        ) from None
    if not isinstance(payload, dict):
        _invalid()
    return payload


def _map_status(status: int) -> None:
    if status in (401, 403):
        raise SteamActivityApiError("AUTHENTICATION_FAILED", retryable=False)
    if status == 400:
        raise SteamActivityApiError("INVALID_REQUEST", retryable=False)
    if status == 429:
        raise SteamActivityApiError("RATE_LIMITED", retryable=True)
    if status >= 500:
        raise SteamActivityApiError("PROVIDER_UNAVAILABLE", retryable=True)
    if status != 200:
        raise SteamActivityApiError("PROVIDER_RESPONSE_INVALID", retryable=False)


def _optional_uint32(raw: Mapping[object, object], name: str) -> int | None:
    value = raw.get(name)
    if value is None:
        return None
    if not _uint32(value):
        _invalid()
    return value


def _validate_steamid(steamid: str) -> None:
    if (
        not isinstance(steamid, str)
        or not steamid.isdecimal()
        or not 1 <= int(steamid) <= MAX_UNSIGNED_64
    ):
        raise ValueError("steamid must be an unsigned 64-bit decimal value")


def _validate_appid(appid: int) -> None:
    if not _uint32(appid, positive=True):
        raise ValueError("appid must be a positive unsigned 32-bit integer")


def _uint32(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (value > 0 if positive else value >= 0)
        and value <= MAX_UNSIGNED_32
    )


def _zero_or_one(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in (0, 1)


def _safe_text(value: object, *, max_bytes: int, required: bool = False) -> bool:
    if value is None:
        return not required
    if not isinstance(value, str) or (required and not value) or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeError:
        return False


def _invalid() -> None:
    raise SteamActivityApiError("PROVIDER_RESPONSE_INVALID", retryable=False)


__all__ = [
    "AchievementDefinition",
    "ActivityAcquisition",
    "ActivityGame",
    "ActivityList",
    "GameAchievementSchema",
    "HttpResponse",
    "PlayerAchievement",
    "PlayerAchievements",
    "SteamActivityApiClient",
    "SteamActivityApiError",
]

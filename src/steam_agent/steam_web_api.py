"""Minimal, non-retaining Steam Web API capability probe.

The API key is carried only in the ``x-webapi-key`` header to a fixed HTTPS
host. Redirects are not followed, response bodies are bounded, and callers
receive typed outcomes rather than provider text or raw payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from typing import Mapping, Protocol
from urllib.parse import urlencode

from steam_agent.credentials import SecretValue


STEAM_WEB_API_HOST = "api.steampowered.com"
OWNED_GAMES_PATH = "/IPlayerService/GetOwnedGames/v1/"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0


class SteamApiError(RuntimeError):
    """Sanitized provider failure safe to convert into a typed CLI result."""

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
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...


class FixedHttpsTransport:
    """One-shot HTTPS transport which deliberately has no redirect handling."""

    def request(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        connection = http.client.HTTPSConnection(host, timeout=timeout)
        failed = False
        response: http.client.HTTPResponse | None = None
        body = b""
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            failed = True
        finally:
            connection.close()
        if failed or response is None:
            raise SteamApiError("PROVIDER_UNAVAILABLE", retryable=True)
        if len(body) > MAX_RESPONSE_BYTES:
            raise SteamApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return HttpResponse(status=response.status, body=body)


@dataclass(frozen=True, slots=True)
class OwnedGamesProbe:
    probe_state: str
    visible_game_count: int | None
    retryable: bool
    limitations: tuple[str, ...] = (
        "individually_private_games_may_be_omitted",
        "unplayed_free_entitlements_are_not_complete",
    )


class SteamWebApiClient:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport or FixedHttpsTransport()
        self._timeout = timeout

    def probe_visible_owned_games(
        self, *, steamid: str, api_key: SecretValue
    ) -> OwnedGamesProbe:
        if not steamid.isdecimal() or not 1 <= int(steamid) <= (1 << 64) - 1:
            raise ValueError("steamid must be an unsigned 64-bit decimal value")
        request_input = json.dumps(
            {
                "steamid": steamid,
                "include_appinfo": False,
                "include_played_free_games": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        path = f"{OWNED_GAMES_PATH}?{urlencode({'input_json': request_input})}"
        response = self._transport.request(
            host=STEAM_WEB_API_HOST,
            path=path,
            headers={
                "Accept": "application/json",
                "User-Agent": "steam-agent/0.1",
                "x-webapi-key": api_key.reveal(),
            },
            timeout=self._timeout,
        )
        return _interpret_owned_response(response)


def _interpret_owned_response(response: HttpResponse) -> OwnedGamesProbe:
    status = response.status
    if status in (401, 403):
        raise SteamApiError("AUTHENTICATION_FAILED", retryable=False)
    if status == 400:
        raise SteamApiError("INVALID_REQUEST", retryable=False)
    if status == 429:
        raise SteamApiError("RATE_LIMITED", retryable=True)
    if status >= 500:
        raise SteamApiError("PROVIDER_UNAVAILABLE", retryable=True)
    if status != 200:
        raise SteamApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    decoded, payload = _decode_json(response.body)
    if not decoded:
        raise SteamApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    if not isinstance(payload, dict) or set(payload) != {"response"}:
        raise SteamApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    body = payload["response"]
    if not isinstance(body, dict):
        raise SteamApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    if body == {}:
        return OwnedGamesProbe(
            probe_state="data_inaccessible",
            visible_game_count=None,
            retryable=False,
        )
    count = body.get("game_count")
    games_present = "games" in body
    games = body.get("games")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or (games_present and not isinstance(games, list))
        or (count > 0 and not games_present)
        or (isinstance(games, list) and len(games) != count)
        or (
            isinstance(games, list)
            and not _valid_game_entries(games)
        )
    ):
        raise SteamApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return OwnedGamesProbe(
        probe_state="ready",
        visible_game_count=count,
        retryable=False,
    )


def _decode_json(body: bytes) -> tuple[bool, object]:
    try:
        return True, json.loads(body)
    except (UnicodeError, ValueError, RecursionError):
        return False, None


def _valid_game_entries(games: list[object]) -> bool:
    appids: set[int] = set()
    for game in games:
        if not isinstance(game, dict):
            return False
        appid = game.get("appid")
        if (
            not isinstance(appid, int)
            or isinstance(appid, bool)
            or appid <= 0
            or appid in appids
        ):
            return False
        appids.add(appid)
    return True


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FixedHttpsTransport",
    "HttpResponse",
    "HttpTransport",
    "MAX_RESPONSE_BYTES",
    "OWNED_GAMES_PATH",
    "OwnedGamesProbe",
    "STEAM_WEB_API_HOST",
    "SteamApiError",
    "SteamWebApiClient",
]

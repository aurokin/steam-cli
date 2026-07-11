"""Bounded, provisional Steam wishlist reader.

Only normalized allowlisted fields leave this module. Response bodies are
bounded, processed in memory, and never attached to results or exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from typing import Mapping, Protocol
from urllib.parse import urlencode

from steam_agent.credentials import SecretValue


STEAM_WEB_API_HOST = "api.steampowered.com"
WISHLIST_PATH = "/IWishlistService/GetWishlist/v1/"
WISHLIST_COUNT_PATH = "/IWishlistService/GetWishlistItemCount/v1/"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 100_000
MAX_UNSIGNED_32 = (1 << 32) - 1
DEFAULT_TIMEOUT_SECONDS = 10.0


class WishlistApiError(RuntimeError):
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
    def request(
        self, *, host: str, path: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        connection = http.client.HTTPSConnection(host, timeout=timeout)
        response: http.client.HTTPResponse | None = None
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise WishlistApiError("PROVIDER_UNAVAILABLE", retryable=True) from None
        finally:
            connection.close()
        if response is None or len(body) > MAX_RESPONSE_BYTES:
            raise WishlistApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return HttpResponse(response.status, body)


@dataclass(frozen=True, slots=True)
class WishlistItem:
    appid: int
    priority: int
    date_added: int


@dataclass(frozen=True, slots=True)
class WishlistItems:
    state: str
    items: tuple[WishlistItem, ...] = ()


@dataclass(frozen=True, slots=True)
class WishlistCount:
    state: str
    count: int | None = None


class SteamWishlistClient:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport or FixedHttpsTransport()
        self._timeout = timeout

    def fetch_items(self, *, steamid: str, api_key: SecretValue) -> WishlistItems:
        return _items(self._request(WISHLIST_PATH, steamid, api_key))

    def fetch_count(self, *, steamid: str, api_key: SecretValue) -> WishlistCount:
        return _count(self._request(WISHLIST_COUNT_PATH, steamid, api_key))

    def _request(
        self, endpoint: str, steamid: str, api_key: SecretValue
    ) -> HttpResponse:
        if not steamid.isdecimal() or not 1 <= int(steamid) <= (1 << 64) - 1:
            raise ValueError("steamid must be an unsigned 64-bit decimal value")
        input_json = json.dumps(
            {"steamid": steamid}, sort_keys=True, separators=(",", ":")
        )
        return self._transport.request(
            host=STEAM_WEB_API_HOST,
            path=f"{endpoint}?{urlencode({'input_json': input_json})}",
            headers={
                "Accept": "application/json",
                "User-Agent": "steam-agent/0.1",
                "x-webapi-key": api_key.reveal(),
            },
            timeout=self._timeout,
        )


def _payload(response: HttpResponse) -> dict[object, object]:
    if response.status in (401, 403):
        raise WishlistApiError("AUTHENTICATION_FAILED", retryable=False)
    if response.status == 429:
        raise WishlistApiError("PROVIDER_RATE_LIMITED", retryable=True)
    if response.status >= 500:
        raise WishlistApiError("PROVIDER_UNAVAILABLE", retryable=True)
    if response.status != 200:
        raise WishlistApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    try:
        payload = json.loads(response.body)
    except (UnicodeError, ValueError, RecursionError):
        raise WishlistApiError("PROVIDER_RESPONSE_INVALID", retryable=False) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise WishlistApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return payload["response"]


def _items(response: HttpResponse) -> WishlistItems:
    body = _payload(response)
    if body == {}:
        return WishlistItems("ambiguous")
    value = body.get("items")
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise WishlistApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    normalized: list[WishlistItem] = []
    seen: set[int] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise WishlistApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        appid = raw.get("appid")
        priority = raw.get("priority")
        date_added = raw.get("date_added")
        if (
            not _uint32(appid, positive=True)
            or not _uint32(priority)
            or not _uint32(date_added)
            or appid in seen
        ):
            raise WishlistApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        seen.add(appid)
        normalized.append(WishlistItem(appid, priority, date_added))
    normalized.sort(key=lambda item: item.appid)
    return WishlistItems("ready", tuple(normalized))


def _count(response: HttpResponse) -> WishlistCount:
    body = _payload(response)
    if body == {}:
        return WishlistCount("ambiguous")
    value = body.get("count")
    if not _uint32(value) or value > MAX_ITEMS:
        raise WishlistApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return WishlistCount("ready", value)


def _uint32(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (value > 0 if positive else value >= 0)
        and value <= MAX_UNSIGNED_32
    )


__all__ = [
    "HttpResponse",
    "SteamWishlistClient",
    "WishlistApiError",
    "WishlistCount",
    "WishlistItem",
    "WishlistItems",
]

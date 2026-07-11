"""Non-retaining credential probes for optional third-party providers."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from typing import Mapping, Protocol
from urllib.parse import urlencode

from steam_agent.credentials import SecretValue


MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0


class ProviderAuthError(RuntimeError):
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
            raise ProviderAuthError("PROVIDER_UNAVAILABLE", retryable=True)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProviderAuthError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return HttpResponse(response.status, body)


@dataclass(frozen=True, slots=True)
class ProviderAuthProbe:
    provider: str
    state: str = "ready"
    retryable: bool = False


class ProviderAuthClient:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport or FixedHttpsTransport()
        self._timeout = timeout

    def probe(self, *, provider: str, api_key: SecretValue) -> ProviderAuthProbe:
        host, path, headers = _request(provider, api_key)
        response = self._transport.request(
            host=host,
            path=path,
            headers=headers,
            timeout=self._timeout,
        )
        payload = _response_payload(response)
        if provider == "isthereanydeal":
            valid = isinstance(payload.get("found"), bool)
        elif provider in ("steamgriddb", "gg-deals"):
            valid = payload.get("success") is True
        else:
            raise ValueError("unsupported credential probe provider")
        if not valid:
            raise ProviderAuthError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return ProviderAuthProbe(provider)


def _request(
    provider: str, api_key: SecretValue
) -> tuple[str, str, dict[str, str]]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "steam-agent/0.1 (+https://hsadler.com)",
    }
    if provider == "isthereanydeal":
        # Official ITAD docs name this header ITAD-API-Key and document the
        # lookup response as {"found": bool, "game": ...}. The CLI keeps this
        # adapter disabled until the public/private-use terms gate is satisfied.
        headers["ITAD-API-Key"] = api_key.reveal()
        return "api.isthereanydeal.com", "/games/lookup/v1?appid=220", headers
    if provider == "steamgriddb":
        headers["Authorization"] = f"Bearer {api_key.reveal()}"
        return "www.steamgriddb.com", "/api/v2/games/steam/220", headers
    if provider == "gg-deals":
        # GG.deals documents only query authentication. The constructed path is
        # confined to this transport boundary and is never logged or returned.
        query = urlencode({"ids": "220", "key": api_key.reveal(), "region": "us"})
        return (
            "api.gg.deals",
            f"/v1/prices/by-steam-app-id/?{query}",
            headers,
        )
    raise ValueError("unsupported credential probe provider")


def _response_payload(response: HttpResponse) -> dict[str, object]:
    if response.status in (401, 403):
        raise ProviderAuthError("AUTHENTICATION_FAILED", retryable=False)
    if response.status == 429:
        raise ProviderAuthError("PROVIDER_RATE_LIMITED", retryable=True)
    if response.status >= 500:
        raise ProviderAuthError("PROVIDER_UNAVAILABLE", retryable=True)
    if response.status != 200:
        raise ProviderAuthError("PROVIDER_RESPONSE_INVALID", retryable=False)
    decoded, payload = _decode_json(response.body)
    if not decoded or not isinstance(payload, dict):
        raise ProviderAuthError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return payload


def _decode_json(body: bytes) -> tuple[bool, object]:
    try:
        return True, json.loads(body)
    except (UnicodeError, ValueError, RecursionError):
        return False, None


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FixedHttpsTransport",
    "HttpResponse",
    "HttpTransport",
    "MAX_RESPONSE_BYTES",
    "ProviderAuthClient",
    "ProviderAuthError",
    "ProviderAuthProbe",
]

"""Bounded Valve aggregate-review evidence for exact Steam AppIDs.

Valve documents the store-hosted ``appreviews`` endpoint.  M4 retains only the
first-page aggregate summary and request context; review text, cursors, and
reviewer/account data never cross this adapter boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from typing import Mapping, Protocol
from urllib.parse import urlencode


STEAM_STORE_HOST = "store.steampowered.com"
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0


class SteamReviewError(RuntimeError):
    """Sanitized provider failure safe to expose through the CLI."""

    def __init__(
        self, code: str, *, retryable: bool, retry_after_seconds: int | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


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
    """One-shot HTTPS transport with no redirect support."""

    def request(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        connection = http.client.HTTPSConnection(host, timeout=timeout)
        response: http.client.HTTPResponse | None = None
        body = b""
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise SteamReviewError("PROVIDER_UNAVAILABLE", retryable=True) from None
        finally:
            connection.close()
        if response is None:
            raise SteamReviewError("PROVIDER_UNAVAILABLE", retryable=True)
        if len(body) > MAX_RESPONSE_BYTES:
            raise SteamReviewError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return HttpResponse(
            response.status,
            body,
            {key.lower(): value for key, value in response.getheaders()},
        )


@dataclass(frozen=True, slots=True)
class SteamReviewSummary:
    appid: int
    review_score: int
    total_positive: int
    total_negative: int
    total_reviews: int
    language: str = "all"
    review_type: str = "all"
    purchase_type: str = "all"
    off_topic_activity_filtered: bool = True
    source_url: str = ""


class SteamReviewClient:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._transport = transport or FixedHttpsTransport()
        self._timeout = timeout

    def fetch_summary(self, appid: int) -> SteamReviewSummary:
        if (
            not isinstance(appid, int)
            or isinstance(appid, bool)
            or not 1 <= appid <= (1 << 32) - 1
        ):
            raise ValueError("appid must be a positive unsigned 32-bit integer")
        parameters = {
            "json": "1",
            "filter": "all",
            "language": "all",
            "day_range": "365",
            "review_type": "all",
            "purchase_type": "all",
            "num_per_page": "1",
            "filter_offtopic_activity": "1",
        }
        path = f"/appreviews/{appid}?{urlencode(parameters)}"
        response = self._transport.request(
            host=STEAM_STORE_HOST,
            path=path,
            headers={
                "Accept": "application/json",
                "User-Agent": "steam-agent/0.1",
            },
            timeout=self._timeout,
        )
        _raise_for_status(response)
        try:
            value = json.loads(response.body)
        except (UnicodeError, ValueError, RecursionError):
            raise SteamReviewError(
                "PROVIDER_RESPONSE_INVALID", retryable=False
            ) from None
        if not isinstance(value, dict) or value.get("success") != 1:
            raise SteamReviewError("PROVIDER_RESPONSE_INVALID", retryable=False)
        summary = value.get("query_summary")
        if not isinstance(summary, dict):
            raise SteamReviewError("PROVIDER_RESPONSE_INVALID", retryable=False)
        review_score = _bounded_int(summary.get("review_score"), maximum=10)
        positive = _bounded_int(summary.get("total_positive"))
        negative = _bounded_int(summary.get("total_negative"))
        total = _bounded_int(summary.get("total_reviews"))
        if positive + negative != total:
            raise SteamReviewError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return SteamReviewSummary(
            appid=appid,
            review_score=review_score,
            total_positive=positive,
            total_negative=negative,
            total_reviews=total,
            source_url=f"https://{STEAM_STORE_HOST}/appreviews/{appid}",
        )


def _raise_for_status(response: HttpResponse) -> None:
    if response.status == 429:
        raise SteamReviewError(
            "RATE_LIMITED",
            retryable=True,
            retry_after_seconds=_retry_after(response.headers),
        )
    if response.status >= 500:
        raise SteamReviewError("PROVIDER_UNAVAILABLE", retryable=True)
    if response.status != 200:
        raise SteamReviewError("PROVIDER_RESPONSE_INVALID", retryable=False)


def _bounded_int(value: object, *, maximum: int = (1 << 63) - 1) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= maximum
    ):
        raise SteamReviewError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return value


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = headers.get("retry-after")
    if value is None or not value.isascii() or not value.isdecimal() or len(value) > 5:
        return None
    seconds = int(value)
    return seconds if 0 <= seconds <= 86_400 else None


__all__ = [
    "HttpResponse",
    "STEAM_STORE_HOST",
    "SteamReviewClient",
    "SteamReviewError",
    "SteamReviewSummary",
]

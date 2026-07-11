"""Bounded, non-retaining GG.deals Free price adapter.

GG.deals authenticates with a query parameter.  The secret-bearing request path
is therefore constructed only inside :class:`FixedGgDealsHttpsTransport`; the
client and its typed errors never receive or expose it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import http.client
import json
import re
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from steam_agent.credentials import SecretValue
from steam_agent.deal_evidence import (
    DealEvidenceSnapshot,
    HistoricalLowSummary,
    ManualReference,
    Money,
    OfferEvidence,
    ProductIdentity,
)


GG_DEALS_API_HOST = "api.gg.deals"
GG_DEALS_PRICE_PATH = "/v1/prices/by-steam-app-id/"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 1_000
MAX_APPIDS_PER_REQUEST = 50
DEFAULT_TIMEOUT_SECONDS = 10.0
_SAFE_PAGE_CATEGORIES = frozenset({"game", "dlc", "pack"})
_SAFE_PAGE_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,255}\Z")


class GgDealsError(RuntimeError):
    """Sanitized provider failure without URL, secret, or response content."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        retry_after_seconds: int | None = None,
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


class GgDealsTransport(Protocol):
    def request_app_prices(
        self,
        *,
        appids: tuple[int, ...],
        region: str,
        api_key: SecretValue,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...


class FixedGgDealsHttpsTransport:
    """Fixed-host HTTPS transport with no redirect support."""

    def request_app_prices(
        self,
        *,
        appids: tuple[int, ...],
        region: str,
        api_key: SecretValue,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        query = urlencode(
            {
                "ids": ",".join(str(appid) for appid in appids),
                "key": api_key.reveal(),
                "region": region,
            }
        )
        path = f"{GG_DEALS_PRICE_PATH}?{query}"
        connection = http.client.HTTPSConnection(GG_DEALS_API_HOST, timeout=timeout)
        response: http.client.HTTPResponse | None = None
        body = b""
        failed = False
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            response_headers = {key.lower(): value for key, value in response.getheaders()}
        except (OSError, TimeoutError, http.client.HTTPException):
            failed = True
            response_headers = {}
        finally:
            connection.close()
        if failed or response is None:
            raise GgDealsError("PROVIDER_UNAVAILABLE", retryable=True)
        if len(body) > MAX_RESPONSE_BYTES:
            raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return HttpResponse(response.status, body, response_headers)


Clock = Callable[[], datetime]
RequestGate = Callable[[], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class RateLimitMetadata:
    limit: int | None
    remaining: int | None
    reset_value: int | None


@dataclass(frozen=True, slots=True)
class GgDealsBatch:
    requested_appids: tuple[int, ...]
    snapshots: tuple[DealEvidenceSnapshot, ...]
    not_found_appids: tuple[int, ...]
    rate_limit: RateLimitMetadata


class GgDealsClient:
    def __init__(
        self,
        *,
        transport: GgDealsTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Clock = _utc_now,
        request_gate: RequestGate = lambda: None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._transport = transport or FixedGgDealsHttpsTransport()
        self._timeout = timeout
        self._clock = clock
        self._request_gate = request_gate

    def fetch_app_price_summary(
        self, *, appid: int, api_key: SecretValue
    ) -> DealEvidenceSnapshot:
        batch = self.fetch_app_price_summaries(appids=(appid,), api_key=api_key)
        if not batch.snapshots:
            raise GgDealsError("PRODUCT_NOT_FOUND", retryable=False)
        return batch.snapshots[0]

    def fetch_app_price_summaries(
        self, *, appids: Iterable[int], api_key: SecretValue
    ) -> GgDealsBatch:
        requested = _normalize_appids(appids)
        self._request_gate()
        response = self._transport.request_app_prices(
            appids=requested,
            region="us",
            api_key=api_key,
            headers={
                "Accept": "application/json",
                "User-Agent": "steam-agent/0.1 (+https://hsadler.com)",
            },
            timeout=self._timeout,
        )
        observed_at = _timestamp(self._clock())
        return _interpret_batch_response(
            response, requested=requested, observed_at=observed_at
        )


def _interpret_batch_response(
    response: HttpResponse, *, requested: tuple[int, ...], observed_at: str
) -> GgDealsBatch:
    _raise_for_status(response)
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)
    payload = _decode_bounded_json(response.body)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)
    data = payload.get("data")
    if not isinstance(data, dict) or len(data) > len(requested):
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)
    requested_keys = {str(appid) for appid in requested}
    if any(not isinstance(key, str) or key not in requested_keys for key in data):
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)
    snapshots: list[DealEvidenceSnapshot] = []
    not_found: list[int] = []
    for appid in requested:
        item = data.get(str(appid))
        if item is None:
            not_found.append(appid)
            continue
        product = ProductIdentity(
            provider_product_id=f"steam/app/{appid}", steam_appid=appid
        )
        snapshots.append(
            _interpret_item(item, product=product, observed_at=observed_at)
        )
    return GgDealsBatch(
        requested_appids=requested,
        snapshots=tuple(snapshots),
        not_found_appids=tuple(not_found),
        rate_limit=_rate_limit_metadata(response.headers),
    )


def _interpret_item(
    item: object, *, product: ProductIdentity, observed_at: str
) -> DealEvidenceSnapshot:
    if item is None:
        raise GgDealsError("PRODUCT_NOT_FOUND", retryable=False)
    if not isinstance(item, dict):
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)
    prices = item.get("prices")
    provider_url_value = item.get("url")
    if not isinstance(prices, dict) or not _safe_provider_url(provider_url_value):
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)
    provider_url = ManualReference(
        url=provider_url_value,
        purpose="GG.deals attributed product and offer details",
    )

    offers: list[OfferEvidence] = []
    history_lows: list[HistoricalLowSummary] = []
    for field, store_class in (
        ("currentRetail", "official"),
        ("currentKeyshops", "keyshop"),
    ):
        price = _optional_usd(prices.get(field))
        if price is not None:
            offers.append(
                OfferEvidence(
                    provider="gg-deals",
                    product=product,
                    price=price,
                    regular_price=None,
                    discount_percent=None,
                    store_class=store_class,
                    observed_at=observed_at,
                    provider_url=provider_url,
                    comparability="normalized_game",
                )
            )
    for field, store_class, scope in (
        ("historicalRetail", "official", "all_time_official_stores"),
        ("historicalKeyshops", "keyshop", "all_time_keyshops"),
    ):
        price = _optional_usd(prices.get(field))
        if price is not None:
            history_lows.append(
                HistoricalLowSummary(
                    provider="gg-deals",
                    product=product,
                    price=price,
                    observed_at=observed_at,
                    effective_at=None,
                    scope=scope,
                    provider_url=provider_url,
                    comparability="normalized_game",
                )
            )

    return DealEvidenceSnapshot(
        provider="gg-deals",
        product=product,
        offers=tuple(offers),
        history_lows=tuple(history_lows),
        observed_at=observed_at,
        limitations=(
            "historical_low_summary_not_price_event_history",
            "individual_store_and_drm_details_not_exposed",
            "country_region_and_activation_eligibility_may_differ",
            "provider_page_is_manual_only",
        ),
    )


def _normalize_appids(appids: Iterable[int]) -> tuple[int, ...]:
    normalized: set[int] = set()
    for appid in appids:
        # Reuse the invariant-enforcing value object without retaining it.
        ProductIdentity(provider_product_id="validation", steam_appid=appid)
        normalized.add(appid)
        if len(normalized) > MAX_APPIDS_PER_REQUEST:
            raise ValueError("at most 50 unique appids may be requested")
    if not normalized:
        raise ValueError("at least one appid is required")
    return tuple(sorted(normalized))


def _raise_for_status(response: HttpResponse) -> None:
    if response.status in (401, 403):
        raise GgDealsError("AUTHENTICATION_FAILED", retryable=False)
    if response.status == 429:
        raise GgDealsError(
            "PROVIDER_RATE_LIMITED",
            retryable=True,
            retry_after_seconds=_retry_after(response.headers),
        )
    if response.status >= 500:
        raise GgDealsError("PROVIDER_UNAVAILABLE", retryable=True)
    if response.status != 200:
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)


def _decode_bounded_json(body: bytes) -> object:
    try:
        payload = json.loads(body, parse_float=Decimal)
    except (UnicodeError, ValueError, RecursionError):
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False) from None
    nodes = 0
    stack: list[tuple[object, int]] = [(payload, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return payload


def _optional_usd(value: object) -> Money | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False)
    try:
        decimal = Decimal(str(value))
        minor = decimal * 100
        if (
            not decimal.is_finite()
            or minor != minor.to_integral_value()
            or minor < 0
            or minor > (1 << 63) - 1
        ):
            raise InvalidOperation
        amount_minor = int(minor)
    except (InvalidOperation, ValueError):
        raise GgDealsError("PROVIDER_RESPONSE_INVALID", retryable=False) from None
    return Money(amount_minor=amount_minor, currency="USD", country="US")


def _safe_provider_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    segments = parsed.path.strip("/").split("/")
    return (
        parsed.scheme == "https"
        and parsed.hostname == "gg.deals"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and not parsed.query
        and not parsed.fragment
        and len(segments) == 2
        and segments[0] in _SAFE_PAGE_CATEGORIES
        and _SAFE_PAGE_SLUG.fullmatch(segments[1]) is not None
    )


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None or not value.isdecimal():
        return None
    seconds = int(value)
    return seconds if seconds <= 86_400 else None


def _rate_limit_metadata(headers: Mapping[str, str]) -> RateLimitMetadata:
    lowered = {key.lower(): value for key, value in headers.items()}
    limit = _bounded_header_int(lowered.get("x-ratelimit-limit"))
    remaining = _bounded_header_int(lowered.get("x-ratelimit-remaining"))
    reset_value = _bounded_header_int(lowered.get("x-ratelimit-reset"))
    if limit is not None and remaining is not None and remaining > limit:
        remaining = None
    return RateLimitMetadata(
        limit=limit, remaining=remaining, reset_value=reset_value
    )


def _bounded_header_int(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed <= (1 << 63) - 1 else None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FixedGgDealsHttpsTransport",
    "GG_DEALS_API_HOST",
    "GG_DEALS_PRICE_PATH",
    "GgDealsBatch",
    "GgDealsClient",
    "GgDealsError",
    "GgDealsTransport",
    "HttpResponse",
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "MAX_APPIDS_PER_REQUEST",
    "MAX_RESPONSE_BYTES",
    "RateLimitMetadata",
]

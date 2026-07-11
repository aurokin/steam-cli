"""Bounded, on-demand CheapShark deal evidence adapter.

CheapShark exposes current USD offers and a cheapest-ever summary.  The
provider groups offers at game level, so the normalized evidence deliberately
does not claim exact edition, DRM, or regional comparability.  Raw responses
are interpreted in memory and discarded.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import http.client
import json
import re
from typing import Protocol
from urllib.parse import urlencode

from steam_agent.deal_evidence import (
    DealEvidenceSnapshot,
    HistoricalLowSummary,
    ManualReference,
    Money,
    OfferEvidence,
    ProductIdentity,
)


CHEAPSHARK_HOST = "www.cheapshark.com"
CHEAPSHARK_API_PATH = "/api/1.0/games"
USER_AGENT = "steam-agent/0.1 (+https://hsadler.com)"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 2_000
MAX_STRING_LENGTH = 8_192
DEFAULT_MAX_DEALS = 100
MAX_UNSIGNED_32 = (1 << 32) - 1
_DECIMAL_TEXT = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


class CheapSharkError(RuntimeError):
    """Sanitized provider failure with optional retry guidance."""

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
    headers: Mapping[str, str] = field(default_factory=dict)


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
    """One-shot fixed-host HTTPS transport with no redirect handling."""

    def request(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        if host != CHEAPSHARK_HOST:
            raise CheapSharkError("PROVIDER_REQUEST_INVALID", retryable=False)
        connection = http.client.HTTPSConnection(CHEAPSHARK_HOST, timeout=timeout)
        response: http.client.HTTPResponse | None = None
        body = b""
        failed = False
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            failed = True
        finally:
            connection.close()
        if failed or response is None:
            raise CheapSharkError("PROVIDER_UNAVAILABLE", retryable=True)
        if len(body) > MAX_RESPONSE_BYTES:
            raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return HttpResponse(
            status=response.status,
            body=body,
            headers={name: value for name, value in response.getheaders()},
        )


Clock = Callable[[], datetime]
RequestGate = Callable[[], None]
RetryObserver = Callable[[int], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CheapSharkClient:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_deals: int = DEFAULT_MAX_DEALS,
        clock: Clock = _utc_now,
        request_gate: RequestGate = lambda: None,
        retry_observer: RetryObserver = lambda _seconds: None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if (
            not isinstance(max_deals, int)
            or isinstance(max_deals, bool)
            or not 1 <= max_deals <= DEFAULT_MAX_DEALS
        ):
            raise ValueError("max_deals must be between 1 and 100")
        self._transport = transport or FixedHttpsTransport()
        self._timeout = timeout
        self._max_deals = max_deals
        self._clock = clock
        self._request_gate = request_gate
        self._retry_observer = retry_observer

    def lookup_steam_app(self, appid: int) -> DealEvidenceSnapshot:
        """Retrieve current offers and cheapest-ever summary for one AppID."""

        _validate_appid(appid)
        matches = self._get_json(
            urlencode({"steamAppID": appid, "limit": 1, "exact": 1})
        )
        game_id = _select_game_id(matches, appid=appid)
        payload = self._get_json(urlencode({"id": game_id}))
        observed_at = _timestamp(self._clock())
        return _normalize_game(
            payload,
            appid=appid,
            game_id=game_id,
            observed_at=observed_at,
            max_deals=self._max_deals,
        )

    def _get_json(self, query: str) -> object:
        self._request_gate()
        response = self._transport.request(
            host=CHEAPSHARK_HOST,
            path=f"{CHEAPSHARK_API_PATH}?{query}",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=self._timeout,
        )
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
        try:
            _check_status(response)
        except CheapSharkError as exc:
            if exc.retry_after_seconds is not None:
                self._retry_observer(exc.retry_after_seconds)
            raise
        try:
            payload = json.loads(response.body)
        except (UnicodeError, ValueError, RecursionError):
            raise CheapSharkError(
                "PROVIDER_RESPONSE_INVALID", retryable=False
            ) from None
        _validate_json_bounds(payload)
        return payload


def _check_status(response: HttpResponse) -> None:
    if response.status == 429:
        raise CheapSharkError(
            "PROVIDER_RATE_LIMITED",
            retryable=True,
            retry_after_seconds=_retry_after(response.headers),
        )
    if response.status >= 500:
        raise CheapSharkError("PROVIDER_UNAVAILABLE", retryable=True)
    if response.status in (401, 403):
        raise CheapSharkError("PROVIDER_ACCESS_DENIED", retryable=False)
    if response.status == 404:
        raise CheapSharkError("GAME_NOT_FOUND", retryable=False)
    if response.status != 200:
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)


def _retry_after(headers: Mapping[str, str]) -> int | None:
    raw = next(
        (value for name, value in headers.items() if name.lower() == "retry-after"),
        None,
    )
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    return _bounded_digit_string(raw, maximum=86_400)


def _select_game_id(payload: object, *, appid: int) -> str:
    if not isinstance(payload, list):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    matches: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
        candidate_appid = _positive_int_string(item.get("steamAppID"))
        if candidate_appid != appid:
            continue
        game_id = _identifier(item.get("gameID"))
        matches.add(game_id)
    if not matches:
        raise CheapSharkError("GAME_NOT_FOUND", retryable=False)
    if len(matches) != 1:
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return next(iter(matches))


def _normalize_game(
    payload: object,
    *,
    appid: int,
    game_id: str,
    observed_at: str,
    max_deals: int,
) -> DealEvidenceSnapshot:
    if not isinstance(payload, dict):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    info = payload.get("info")
    deals = payload.get("deals")
    if not isinstance(info, dict) or not isinstance(deals, list):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    if _positive_int_string(info.get("steamAppID")) != appid:
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    if len(deals) > max_deals:
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)

    product = ProductIdentity(provider_product_id=game_id, steam_appid=appid)
    offers = tuple(
        _normalize_offer(item, product=product, observed_at=observed_at)
        for item in deals
    )
    history_lows: tuple[HistoricalLowSummary, ...] = ()
    cheapest = payload.get("cheapestPriceEver")
    if cheapest is not None:
        history_lows = (
            _normalize_history_low(
                cheapest,
                product=product,
                observed_at=observed_at,
            ),
        )
    return DealEvidenceSnapshot(
        provider="cheapshark",
        product=product,
        offers=offers,
        history_lows=history_lows,
        observed_at=observed_at,
        limitations=(
            "USD and US context only",
            "provider groups offers at game level; edition and DRM are not exposed",
            "cheapest-ever is a summary, not a full historical series",
            "deal redirects are manual-only and must not be fetched automatically",
        ),
    )


def _normalize_offer(
    payload: object, *, product: ProductIdentity, observed_at: str
) -> OfferEvidence:
    if not isinstance(payload, dict):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    deal_id = _identifier(payload.get("dealID"))
    store_id = _identifier(payload.get("storeID"))
    price = _money(payload.get("price"))
    regular = _money(payload.get("retailPrice"))
    discount = _discount_percent(payload.get("savings"))
    reference = ManualReference(
        url=f"https://www.cheapshark.com/redirect?{urlencode({'dealID': deal_id})}",
        purpose="open CheapShark deal for a human",
    )
    return OfferEvidence(
        provider="cheapshark",
        product=product,
        price=price,
        regular_price=regular,
        discount_percent=discount,
        store_class="unknown",
        observed_at=observed_at,
        provider_url=reference,
        comparability="normalized_game",
        seller_id=store_id,
    )


def _normalize_history_low(
    payload: object,
    *,
    product: ProductIdentity,
    observed_at: str,
) -> HistoricalLowSummary:
    if not isinstance(payload, dict):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    effective_at = _unix_timestamp(payload.get("date"))
    reference = ManualReference(
        url=(
            "https://www.cheapshark.com/search?"
            + urlencode({"steamAppID": product.steam_appid})
        ),
        purpose="open the CheapShark game search for historical context",
    )
    return HistoricalLowSummary(
        provider="cheapshark",
        product=product,
        price=_money(payload.get("price")),
        observed_at=observed_at,
        effective_at=effective_at,
        scope="all_time_any_store",
        provider_url=reference,
        comparability="normalized_game",
    )


def _money(value: object) -> Money:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32
        or _DECIMAL_TEXT.fullmatch(value) is None
    ):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount.as_tuple().exponent < -2:
            raise InvalidOperation
        minor = amount * 100
        if minor != minor.to_integral_value() or minor > 1_000_000_000:
            raise InvalidOperation
        amount_minor = int(minor)
    except (InvalidOperation, ValueError, OverflowError):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return Money(amount_minor=amount_minor, currency="USD", country="US")


def _discount_percent(value: object) -> int | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32
        or _DECIMAL_TEXT.fullmatch(value) is None
    ):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    try:
        percent = Decimal(value)
        if not percent.is_finite() or not 0 <= percent <= 100:
            raise InvalidOperation
        return int(percent.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False) from None


def _unix_timestamp(value: object) -> str | None:
    if value is None:
        return None
    parsed = _positive_int_string(value, allow_zero=True)
    try:
        return _timestamp(datetime.fromtimestamp(parsed, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False) from None


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return value


def _positive_int_string(value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        result = value
    elif isinstance(value, str):
        parsed = _bounded_digit_string(value, maximum=(1 << 63) - 1)
        if parsed is None:
            raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
        result = parsed
    else:
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    minimum = 0 if allow_zero else 1
    if not minimum <= result <= (1 << 63) - 1:
        raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return result


def _bounded_digit_string(value: str, *, maximum: int) -> int | None:
    """Parse ASCII digits only after their magnitude is proven bounded."""

    if not value or not value.isascii() or not value.isdigit():
        return None
    normalized = value.lstrip("0") or "0"
    upper = str(maximum)
    if len(normalized) > len(upper) or (
        len(normalized) == len(upper) and normalized > upper
    ):
        return None
    return int(normalized)


def _validate_appid(appid: int) -> None:
    if (
        not isinstance(appid, int)
        or isinstance(appid, bool)
        or not 1 <= appid <= MAX_UNSIGNED_32
    ):
        raise ValueError("appid must be an unsigned 32-bit integer")


def _validate_json_bounds(payload: object) -> None:
    remaining = MAX_JSON_ITEMS
    stack: list[tuple[object, int]] = [(payload, 1)]
    while stack:
        value, depth = stack.pop()
        remaining -= 1
        if remaining < 0 or depth > MAX_JSON_DEPTH:
            raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
        if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
            raise CheapSharkError("PROVIDER_RESPONSE_INVALID", retryable=False)
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CHEAPSHARK_API_PATH",
    "CHEAPSHARK_HOST",
    "DEFAULT_MAX_DEALS",
    "DEFAULT_TIMEOUT_SECONDS",
    "FixedHttpsTransport",
    "HttpResponse",
    "HttpTransport",
    "MAX_RESPONSE_BYTES",
    "CheapSharkClient",
    "CheapSharkError",
    "USER_AGENT",
]

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

import pytest

from steam_agent.cheapshark import (
    CHEAPSHARK_HOST,
    USER_AGENT,
    CheapSharkClient,
    CheapSharkError,
    HttpResponse,
    MAX_RESPONSE_BYTES,
)


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Call:
    host: str
    path: str
    headers: dict[str, str]
    timeout: float


class SequenceTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.calls: list[Call] = []

    def request(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        self.calls.append(Call(host, path, dict(headers), timeout))
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def response(payload: object, *, status: int = 200, headers=None) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode(), headers or {})


def lookup_payload(*, deals: list[object] | None = None) -> dict[str, object]:
    return {
        "info": {
            "title": "Synthetic game",
            "steamAppID": "220",
            "thumb": "discarded",
            "future": {"additive": True},
        },
        "cheapestPriceEver": {
            "price": "1.25",
            "date": 1_700_000_000,
            "additive": True,
        },
        "deals": deals
        if deals is not None
        else [
            {
                "storeID": "1",
                "dealID": "encoded+/= deal",
                "price": "2.50",
                "retailPrice": "9.99",
                "savings": "74.9749",
                "additive": "ignored",
            }
        ],
        "additive": ["ignored"],
    }


def test_lookup_is_fixed_host_bounded_on_demand_and_normalized() -> None:
    transport = SequenceTransport(
        response(
            [
                {
                    "gameID": "42",
                    "steamAppID": "220",
                    "cheapest": "2.50",
                    "additive": True,
                }
            ]
        ),
        response(lookup_payload()),
    )
    gate_calls = 0

    def gate() -> None:
        nonlocal gate_calls
        gate_calls += 1

    result = CheapSharkClient(
        transport=transport, clock=lambda: NOW, request_gate=gate
    ).lookup_steam_app(220)

    assert gate_calls == 2
    assert len(transport.calls) == 2
    assert all(call.host == CHEAPSHARK_HOST for call in transport.calls)
    assert all(call.headers["User-Agent"] == USER_AGENT for call in transport.calls)
    assert all(call.headers["Accept"] == "application/json" for call in transport.calls)
    first_query = parse_qs(urlsplit(transport.calls[0].path).query)
    assert first_query == {"steamAppID": ["220"], "limit": ["1"], "exact": ["1"]}
    assert parse_qs(urlsplit(transport.calls[1].path).query) == {"id": ["42"]}

    assert result.provider == "cheapshark"
    assert result.product.provider_product_id == "42"
    assert result.product.steam_appid == 220
    assert result.observed_at == "2026-07-11T12:00:00Z"
    assert len(result.offers) == 1
    offer = result.offers[0]
    assert offer.price.amount_minor == 250
    assert offer.price.currency == "USD"
    assert offer.price.country == "US"
    assert offer.regular_price is not None
    assert offer.regular_price.amount_minor == 999
    assert offer.discount_percent == 75
    assert offer.store_class == "unknown"
    assert offer.comparability == "normalized_game"
    assert offer.provider_url.access_mode == "manual_only"
    assert offer.provider_url.automation_supported is False
    assert offer.provider_url.url == (
        "https://www.cheapshark.com/redirect?dealID=encoded%2B%2F%3D+deal"
    )
    assert len(result.history_lows) == 1
    low = result.history_lows[0]
    assert low.provider_url.url == (
        "https://www.cheapshark.com/search?steamAppID=220"
    )
    assert low.provider_url.url != offer.provider_url.url
    assert low.provider_url.access_mode == "manual_only"
    assert low.provider_url.automation_supported is False
    assert low.price.amount_minor == 125
    assert low.scope == "all_time_any_store"
    assert low.effective_at == "2023-11-14T22:13:20Z"
    assert "full historical series" in " ".join(result.limitations)


def test_no_matching_app_is_typed_and_stops_before_detail_lookup() -> None:
    transport = SequenceTransport(response([]))

    with pytest.raises(CheapSharkError) as caught:
        CheapSharkClient(transport=transport).lookup_steam_app(220)

    assert caught.value.code == "GAME_NOT_FOUND"
    assert caught.value.retryable is False
    assert len(transport.calls) == 1


@pytest.mark.parametrize("appid", [True, 0, -1, 1 << 32, "220"])
def test_invalid_appid_is_rejected_without_network(appid: object) -> None:
    transport = SequenceTransport()
    with pytest.raises(ValueError):
        CheapSharkClient(transport=transport).lookup_steam_app(appid)  # type: ignore[arg-type]
    assert transport.calls == []


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (403, "PROVIDER_ACCESS_DENIED", False),
        (404, "GAME_NOT_FOUND", False),
        (500, "PROVIDER_UNAVAILABLE", True),
        (302, "PROVIDER_RESPONSE_INVALID", False),
    ],
)
def test_http_failures_are_sanitized_and_typed(
    status: int, code: str, retryable: bool
) -> None:
    transport = SequenceTransport(HttpResponse(status, b"provider-body-canary"))
    with pytest.raises(CheapSharkError) as caught:
        CheapSharkClient(transport=transport).lookup_steam_app(220)
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "provider-body-canary" not in str(caught.value)


def test_rate_limit_preserves_only_bounded_retry_after() -> None:
    transport = SequenceTransport(
        HttpResponse(429, b"secret", {"rEtRy-AfTeR": "37", "Other": "discard"})
    )
    with pytest.raises(CheapSharkError) as caught:
        CheapSharkClient(transport=transport).lookup_steam_app(220)
    assert caught.value.code == "PROVIDER_RATE_LIMITED"
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 37


@pytest.mark.parametrize("retry_after", ["date", "-1", "86401"])
def test_invalid_retry_after_is_discarded(retry_after: str) -> None:
    transport = SequenceTransport(
        HttpResponse(429, b"secret", {"Retry-After": retry_after})
    )
    with pytest.raises(CheapSharkError) as caught:
        CheapSharkClient(transport=transport).lookup_steam_app(220)
    assert caught.value.retry_after_seconds is None


@pytest.mark.parametrize("price", ["1.234", "-1.00", "NaN", 1.25])
def test_invalid_price_shapes_are_rejected(price: object) -> None:
    payload = lookup_payload()
    deals = payload["deals"]
    assert isinstance(deals, list)
    assert isinstance(deals[0], dict)
    deals[0]["price"] = price
    transport = SequenceTransport(
        response([{"gameID": "42", "steamAppID": "220"}]), response(payload)
    )
    with pytest.raises(CheapSharkError, match="PROVIDER_RESPONSE_INVALID"):
        CheapSharkClient(transport=transport).lookup_steam_app(220)


def test_deal_count_is_bounded() -> None:
    deal = {
        "storeID": "1",
        "dealID": "deal",
        "price": "1.00",
        "retailPrice": "2.00",
        "savings": "50",
    }
    transport = SequenceTransport(
        response([{"gameID": "42", "steamAppID": "220"}]),
        response(lookup_payload(deals=[deal, deal])),
    )
    with pytest.raises(CheapSharkError, match="PROVIDER_RESPONSE_INVALID"):
        CheapSharkClient(transport=transport, max_deals=1).lookup_steam_app(220)


def test_deep_or_oversized_json_is_rejected_before_normalization() -> None:
    nested: object = "leaf"
    for _ in range(9):
        nested = {"next": nested}
    transport = SequenceTransport(response(nested))
    with pytest.raises(CheapSharkError, match="PROVIDER_RESPONSE_INVALID"):
        CheapSharkClient(transport=transport).lookup_steam_app(220)


def test_transport_contract_cannot_bypass_body_bound() -> None:
    transport = SequenceTransport(HttpResponse(200, b"x" * (MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(CheapSharkError, match="PROVIDER_RESPONSE_INVALID"):
        CheapSharkClient(transport=transport).lookup_steam_app(220)

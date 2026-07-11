from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Mapping

import pytest

from steam_agent import gg_deals
from steam_agent.credentials import SecretValue
from steam_agent.gg_deals import (
    FixedGgDealsHttpsTransport,
    GgDealsClient,
    GgDealsError,
    HttpResponse,
    RateLimitMetadata,
)


@dataclass
class RecordingTransport:
    response: HttpResponse
    call: dict[str, object] | None = None

    def request_app_prices(
        self,
        *,
        appids: tuple[int, ...],
        region: str,
        api_key: SecretValue,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        self.call = {
            "appids": appids,
            "region": region,
            "api_key": api_key,
            "headers": dict(headers),
            "timeout": timeout,
        }
        return self.response


def _response(payload: object, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode(), {})


def test_appid_price_summary_normalizes_money_identity_and_attribution() -> None:
    payload = {
        "success": True,
        "data": {
            "220": {
                "url": "https://gg.deals/game/synthetic-game/",
                "prices": {
                    "currentRetail": "12.34",
                    "currentKeyshops": 10,
                    "historicalRetail": "4.50",
                    "historicalKeyshops": None,
                    "futureAdditiveField": {"ignored": True},
                },
                "additive": "ignored",
            }
        },
        "additive": ["ignored"],
    }
    transport = RecordingTransport(_response(payload))
    client = GgDealsClient(
        transport=transport,
        clock=lambda: datetime(2026, 7, 11, 12, 30, tzinfo=timezone.utc),
    )

    result = client.fetch_app_price_summary(
        appid=220, api_key=SecretValue("gg-secret-canary")
    )

    assert result.product.steam_appid == 220
    assert result.product.mapping == "exact"
    assert result.product.provider_product_id == "steam/app/220"
    assert [offer.price.amount_minor for offer in result.offers] == [1234, 1000]
    assert [offer.store_class for offer in result.offers] == ["official", "keyshop"]
    assert all(offer.price.currency == "USD" for offer in result.offers)
    assert all(offer.price.country == "US" for offer in result.offers)
    assert all(offer.comparability == "normalized_game" for offer in result.offers)
    assert len(result.history_lows) == 1
    assert result.history_lows[0].price.amount_minor == 450
    reference = result.offers[0].provider_url
    assert reference.url == "https://gg.deals/game/synthetic-game/"
    assert reference.access_mode == "manual_only"
    assert reference.automation_supported is False
    assert result.observed_at == "2026-07-11T12:30:00Z"
    assert transport.call is not None
    assert transport.call["appids"] == (220,)
    assert transport.call["region"] == "us"
    assert "key" not in str(transport.call["headers"])
    assert "gg-secret-canary" not in str(result)


def test_query_key_never_appears_in_typed_failure() -> None:
    transport = RecordingTransport(HttpResponse(403, b"body-secret-canary", {}))

    with pytest.raises(GgDealsError) as caught:
        GgDealsClient(transport=transport).fetch_app_price_summary(
            appid=220, api_key=SecretValue("gg-secret-canary")
        )

    assert caught.value.code == "AUTHENTICATION_FAILED"
    assert caught.value.retryable is False
    assert "gg-secret-canary" not in str(caught.value)
    assert "body-secret-canary" not in str(caught.value)


def test_rate_limit_preserves_bounded_retry_after() -> None:
    response = HttpResponse(429, b"not retained", {"retry-after": "17"})

    with pytest.raises(GgDealsError) as caught:
        GgDealsClient(transport=RecordingTransport(response)).fetch_app_price_summary(
            appid=220, api_key=SecretValue("secret")
        )

    assert caught.value.code == "PROVIDER_RATE_LIMITED"
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 17


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "PROVIDER_RESPONSE_INVALID", False),
        (401, "AUTHENTICATION_FAILED", False),
        (500, "PROVIDER_UNAVAILABLE", True),
        (302, "PROVIDER_RESPONSE_INVALID", False),
    ],
)
def test_statuses_map_to_sanitized_failures(
    status: int, code: str, retryable: bool
) -> None:
    with pytest.raises(GgDealsError) as caught:
        GgDealsClient(
            transport=RecordingTransport(HttpResponse(status, b"canary", {}))
        ).fetch_app_price_summary(appid=220, api_key=SecretValue("secret"))

    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "canary" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "data": {}},
        {"success": True, "data": []},
        {"success": True, "data": {"220": None}},
        {
            "success": True,
            "data": {
                "220": {
                    "url": "http://gg.deals/game/not-https/",
                    "prices": {},
                }
            },
        },
        {
            "success": True,
            "data": {
                "220": {
                    "url": "https://evil.example/game/wrong-host/",
                    "prices": {},
                }
            },
        },
        {
            "success": True,
            "data": {
                "220": {
                    "url": "https://gg.deals/game/synthetic/",
                    "prices": {"currentRetail": "12.345"},
                }
            },
        },
        {
            "success": True,
            "data": {
                "220": {
                    "url": "https://gg.deals/game/synthetic/",
                    "prices": {},
                },
                "221": {
                    "url": "https://gg.deals/game/unrequested/",
                    "prices": {},
                },
            },
        },
    ],
)
def test_invalid_or_unbounded_shapes_are_rejected(payload: object) -> None:
    with pytest.raises(GgDealsError) as caught:
        GgDealsClient(transport=RecordingTransport(_response(payload))).fetch_app_price_summary(
            appid=220, api_key=SecretValue("secret")
        )

    assert caught.value.code in {"PROVIDER_RESPONSE_INVALID", "PRODUCT_NOT_FOUND"}


@pytest.mark.parametrize(
    "url",
    [
        "https://gg.deals/game/synthetic/?key=secret-canary",
        "https://gg.deals/game/synthetic/#fragment-canary",
        "https://user@gg.deals/game/synthetic/",
        "https://gg.deals:444/game/synthetic/",
        "https://gg.deals/api/opaque-route/",
        "https://gg.deals/game/not_safe/",
    ],
)
def test_unsafe_provider_url_is_rejected_without_echo(url: str) -> None:
    payload = {
        "success": True,
        "data": {"220": {"url": url, "prices": {}}},
    }

    with pytest.raises(GgDealsError) as caught:
        GgDealsClient(
            transport=RecordingTransport(_response(payload))
        ).fetch_app_price_summary(appid=220, api_key=SecretValue("secret"))

    assert caught.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "canary" not in str(caught.value)


@pytest.mark.parametrize("category", ["game", "dlc", "pack"])
def test_known_clean_page_categories_are_accepted(category: str) -> None:
    url = f"https://gg.deals/{category}/synthetic-page/"
    payload = {
        "success": True,
        "data": {
            "220": {
                "url": url,
                "prices": {"currentRetail": "1.00"},
            }
        },
    }

    result = GgDealsClient(
        transport=RecordingTransport(_response(payload))
    ).fetch_app_price_summary(appid=220, api_key=SecretValue("secret"))

    assert result.offers[0].provider_url.url == url


def test_nested_additive_payload_is_depth_bounded() -> None:
    nested: object = "leaf"
    for _ in range(20):
        nested = {"child": nested}
    payload = {
        "success": True,
        "data": {
            "220": {
                "url": "https://gg.deals/game/synthetic/",
                "prices": {},
                "additive": nested,
            }
        },
    }

    with pytest.raises(GgDealsError, match="PROVIDER_RESPONSE_INVALID"):
        GgDealsClient(transport=RecordingTransport(_response(payload))).fetch_app_price_summary(
            appid=220, api_key=SecretValue("secret")
        )


def test_invalid_appid_is_rejected_before_transport() -> None:
    transport = RecordingTransport(_response({}))

    with pytest.raises(ValueError, match="steam_appid"):
        GgDealsClient(transport=transport).fetch_app_price_summary(
            appid=0, api_key=SecretValue("secret")
        )

    assert transport.call is None


def test_fixed_transport_confines_query_key_to_fixed_https_request(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def read(self, limit: int) -> bytes:
            captured["read_limit"] = limit
            return b"{}"

        def getheaders(self) -> list[tuple[str, str]]:
            return [("Retry-After", "3")]

    class FakeConnection:
        def __init__(self, host: str, *, timeout: float) -> None:
            captured["host"] = host
            captured["timeout"] = timeout

        def request(
            self, method: str, path: str, *, headers: dict[str, str]
        ) -> None:
            captured.update(method=method, path=path, headers=headers)

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(gg_deals.http.client, "HTTPSConnection", FakeConnection)

    response = FixedGgDealsHttpsTransport().request_app_prices(
        appids=(220,),
        region="us",
        api_key=SecretValue("synthetic-query-secret"),
        headers={"Accept": "application/json"},
        timeout=4.0,
    )

    assert captured["host"] == "api.gg.deals"
    assert captured["method"] == "GET"
    assert str(captured["path"]).startswith(
        "/v1/prices/by-steam-app-id/?ids=220&key="
    )
    assert "synthetic-query-secret" in str(captured["path"])
    assert "synthetic-query-secret" not in str(captured["headers"])
    assert captured["read_limit"] == gg_deals.MAX_RESPONSE_BYTES + 1
    assert captured["closed"] is True
    assert response.headers == {"retry-after": "3"}


def test_fixed_transport_rejects_oversized_body(monkeypatch) -> None:
    class FakeResponse:
        status = 200

        def read(self, limit: int) -> bytes:
            return b"x" * limit

        def getheaders(self) -> list[tuple[str, str]]:
            return []

    class FakeConnection:
        def __init__(self, host: str, *, timeout: float) -> None:
            pass

        def request(
            self, method: str, path: str, *, headers: dict[str, str]
        ) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(gg_deals.http.client, "HTTPSConnection", FakeConnection)

    with pytest.raises(GgDealsError, match="PROVIDER_RESPONSE_INVALID"):
        FixedGgDealsHttpsTransport().request_app_prices(
            appids=(220,),
            region="us",
            api_key=SecretValue("secret"),
            headers={},
            timeout=4.0,
        )


def test_batch_is_sorted_deduplicated_and_returns_exact_subset_with_rate_metadata() -> None:
    payload = {
        "success": True,
        "data": {
            "440": None,
            "730": {
                "url": "https://gg.deals/game/synthetic-730/",
                "prices": {"currentRetail": "1.00"},
                "title": "ignored and not retained",
            },
            "1091500": {
                "url": "https://gg.deals/game/synthetic-1091500/",
                "prices": {"historicalRetail": "2.00"},
            },
        },
    }
    transport = RecordingTransport(
        HttpResponse(
            200,
            json.dumps(payload).encode(),
            {
                "X-RateLimit-Limit": "100",
                "x-ratelimit-remaining": "97",
                "x-ratelimit-reset": "1783780000",
            },
        )
    )
    gates = 0

    def gate() -> None:
        nonlocal gates
        gates += 1

    result = GgDealsClient(
        transport=transport, request_gate=gate
    ).fetch_app_price_summaries(
        appids=(1091500, 730, 440, 730), api_key=SecretValue("secret")
    )

    assert gates == 1
    assert transport.call is not None
    assert transport.call["appids"] == (440, 730, 1091500)
    assert result.requested_appids == (440, 730, 1091500)
    assert [item.product.steam_appid for item in result.snapshots] == [730, 1091500]
    assert result.not_found_appids == (440,)
    assert result.rate_limit.limit == 100
    assert result.rate_limit.remaining == 97
    assert result.rate_limit.reset_value == 1783780000
    assert "ignored and not retained" not in str(result)


def test_oversized_numeric_strings_are_rejected_or_discarded_before_conversion() -> None:
    payload = {
        "success": True,
        "data": {
            "220": {
                "url": "https://gg.deals/game/synthetic-220/",
                "prices": {"currentRetail": "9" * 8_192},
            }
        },
    }
    transport = RecordingTransport(
        HttpResponse(
            200,
            json.dumps(payload).encode(),
            {"X-RateLimit-Limit": "9" * 8_192},
        )
    )

    with pytest.raises(GgDealsError, match="PROVIDER_RESPONSE_INVALID"):
        GgDealsClient(transport=transport).fetch_app_price_summaries(
            appids=(220,), api_key=SecretValue("secret")
        )


def test_oversized_rate_headers_are_discarded_without_integer_conversion() -> None:
    payload = {
        "success": True,
        "data": {
            "220": {
                "url": "https://gg.deals/game/synthetic-220/",
                "prices": {"currentRetail": "1.00"},
            }
        },
    }
    transport = RecordingTransport(
        HttpResponse(
            200,
            json.dumps(payload).encode(),
            {
                "X-RateLimit-Limit": "9" * 8_192,
                "X-RateLimit-Remaining": "9" * 8_192,
                "X-RateLimit-Reset": "9" * 8_192,
            },
        )
    )

    result = GgDealsClient(transport=transport).fetch_app_price_summaries(
        appids=(220,), api_key=SecretValue("secret")
    )

    assert result.rate_limit == RateLimitMetadata(None, None, None)


def test_batch_rejects_too_many_unique_appids_before_transport() -> None:
    transport = RecordingTransport(_response({}))

    with pytest.raises(ValueError, match="at most 50"):
        GgDealsClient(transport=transport).fetch_app_price_summaries(
            appids=range(1, 52), api_key=SecretValue("secret")
        )

    assert transport.call is None


def test_batch_rejects_unrequested_result_mapping() -> None:
    payload = {
        "success": True,
        "data": {
            "221": {
                "url": "https://gg.deals/game/wrong/",
                "prices": {},
            }
        },
    }

    with pytest.raises(GgDealsError, match="PROVIDER_RESPONSE_INVALID"):
        GgDealsClient(transport=RecordingTransport(_response(payload))).fetch_app_price_summaries(
            appids=(220,), api_key=SecretValue("secret")
        )

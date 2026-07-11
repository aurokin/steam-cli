from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping

import pytest

from steam_agent.credentials import SecretValue
from steam_agent.provider_auth import (
    HttpResponse,
    ProviderAuthClient,
    ProviderAuthError,
)


@dataclass
class RecordingTransport:
    response: HttpResponse
    call: dict[str, object] | None = None

    def request(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        self.call = {
            "host": host,
            "path": path,
            "headers": dict(headers),
            "timeout": timeout,
        }
        return self.response


@pytest.mark.parametrize(
    ("provider", "payload", "host", "header"),
    [
        (
            "isthereanydeal",
            {"found": True, "game": {"id": "synthetic"}},
            "api.isthereanydeal.com",
            "ITAD-API-Key",
        ),
        (
            "steamgriddb",
            {"success": True, "data": {"id": 1}},
            "www.steamgriddb.com",
            "Authorization",
        ),
    ],
)
def test_header_authenticated_provider_probe_is_fixed_and_non_retaining(
    provider: str,
    payload: object,
    host: str,
    header: str,
) -> None:
    sentinel = "provider-secret-canary"
    transport = RecordingTransport(HttpResponse(200, json.dumps(payload).encode()))
    client = ProviderAuthClient(transport=transport)

    result = client.probe(provider=provider, api_key=SecretValue(sentinel))

    assert result.state == "ready"
    assert transport.call is not None
    assert transport.call["host"] == host
    assert sentinel not in str(transport.call["path"])
    assert sentinel in str(transport.call["headers"][header])


def test_gg_deals_query_authenticated_probe_never_exposes_path_in_error() -> None:
    sentinel = "gg-secret-canary"
    transport = RecordingTransport(HttpResponse(403, b"provider body canary"))
    client = ProviderAuthClient(transport=transport)

    with pytest.raises(ProviderAuthError) as caught:
        client.probe(provider="gg-deals", api_key=SecretValue(sentinel))

    assert caught.value.code == "AUTHENTICATION_FAILED"
    assert sentinel not in str(caught.value)
    assert "provider body canary" not in str(caught.value)
    assert transport.call is not None
    assert transport.call["host"] == "api.gg.deals"
    assert sentinel in str(transport.call["path"])
    assert sentinel not in str(transport.call["headers"])


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "AUTHENTICATION_FAILED", False),
        (403, "AUTHENTICATION_FAILED", False),
        (429, "PROVIDER_RATE_LIMITED", True),
        (500, "PROVIDER_UNAVAILABLE", True),
        (302, "PROVIDER_RESPONSE_INVALID", False),
    ],
)
def test_provider_probe_maps_status_without_retaining_body(
    status: int, code: str, retryable: bool
) -> None:
    transport = RecordingTransport(HttpResponse(status, b"body-secret-canary"))
    client = ProviderAuthClient(transport=transport)

    with pytest.raises(ProviderAuthError) as caught:
        client.probe(
            provider="steamgriddb", api_key=SecretValue("key-secret-canary")
        )

    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "body-secret-canary" not in str(caught.value)


@pytest.mark.parametrize(
    ("provider", "payload"),
    [
        ("isthereanydeal", {"found": "yes"}),
        ("steamgriddb", {"success": False}),
        ("gg-deals", {"success": False}),
        ("steamgriddb", []),
    ],
)
def test_provider_probe_rejects_unsupported_success_shape(
    provider: str, payload: object
) -> None:
    transport = RecordingTransport(HttpResponse(200, json.dumps(payload).encode()))

    with pytest.raises(ProviderAuthError, match="PROVIDER_RESPONSE_INVALID"):
        ProviderAuthClient(transport=transport).probe(
            provider=provider, api_key=SecretValue("key-secret-canary")
        )

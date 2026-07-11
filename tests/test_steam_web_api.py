from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping

import pytest

from steam_agent.credentials import SecretValue
from steam_agent.steam_web_api import (
    HttpResponse,
    STEAM_WEB_API_HOST,
    SteamApiError,
    SteamWebApiClient,
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


def client_for(status: int, payload: object) -> tuple[SteamWebApiClient, RecordingTransport]:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    transport = RecordingTransport(HttpResponse(status, body))
    return SteamWebApiClient(transport=transport), transport


def test_probe_uses_fixed_https_host_and_header_only_secret() -> None:
    sentinel = "secret-canary-value"
    client, transport = client_for(200, {"response": {"game_count": 0, "games": []}})

    result = client.probe_visible_owned_games(
        steamid="76561197960265728", api_key=SecretValue(sentinel)
    )

    assert result.probe_state == "ready"
    assert result.visible_game_count == 0
    assert transport.call is not None
    assert transport.call["host"] == STEAM_WEB_API_HOST
    assert transport.call["headers"]["x-webapi-key"] == sentinel
    assert sentinel not in str(transport.call["path"])
    assert "include_appinfo%22%3Afalse" in str(transport.call["path"])
    assert "include_played_free_games%22%3Atrue" in str(transport.call["path"])


def test_probe_accepts_consistent_minimal_game_entries() -> None:
    client, _ = client_for(
        200,
        {"response": {"game_count": 1, "games": [{"appid": 10}]}},
    )

    result = client.probe_visible_owned_games(
        steamid="76561197960265728", api_key=SecretValue("canary")
    )

    assert result.probe_state == "ready"
    assert result.visible_game_count == 1


def test_empty_response_is_inaccessible_not_confirmed_empty() -> None:
    client, _ = client_for(200, {"response": {}})
    result = client.probe_visible_owned_games(
        steamid="76561197960265728", api_key=SecretValue("canary")
    )
    assert result.probe_state == "data_inaccessible"
    assert result.visible_game_count is None


def test_invalid_response_does_not_retain_provider_body_in_exception_chain() -> None:
    sentinel = b"provider-body-canary"
    client, _ = client_for(200, sentinel)

    with pytest.raises(SteamApiError) as caught:
        client.probe_visible_owned_games(
            steamid="76561197960265728", api_key=SecretValue("canary")
        )

    assert caught.value.__cause__ is None
    assert sentinel.decode() not in repr(caught.value.__context__)


def test_deeply_nested_json_is_typed_invalid_without_recursion_escape() -> None:
    nested = b"[" * 2000 + b"0" + b"]" * 2000
    client, _ = client_for(200, nested)

    with pytest.raises(SteamApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.probe_visible_owned_games(
            steamid="76561197960265728", api_key=SecretValue("canary")
        )


def test_oversized_json_integer_is_typed_invalid() -> None:
    payload = b'{"response":{"game_count":' + (b"9" * 5000) + b"}}"
    client, _ = client_for(200, payload)

    with pytest.raises(SteamApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.probe_visible_owned_games(
            steamid="76561197960265728", api_key=SecretValue("canary")
        )


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "INVALID_REQUEST", False),
        (401, "AUTHENTICATION_FAILED", False),
        (403, "AUTHENTICATION_FAILED", False),
        (429, "RATE_LIMITED", True),
        (500, "PROVIDER_UNAVAILABLE", True),
        (503, "PROVIDER_UNAVAILABLE", True),
        (302, "PROVIDER_RESPONSE_INVALID", False),
    ],
)
def test_probe_maps_http_status_without_provider_body(
    status: int, code: str, retryable: bool
) -> None:
    client, _ = client_for(status, b"secret provider body")
    with pytest.raises(SteamApiError) as caught:
        client.probe_visible_owned_games(
            steamid="76561197960265728", api_key=SecretValue("canary")
        )
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "secret provider body" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        [],
        {"response": None},
        {"response": {"games": []}},
        {"response": {"game_count": -1}},
        {"response": {"game_count": True}},
        {"response": {"game_count": 1, "games": {}}},
        {"response": {"game_count": 1}},
        {"response": {"game_count": 1, "games": []}},
        {"response": {"game_count": 0, "games": [{}]}},
        {"response": {"game_count": 0, "games": None}},
        {"response": {"game_count": 1, "games": [None]}},
        {"response": {"game_count": 1, "games": [{"appid": True}]}},
        {
            "response": {
                "game_count": 2,
                "games": [{"appid": 10}, {"appid": 10}],
            }
        },
    ],
)
def test_invalid_provider_shapes_are_typed(payload: object) -> None:
    client, _ = client_for(200, payload)
    with pytest.raises(SteamApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.probe_visible_owned_games(
            steamid="76561197960265728", api_key=SecretValue("canary")
        )


def test_invalid_steamid_makes_no_request() -> None:
    client, transport = client_for(200, {"response": {"game_count": 0}})
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        client.probe_visible_owned_games(steamid="not-an-id", api_key=SecretValue("canary"))
    assert transport.call is None


def test_secret_value_is_redacted_from_repr() -> None:
    secret = SecretValue("secret-canary-value")
    assert "secret-canary-value" not in repr(secret)
    assert "secret-canary-value" not in str(secret)

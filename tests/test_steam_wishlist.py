import json

import pytest

from steam_agent.credentials import SecretValue
from steam_agent.steam_wishlist import (
    HttpResponse,
    SteamWishlistClient,
    WishlistApiError,
)


class Transport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> HttpResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def response(body: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(body).encode())


def test_fixed_host_header_and_additive_fields_are_tolerated() -> None:
    secret = "wishlist-secret-canary"
    transport = Transport(
        [
            response(
                {
                    "response": {
                        "items": [
                            {
                                "appid": 20,
                                "priority": 2,
                                "date_added": 100,
                                "future": "ignored",
                            },
                            {"appid": 10, "priority": 0, "date_added": 0},
                        ],
                        "future": True,
                    },
                    "future": {},
                }
            ),
            response({"response": {"count": 2, "future": 1}}),
        ]
    )
    client = SteamWishlistClient(transport=transport)

    items = client.fetch_items(steamid="76561198000000000", api_key=SecretValue(secret))
    count = client.fetch_count(steamid="76561198000000000", api_key=SecretValue(secret))

    assert [item.appid for item in items.items] == [10, 20]
    assert count.count == 2
    assert all(call["host"] == "api.steampowered.com" for call in transport.calls)
    assert all(call["headers"]["x-webapi-key"] == secret for call in transport.calls)  # type: ignore[index]
    assert all(secret not in str(call["path"]) for call in transport.calls)


def test_empty_response_is_ambiguous_not_empty() -> None:
    transport = Transport([response({"response": {}}), response({"response": {}})])
    client = SteamWishlistClient(transport=transport)
    secret = SecretValue("secret")

    assert client.fetch_items(steamid="1", api_key=secret).state == "ambiguous"
    assert client.fetch_count(steamid="1", api_key=secret).state == "ambiguous"


@pytest.mark.parametrize(
    "body",
    [
        {"response": {"items": [{"appid": 1, "priority": 0}]}},
        {"response": {"items": [{"appid": 0, "priority": 0, "date_added": 0}]}},
        {"response": {"items": [1]}},
        {"response": {"count": True}},
        {"response": []},
    ],
)
def test_malformed_shapes_are_typed_and_do_not_echo_body(body: object) -> None:
    encoded = json.dumps(body)
    client = SteamWishlistClient(transport=Transport([response(body)]))
    with pytest.raises(WishlistApiError) as caught:
        if "count" in encoded:
            client.fetch_count(steamid="1", api_key=SecretValue("secret"))
        else:
            client.fetch_items(steamid="1", api_key=SecretValue("secret"))
    assert caught.value.code == "PROVIDER_RESPONSE_INVALID"
    assert encoded not in str(caught.value)

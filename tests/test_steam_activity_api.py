from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

import pytest

from steam_agent.credentials import SecretValue
from steam_agent.steam_activity_api import (
    ActivityGame,
    HttpResponse,
    MAX_ACHIEVEMENTS,
    MAX_GAMES,
    MAX_RESPONSE_BYTES,
    SteamActivityApiClient,
    SteamActivityApiError,
)


@dataclass
class Transport:
    responses: list[HttpResponse]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            {"host": host, "path": path, "headers": dict(headers), "timeout": timeout}
        )
        return self.responses.pop(0)


def response(payload: object, status: int = 200) -> HttpResponse:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return HttpResponse(status, body)


def client_for(*responses: HttpResponse) -> tuple[SteamActivityApiClient, Transport]:
    transport = Transport(list(responses))
    return SteamActivityApiClient(transport=transport), transport


def activity_game(appid: int, **fields: object) -> dict[str, object]:
    return {"appid": appid, **fields}


def test_activity_uses_fixed_host_header_secret_and_bounded_service_inputs() -> None:
    secret = "activity-secret-canary"
    client, transport = client_for(
        response({"response": {"game_count": 0}}),
        response({"response": {"total_count": 0}}),
    )

    result = client.fetch_activity(
        steamid="76561198000000000",
        api_key=SecretValue(secret),
        recent_count=20,
    )

    assert result.owned.state == result.recent.state == "ready"
    assert len(transport.calls) == 2
    for call in transport.calls:
        assert call["host"] == "api.steampowered.com"
        assert call["headers"]["x-webapi-key"] == secret  # type: ignore[index]
        assert secret not in str(call["path"])
        assert "steamid" not in repr(result)
    owned_input = json.loads(
        parse_qs(urlsplit(str(transport.calls[0]["path"])).query)["input_json"][0]
    )
    recent_input = json.loads(
        parse_qs(urlsplit(str(transport.calls[1]["path"])).query)["input_json"][0]
    )
    assert owned_input == {
        "include_appinfo": False,
        "include_played_free_games": True,
        "steamid": "76561198000000000",
    }
    assert recent_input == {"count": 20, "steamid": "76561198000000000"}


def test_activity_normalizes_live_observed_fields_and_sorts() -> None:
    client, _ = client_for(
        response(
            {
                "response": {
                    "game_count": 2,
                    "games": [
                        activity_game(
                            20,
                            playtime_forever=40,
                            playtime_2weeks=2,
                            playtime_windows_forever=30,
                            playtime_mac_forever=1,
                            playtime_linux_forever=9,
                            playtime_deck_forever=7,
                            playtime_disconnected=3,
                            rtime_last_played=100,
                            ignored="future",
                        ),
                        activity_game(10),
                    ],
                    "ignored": True,
                }
            }
        ),
        response(
            {
                "response": {
                    "total_count": 2,
                    "games": [
                        activity_game(20, playtime_forever=40, playtime_2weeks=2),
                        activity_game(10, playtime_forever=1, playtime_2weeks=1),
                    ],
                }
            }
        ),
    )

    result = client.fetch_activity(steamid="1", api_key=SecretValue("secret"))

    assert result.owned.reported_count == 2
    assert result.owned.games == (
        ActivityGame(10, None, None, None, None, None, None, None, None),
        ActivityGame(20, 40, 2, 30, 1, 9, 7, 3, 100),
    )
    assert [game.appid for game in result.recent.games] == [10, 20]


def test_explicit_empty_and_inaccessible_activity_remain_distinct() -> None:
    ready, _ = client_for(
        response({"response": {"game_count": 0, "games": []}}),
        response({"response": {"total_count": 0, "games": []}}),
    )
    inaccessible, _ = client_for(response({"response": {}}), response({"response": {}}))

    empty = ready.fetch_activity(steamid="1", api_key=SecretValue("secret"))
    hidden = inaccessible.fetch_activity(steamid="1", api_key=SecretValue("secret"))

    assert empty.owned.state == empty.recent.state == "ready"
    assert empty.owned.reported_count == empty.recent.reported_count == 0
    assert hidden.owned.state == hidden.recent.state == "data_inaccessible"
    assert hidden.owned.reported_count is hidden.recent.reported_count is None


def test_recent_total_may_exceed_bounded_return_count() -> None:
    client, _ = client_for(
        response({"response": {"game_count": 0}}),
        response(
            {
                "response": {
                    "total_count": 2,
                    "games": [activity_game(10, playtime_2weeks=1)],
                }
            }
        ),
    )

    result = client.fetch_activity(
        steamid="1", api_key=SecretValue("secret"), recent_count=1
    )

    assert result.recent.reported_count == 2
    assert len(result.recent.games) == 1


@pytest.mark.parametrize(
    ("owned", "recent"),
    [
        ({"game_count": 1, "games": []}, {"total_count": 0}),
        (
            {"game_count": 1, "games": [activity_game(1), activity_game(1)]},
            {"total_count": 0},
        ),
        ({"game_count": True, "games": []}, {"total_count": 0}),
        ({"game_count": 1, "games": [activity_game(0)]}, {"total_count": 0}),
        (
            {"game_count": 1, "games": [activity_game(1, playtime_forever=-1)]},
            {"total_count": 0},
        ),
        ({"game_count": 0}, {"total_count": 1}),
        ({"game_count": 0}, {"total_count": 1, "games": [activity_game(1)]}),
        (
            {"game_count": 0},
            {"total_count": 1, "games": [activity_game(1, playtime_2weeks=True)]},
        ),
    ],
)
def test_activity_rejects_malformed_counts_duplicates_and_numbers(
    owned: object, recent: object
) -> None:
    client, _ = client_for(
        response({"response": owned}), response({"response": recent})
    )

    with pytest.raises(SteamActivityApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch_activity(steamid="1", api_key=SecretValue("secret"))


@pytest.mark.parametrize("recent_count", [-1, 0, 101, True])
def test_activity_rejects_unbounded_recent_count(recent_count: object) -> None:
    client, transport = client_for()
    with pytest.raises(ValueError):
        client.fetch_activity(
            steamid="1",
            api_key=SecretValue("secret"),
            recent_count=recent_count,  # type: ignore[arg-type]
        )
    assert transport.calls == []


def test_player_achievements_normalize_and_sort_without_names() -> None:
    client, transport = client_for(
        response(
            {
                "playerstats": {
                    "success": True,
                    "gameName": "not retained",
                    "achievements": [
                        {
                            "apiname": "B",
                            "achieved": 1,
                            "unlocktime": 100,
                            "name": "not retained",
                            "description": "not retained",
                        },
                        {"apiname": "A", "achieved": 0, "unlocktime": 0},
                    ],
                }
            }
        )
    )

    result = client.fetch_player_achievements(
        steamid="76561198000000000", appid=10, api_key=SecretValue("secret")
    )

    assert result.state == "ready"
    assert [
        (item.api_name, item.achieved, item.unlock_time_unix)
        for item in result.achievements
    ] == [
        ("A", False, None),
        ("B", True, 100),
    ]
    assert "steamid" not in repr(result)
    assert "not retained" not in repr(result)
    assert "76561198000000000" in str(transport.calls[0]["path"])


@pytest.mark.parametrize("status", [200, 403])
def test_player_profile_not_public_is_typed(status: int) -> None:
    client, _ = client_for(
        response(
            {"playerstats": {"success": False, "error": "Profile is not public"}},
            status,
        )
    )
    result = client.fetch_player_achievements(
        steamid="1", appid=10, api_key=SecretValue("secret")
    )
    assert result.state == "profile_not_public"
    assert result.achievements == ()


@pytest.mark.parametrize("status", [200, 400])
def test_player_no_stats_is_typed_not_supported(status: int) -> None:
    client, _ = client_for(
        response(
            {
                "playerstats": {
                    "success": False,
                    "error": "Requested app has no stats",
                }
            },
            status,
        )
    )
    result = client.fetch_player_achievements(
        steamid="1", appid=10, api_key=SecretValue("secret")
    )
    assert result.state == "achievements_not_supported"


@pytest.mark.parametrize(
    "achievement",
    [
        {"apiname": "A", "achieved": 2, "unlocktime": 0},
        {"apiname": "A", "achieved": 1, "unlocktime": -1},
        {"apiname": "", "achieved": 1, "unlocktime": 0},
        {"apiname": "A\x00B", "achieved": 1, "unlocktime": 0},
        {"apiname": "\ud800", "achieved": 1, "unlocktime": 0},
    ],
)
def test_player_achievements_reject_malformed_entries(achievement: object) -> None:
    client, _ = client_for(
        response({"playerstats": {"success": True, "achievements": [achievement]}})
    )
    with pytest.raises(SteamActivityApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch_player_achievements(
            steamid="1", appid=10, api_key=SecretValue("secret")
        )


def test_player_achievements_reject_duplicate_names() -> None:
    item = {"apiname": "A", "achieved": 1, "unlocktime": 1}
    client, _ = client_for(
        response({"playerstats": {"success": True, "achievements": [item, item]}})
    )
    with pytest.raises(SteamActivityApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch_player_achievements(
            steamid="1", appid=10, api_key=SecretValue("secret")
        )


def test_schema_preserves_hidden_metadata_but_drops_icons() -> None:
    client, _ = client_for(
        response(
            {
                "game": {
                    "gameName": "not retained",
                    "availableGameStats": {
                        "achievements": [
                            {
                                "name": "HIDDEN_ONE",
                                "displayName": "Secret title",
                                "description": "Secret description",
                                "hidden": 1,
                                "icon": "https://cdn.invalid/icon.jpg",
                                "icongray": "https://cdn.invalid/gray.jpg",
                                "defaultvalue": 0,
                            }
                        ]
                    },
                }
            }
        )
    )

    result = client.fetch_achievement_schema(
        appid=10, api_key=SecretValue("secret"), language="english"
    )

    assert result.state == "ready"
    assert result.achievements[0].hidden is True
    assert result.achievements[0].display_name == "Secret title"
    assert result.achievements[0].description == "Secret description"
    assert "cdn.invalid" not in repr(result)
    assert "not retained" not in repr(result)


@pytest.mark.parametrize(
    "payload",
    [
        {"game": {}},
        {"game": {"availableGameStats": None}},
        {"game": {"gameName": "No stats"}},
    ],
)
def test_schema_without_available_stats_is_not_supported(payload: object) -> None:
    client, _ = client_for(response(payload))
    result = client.fetch_achievement_schema(appid=10, api_key=SecretValue("secret"))
    assert result.state == "achievements_not_supported"


def test_schema_allows_explicit_empty_achievement_list() -> None:
    client, _ = client_for(
        response({"game": {"availableGameStats": {"achievements": []}}})
    )
    result = client.fetch_achievement_schema(appid=10, api_key=SecretValue("secret"))
    assert result.state == "ready"
    assert result.achievements == ()


@pytest.mark.parametrize(
    "definition",
    [
        {"name": "A", "displayName": "A", "description": "D", "hidden": 2},
        {"name": "", "displayName": "A", "description": "D", "hidden": 0},
        {"name": "A", "displayName": 1, "description": "D", "hidden": 0},
        {"name": "A", "displayName": "A", "description": "D", "hidden": False},
    ],
)
def test_schema_rejects_malformed_definitions(definition: object) -> None:
    client, _ = client_for(
        response({"game": {"availableGameStats": {"achievements": [definition]}}})
    )
    with pytest.raises(SteamActivityApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch_achievement_schema(appid=10, api_key=SecretValue("secret"))


def test_schema_rejects_duplicate_api_names() -> None:
    definition = {
        "name": "A",
        "displayName": "A",
        "description": "D",
        "hidden": 0,
    }
    client, _ = client_for(
        response(
            {"game": {"availableGameStats": {"achievements": [definition, definition]}}}
        )
    )
    with pytest.raises(SteamActivityApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch_achievement_schema(appid=10, api_key=SecretValue("secret"))


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "INVALID_REQUEST", False),
        (401, "AUTHENTICATION_FAILED", False),
        (403, "AUTHENTICATION_FAILED", False),
        (429, "RATE_LIMITED", True),
        (500, "PROVIDER_UNAVAILABLE", True),
        (503, "PROVIDER_UNAVAILABLE", True),
        (301, "PROVIDER_RESPONSE_INVALID", False),
        (302, "PROVIDER_RESPONSE_INVALID", False),
        (307, "PROVIDER_RESPONSE_INVALID", False),
    ],
)
def test_status_and_redirect_boundaries_are_sanitized(
    status: int, code: str, retryable: bool
) -> None:
    canary = b"provider body canary"
    client, _ = client_for(response(canary, status))
    with pytest.raises(SteamActivityApiError) as caught:
        client.fetch_achievement_schema(appid=10, api_key=SecretValue("secret"))
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert canary.decode() not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "INVALID_REQUEST"),
        (401, "AUTHENTICATION_FAILED"),
        (403, "AUTHENTICATION_FAILED"),
    ],
)
def test_player_status_wins_over_malformed_provider_body(
    status: int, code: str
) -> None:
    client, _ = client_for(response(b"provider body canary", status))
    with pytest.raises(SteamActivityApiError) as caught:
        client.fetch_player_achievements(
            steamid="1", appid=10, api_key=SecretValue("secret")
        )
    assert caught.value.code == code
    assert "canary" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b"not json body canary",
        b"[1,2,3]",
        b"[" * 2000 + b"0" + b"]" * 2000,
        b'{"game":{"availableGameStats":{"achievements":['
        + b'{"name":"A","hidden":0,"number":'
        + b"9" * 5000
        + b"}]}}}",
        b"x" * (MAX_RESPONSE_BYTES + 1),
    ],
)
def test_malformed_oversize_and_numeric_bomb_bodies_are_sanitized(body: bytes) -> None:
    client, _ = client_for(response(body))
    with pytest.raises(
        SteamActivityApiError, match="PROVIDER_RESPONSE_INVALID"
    ) as caught:
        client.fetch_achievement_schema(appid=10, api_key=SecretValue("secret"))
    assert "canary" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_declared_collection_bounds_are_enforced_without_large_allocations() -> None:
    client, _ = client_for(
        response({"response": {"game_count": MAX_GAMES + 1}}),
        response({"response": {"total_count": 0}}),
    )
    with pytest.raises(SteamActivityApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch_activity(steamid="1", api_key=SecretValue("secret"))

    achievements = [
        {"apiname": f"A{i}", "achieved": 0, "unlocktime": 0}
        for i in range(MAX_ACHIEVEMENTS + 1)
    ]
    client, _ = client_for(
        response({"playerstats": {"success": True, "achievements": achievements}})
    )
    with pytest.raises(SteamActivityApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch_player_achievements(
            steamid="1", appid=10, api_key=SecretValue("secret")
        )


@pytest.mark.parametrize(
    ("steamid", "appid"),
    [("0", 1), ("-1", 1), ("not-a-steamid", 1), ("1", 0), ("1", True)],
)
def test_invalid_identifiers_fail_before_transport(steamid: str, appid: object) -> None:
    client, transport = client_for()
    with pytest.raises(ValueError):
        client.fetch_player_achievements(
            steamid=steamid,
            appid=appid,  # type: ignore[arg-type]
            api_key=SecretValue("secret"),
        )
    assert transport.calls == []

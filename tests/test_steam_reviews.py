from __future__ import annotations

import json
from typing import Mapping

import pytest

from steam_agent.steam_reviews import (
    HttpResponse,
    STEAM_STORE_HOST,
    SteamReviewClient,
    SteamReviewError,
)


class Transport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, Mapping[str, str], float]] = []

    def request(self, *, host, path, headers, timeout):
        self.calls.append((host, path, headers, timeout))
        return self.response


def response(payload: object, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode(), {})


def test_fetches_only_exact_aggregate_summary() -> None:
    transport = Transport(
        response(
            {
                "success": 1,
                "query_summary": {
                    "num_reviews": 1,
                    "review_score": 8,
                    "review_score_desc": "Synthetic",
                    "total_positive": 80,
                    "total_negative": 20,
                    "total_reviews": 100,
                },
                "reviews": [
                    {
                        "review": "secret canary text",
                        "author": {"steamid": "76561198000000000"},
                    }
                ],
                "cursor": "private cursor",
            }
        )
    )

    summary = SteamReviewClient(transport=transport).fetch_summary(10)

    assert summary.appid == 10
    assert (summary.review_score, summary.total_positive) == (8, 80)
    assert summary.total_negative == 20 and summary.total_reviews == 100
    assert "secret" not in repr(summary)
    assert "76561198000000000" not in repr(summary)
    host, path, headers, timeout = transport.calls[0]
    assert host == STEAM_STORE_HOST and path.startswith("/appreviews/10?")
    assert "num_per_page=1" in path and "filter_offtopic_activity=1" in path
    assert set(headers) == {"Accept", "User-Agent"}
    assert timeout > 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"success": 0},
        {"success": 1},
        {"success": 1, "query_summary": []},
        {
            "success": 1,
            "query_summary": {
                "review_score": 8,
                "total_positive": 2,
                "total_negative": 2,
                "total_reviews": 5,
            },
        },
        {
            "success": 1,
            "query_summary": {
                "review_score": 11,
                "total_positive": 0,
                "total_negative": 0,
                "total_reviews": 0,
            },
        },
    ],
)
def test_rejects_malformed_summaries(payload: object) -> None:
    with pytest.raises(SteamReviewError) as caught:
        SteamReviewClient(transport=Transport(response(payload))).fetch_summary(10)
    assert caught.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("status,retryable", [(301, False), (404, False), (500, True)])
def test_statuses_are_sanitized(status: int, retryable: bool) -> None:
    with pytest.raises(SteamReviewError) as caught:
        SteamReviewClient(
            transport=Transport(HttpResponse(status, b"provider secret", {}))
        ).fetch_summary(10)
    assert caught.value.code in {"PROVIDER_RESPONSE_INVALID", "PROVIDER_UNAVAILABLE"}
    assert caught.value.retryable is retryable
    assert "secret" not in str(caught.value)


def test_rate_limit_retains_only_bounded_retry_after() -> None:
    with pytest.raises(SteamReviewError) as caught:
        SteamReviewClient(
            transport=Transport(HttpResponse(429, b"secret", {"retry-after": "37"}))
        ).fetch_summary(10)
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.retry_after_seconds == 37


@pytest.mark.parametrize("appid", [True, 0, -1, 1 << 32, "10"])
def test_rejects_invalid_appids(appid: object) -> None:
    with pytest.raises(ValueError):
        SteamReviewClient(transport=Transport(response({}))).fetch_summary(appid)  # type: ignore[arg-type]


def test_rejects_oversized_or_invalid_json() -> None:
    for body in (b"{", b"x" * (64 * 1024 + 1)):
        with pytest.raises(SteamReviewError) as caught:
            SteamReviewClient(
                transport=Transport(HttpResponse(200, body, {}))
            ).fetch_summary(10)
        assert caught.value.code == "PROVIDER_RESPONSE_INVALID"

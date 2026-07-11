from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

import pytest

from steam_agent.credentials import SecretValue
from steam_agent.steam_store_catalog import (
    CatalogApiError,
    CatalogStream,
    FixedHttpsTransport,
    HttpResponse,
    MAX_RESPONSE_BYTES,
    STEAM_STORE_API_HOST,
    SteamStoreCatalogClient,
)


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(seconds=1)
        return result


@dataclass
class Call:
    host: str
    path: str
    headers: dict[str, str]
    timeout: float


class SequenceTransport:
    def __init__(self, *responses: HttpResponse | CatalogApiError) -> None:
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
            raise AssertionError("unexpected catalog request")
        response = self.responses.pop(0)
        if isinstance(response, CatalogApiError):
            raise response
        return response


def response(
    apps: list[dict[str, object]],
    *,
    more: bool = False,
    last_appid: int | None = None,
    status: int = 200,
    extra_body: dict[str, object] | None = None,
) -> HttpResponse:
    body: dict[str, object] = {
        "apps": apps,
        "have_more_results": more,
    }
    if last_appid is not None:
        body["last_appid"] = last_appid
    if extra_body:
        body.update(extra_body)
    return HttpResponse(status, json.dumps({"response": body}).encode())


def request_input(call: Call) -> dict[str, object]:
    query = parse_qs(urlsplit(call.path).query)
    return json.loads(query["input_json"][0])


def test_games_scan_uses_fixed_host_header_key_and_explicit_filter() -> None:
    sentinel = "catalog-secret-canary"
    transport = SequenceTransport(
        response(
            [
                {"appid": 10, "last_modified": 100, "price_change_number": 7},
                {"appid": 20},
            ],
            more=True,
            last_appid=20,
        )
    )
    client = SteamStoreCatalogClient(transport=transport, clock=Clock())

    result = client.scan_demanded_apps(
        api_key=SecretValue(sentinel),
        demanded_appids={10},
        stream=CatalogStream.GAMES,
    )

    assert result.state == "complete"
    assert result.termination == "demand_boundary"
    assert result.hits[0].appid == 10
    assert result.hits[0].last_modified == 100
    assert result.hits[0].price_change_number == 7
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.host == STEAM_STORE_API_HOST
    assert call.path.startswith("/IStoreService/GetAppList/v1/?")
    assert sentinel not in call.path
    assert call.headers["x-webapi-key"] == sentinel
    assert request_input(call) == {
        "include_dlc": False,
        "include_games": True,
        "include_hardware": False,
        "include_software": False,
        "include_videos": False,
        "last_appid": 0,
        "max_results": 50000,
    }


def test_non_games_scan_sets_aggregate_filter_and_scans_until_max_demand() -> None:
    transport = SequenceTransport(
        response([{"appid": 5}, {"appid": 10}], more=True, last_appid=10),
        response([{"appid": 20}, {"appid": 30}], more=True, last_appid=30),
        response([{"appid": 40}], more=False, last_appid=40),
    )
    result = SteamStoreCatalogClient(
        transport=transport, clock=Clock(), max_results=2
    ).scan_demanded_apps(
        api_key=SecretValue("secret"),
        demanded_appids=[5, 15, 20],
        stream="non_games",
    )

    assert result.state == "complete"
    assert result.termination == "demand_boundary"
    assert [hit.appid for hit in result.hits] == [5, 20]
    assert result.confirmed_absent_appids == (15,)
    assert result.unresolved_appids == ()
    assert result.scanned_through_appid == 30
    assert len(result.pages) == 2
    assert [page.page_number for page in result.pages] == [1, 2]
    assert [page.requested_last_appid for page in result.pages] == [0, 10]
    assert [page.retrieved_at for page in result.pages] == [
        "2026-07-11T12:00:00Z",
        "2026-07-11T12:00:01Z",
    ]
    assert len(transport.calls) == 2
    assert request_input(transport.calls[1])["last_appid"] == 10
    assert request_input(transport.calls[0]) == {
        "include_dlc": True,
        "include_games": False,
        "include_hardware": True,
        "include_software": True,
        "include_videos": True,
        "last_appid": 0,
        "max_results": 2,
    }


def test_end_of_stream_confirms_missing_demands() -> None:
    transport = SequenceTransport(response([{"appid": 10}], more=False, last_appid=10))
    result = SteamStoreCatalogClient(
        transport=transport, clock=Clock()
    ).scan_demanded_apps(
        api_key=SecretValue("secret"),
        demanded_appids=[10, 999],
        stream="games",
    )

    assert result.termination == "end_of_stream"
    assert result.state == "complete"
    assert [hit.appid for hit in result.hits] == [10]
    assert result.confirmed_absent_appids == (999,)
    assert result.unresolved_appids == ()


def test_empty_demand_makes_no_request() -> None:
    transport = SequenceTransport()
    result = SteamStoreCatalogClient(transport=transport).scan_demanded_apps(
        api_key=SecretValue("secret"), demanded_appids=[], stream="games"
    )
    assert result.termination == "no_demand"
    assert result.state == "complete"
    assert result.pages == ()
    assert transport.calls == []


def test_later_provider_failure_returns_sanitized_partial_result() -> None:
    sentinel = b"raw-provider-canary"
    transport = SequenceTransport(
        response([{"appid": 10}], more=True, last_appid=10),
        HttpResponse(503, sentinel),
    )
    result = SteamStoreCatalogClient(
        transport=transport, clock=Clock()
    ).scan_demanded_apps(
        api_key=SecretValue("secret"),
        demanded_appids=[10, 20],
        stream="games",
    )

    assert result.state == "partial"
    assert result.termination == "provider_error"
    assert result.error_code == "PROVIDER_UNAVAILABLE"
    assert result.retryable
    assert [hit.appid for hit in result.hits] == [10]
    assert result.unresolved_appids == (20,)
    assert sentinel.decode() not in repr(result)


def test_first_provider_failure_raises_typed_sanitized_error() -> None:
    sentinel = b"raw-provider-canary"
    client = SteamStoreCatalogClient(
        transport=SequenceTransport(HttpResponse(503, sentinel)), clock=Clock()
    )
    with pytest.raises(CatalogApiError) as caught:
        client.scan_demanded_apps(
            api_key=SecretValue("secret"), demanded_appids=[10], stream="games"
        )
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert caught.value.retryable
    assert sentinel.decode() not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "INVALID_REQUEST", False),
        (401, "AUTHENTICATION_FAILED", False),
        (403, "AUTHENTICATION_FAILED", False),
        (429, "RATE_LIMITED", True),
        (500, "PROVIDER_UNAVAILABLE", True),
        (302, "PROVIDER_RESPONSE_INVALID", False),
    ],
)
def test_status_mapping(status: int, code: str, retryable: bool) -> None:
    client = SteamStoreCatalogClient(
        transport=SequenceTransport(HttpResponse(status, b"provider body"))
    )
    with pytest.raises(CatalogApiError) as caught:
        client.scan_demanded_apps(
            api_key=SecretValue("secret"), demanded_appids=[10], stream="games"
        )
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "provider body" not in str(caught.value)


@pytest.mark.parametrize(
    "bad_response",
    [
        HttpResponse(200, b"not json"),
        HttpResponse(200, json.dumps([]).encode()),
        HttpResponse(200, json.dumps({"response": None}).encode()),
        HttpResponse(200, json.dumps({"response": {"apps": {}}}).encode()),
        response([{"appid": 10}, {"appid": 10}]),
        response([{"appid": 20}, {"appid": 10}]),
        response([{"appid": True}]),
        response([{"appid": 10, "last_modified": -1}]),
        response([{"appid": 10, "price_change_number": True}]),
        response([{"appid": 10, "unknown": 1}]),
        response([{"appid": 10}], last_appid=11),
        response([], more=True, last_appid=0),
        response([], more=False, last_appid=1),
        response([{"appid": 10}], extra_body={"unknown": 1}),
    ],
)
def test_invalid_page_shapes_are_typed(bad_response: HttpResponse) -> None:
    client = SteamStoreCatalogClient(
        transport=SequenceTransport(bad_response), clock=Clock()
    )
    with pytest.raises(CatalogApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.scan_demanded_apps(
            api_key=SecretValue("secret"), demanded_appids=[20], stream="games"
        )


def test_cross_page_progress_must_be_strict() -> None:
    transport = SequenceTransport(
        response([{"appid": 10}], more=True, last_appid=10),
        response([{"appid": 10}], more=False, last_appid=10),
    )
    result = SteamStoreCatalogClient(
        transport=transport, clock=Clock()
    ).scan_demanded_apps(
        api_key=SecretValue("secret"), demanded_appids=[20], stream="games"
    )
    assert result.state == "partial"
    assert result.error_code == "PROVIDER_RESPONSE_INVALID"
    assert result.scanned_through_appid == 10


@pytest.mark.parametrize(
    "demanded",
    [[10, 10], [0], [-1], [True], [1 << 32], ["10"]],
)
def test_invalid_demands_are_rejected_without_request(demanded: list[object]) -> None:
    transport = SequenceTransport()
    with pytest.raises(ValueError, match="positive unique uint32"):
        SteamStoreCatalogClient(transport=transport).scan_demanded_apps(
            api_key=SecretValue("secret"),
            demanded_appids=demanded,  # type: ignore[arg-type]
            stream="games",
        )
    assert transport.calls == []


def test_invalid_stream_and_client_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported catalog stream"):
        SteamStoreCatalogClient().scan_demanded_apps(
            api_key=SecretValue("secret"), demanded_appids=[10], stream="all"
        )
    for value in (0, 50_001, True):
        with pytest.raises(ValueError):
            SteamStoreCatalogClient(max_results=value)  # type: ignore[arg-type]


def test_deeply_nested_json_is_typed_invalid() -> None:
    body = b"[" * 2000 + b"0" + b"]" * 2000
    client = SteamStoreCatalogClient(
        transport=SequenceTransport(HttpResponse(200, body))
    )
    with pytest.raises(CatalogApiError, match="PROVIDER_RESPONSE_INVALID"):
        client.scan_demanded_apps(
            api_key=SecretValue("secret"), demanded_appids=[10], stream="games"
        )


class _FakeHttpResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, limit: int) -> bytes:
        assert limit == MAX_RESPONSE_BYTES + 1
        return self.body


class _FakeConnection:
    def __init__(self, host: str, *, timeout: float, body: bytes) -> None:
        self.host = host
        self.timeout = timeout
        self.body = body
        self.closed = False
        self.requested: tuple[str, str, dict[str, str]] | None = None

    def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
        self.requested = (method, path, headers)

    def getresponse(self) -> _FakeHttpResponse:
        return _FakeHttpResponse(self.body)

    def close(self) -> None:
        self.closed = True


def test_fixed_transport_bounds_body_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        STEAM_STORE_API_HOST,
        timeout=1.0,
        body=b"x" * (MAX_RESPONSE_BYTES + 1),
    )
    monkeypatch.setattr(
        "steam_agent.steam_store_catalog.http.client.HTTPSConnection",
        lambda host, timeout: connection,
    )

    with pytest.raises(CatalogApiError, match="PROVIDER_RESPONSE_INVALID"):
        FixedHttpsTransport().request(
            host=STEAM_STORE_API_HOST,
            path="/fixed",
            headers={"Accept": "application/json"},
            timeout=1.0,
        )
    assert connection.requested == (
        "GET",
        "/fixed",
        {"Accept": "application/json"},
    )
    assert connection.closed

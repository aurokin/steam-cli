"""Demand-driven, non-retaining Steam store catalog scanner.

The adapter scans the documented ``IStoreService/GetAppList`` streams in AppID
order. It retains only demanded normalized hits and coarse page provenance; raw
provider pages are discarded after each page is interpreted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import http.client
import json
from typing import Literal, Protocol
from urllib.parse import urlencode

from steam_agent.credentials import SecretValue


STEAM_STORE_API_HOST = "api.steampowered.com"
GET_APP_LIST_PATH = "/IStoreService/GetAppList/v1/"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESULTS = 50_000
MAX_UNSIGNED_32 = (1 << 32) - 1
MAX_UNSIGNED_64 = (1 << 64) - 1


class CatalogStream(StrEnum):
    GAMES = "games"
    NON_GAMES = "non_games"


class CatalogApiError(RuntimeError):
    """Sanitized catalog failure containing no response or credential data."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


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
    """One-shot HTTPS transport with bounded reads and no redirect support."""

    def request(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        connection = http.client.HTTPSConnection(host, timeout=timeout)
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
            raise CatalogApiError("PROVIDER_UNAVAILABLE", retryable=True)
        if len(body) > MAX_RESPONSE_BYTES:
            raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        return HttpResponse(status=response.status, body=body)


@dataclass(frozen=True, slots=True)
class CatalogApp:
    appid: int
    stream: CatalogStream
    last_modified: int | None
    price_change_number: int | None


@dataclass(frozen=True, slots=True)
class CatalogPageProvenance:
    page_number: int
    requested_last_appid: int
    first_appid: int | None
    last_appid: int
    item_count: int
    have_more_results: bool
    retrieved_at: str


@dataclass(frozen=True, slots=True)
class CatalogScan:
    stream: CatalogStream
    max_results: int
    state: Literal["complete", "partial"]
    termination: Literal[
        "no_demand", "demand_boundary", "end_of_stream", "provider_error"
    ]
    demanded_appids: tuple[int, ...]
    hits: tuple[CatalogApp, ...]
    confirmed_absent_appids: tuple[int, ...]
    unresolved_appids: tuple[int, ...]
    pages: tuple[CatalogPageProvenance, ...]
    scanned_through_appid: int
    error_code: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class _Page:
    apps: tuple[tuple[int, int | None, int | None], ...]
    last_appid: int
    have_more_results: bool


Clock = Callable[[], datetime]
RequestGate = Callable[[], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SteamStoreCatalogClient:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_results: int = DEFAULT_MAX_RESULTS,
        clock: Clock = _utc_now,
        request_gate: RequestGate = lambda: None,
    ) -> None:
        if not isinstance(max_results, int) or isinstance(max_results, bool):
            raise ValueError("max_results must be an integer")
        if not 1 <= max_results <= DEFAULT_MAX_RESULTS:
            raise ValueError("max_results must be between 1 and 50000")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._transport = transport or FixedHttpsTransport()
        self._timeout = timeout
        self._max_results = max_results
        self._clock = clock
        self._request_gate = request_gate

    def scan_demanded_apps(
        self,
        *,
        api_key: SecretValue | None,
        demanded_appids: Iterable[int],
        stream: CatalogStream | str,
    ) -> CatalogScan:
        demanded = _normalize_demanded_appids(demanded_appids)
        try:
            selected_stream = CatalogStream(stream)
        except (TypeError, ValueError):
            raise ValueError("unsupported catalog stream") from None
        if not demanded:
            return CatalogScan(
                stream=selected_stream,
                max_results=self._max_results,
                state="complete",
                termination="no_demand",
                demanded_appids=(),
                hits=(),
                confirmed_absent_appids=(),
                unresolved_appids=(),
                pages=(),
                scanned_through_appid=0,
            )
        if api_key is None:
            raise CatalogApiError("AUTHENTICATION_FAILED", retryable=False)

        demanded_set = set(demanded)
        maximum_demanded = demanded[-1]
        hits: dict[int, CatalogApp] = {}
        pages: list[CatalogPageProvenance] = []
        cursor = 0
        page_number = 1
        while True:
            try:
                self._request_gate()
                response = self._transport.request(
                    host=STEAM_STORE_API_HOST,
                    path=self._request_path(stream=selected_stream, last_appid=cursor),
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "steam-agent/0.1",
                        "x-webapi-key": api_key.reveal(),
                    },
                    timeout=self._timeout,
                )
                page = _interpret_page(response, requested_last_appid=cursor)
                retrieved_at = _timestamp(self._clock())
            except CatalogApiError as exc:
                if not pages:
                    raise
                return _scan_result(
                    stream=selected_stream,
                    max_results=self._max_results,
                    state="partial",
                    termination="provider_error",
                    demanded=demanded,
                    hits=hits,
                    pages=pages,
                    cursor=cursor,
                    error_code=exc.code,
                    retryable=exc.retryable,
                )

            for appid, last_modified, price_change_number in page.apps:
                if appid in demanded_set:
                    hits[appid] = CatalogApp(
                        appid=appid,
                        stream=selected_stream,
                        last_modified=last_modified,
                        price_change_number=price_change_number,
                    )
            pages.append(
                CatalogPageProvenance(
                    page_number=page_number,
                    requested_last_appid=cursor,
                    first_appid=page.apps[0][0] if page.apps else None,
                    last_appid=page.last_appid,
                    item_count=len(page.apps),
                    have_more_results=page.have_more_results,
                    retrieved_at=retrieved_at,
                )
            )
            cursor = page.last_appid
            if not page.have_more_results:
                return _scan_result(
                    stream=selected_stream,
                    max_results=self._max_results,
                    state="complete",
                    termination="end_of_stream",
                    demanded=demanded,
                    hits=hits,
                    pages=pages,
                    cursor=cursor,
                )
            if cursor >= maximum_demanded:
                return _scan_result(
                    stream=selected_stream,
                    max_results=self._max_results,
                    state="complete",
                    termination="demand_boundary",
                    demanded=demanded,
                    hits=hits,
                    pages=pages,
                    cursor=cursor,
                )
            page_number += 1

    def _request_path(self, *, stream: CatalogStream, last_appid: int) -> str:
        games = stream is CatalogStream.GAMES
        request = {
            "include_games": games,
            "include_dlc": not games,
            "include_software": not games,
            "include_videos": not games,
            "include_hardware": not games,
            "last_appid": last_appid,
            "max_results": self._max_results,
        }
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
        return f"{GET_APP_LIST_PATH}?{urlencode({'input_json': encoded})}"


def _scan_result(
    *,
    stream: CatalogStream,
    max_results: int,
    state: Literal["complete", "partial"],
    termination: Literal["demand_boundary", "end_of_stream", "provider_error"],
    demanded: tuple[int, ...],
    hits: Mapping[int, CatalogApp],
    pages: list[CatalogPageProvenance],
    cursor: int,
    error_code: str | None = None,
    retryable: bool = False,
) -> CatalogScan:
    if termination == "end_of_stream":
        confirmed_absent = tuple(appid for appid in demanded if appid not in hits)
        unresolved: tuple[int, ...] = ()
    else:
        confirmed_limit = min(cursor, demanded[-1])
        confirmed_absent = tuple(
            appid
            for appid in demanded
            if appid <= confirmed_limit and appid not in hits
        )
        unresolved = tuple(appid for appid in demanded if appid > cursor)
    return CatalogScan(
        stream=stream,
        max_results=max_results,
        state=state,
        termination=termination,
        demanded_appids=demanded,
        hits=tuple(hits[appid] for appid in sorted(hits)),
        confirmed_absent_appids=confirmed_absent,
        unresolved_appids=unresolved,
        pages=tuple(pages),
        scanned_through_appid=cursor,
        error_code=error_code,
        retryable=retryable,
    )


def _interpret_page(response: HttpResponse, *, requested_last_appid: int) -> _Page:
    _raise_for_status(response.status)
    decoded, payload = _decode_json(response.body)
    if not decoded or not isinstance(payload, dict) or "response" not in payload:
        raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    body = payload["response"]
    if not isinstance(body, dict):
        raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    apps_value = body.get("apps", [])
    have_more = body.get("have_more_results", False)
    if not isinstance(apps_value, list) or not isinstance(have_more, bool):
        raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)

    apps: list[tuple[int, int | None, int | None]] = []
    previous = requested_last_appid
    for value in apps_value:
        if not isinstance(value, dict):
            raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        appid = value.get("appid")
        if not _positive_unsigned_32(appid) or appid <= previous:
            raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        last_modified = _optional_unsigned(value, "last_modified", MAX_UNSIGNED_32)
        price_change = _optional_unsigned(value, "price_change_number", MAX_UNSIGNED_64)
        apps.append((appid, last_modified, price_change))
        previous = appid

    supplied_last = body.get("last_appid")
    if supplied_last is not None and not _unsigned(supplied_last, MAX_UNSIGNED_32):
        raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    if apps:
        derived_last = apps[-1][0]
        if supplied_last is not None and supplied_last != derived_last:
            raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        last_appid = derived_last
    else:
        if have_more:
            raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        if supplied_last is not None and supplied_last != requested_last_appid:
            raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
        last_appid = requested_last_appid
    if have_more and last_appid <= requested_last_appid:
        raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return _Page(tuple(apps), last_appid, have_more)


def _raise_for_status(status: int) -> None:
    if status in (401, 403):
        raise CatalogApiError("AUTHENTICATION_FAILED", retryable=False)
    if status == 400:
        raise CatalogApiError("INVALID_REQUEST", retryable=False)
    if status == 429:
        raise CatalogApiError("RATE_LIMITED", retryable=True)
    if status >= 500:
        raise CatalogApiError("PROVIDER_UNAVAILABLE", retryable=True)
    if status != 200:
        raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)


def _normalize_demanded_appids(values: Iterable[int]) -> tuple[int, ...]:
    normalized: list[int] = []
    seen: set[int] = set()
    try:
        iterator = iter(values)
    except TypeError:
        raise ValueError("demanded_appids must be iterable") from None
    for value in iterator:
        if not _positive_unsigned_32(value) or value in seen:
            raise ValueError("demanded AppIDs must be positive unique uint32 values")
        seen.add(value)
        normalized.append(value)
    return tuple(sorted(normalized))


def _optional_unsigned(
    value: Mapping[object, object], key: str, maximum: int
) -> int | None:
    if key not in value:
        return None
    candidate = value[key]
    if not _unsigned(candidate, maximum):
        raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    return candidate


def _unsigned(value: object, maximum: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum
    )


def _positive_unsigned_32(value: object) -> bool:
    return _unsigned(value, MAX_UNSIGNED_32) and value > 0


def _decode_json(body: bytes) -> tuple[bool, object]:
    try:
        return True, json.loads(body)
    except (UnicodeError, ValueError, RecursionError):
        return False, None


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CatalogApiError("PROVIDER_RESPONSE_INVALID", retryable=False)
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


__all__ = [
    "CatalogApiError",
    "CatalogApp",
    "CatalogPageProvenance",
    "CatalogScan",
    "CatalogStream",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "FixedHttpsTransport",
    "GET_APP_LIST_PATH",
    "HttpResponse",
    "HttpTransport",
    "MAX_RESPONSE_BYTES",
    "STEAM_STORE_API_HOST",
    "SteamStoreCatalogClient",
]

"""Bounded provisional Steam storefront declarations for exact AppIDs.

The storefront ``api/appdetails`` JSON shape is publicly reachable but not a
documented Steam Web API.  This adapter is consequently narrow, independently
disableable, and deliberately retains only normalized declarations.  Raw JSON
and HTML never cross the adapter boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import http.client
import json
import re
from typing import Any, Literal, Mapping, Protocol
import unicodedata
from urllib.parse import urlencode, urlsplit
import zlib


STEAM_STORE_HOST = "store.steampowered.com"
APP_DETAILS_PATH = "/api/appdetails"
MAX_COMPRESSED_BYTES = 512 * 1024
MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024
MAX_HTML_FIELD_BYTES = 128 * 1024
MAX_SANITIZED_TEXT_CHARS = 32 * 1024
MAX_HTML_TOKENS = 8_192
MAX_HTML_DEPTH = 64
DEFAULT_TIMEOUT_SECONDS = 15.0
DECLARED_FACTS_DISCLOSURE_VERSION = "m6-declared-app-facts-v2"
MAX_UNSIGNED_32 = (1 << 32) - 1
MAX_DECLARATION_ITEMS = 256
MAX_LOCALIZED_LABEL_CHARS = 256

_COUNTRY = re.compile(r"[A-Z]{2}\Z")
SUPPORTED_REQUEST_LANGUAGES = frozenset(
    {
        "arabic",
        "brazilian",
        "bulgarian",
        "czech",
        "danish",
        "dutch",
        "english",
        "finnish",
        "french",
        "german",
        "greek",
        "hungarian",
        "indonesian",
        "italian",
        "japanese",
        "koreana",
        "latam",
        "malay",
        "norwegian",
        "polish",
        "portuguese",
        "romanian",
        "russian",
        "schinese",
        "spanish",
        "swedish",
        "tchinese",
        "thai",
        "turkish",
        "ukrainian",
        "vietnamese",
    }
)

# Valve documents these meanings, but not their storefront numeric IDs.  IDs
# are therefore a provisional transport mapping and unknown values survive as
# numeric evidence rather than being guessed from localized descriptions.
CATEGORY_SLUGS: Mapping[int, str] = {
    1: "multi_player",
    9: "co_op",
    18: "partial_controller_support",
    24: "shared_split_screen",
    27: "cross_platform_multiplayer",
    28: "full_controller_support",
    36: "online_pvp",
    37: "shared_split_screen_pvp",
    38: "online_co_op",
    39: "shared_split_screen_co_op",
    44: "remote_play_together",
    48: "lan_co_op",
    49: "pvp",
    52: "tracked_controller_support",
    55: "dualshock_usb_support",
    56: "dualshock_bluetooth_support",
    57: "dualsense_usb_support",
    58: "dualsense_bluetooth_support",
    59: "steam_input_api_support",
    60: "gamepad_preferred",
    64: "adjustable_text_size",
    65: "subtitle_options",
    66: "color_alternatives",
    67: "camera_comfort",
    68: "custom_volume_controls",
    69: "stereo_sound",
    70: "surround_sound",
    71: "narrated_game_menus",
    72: "chat_speech_to_text",
    73: "chat_text_to_speech",
    74: "playable_without_timed_input",
    75: "keyboard_only_option",
    76: "mouse_only_option",
    77: "touch_only_option",
    78: "adjustable_difficulty",
    79: "save_anytime",
    80: "playable_at_your_own_pace",
    81: "playable_without_vision",
    82: "contrast_controls",
}

MULTIPLAYER_CATEGORY_SLUGS: Mapping[int, str] = {
    identifier: CATEGORY_SLUGS[identifier]
    for identifier in (1, 9, 24, 27, 36, 37, 38, 39, 44, 48, 49)
}

_LANGUAGE_NAMES: Mapping[str, str] = {
    "arabic": "arabic",
    "bulgarian": "bulgarian",
    "simplified chinese": "schinese",
    "traditional chinese": "tchinese",
    "czech": "czech",
    "danish": "danish",
    "dutch": "dutch",
    "english": "english",
    "finnish": "finnish",
    "french": "french",
    "german": "german",
    "greek": "greek",
    "hungarian": "hungarian",
    "indonesian": "indonesian",
    "italian": "italian",
    "japanese": "japanese",
    "korean": "koreana",
    "malay": "malay",
    "norwegian": "norwegian",
    "polish": "polish",
    "portuguese - portugal": "portuguese",
    "portuguese - brazil": "brazilian",
    "romanian": "romanian",
    "russian": "russian",
    "spanish - spain": "spanish",
    "spanish - latin america": "latam",
    "swedish": "swedish",
    "thai": "thai",
    "turkish": "turkish",
    "ukrainian": "ukrainian",
    "vietnamese": "vietnamese",
}

_MISSING = object()


class SteamDeclaredFactsError(RuntimeError):
    """Sanitized provider failure with no response content."""

    def __init__(
        self, code: str, *, retryable: bool, retry_after_seconds: int | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


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
    """GET one fixed HTTPS origin with bounded gzip decompression."""

    def request(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        if host != STEAM_STORE_HOST or not path.startswith(f"{APP_DETAILS_PATH}?"):
            raise SteamDeclaredFactsError("PROVIDER_REQUEST_INVALID", retryable=False)
        connection = http.client.HTTPSConnection(host, timeout=timeout)
        response: http.client.HTTPResponse | None = None
        compressed = bytearray()
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            response_headers = {
                key.lower(): value for key, value in response.getheaders()
            }
            # Provider status is authoritative even when an error response uses
            # an unsupported encoding or contains a malformed/oversized body.
            # Do not read or decode such bodies: they are neither useful nor
            # safe to retain, and doing so could mask retry metadata.
            _raise_for_status(HttpResponse(response.status, b"", response_headers))
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                compressed.extend(chunk)
                if len(compressed) > MAX_COMPRESSED_BYTES:
                    raise SteamDeclaredFactsError(
                        "PROVIDER_RESPONSE_INVALID", retryable=False
                    )
        except SteamDeclaredFactsError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException):
            raise SteamDeclaredFactsError(
                "PROVIDER_UNAVAILABLE", retryable=True
            ) from None
        finally:
            connection.close()
        if response is None:
            raise SteamDeclaredFactsError("PROVIDER_UNAVAILABLE", retryable=True)
        body = _decode_content(bytes(compressed), response_headers)
        return HttpResponse(response.status, body, response_headers)


@dataclass(frozen=True, slots=True)
class SteamDeclaredFactsRequestContext:
    country: str
    language: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.country, str)
            or _COUNTRY.fullmatch(self.country) is None
        ):
            raise ValueError("country must be uppercase ISO-like alpha-2")
        if self.language not in SUPPORTED_REQUEST_LANGUAGES:
            raise ValueError("unsupported Steam request language")


@dataclass(frozen=True, slots=True)
class DeclaredText:
    state: Literal["declared", "undeclared", "unknown"]
    text: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or self.state not in {
            "declared",
            "undeclared",
            "unknown",
        }:
            raise ValueError("invalid declared-text state")
        if (self.state == "declared") != (self.text is not None):
            raise ValueError("declared text and state disagree")
        if self.text is not None and (
            not self.text or len(self.text) > MAX_SANITIZED_TEXT_CHARS
        ):
            raise ValueError("declared text is invalid")


@dataclass(frozen=True, slots=True)
class RequirementDeclaration:
    platform: Literal["windows", "macos", "linux"]
    state: Literal["declared", "undeclared", "unknown"]
    minimum: str | None
    recommended: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.platform, str) or self.platform not in {
            "windows",
            "macos",
            "linux",
        }:
            raise ValueError("invalid requirement platform")
        has_text = self.minimum is not None or self.recommended is not None
        if (self.state == "declared") != has_text:
            raise ValueError("requirement declaration and state disagree")
        if not isinstance(self.state, str) or self.state not in {
            "declared",
            "undeclared",
            "unknown",
        }:
            raise ValueError("invalid requirement state")
        for value in (self.minimum, self.recommended):
            if value is not None and (
                not value or len(value) > MAX_SANITIZED_TEXT_CHARS
            ):
                raise ValueError("requirement text is invalid")


@dataclass(frozen=True, slots=True)
class PlatformDeclarations:
    state: Literal["declared", "unknown"]
    windows: bool | None
    macos: bool | None
    linux: bool | None

    def __post_init__(self) -> None:
        values = (self.windows, self.macos, self.linux)
        if self.state == "unknown" and any(value is not None for value in values):
            raise ValueError("unknown platform declarations carry values")
        if self.state == "declared" and any(
            not isinstance(value, bool) for value in values
        ):
            raise ValueError("declared platforms require booleans")
        if not isinstance(self.state, str) or self.state not in {"declared", "unknown"}:
            raise ValueError("invalid platform state")


@dataclass(frozen=True, slots=True, order=True)
class LanguageDeclaration:
    code: str
    full_audio: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or self.code not in set(_LANGUAGE_NAMES.values())
            or not isinstance(self.full_audio, bool)
        ):
            raise ValueError("invalid language declaration")


@dataclass(frozen=True, slots=True)
class LanguageDeclarations:
    state: Literal["declared", "undeclared", "unknown"]
    items: tuple[LanguageDeclaration, ...]
    unrecognized_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or self.state not in {
            "declared",
            "undeclared",
            "unknown",
        }:
            raise ValueError("invalid language state")
        if self.state != "declared" and self.items:
            raise ValueError("non-declared languages carry items")
        if self.state == "declared" and not self.items and not self.unrecognized_count:
            raise ValueError("declared language evidence is empty")
        if (
            not isinstance(self.unrecognized_count, int)
            or isinstance(self.unrecognized_count, bool)
            or self.unrecognized_count < 0
        ):
            raise ValueError("invalid unrecognized language count")
        if tuple(sorted(self.items)) != self.items or len(
            {item.code for item in self.items}
        ) != len(self.items):
            raise ValueError("language declarations must be unique and sorted")


@dataclass(frozen=True, slots=True)
class CategoryDeclarations:
    state: Literal["declared", "undeclared", "unknown"]
    known_slugs: tuple[str, ...]
    unknown_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or self.state not in {
            "declared",
            "undeclared",
            "unknown",
        }:
            raise ValueError("invalid category state")
        if self.state != "declared" and (self.known_slugs or self.unknown_ids):
            raise ValueError("non-declared categories carry values")
        if self.state == "declared" and not (self.known_slugs or self.unknown_ids):
            raise ValueError("declared categories are empty")
        if tuple(sorted(set(self.known_slugs))) != self.known_slugs:
            raise ValueError("known categories must be unique and sorted")
        if tuple(sorted(set(self.unknown_ids))) != self.unknown_ids:
            raise ValueError("unknown category IDs must be unique and sorted")


@dataclass(frozen=True, slots=True, order=True)
class GenreDeclaration:
    id: int
    localized_label: str

    def __post_init__(self) -> None:
        _validate_source_id(self.id, "genre")
        object.__setattr__(
            self,
            "localized_label",
            _localized_label(self.localized_label, name="genre label"),
        )


@dataclass(frozen=True, slots=True)
class GenreDeclarations:
    state: Literal["declared", "undeclared", "unknown"]
    source: Literal["steam_store_appdetails"] | None
    items: tuple[GenreDeclaration, ...]

    def __post_init__(self) -> None:
        if self.state not in {"declared", "undeclared", "unknown"}:
            raise ValueError("invalid genre state")
        if self.state == "unknown":
            if self.source is not None or self.items:
                raise ValueError("unknown genres cannot claim provider evidence")
        elif self.source != "steam_store_appdetails":
            raise ValueError("known genre state requires an exact source")
        if self.state != "declared" and self.items:
            raise ValueError("non-declared genres carry items")
        if self.state == "declared" and not self.items:
            raise ValueError("declared genres are empty")
        if len(self.items) > MAX_DECLARATION_ITEMS:
            raise ValueError("genre declarations exceed the supported bound")
        if tuple(sorted(self.items)) != self.items or len(
            {item.id for item in self.items}
        ) != len(self.items):
            raise ValueError("genre declarations must be unique and sorted")


@dataclass(frozen=True, slots=True)
class ComingSoonDeclaration:
    state: Literal["present", "absent", "unknown"]
    localized_date_display: str | None

    def __post_init__(self) -> None:
        if self.state not in {"present", "absent", "unknown"}:
            raise ValueError("invalid coming-soon state")
        if self.localized_date_display is not None:
            object.__setattr__(
                self,
                "localized_date_display",
                _localized_label(
                    self.localized_date_display, name="localized date display"
                ),
            )


@dataclass(frozen=True, slots=True)
class DeclaredDiscoveryFacts:
    """Safe discovery view shared by 0.1 and 0.2 readers.

    ``multiplayer_modes`` contains positive category matches only.  An empty
    tuple never means single-player, and category evidence never implies a
    player-count range.
    """

    category_state: Literal["declared", "undeclared", "unknown"]
    category_source: Literal["steam_store_appdetails"] | None
    category_ids: tuple[int, ...]
    multiplayer_modes: tuple[str, ...]
    genres: GenreDeclarations
    coming_soon: ComingSoonDeclaration


@dataclass(frozen=True, slots=True)
class SteamDeclaredFactsHumanReference:
    appid: int
    country: str
    language: str
    url: str
    access_mode: Literal["manual_only"] = "manual_only"
    automation_supported: Literal[False] = False

    def __post_init__(self) -> None:
        _validate_appid(self.appid)
        SteamDeclaredFactsRequestContext(self.country, self.language)
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError:
            raise ValueError("invalid Steam human-reference URL") from None
        query = urlencode({"cc": self.country, "l": self.language})
        if (
            parsed.scheme != "https"
            or parsed.hostname != STEAM_STORE_HOST
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.path != f"/app/{self.appid}/"
            or parsed.query != query
            or parsed.fragment
            or self.access_mode != "manual_only"
            or self.automation_supported is not False
        ):
            raise ValueError("invalid Steam human-reference URL")


@dataclass(frozen=True, slots=True)
class SteamDeclaredFacts:
    appid: int
    request_context: SteamDeclaredFactsRequestContext
    platforms: PlatformDeclarations
    requirements: tuple[RequirementDeclaration, ...]
    languages: LanguageDeclarations
    categories: CategoryDeclarations
    category_source_ids: tuple[int, ...]
    genres: GenreDeclarations
    coming_soon: ComingSoonDeclaration
    controller_support: Literal["full", "partial"] | None
    external_account_notice: DeclaredText
    drm_notice: DeclaredText
    source_locator: Literal["steam_store_appdetails"]
    support_level: Literal["provisional"]
    human_reference: SteamDeclaredFactsHumanReference

    def __post_init__(self) -> None:
        _validate_appid(self.appid)
        if (
            len(self.category_source_ids) > MAX_DECLARATION_ITEMS
            or tuple(sorted(set(self.category_source_ids))) != self.category_source_ids
        ):
            raise ValueError("source category IDs must be bounded, unique, and sorted")
        for identifier in self.category_source_ids:
            _validate_source_id(identifier, "category")
        if (
            self.categories.state == "declared"
            and self.category_source_ids
            and set(self.category_source_ids)
            != {
                *(
                    identifier
                    for identifier, slug in CATEGORY_SLUGS.items()
                    if slug in self.categories.known_slugs
                ),
                *self.categories.unknown_ids,
            }
        ):
            raise ValueError("source category IDs disagree with declarations")
        if self.categories.state != "declared" and self.category_source_ids:
            raise ValueError("non-declared categories carry source IDs")
        if tuple(item.platform for item in self.requirements) != (
            "linux",
            "macos",
            "windows",
        ):
            raise ValueError("requirements must contain each platform in order")
        if self.controller_support is not None and (
            not isinstance(self.controller_support, str)
            or self.controller_support not in {"full", "partial"}
        ):
            raise ValueError("invalid controller support")
        if (
            self.source_locator != "steam_store_appdetails"
            or self.support_level != "provisional"
            or self.human_reference.appid != self.appid
            or self.human_reference.country != self.request_context.country
            or self.human_reference.language != self.request_context.language
        ):
            raise ValueError("declared-fact provenance disagrees")


@dataclass(frozen=True, slots=True)
class SteamDeclaredFactsResult:
    appid: int
    state: Literal["ready", "not_found"]
    request_context: SteamDeclaredFactsRequestContext
    facts: SteamDeclaredFacts | None

    def __post_init__(self) -> None:
        _validate_appid(self.appid)
        if (
            not isinstance(self.state, str)
            or self.state not in {"ready", "not_found"}
            or (self.state == "ready") != (self.facts is not None)
        ):
            raise ValueError("declared-fact result is invalid")


class SteamDeclaredFactsClient:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ValueError("timeout must be positive")
        self._transport = transport or FixedHttpsTransport()
        self._timeout = float(timeout)

    def fetch(
        self, appid: int, *, country: str, language: str
    ) -> SteamDeclaredFactsResult:
        _validate_appid(appid)
        context = SteamDeclaredFactsRequestContext(country, language)
        path = f"{APP_DETAILS_PATH}?{urlencode({'appids': appid, 'cc': country, 'l': language})}"
        response = self._transport.request(
            host=STEAM_STORE_HOST,
            path=path,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "steam-agent/0.1 (+https://github.com/aurokin/steam-cli)",
            },
            timeout=self._timeout,
        )
        _raise_for_status(response)
        media_type = _header(response.headers, "content-type")
        if (
            media_type is None
            or media_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise SteamDeclaredFactsError("PROVIDER_RESPONSE_INVALID", retryable=False)
        payload = _load_json(response.body)
        key = str(appid)
        if not isinstance(payload, dict) or tuple(payload) != (key,):
            raise SteamDeclaredFactsError("PROVIDER_RESPONSE_INVALID", retryable=False)
        envelope = payload[key]
        if not isinstance(envelope, dict) or not isinstance(
            envelope.get("success"), bool
        ):
            raise SteamDeclaredFactsError("PROVIDER_RESPONSE_INVALID", retryable=False)
        if envelope["success"] is False:
            return SteamDeclaredFactsResult(appid, "not_found", context, None)
        data = envelope.get("data")
        if not isinstance(data, dict) or data.get("steam_appid") != appid:
            raise SteamDeclaredFactsError("PROVIDER_RESPONSE_INVALID", retryable=False)
        facts = _interpret_facts(appid, context, data)
        return SteamDeclaredFactsResult(appid, "ready", context, facts)


def declared_facts_payload(facts: SteamDeclaredFacts) -> Mapping[str, Any]:
    """Return the complete normalized-only persistence payload."""

    return _declared_facts_payload(facts, schema_id="declared-app-facts/0.2")


def _declared_facts_payload(
    facts: SteamDeclaredFacts,
    *,
    schema_id: Literal["declared-app-facts/0.1", "declared-app-facts/0.2"],
) -> Mapping[str, Any]:
    """Serialize an exact schema without silently upgrading stored 0.1 facts."""

    payload: dict[str, Any] = {
        "schema_id": schema_id,
        "appid": facts.appid,
        "context": {
            "country": facts.request_context.country,
            "language": facts.request_context.language,
        },
        "platforms": {
            "state": facts.platforms.state,
            "windows": facts.platforms.windows,
            "macos": facts.platforms.macos,
            "linux": facts.platforms.linux,
        },
        "requirements": [
            {
                "platform": item.platform,
                "state": item.state,
                "minimum": item.minimum,
                "recommended": item.recommended,
            }
            for item in facts.requirements
        ],
        "languages": {
            "state": facts.languages.state,
            "items": [
                {"code": item.code, "full_audio": item.full_audio}
                for item in facts.languages.items
            ],
            "unrecognized_count": facts.languages.unrecognized_count,
        },
        "categories": {
            "state": facts.categories.state,
            "known_slugs": list(facts.categories.known_slugs),
            "unknown_ids": list(facts.categories.unknown_ids),
        },
        "controller_support": facts.controller_support,
        "external_account_notice": {
            "state": facts.external_account_notice.state,
            "text": facts.external_account_notice.text,
        },
        "drm_notice": {
            "state": facts.drm_notice.state,
            "text": facts.drm_notice.text,
        },
        "source": {
            "provider": "steam_store",
            "support_level": facts.support_level,
            "source_locator": facts.source_locator,
            "human_reference_url": facts.human_reference.url,
            "access_mode": facts.human_reference.access_mode,
            "automation_supported": facts.human_reference.automation_supported,
        },
    }
    if schema_id == "declared-app-facts/0.2":
        payload["categories"].update(
            {
                "source": (
                    "steam_store_appdetails"
                    if facts.categories.state != "unknown"
                    else None
                ),
                "numeric_ids": list(facts.category_source_ids),
            }
        )
        payload["genres"] = {
            "state": facts.genres.state,
            "source": facts.genres.source,
            "items": [
                {"id": item.id, "localized_label": item.localized_label}
                for item in facts.genres.items
            ],
        }
        payload["coming_soon"] = {
            "state": facts.coming_soon.state,
            "localized_date_display": facts.coming_soon.localized_date_display,
        }
    return payload


def validate_declared_facts_payload(
    payload: Mapping[str, Any],
    *,
    appid: int | None = None,
    country: str | None = None,
    language: str | None = None,
) -> Mapping[str, Any]:
    """Validate and canonicalize a normalized persistence payload."""

    if not isinstance(payload, Mapping):
        raise ValueError("declared-app payload fields are invalid")
    schema_id = payload.get("schema_id")
    common_fields = {
        "schema_id",
        "appid",
        "context",
        "platforms",
        "requirements",
        "languages",
        "categories",
        "controller_support",
        "external_account_notice",
        "drm_notice",
        "source",
    }
    expected_fields = (
        common_fields
        if schema_id == "declared-app-facts/0.1"
        else common_fields | {"genres", "coming_soon"}
    )
    if (
        schema_id
        not in {
            "declared-app-facts/0.1",
            "declared-app-facts/0.2",
        }
        or set(payload) != expected_fields
    ):
        raise ValueError("declared-app payload fields are invalid")
    payload_appid = payload["appid"]
    _validate_appid(payload_appid)
    if appid is not None and payload_appid != appid:
        raise ValueError("declared-app payload AppID disagrees")
    context = _strict_mapping(payload["context"], {"country", "language"})
    request_context = SteamDeclaredFactsRequestContext(
        context["country"], context["language"]
    )
    if country is not None and request_context.country != country:
        raise ValueError("declared-app payload country disagrees")
    if language is not None and request_context.language != language:
        raise ValueError("declared-app payload language disagrees")
    platform = _strict_mapping(
        payload["platforms"], {"state", "windows", "macos", "linux"}
    )
    platforms = PlatformDeclarations(
        platform["state"], platform["windows"], platform["macos"], platform["linux"]
    )
    raw_requirements = payload["requirements"]
    if not isinstance(raw_requirements, (list, tuple)):
        raise ValueError("declared-app requirements are invalid")
    requirements = tuple(
        RequirementDeclaration(
            item["platform"], item["state"], item["minimum"], item["recommended"]
        )
        for raw_item in raw_requirements
        for item in [
            _strict_mapping(raw_item, {"platform", "state", "minimum", "recommended"})
        ]
    )
    raw_languages = _strict_mapping(
        payload["languages"], {"state", "items", "unrecognized_count"}
    )
    language_items = raw_languages["items"]
    if not isinstance(language_items, (list, tuple)):
        raise ValueError("declared-app languages are invalid")
    languages = LanguageDeclarations(
        raw_languages["state"],
        tuple(
            LanguageDeclaration(item["code"], item["full_audio"])
            for raw_item in language_items
            for item in [_strict_mapping(raw_item, {"code", "full_audio"})]
        ),
        raw_languages["unrecognized_count"],
    )
    category_fields = {"state", "known_slugs", "unknown_ids"}
    if schema_id == "declared-app-facts/0.2":
        category_fields |= {"source", "numeric_ids"}
    raw_categories = _strict_mapping(payload["categories"], category_fields)
    if not isinstance(raw_categories["known_slugs"], (list, tuple)) or not isinstance(
        raw_categories["unknown_ids"], (list, tuple)
    ):
        raise ValueError("declared-app categories are invalid")
    categories = CategoryDeclarations(
        raw_categories["state"],
        tuple(raw_categories["known_slugs"]),
        tuple(raw_categories["unknown_ids"]),
    )
    if schema_id == "declared-app-facts/0.2":
        raw_source_ids = raw_categories["numeric_ids"]
        if not isinstance(raw_source_ids, (list, tuple)):
            raise ValueError("declared-app source category IDs are invalid")
        category_source_ids = tuple(raw_source_ids)
        expected_category_source = (
            "steam_store_appdetails" if categories.state != "unknown" else None
        )
        if raw_categories["source"] != expected_category_source:
            raise ValueError("declared-app category source is invalid")
        if categories.state == "declared" and not category_source_ids:
            raise ValueError("declared-app source category IDs are missing")
        raw_genres = _strict_mapping(payload["genres"], {"state", "source", "items"})
        if not isinstance(raw_genres["items"], (list, tuple)):
            raise ValueError("declared-app genres are invalid")
        genres = GenreDeclarations(
            raw_genres["state"],
            raw_genres["source"],
            tuple(
                GenreDeclaration(item["id"], item["localized_label"])
                for raw_item in raw_genres["items"]
                for item in [_strict_mapping(raw_item, {"id", "localized_label"})]
            ),
        )
        raw_coming_soon = _strict_mapping(
            payload["coming_soon"], {"state", "localized_date_display"}
        )
        coming_soon = ComingSoonDeclaration(
            raw_coming_soon["state"], raw_coming_soon["localized_date_display"]
        )
    else:
        category_source_ids = ()
        genres = GenreDeclarations("unknown", None, ())
        coming_soon = ComingSoonDeclaration("unknown", None)
    external = _strict_mapping(payload["external_account_notice"], {"state", "text"})
    drm = _strict_mapping(payload["drm_notice"], {"state", "text"})
    source = _strict_mapping(
        payload["source"],
        {
            "provider",
            "support_level",
            "source_locator",
            "human_reference_url",
            "access_mode",
            "automation_supported",
        },
    )
    if (
        source["provider"] != "steam_store"
        or source["support_level"] != "provisional"
        or source["source_locator"] != "steam_store_appdetails"
        or source["access_mode"] != "manual_only"
        or source["automation_supported"] is not False
    ):
        raise ValueError("declared-app source is invalid")
    facts = SteamDeclaredFacts(
        appid=payload_appid,
        request_context=request_context,
        platforms=platforms,
        requirements=requirements,
        languages=languages,
        categories=categories,
        category_source_ids=category_source_ids,
        genres=genres,
        coming_soon=coming_soon,
        controller_support=payload["controller_support"],
        external_account_notice=DeclaredText(external["state"], external["text"]),
        drm_notice=DeclaredText(drm["state"], drm["text"]),
        source_locator=source["source_locator"],
        support_level=source["support_level"],
        human_reference=SteamDeclaredFactsHumanReference(
            payload_appid,
            request_context.country,
            request_context.language,
            source["human_reference_url"],
            source["access_mode"],
            source["automation_supported"],
        ),
    )
    return _declared_facts_payload(facts, schema_id=schema_id)


def declared_discovery_facts(payload: Mapping[str, Any]) -> DeclaredDiscoveryFacts:
    """Return positive-only discovery evidence from either supported schema."""

    canonical = validate_declared_facts_payload(payload)
    if canonical["schema_id"] == "declared-app-facts/0.1":
        return DeclaredDiscoveryFacts(
            "unknown",
            None,
            (),
            (),
            GenreDeclarations("unknown", None, ()),
            ComingSoonDeclaration("unknown", None),
        )
    categories = canonical["categories"]
    assert isinstance(categories, Mapping)
    identifiers = tuple(categories["numeric_ids"])
    genres_payload = canonical["genres"]
    assert isinstance(genres_payload, Mapping)
    genres = GenreDeclarations(
        genres_payload["state"],
        genres_payload["source"],
        tuple(
            GenreDeclaration(item["id"], item["localized_label"])
            for item in genres_payload["items"]
        ),
    )
    coming_payload = canonical["coming_soon"]
    assert isinstance(coming_payload, Mapping)
    return DeclaredDiscoveryFacts(
        categories["state"],
        categories["source"],
        identifiers,
        tuple(
            MULTIPLAYER_CATEGORY_SLUGS[identifier]
            for identifier in identifiers
            if identifier in MULTIPLAYER_CATEGORY_SLUGS
        ),
        genres,
        ComingSoonDeclaration(
            coming_payload["state"], coming_payload["localized_date_display"]
        ),
    )


def _strict_mapping(value: object, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("declared-app nested payload is invalid")
    return value


def _validate_source_id(value: object, kind: str) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_UNSIGNED_32
    ):
        raise ValueError(f"invalid {kind} ID")


def _localized_label(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > MAX_LOCALIZED_LABEL_CHARS
        or len(normalized.encode("utf-8")) > 1_024
        or any(not character.isprintable() for character in normalized)
    ):
        raise ValueError(f"{name} is invalid")
    return normalized


def _interpret_facts(
    appid: int, context: SteamDeclaredFactsRequestContext, data: Mapping[str, object]
) -> SteamDeclaredFacts:
    controller = data.get("controller_support")
    if controller is not None and (
        not isinstance(controller, str) or controller not in {"full", "partial"}
    ):
        raise _invalid()
    requirements = tuple(
        sorted(
            (
                _requirements("windows", data.get("pc_requirements", _MISSING)),
                _requirements("macos", data.get("mac_requirements", _MISSING)),
                _requirements("linux", data.get("linux_requirements", _MISSING)),
            ),
            key=lambda item: item.platform,
        )
    )
    categories, category_source_ids = _categories(data.get("categories", _MISSING))
    query = urlencode({"cc": context.country, "l": context.language})
    return SteamDeclaredFacts(
        appid=appid,
        request_context=context,
        platforms=_platforms(data.get("platforms", _MISSING)),
        requirements=requirements,
        languages=_languages(data.get("supported_languages", _MISSING)),
        categories=categories,
        category_source_ids=category_source_ids,
        genres=_genres(data.get("genres", _MISSING)),
        coming_soon=_coming_soon(data.get("release_date", _MISSING)),
        controller_support=controller,
        external_account_notice=_declared_text(
            data.get("ext_user_account_notice", _MISSING)
        ),
        drm_notice=_declared_text(data.get("drm_notice", _MISSING)),
        source_locator="steam_store_appdetails",
        support_level="provisional",
        human_reference=SteamDeclaredFactsHumanReference(
            appid,
            context.country,
            context.language,
            f"https://{STEAM_STORE_HOST}/app/{appid}/?{query}",
        ),
    )


def _platforms(value: object) -> PlatformDeclarations:
    if value is _MISSING:
        return PlatformDeclarations("unknown", None, None, None)
    if not isinstance(value, dict):
        raise _invalid()
    recognized = []
    for key in ("windows", "mac", "linux"):
        item = value.get(key)
        if not isinstance(item, bool):
            raise _invalid()
        recognized.append(item)
    return PlatformDeclarations("declared", recognized[0], recognized[1], recognized[2])


def _requirements(
    platform: Literal["windows", "macos", "linux"], value: object
) -> RequirementDeclaration:
    if value is _MISSING:
        return RequirementDeclaration(platform, "unknown", None, None)
    if value is None or value == [] or value == {}:
        return RequirementDeclaration(platform, "undeclared", None, None)
    if not isinstance(value, dict):
        raise _invalid()
    minimum = _optional_sanitized(value.get("minimum", _MISSING))
    recommended = _optional_sanitized(value.get("recommended", _MISSING))
    state = (
        "declared" if minimum is not None or recommended is not None else "undeclared"
    )
    return RequirementDeclaration(platform, state, minimum, recommended)


def _declared_text(value: object) -> DeclaredText:
    if value is _MISSING:
        return DeclaredText("unknown", None)
    if value is None or value == "":
        return DeclaredText("undeclared", None)
    if not isinstance(value, str):
        raise _invalid()
    text = sanitize_html(value)
    return DeclaredText("declared", text) if text else DeclaredText("undeclared", None)


def _optional_sanitized(value: object) -> str | None:
    if value is _MISSING or value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _invalid()
    return sanitize_html(value) or None


def _categories(value: object) -> tuple[CategoryDeclarations, tuple[int, ...]]:
    if value is _MISSING:
        return CategoryDeclarations("unknown", (), ()), ()
    if value is None or value == []:
        return CategoryDeclarations("undeclared", (), ()), ()
    if not isinstance(value, list):
        raise _invalid()
    if len(value) > MAX_DECLARATION_ITEMS:
        raise _invalid()
    identifiers: list[int] = []
    for item in value:
        if not isinstance(item, dict):
            raise _invalid()
        identifier = item.get("id")
        description = item.get("description")
        if (
            not isinstance(identifier, int)
            or isinstance(identifier, bool)
            or not 1 <= identifier <= MAX_UNSIGNED_32
            or not isinstance(description, str)
            or len(description.encode("utf-8")) > 1_024
        ):
            raise _invalid()
        identifiers.append(identifier)
    if len(set(identifiers)) != len(identifiers):
        raise _invalid()
    known = tuple(
        sorted(CATEGORY_SLUGS[item] for item in identifiers if item in CATEGORY_SLUGS)
    )
    unknown = tuple(sorted(item for item in identifiers if item not in CATEGORY_SLUGS))
    source_ids = tuple(sorted(identifiers))
    return CategoryDeclarations("declared", known, unknown), source_ids


def _genres(value: object) -> GenreDeclarations:
    if value is _MISSING:
        return GenreDeclarations("unknown", None, ())
    if value is None or value == []:
        return GenreDeclarations("undeclared", "steam_store_appdetails", ())
    if not isinstance(value, list) or len(value) > MAX_DECLARATION_ITEMS:
        raise _invalid()
    declarations: list[GenreDeclaration] = []
    for raw_item in value:
        if not isinstance(raw_item, dict):
            raise _invalid()
        identifier = raw_item.get("id")
        # appdetails currently encodes genre IDs as canonical decimal strings.
        if (
            not isinstance(identifier, str)
            or not identifier.isascii()
            or not identifier.isdecimal()
            or identifier != str(int(identifier))
        ):
            raise _invalid()
        try:
            declaration = GenreDeclaration(
                int(identifier),
                raw_item.get("description"),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError):
            raise _invalid() from None
        declarations.append(declaration)
    if len({item.id for item in declarations}) != len(declarations):
        raise _invalid()
    return GenreDeclarations(
        "declared", "steam_store_appdetails", tuple(sorted(declarations))
    )


def _coming_soon(value: object) -> ComingSoonDeclaration:
    if value is _MISSING:
        return ComingSoonDeclaration("unknown", None)
    if value is None:
        return ComingSoonDeclaration("unknown", None)
    if not isinstance(value, dict):
        raise _invalid()
    raw_state = value.get("coming_soon", _MISSING)
    if raw_state is _MISSING:
        state: Literal["present", "absent", "unknown"] = "unknown"
    elif isinstance(raw_state, bool):
        state = "present" if raw_state else "absent"
    else:
        raise _invalid()
    raw_date = value.get("date", _MISSING)
    if raw_date is _MISSING or raw_date is None or raw_date == "":
        date = None
    else:
        try:
            date = _localized_label(raw_date, name="localized date display")
        except (TypeError, ValueError):
            raise _invalid() from None
    return ComingSoonDeclaration(state, date)


def _languages(value: object) -> LanguageDeclarations:
    if value is _MISSING:
        return LanguageDeclarations("unknown", (), 0)
    if value is None or value == "":
        return LanguageDeclarations("undeclared", (), 0)
    if not isinstance(value, str):
        raise _invalid()
    parser = _LanguageParser()
    _feed_parser(parser, value)
    declarations: dict[str, bool] = {}
    unrecognized = 0
    for raw_item in parser.items:
        item = " ".join(raw_item.split()).strip()
        full_audio = item.endswith("*")
        name = item[:-1].strip() if full_audio else item
        code = _LANGUAGE_NAMES.get(name.casefold())
        if code is None:
            unrecognized += 1
            continue
        if code in declarations:
            raise _invalid()
        declarations[code] = full_audio
    if not declarations and not unrecognized:
        return LanguageDeclarations("undeclared", (), 0)
    items = tuple(
        sorted(
            (
                LanguageDeclaration(code, full_audio)
                for code, full_audio in declarations.items()
            )
        )
    )
    return LanguageDeclarations("declared", items, unrecognized)


class _BoundedHTMLParser(HTMLParser):
    _BREAK_TAGS = frozenset({"br", "div", "li", "ol", "p", "tr", "ul"})
    _SKIP_TAGS = frozenset({"script", "style", "template"})
    _VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.tokens = 0
        self.open_tags: list[str] = []
        self.skip_depth = 0
        self.characters = 0

    def _token(self) -> None:
        self.tokens += 1
        if self.tokens > MAX_HTML_TOKENS:
            raise _invalid()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._token()
        lowered = tag.casefold()
        if lowered not in self._VOID_TAGS:
            self.open_tags.append(lowered)
            if len(self.open_tags) > MAX_HTML_DEPTH:
                raise _invalid()
        if lowered in self._SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and lowered in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._token()
        lowered = tag.casefold()
        # In HTML, a slash does not make script/style/template void.  Treating
        # one as closed here could expose the following hidden text as trusted
        # requirement prose, so reject the malformed provider field.
        if lowered in self._SKIP_TAGS:
            raise _invalid()
        if not self.skip_depth and lowered in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        self._token()
        lowered = tag.casefold()
        if lowered in self._VOID_TAGS:
            if not self.skip_depth and lowered in self._BREAK_TAGS:
                self.parts.append("\n")
            return
        if not self.open_tags or self.open_tags[-1] != lowered:
            raise _invalid()
        self.open_tags.pop()
        if lowered in self._SKIP_TAGS:
            self.skip_depth -= 1
        elif not self.skip_depth and lowered in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._token()
        if self.skip_depth:
            return
        self.characters += len(data)
        if self.characters > MAX_SANITIZED_TEXT_CHARS * 2:
            raise _invalid()
        self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        self._token()


class _LanguageParser(_BoundedHTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.before_break = True

    @property
    def items(self) -> tuple[str, ...]:
        text = "".join(self.parts)
        return tuple(part for part in text.split(",") if part.strip())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "br":
            self.before_break = False
        if self.before_break:
            super().handle_starttag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "br":
            self.before_break = False
            return
        if self.before_break:
            super().handle_startendtag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.before_break:
            super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.before_break:
            super().handle_data(data)

    def handle_comment(self, data: str) -> None:
        if self.before_break:
            super().handle_comment(data)


def sanitize_html(value: str) -> str:
    if not isinstance(value, str):
        raise _invalid()
    try:
        encoded_length = len(value.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise _invalid() from None
    if encoded_length > MAX_HTML_FIELD_BYTES:
        raise _invalid()
    parser = _BoundedHTMLParser()
    _feed_parser(parser, value)
    text = unicodedata.normalize("NFC", "".join(parser.parts))
    text = "".join(
        character if character in {"\n", "\t"} or ord(character) >= 32 else " "
        for character in text
    )
    lines = [" ".join(line.split()) for line in text.replace("\r", "\n").split("\n")]
    compact: list[str] = []
    for line in lines:
        if line:
            compact.append(line)
        elif compact and compact[-1] != "":
            compact.append("")
    result = "\n".join(compact).strip()
    if len(result) > MAX_SANITIZED_TEXT_CHARS:
        raise _invalid()
    return result


def _feed_parser(parser: HTMLParser, value: str) -> None:
    try:
        parser.feed(value)
        parser.close()
    except (RecursionError, UnicodeError, ValueError, AssertionError):
        raise _invalid() from None


def _load_json(body: bytes) -> object:
    if len(body) > MAX_DECOMPRESSED_BYTES:
        raise _invalid()

    def reject_constant(_: str) -> object:
        raise ValueError

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        text = body.decode("utf-8", errors="strict")
        value = json.loads(
            text, object_pairs_hook=unique_object, parse_constant=reject_constant
        )
        _validate_json_unicode(value)
        return value
    except (UnicodeError, ValueError, RecursionError):
        raise _invalid() from None


def _validate_json_unicode(value: object) -> None:
    """Reject JSON strings that cannot be represented as valid UTF-8."""
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_unicode(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8", errors="strict")
            _validate_json_unicode(item)


def _decode_content(body: bytes, headers: Mapping[str, str]) -> bytes:
    encoding = (_header(headers, "content-encoding") or "identity").casefold().strip()
    if encoding in {"", "identity"}:
        if len(body) > MAX_DECOMPRESSED_BYTES:
            raise _invalid()
        return body
    if encoding != "gzip":
        raise _invalid()
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(body, MAX_DECOMPRESSED_BYTES + 1)
        if len(decoded) > MAX_DECOMPRESSED_BYTES or decoder.unconsumed_tail:
            raise _invalid()
        decoded += decoder.flush(MAX_DECOMPRESSED_BYTES + 1 - len(decoded))
        if (
            len(decoded) > MAX_DECOMPRESSED_BYTES
            or not decoder.eof
            or decoder.unused_data
        ):
            raise _invalid()
        return decoded
    except zlib.error:
        raise _invalid() from None


def _raise_for_status(response: HttpResponse) -> None:
    if response.status == 429:
        raise SteamDeclaredFactsError(
            "RATE_LIMITED",
            retryable=True,
            retry_after_seconds=_retry_after(response.headers),
        )
    if response.status in {408, 425} or response.status >= 500:
        raise SteamDeclaredFactsError(
            "PROVIDER_UNAVAILABLE",
            retryable=True,
            retry_after_seconds=_retry_after(response.headers),
        )
    if response.status != 200:
        raise _invalid()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == lowered), None
    )


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = _header(headers, "retry-after")
    if value is None or not value.isascii() or not value.isdecimal() or len(value) > 5:
        return None
    seconds = int(value)
    return seconds if 0 <= seconds <= 86_400 else None


def _validate_appid(appid: object) -> None:
    if (
        not isinstance(appid, int)
        or isinstance(appid, bool)
        or not 1 <= appid <= MAX_UNSIGNED_32
    ):
        raise ValueError("appid must be a positive unsigned 32-bit integer")


def _invalid() -> SteamDeclaredFactsError:
    return SteamDeclaredFactsError("PROVIDER_RESPONSE_INVALID", retryable=False)


__all__ = [
    "APP_DETAILS_PATH",
    "CATEGORY_SLUGS",
    "CategoryDeclarations",
    "DeclaredText",
    "FixedHttpsTransport",
    "HttpResponse",
    "LanguageDeclaration",
    "LanguageDeclarations",
    "PlatformDeclarations",
    "RequirementDeclaration",
    "STEAM_STORE_HOST",
    "SUPPORTED_REQUEST_LANGUAGES",
    "SteamDeclaredFacts",
    "SteamDeclaredFactsClient",
    "SteamDeclaredFactsError",
    "SteamDeclaredFactsHumanReference",
    "SteamDeclaredFactsRequestContext",
    "SteamDeclaredFactsResult",
    "declared_facts_payload",
    "sanitize_html",
    "validate_declared_facts_payload",
]

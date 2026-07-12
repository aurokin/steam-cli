from __future__ import annotations

from dataclasses import replace
import gzip
import json
from pathlib import Path
from typing import Mapping

import pytest

import steam_agent.steam_declared_facts as subject
from steam_agent.steam_declared_facts import (
    CATEGORY_SLUGS,
    CategoryDeclarations,
    DeclaredText,
    HttpResponse,
    LanguageDeclaration,
    LanguageDeclarations,
    PlatformDeclarations,
    RequirementDeclaration,
    SteamDeclaredFactsClient,
    SteamDeclaredFactsError,
    SteamDeclaredFactsHumanReference,
    SteamDeclaredFactsRequestContext,
    sanitize_html,
)


FIXTURES = Path(__file__).parent / "fixtures" / "steam_declared_facts"
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[Mapping[str, object]] = []

    def request(self, **kwargs: object) -> HttpResponse:
        self.calls.append(kwargs)
        return self.response


def response_fixture(name: str) -> HttpResponse:
    return HttpResponse(200, (FIXTURES / name).read_bytes(), JSON_HEADERS)


def client_for(response: HttpResponse) -> tuple[SteamDeclaredFactsClient, FakeTransport]:
    transport = FakeTransport(response)
    return SteamDeclaredFactsClient(transport=transport), transport


def test_legacy_public_shape_is_reduced_to_allowlisted_facts() -> None:
    client, transport = client_for(response_fixture("legacy_shape.json"))

    result = client.fetch(400, country="US", language="english")

    assert result.state == "ready"
    assert result.facts is not None
    facts = result.facts
    assert facts.platforms == PlatformDeclarations("declared", True, False, True)
    assert facts.controller_support == "full"
    assert facts.categories == CategoryDeclarations(
        "declared", ("camera_comfort", "full_controller_support"), (9001,)
    )
    assert facts.languages.items == (
        LanguageDeclaration("english", True),
        LanguageDeclaration("french", False),
    )
    assert facts.languages.unrecognized_count == 0
    requirements = {item.platform: item for item in facts.requirements}
    assert requirements["linux"].state == "undeclared"
    assert requirements["windows"].minimum == (
        "Minimum: 512MB RAM\n\nRecommended: 1GB RAM"
    )
    assert "<" not in requirements["windows"].minimum
    assert facts.external_account_notice.state == "unknown"
    assert facts.drm_notice.state == "unknown"
    assert facts.human_reference.url == (
        "https://store.steampowered.com/app/400/?cc=US&l=english"
    )
    assert transport.calls == [
        {
            "host": "store.steampowered.com",
            "path": "/api/appdetails?appids=400&cc=US&l=english",
            "headers": {
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": (
                    "steam-agent/0.1 (+https://github.com/aurokin/steam-cli)"
                ),
            },
            "timeout": 15.0,
        }
    ]
    sent_headers = transport.calls[0]["headers"]
    assert isinstance(sent_headers, dict)
    assert not {"Authorization", "Cookie", "x-webapi-key"} & set(sent_headers)


def test_modern_shape_preserves_notice_and_empty_states_without_html() -> None:
    client, _ = client_for(response_fixture("list_shape.json"))

    facts = client.fetch(620, country="DE", language="english").facts

    assert facts is not None
    assert facts.categories.state == "undeclared"
    assert facts.external_account_notice.text == "Example account"
    assert facts.drm_notice.text == "Example DRM"
    assert facts.languages.items == (
        LanguageDeclaration("brazilian", False),
        LanguageDeclaration("koreana", True),
    )
    requirements = {item.platform: item for item in facts.requirements}
    assert requirements["macos"].state == "undeclared"
    assert requirements["linux"].state == "undeclared"
    assert requirements["windows"].minimum == "Minimum:\n\nMemory: 2 GB RAM"


def test_missing_optional_fields_remain_unknown() -> None:
    client, _ = client_for(response_fixture("missing_optional_shape.json"))

    facts = client.fetch(570, country="US", language="english").facts

    assert facts is not None
    assert facts.platforms == PlatformDeclarations("unknown", None, None, None)
    assert all(item.state == "unknown" for item in facts.requirements)
    assert facts.languages.state == "unknown"
    assert facts.controller_support is None
    assert facts.external_account_notice.state == "unknown"
    assert facts.categories.known_slugs == (
        "adjustable_difficulty",
        "custom_volume_controls",
    )


def test_not_found_is_typed_and_carries_no_facts() -> None:
    client, _ = client_for(
        HttpResponse(
            200,
            b'{"4294967294":{"success":false}}',
            JSON_HEADERS,
        )
    )

    result = client.fetch(4294967294, country="US", language="english")

    assert result.state == "not_found"
    assert result.facts is None


@pytest.mark.parametrize("appid", [0, -1, True, 1 << 32, "400"])
def test_appid_is_strict(appid: object) -> None:
    client, transport = client_for(response_fixture("legacy_shape.json"))
    with pytest.raises(ValueError, match="appid"):
        client.fetch(appid, country="US", language="english")  # type: ignore[arg-type]
    assert transport.calls == []


@pytest.mark.parametrize("country", ["us", "USA", "U1", "", True])
def test_country_is_strict(country: object) -> None:
    with pytest.raises(ValueError, match="country"):
        SteamDeclaredFactsRequestContext(country, "english")  # type: ignore[arg-type]


@pytest.mark.parametrize("language", ["en", "English", "", "newlang", True])
def test_language_is_strict(language: object) -> None:
    with pytest.raises(ValueError, match="language"):
        SteamDeclaredFactsRequestContext("US", language)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "PROVIDER_RESPONSE_INVALID", False),
        (302, "PROVIDER_RESPONSE_INVALID", False),
        (408, "PROVIDER_UNAVAILABLE", True),
        (425, "PROVIDER_UNAVAILABLE", True),
        (500, "PROVIDER_UNAVAILABLE", True),
        (503, "PROVIDER_UNAVAILABLE", True),
    ],
)
def test_http_statuses_are_typed(status: int, code: str, retryable: bool) -> None:
    client, _ = client_for(HttpResponse(status, b"null", {}))
    with pytest.raises(SteamDeclaredFactsError) as raised:
        client.fetch(400, country="US", language="english")
    assert (raised.value.code, raised.value.retryable) == (code, retryable)


def test_rate_limit_honors_bounded_retry_after_case_insensitively() -> None:
    client, _ = client_for(HttpResponse(429, b"", {"Retry-After": "45"}))
    with pytest.raises(SteamDeclaredFactsError) as raised:
        client.fetch(400, country="US", language="english")
    assert raised.value.code == "RATE_LIMITED"
    assert raised.value.retryable is True
    assert raised.value.retry_after_seconds == 45


@pytest.mark.parametrize(
    ("status", "code"), [(429, "RATE_LIMITED"), (503, "PROVIDER_UNAVAILABLE")]
)
def test_fixed_transport_classifies_retryable_status_before_reading_body(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    class ErrorResponse:
        def __init__(self) -> None:
            self.status = status
            self.read_called = False

        def getheaders(self) -> list[tuple[str, str]]:
            return [("Content-Encoding", "unsupported"), ("Retry-After", "17")]

        def read(self, size: int) -> bytes:
            self.read_called = True
            raise AssertionError("provider error bodies must not be read")

    response = ErrorResponse()

    class Connection:
        def __init__(self, host: str, *, timeout: float) -> None:
            pass

        def request(
            self, method: str, path: str, *, headers: Mapping[str, str]
        ) -> None:
            pass

        def getresponse(self) -> ErrorResponse:
            return response

        def close(self) -> None:
            pass

    monkeypatch.setattr(subject.http.client, "HTTPSConnection", Connection)

    with pytest.raises(SteamDeclaredFactsError) as raised:
        subject.FixedHttpsTransport().request(
            host=subject.STEAM_STORE_HOST,
            path="/api/appdetails?appids=400&cc=US&l=english",
            headers={},
            timeout=1.0,
        )

    assert raised.value.code == code
    assert raised.value.retry_after_seconds == 17
    assert response.read_called is False


@pytest.mark.parametrize("retry_after", ["-1", "86401", "1.5", "999999", "soon"])
def test_invalid_retry_after_is_not_retained(retry_after: str) -> None:
    client, _ = client_for(HttpResponse(429, b"", {"retry-after": retry_after}))
    with pytest.raises(SteamDeclaredFactsError) as raised:
        client.fetch(400, country="US", language="english")
    assert raised.value.retry_after_seconds is None


@pytest.mark.parametrize("content_type", [None, "text/html", "application/problem+json"])
def test_success_requires_json_content_type(content_type: str | None) -> None:
    headers = {} if content_type is None else {"content-type": content_type}
    client, _ = client_for(HttpResponse(200, b"{}", headers))
    with pytest.raises(SteamDeclaredFactsError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch(400, country="US", language="english")


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b'{"400":{"success":true},"400":{"success":false}}',
        b'{"400":{"success":true,"data":{"steam_appid":400,"steam_appid":401}}}',
        b'{"400":{"success":NaN}}',
        b'{"400":{"success":1}}',
        b'{"400":{"success":true,"data":{"steam_appid":401}}}',
        b'{"400":{"success":true,"data":[]}}',
        b'{"other":{"success":false}}',
        b'{"400":{"success":false},"extra":{}}',
        b'\xff',
    ],
)
def test_json_and_envelope_are_strict(body: bytes) -> None:
    client, _ = client_for(HttpResponse(200, body, JSON_HEADERS))
    with pytest.raises(SteamDeclaredFactsError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch(400, country="US", language="english")


@pytest.mark.parametrize(
    "field",
    [
        b'"minimum":"\\ud800"',
        b'"minimum":"\\udfff"',
        b'"\\ud800":"discarded"',
    ],
)
def test_escaped_lone_surrogates_are_typed_provider_errors(field: bytes) -> None:
    body = (
        b'{"400":{"success":true,"data":{"steam_appid":400,'
        b'"platforms":{"windows":true,"mac":false,"linux":true},'
        b'"pc_requirements":{'
        + field
        + b'},"mac_requirements":{},"linux_requirements":{},'
        b'"supported_languages":"English","categories":[]}}}'
    )
    client, _ = client_for(HttpResponse(200, body, JSON_HEADERS))

    with pytest.raises(SteamDeclaredFactsError) as raised:
        client.fetch(400, country="US", language="english")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert raised.value.retryable is False


def valid_data(**updates: object) -> bytes:
    data: dict[str, object] = {
        "steam_appid": 400,
        "platforms": {"windows": True, "mac": False, "linux": True},
        "pc_requirements": {},
        "mac_requirements": {},
        "linux_requirements": {},
        "supported_languages": "English",
        "categories": [],
    }
    data.update(updates)
    return json.dumps({"400": {"success": True, "data": data}}).encode()


@pytest.mark.parametrize(
    "updates",
    [
        {"platforms": []},
        {"platforms": {"windows": 1, "mac": False, "linux": True}},
        {"platforms": {"windows": True, "mac": False}},
        {"pc_requirements": "minimum"},
        {"pc_requirements": ["unexpected"]},
        {"pc_requirements": {"minimum": 2}},
        {"supported_languages": []},
        {"categories": {}},
        {"categories": [{"id": True, "description": "bad"}]},
        {"categories": [{"id": 64}]},
        {"categories": [{"id": 64, "description": "a"}, {"id": 64, "description": "b"}]},
        {"controller_support": "yes"},
        {"ext_user_account_notice": 1},
        {"drm_notice": []},
    ],
)
def test_normalized_field_shapes_are_strict(updates: Mapping[str, object]) -> None:
    client, _ = client_for(HttpResponse(200, valid_data(**updates), JSON_HEADERS))
    with pytest.raises(SteamDeclaredFactsError, match="PROVIDER_RESPONSE_INVALID"):
        client.fetch(400, country="US", language="english")


def test_unknown_fields_are_discarded_and_unknown_category_text_is_not_retained() -> None:
    body = valid_data(
        marketing_html="<script>secret</script>",
        platforms={"windows": True, "mac": False, "linux": True, "future": True},
        categories=[{"id": 9002, "description": "untrusted future label"}],
    )
    client, _ = client_for(HttpResponse(200, body, JSON_HEADERS))

    facts = client.fetch(400, country="US", language="english").facts

    assert facts is not None
    assert facts.categories.unknown_ids == (9002,)
    assert "untrusted" not in repr(facts)
    assert "secret" not in repr(facts)


def test_unrecognized_language_is_counted_but_not_retained() -> None:
    client, _ = client_for(
        HttpResponse(
            200,
            valid_data(supported_languages="English, Future<script>x</script> Language"),
            JSON_HEADERS,
        )
    )
    facts = client.fetch(400, country="US", language="english").facts
    assert facts is not None
    assert facts.languages.items == (LanguageDeclaration("english", False),)
    assert facts.languages.unrecognized_count == 1
    assert "Future" not in repr(facts.languages)


def test_sanitizer_drops_active_content_attributes_comments_and_controls() -> None:
    value = (
        '<p onclick="steal()">Safe &amp; sound</p><!--private-->'
        '<script>alert(1)</script><style>secret</style><img onerror="x">'
        "A\x00B<br>Next"
    )
    assert sanitize_html(value) == "Safe & sound\nA B\nNext"


def test_sanitizer_does_not_double_unescape_encoded_markup() -> None:
    assert sanitize_html("&lt;script&gt;kept as text&lt;/script&gt;") == (
        "<script>kept as text</script>"
    )


def test_sanitizer_void_elements_do_not_consume_nesting_depth() -> None:
    value = "<div>" * subject.MAX_HTML_DEPTH + "<img><input><br>Safe"
    value += "</div>" * subject.MAX_HTML_DEPTH

    assert sanitize_html(value) == "Safe"


def test_sanitizer_mismatched_hidden_close_cannot_expose_requirement_text() -> None:
    with pytest.raises(SteamDeclaredFactsError, match="PROVIDER_RESPONSE_INVALID"):
        sanitize_html("<template></script>Memory: 64 GB")


def test_sanitizer_tracks_nested_tags_inside_hidden_regions() -> None:
    assert sanitize_html(
        "Before<template><div><script>Memory: 64 GB</script></div></template>After"
    ) == "BeforeAfter"
    with pytest.raises(SteamDeclaredFactsError, match="PROVIDER_RESPONSE_INVALID"):
        sanitize_html("<template><div>hidden</template></div>Memory: 64 GB")


@pytest.mark.parametrize("tag", ["script", "style", "template"])
def test_sanitizer_rejects_self_closing_syntax_for_non_void_hidden_tags(
    tag: str,
) -> None:
    with pytest.raises(SteamDeclaredFactsError, match="PROVIDER_RESPONSE_INVALID"):
        sanitize_html(f"<{tag}/>Memory: 64 GB RAM")


def test_sanitizer_allows_real_self_closing_void_tags() -> None:
    assert sanitize_html("<img/><br/>Memory: 8 GB") == "Memory: 8 GB"


@pytest.mark.parametrize(
    "value",
    [
        "<div>" * (subject.MAX_HTML_DEPTH + 1),
        "<br>" * (subject.MAX_HTML_TOKENS + 1),
        "x" * (subject.MAX_HTML_FIELD_BYTES + 1),
        "x" * (subject.MAX_SANITIZED_TEXT_CHARS + 1),
    ],
)
def test_sanitizer_bounds_hostile_markup(value: str) -> None:
    with pytest.raises(SteamDeclaredFactsError, match="PROVIDER_RESPONSE_INVALID"):
        sanitize_html(value)


def test_gzip_decoder_is_strict_and_bounded() -> None:
    raw = b'{"400":{"success":false}}'
    assert subject._decode_content(gzip.compress(raw), {"Content-Encoding": "gzip"}) == raw
    with pytest.raises(SteamDeclaredFactsError):
        subject._decode_content(gzip.compress(raw) + b"junk", {"content-encoding": "gzip"})
    with pytest.raises(SteamDeclaredFactsError):
        subject._decode_content(raw, {"content-encoding": "br"})
    bomb = gzip.compress(b"x" * (subject.MAX_DECOMPRESSED_BYTES + 1))
    with pytest.raises(SteamDeclaredFactsError):
        subject._decode_content(bomb, {"content-encoding": "gzip"})


def test_category_map_is_exhaustive_for_accepted_accessibility_ids() -> None:
    assert set(range(64, 83)) <= set(CATEGORY_SLUGS)
    assert len(CATEGORY_SLUGS.values()) == len(set(CATEGORY_SLUGS.values()))


def test_value_object_invariants_reject_contradictions() -> None:
    with pytest.raises(ValueError):
        DeclaredText([], None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PlatformDeclarations("unknown", False, None, None)
    with pytest.raises(ValueError):
        RequirementDeclaration("linux", "declared", None, None)
    with pytest.raises(ValueError):
        LanguageDeclarations("declared", (), 0)
    with pytest.raises(ValueError):
        CategoryDeclarations("unknown", ("camera_comfort",), ())


def test_human_reference_cannot_change_origin_mode_or_context() -> None:
    valid = SteamDeclaredFactsHumanReference(
        400,
        "US",
        "english",
        "https://store.steampowered.com/app/400/?cc=US&l=english",
    )
    with pytest.raises(ValueError):
        replace(valid, url="https://example.com/app/400/?cc=US&l=english")
    with pytest.raises(ValueError):
        replace(valid, url="https://store.steampowered.com/app/400/?l=english&cc=US")
    with pytest.raises(ValueError):
        replace(valid, automation_supported=True)  # type: ignore[arg-type]

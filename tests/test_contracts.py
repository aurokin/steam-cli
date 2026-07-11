from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from steam_agent.contracts import (
    CompletenessStatus,
    ErrorRecord,
    WarningRecord,
    completeness,
    encode_json,
    error_envelope,
    format_timestamp,
    success_envelope,
)


FIXED_TIME = datetime(2026, 7, 10, 22, 0, tzinfo=timezone.utc)


def test_success_envelope_is_deterministic() -> None:
    value = success_envelope(
        command="games.query",
        context={"machine_id": "local"},
        data={"items": [{"appid": 20}, {"appid": 10}]},
        generated_at=FIXED_TIME,
    )

    assert encode_json(value) == (
        '{"command":"games.query","completeness":{"missing_capabilities":[],'
        '"stale_capabilities":[],"status":"complete","warnings":[]},'
        '"context":{"machine_id":"local"},"data":{"items":[{"appid":20},'
        '{"appid":10}]},"generated_at":"2026-07-10T22:00:00Z",'
        '"schema_version":"0.1"}'
    )


def test_partial_completeness_preserves_typed_warning() -> None:
    value = completeness(
        CompletenessStatus.PARTIAL,
        warnings=[
            WarningRecord(
                code="MALFORMED_STEAM_FILE",
                message="One appmanifest could not be parsed.",
                source="appmanifest_10.acf",
            )
        ],
    )

    assert value["status"] == "partial"
    assert value["warnings"][0]["source"] == "appmanifest_10.acf"


def test_error_envelope_does_not_contain_success_fields() -> None:
    value = error_envelope(
        command="sync.installed",
        error=ErrorRecord(
            code="STEAM_NOT_FOUND",
            message="No Steam installation was found.",
            remediation="Pass --steam-root or install Steam.",
        ),
        generated_at=FIXED_TIME,
    )

    assert "data" not in value
    assert "completeness" not in value
    assert value["error"]["retryable"] is False


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_timestamp(datetime(2026, 7, 10, 22, 0))


@pytest.mark.parametrize(
    "value",
    [
        success_envelope(
            command="status", data={"ready": True}, generated_at=FIXED_TIME
        ),
        error_envelope(
            command="sync.installed",
            error=ErrorRecord(code="STEAM_NOT_FOUND", message="Missing"),
            generated_at=FIXED_TIME,
        ),
    ],
)
def test_envelopes_validate_against_published_json_schema(
    value: dict[str, object],
) -> None:
    schema_path = Path(__file__).parents[1] / "schemas" / "response.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)

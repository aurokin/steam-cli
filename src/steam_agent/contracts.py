"""Stable process-level contracts for agent-facing CLI output."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "0.1"


class CompletenessStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ErrorCode(StrEnum):
    STEAM_NOT_FOUND = "STEAM_NOT_FOUND"
    STEAM_ROOT_INACCESSIBLE = "STEAM_ROOT_INACCESSIBLE"
    MALFORMED_STEAM_FILE = "MALFORMED_STEAM_FILE"
    PARTIAL_SCAN = "PARTIAL_SCAN"
    NOT_SYNCED = "NOT_SYNCED"
    SYNC_IN_PROGRESS = "SYNC_IN_PROGRESS"
    STALE_LAST_GOOD = "STALE_LAST_GOOD"
    ACCOUNT_NOT_CONFIGURED = "ACCOUNT_NOT_CONFIGURED"
    ACCOUNT_AMBIGUOUS = "ACCOUNT_AMBIGUOUS"
    ACCOUNT_CONFLICT = "ACCOUNT_CONFLICT"
    ACCOUNT_REGISTRY_UNAVAILABLE = "ACCOUNT_REGISTRY_UNAVAILABLE"
    ACCOUNT_REGISTRY_MALFORMED = "ACCOUNT_REGISTRY_MALFORMED"
    ACCOUNT_SELECTION_NOT_FOUND = "ACCOUNT_SELECTION_NOT_FOUND"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPABILITY_NOT_PROBED = "CAPABILITY_NOT_PROBED"
    CREDENTIAL_STORE_UNAVAILABLE = "CREDENTIAL_STORE_UNAVAILABLE"
    CREDENTIAL_STORE_LOCKED = "CREDENTIAL_STORE_LOCKED"
    CREDENTIAL_NOT_FOUND = "CREDENTIAL_NOT_FOUND"
    CREDENTIAL_WRITE_FAILED = "CREDENTIAL_WRITE_FAILED"
    CREDENTIAL_READ_FAILED = "CREDENTIAL_READ_FAILED"
    CREDENTIAL_DELETE_FAILED = "CREDENTIAL_DELETE_FAILED"
    CREDENTIAL_ROLLBACK_FAILED = "CREDENTIAL_ROLLBACK_FAILED"
    FILE_STORE_NOT_APPROVED = "FILE_STORE_NOT_APPROVED"
    INTERACTIVE_INPUT_REQUIRED = "INTERACTIVE_INPUT_REQUIRED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    OWNED_GAMES_INACCESSIBLE_OR_UNKNOWN_ACCOUNT = (
        "OWNED_GAMES_INACCESSIBLE_OR_UNKNOWN_ACCOUNT"
    )
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_RESPONSE_INVALID = "PROVIDER_RESPONSE_INVALID"
    CAPABILITY_PROBE_STALE = "CAPABILITY_PROBE_STALE"
    REQUEST_THROTTLED = "REQUEST_THROTTLED"
    DATABASE_ERROR = "DATABASE_ERROR"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    SECRET_ON_ARGV = "SECRET_ON_ARGV"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class WarningRecord:
    code: str
    message: str
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    code: str
    message: str
    retryable: bool = False
    remediation: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    """Serialize an aware timestamp as RFC 3339 UTC with second precision."""

    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def completeness(
    status: CompletenessStatus | str,
    *,
    warnings: Sequence[WarningRecord | Mapping[str, Any]] = (),
    stale_capabilities: Sequence[str] = (),
    missing_capabilities: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "status": str(status),
        "missing_capabilities": sorted(missing_capabilities),
        "stale_capabilities": sorted(stale_capabilities),
        "warnings": [_record_dict(item) for item in warnings],
    }


def success_envelope(
    *,
    command: str,
    data: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
    completeness_value: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "generated_at": format_timestamp(generated_at or utc_now()),
        "context": dict(context or {}),
        "completeness": dict(
            completeness_value or completeness(CompletenessStatus.COMPLETE)
        ),
        "data": dict(data),
    }


def error_envelope(
    *,
    command: str,
    error: ErrorRecord,
    context: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "generated_at": format_timestamp(generated_at or utc_now()),
        "context": dict(context or {}),
        "error": _record_dict(error),
    }


def encode_json(value: Mapping[str, Any], *, pretty: bool = False) -> str:
    """Encode deterministic UTF-8 JSON suitable for stdout."""

    if pretty:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _record_dict(value: Any) -> dict[str, Any]:
    result = asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value)
    return {key: item for key, item in result.items() if item is not None}

"""Pure M7 reconstruction of local operational state.

The input boundary is intentionally narrower than the local Steam scanner.  A
caller supplies only facts from the selected machine's promoted installed
projection; this module never receives or emits library, install, or manifest
paths, and it performs no filesystem or process inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, TypeAlias


SCHEMA = "local-operation-state/0.1"
FRESH_FOR = timedelta(minutes=15)
MAX_APPID = (1 << 32) - 1
MAX_BYTES = (1 << 63) - 1
MAX_ITEMS = 100_000
MAX_EVIDENCE_IDS = 64
MAX_TEXT = 256

Presence = Literal["present", "absent", "unknown"]
Freshness = Literal["fresh", "stale", "unknown"]
AttemptStatus = Literal["complete", "partial", "failed", "running"]
EvidenceId: TypeAlias = str | int
TimestampInput: TypeAlias = str | datetime | None

UNSUPPORTED_CAPABILITIES: tuple[str, ...] = (
    "bandwidth",
    "compatibility_tool",
    "completion_time",
    "media",
    "per_library_capacity",
    "runtime",
    "save_state",
    "transfer_queue",
    "update_currency",
    "workshop_mods",
)


def _appid(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_APPID
    ):
        raise ValueError("appid is invalid")
    return value


def _text(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TEXT
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(f"{name} must be a bounded printable string")
    return value


def _timestamp(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _parse_timestamp(value: TimestampInput) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc).replace(microsecond=0)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _json_time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _identity(value: object, *, name: str) -> EvidenceId:
    if isinstance(value, bool):
        raise ValueError(f"{name} is invalid")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{name} is invalid")
        return value
    return _text(value, name=name)


def _evidence_ids(values: tuple[EvidenceId, ...]) -> tuple[EvidenceId, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_EVIDENCE_IDS:
        raise ValueError("evidence_ids must be a bounded tuple")
    checked = tuple(_identity(value, name="evidence_id") for value in values)
    if len(set(checked)) != len(checked):
        raise ValueError("evidence_ids must be unique")
    return tuple(sorted(checked, key=lambda value: (type(value).__name__, str(value))))


@dataclass(frozen=True, slots=True)
class InstalledAttempt:
    """Latest installed-sync attempt in the same selected-machine snapshot."""

    status: AttemptStatus
    attempted_at: TimestampInput
    attempt_id: EvidenceId | None = None

    def __post_init__(self) -> None:
        if self.status not in {"complete", "partial", "failed", "running"}:
            raise ValueError("installed attempt status is invalid")
        if self.attempt_id is not None:
            _identity(self.attempt_id, name="attempt_id")


@dataclass(frozen=True, slots=True)
class PromotedInstalledFact:
    """Privacy-bounded input from one promoted installed projection."""

    appid: int
    presence: Presence
    observed_at: TimestampInput
    evidence_ids: tuple[EvidenceId, ...] = ()
    promoted_sync_run_id: EvidenceId | None = None
    build_id: str | None = None
    size_on_disk_bytes: int | None = None
    manifest_source_modified_at: TimestampInput = None
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        _appid(self.appid)
        if self.presence not in {"present", "absent", "unknown"}:
            raise ValueError("installed presence is invalid")
        object.__setattr__(self, "evidence_ids", _evidence_ids(self.evidence_ids))
        if self.promoted_sync_run_id is not None:
            _identity(self.promoted_sync_run_id, name="promoted_sync_run_id")
        if self.build_id is not None:
            _text(self.build_id, name="build_id")
        if self.size_on_disk_bytes is not None and (
            isinstance(self.size_on_disk_bytes, bool)
            or not isinstance(self.size_on_disk_bytes, int)
            or not 0 <= self.size_on_disk_bytes <= MAX_BYTES
        ):
            raise ValueError("size_on_disk_bytes is invalid")
        if self.unknown_reason is not None:
            _text(self.unknown_reason, name="unknown_reason")
        if self.presence == "unknown":
            if self.unknown_reason is None:
                raise ValueError("unknown presence requires an explanation")
            if self.build_id is not None or self.size_on_disk_bytes is not None:
                raise ValueError("unknown presence cannot carry installed-only values")
        elif self.unknown_reason is not None:
            raise ValueError("known presence cannot carry an unknown reason")
        elif not self.evidence_ids:
            raise ValueError("known presence requires evidence lineage")
        if self.presence != "present" and (
            self.build_id is not None
            or self.size_on_disk_bytes is not None
            or self.manifest_source_modified_at is not None
        ):
            raise ValueError("only present apps can carry manifest-derived values")


@dataclass(frozen=True, slots=True)
class KnownValue:
    state: Literal["known", "unknown"]
    value: str | int | None
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        if self.state == "known":
            if self.value is None or self.unknown_reason is not None:
                raise ValueError("known values require a value only")
            if isinstance(self.value, str):
                _text(self.value, name="known value")
            elif isinstance(self.value, bool) or not isinstance(self.value, int):
                raise ValueError("known value is invalid")
        elif self.state == "unknown":
            if self.value is not None or self.unknown_reason is None:
                raise ValueError("unknown values require an explanation only")
            _text(self.unknown_reason, name="unknown_reason")
        else:
            raise ValueError("value state is invalid")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"state": self.state, "value": self.value}
        if self.unknown_reason is not None:
            result["unknown_reason"] = self.unknown_reason
        return result


@dataclass(frozen=True, slots=True)
class InstalledState:
    state: Presence
    freshness: Freshness
    observed_at: datetime | None
    evidence_ids: tuple[EvidenceId, ...]
    unknown_reason: str | None = None
    freshness_reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"present", "absent", "unknown"}:
            raise ValueError("installed state is invalid")
        if self.freshness not in {"fresh", "stale", "unknown"}:
            raise ValueError("installed freshness is invalid")
        if self.observed_at is not None:
            object.__setattr__(
                self, "observed_at", _timestamp(self.observed_at, name="observed_at")
            )
        object.__setattr__(self, "evidence_ids", _evidence_ids(self.evidence_ids))
        if self.state == "unknown":
            if self.unknown_reason is None:
                raise ValueError("unknown installed state requires an explanation")
            _text(self.unknown_reason, name="unknown_reason")
        elif self.unknown_reason is not None:
            raise ValueError("known installed state cannot carry an unknown reason")
        if self.freshness == "unknown":
            if self.freshness_reason is None:
                raise ValueError("unknown freshness requires an explanation")
        elif self.observed_at is None:
            raise ValueError("known freshness requires an observation time")
        if self.freshness_reason is not None:
            _text(self.freshness_reason, name="freshness_reason")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "state": self.state,
            "freshness": self.freshness,
            "observed_at": _json_time(self.observed_at),
            "evidence_ids": list(self.evidence_ids),
        }
        if self.unknown_reason is not None:
            result["unknown_reason"] = self.unknown_reason
        if self.freshness_reason is not None:
            result["freshness_reason"] = self.freshness_reason
        return result


@dataclass(frozen=True, slots=True)
class LocalOperationItem:
    appid: int
    installed: InstalledState
    build_id: KnownValue
    size_on_disk_bytes: KnownValue
    manifest_source_modified_at: KnownValue

    def __post_init__(self) -> None:
        _appid(self.appid)
        if not isinstance(self.installed, InstalledState):
            raise ValueError("installed state is invalid")
        for name in (
            "build_id",
            "size_on_disk_bytes",
            "manifest_source_modified_at",
        ):
            if not isinstance(getattr(self, name), KnownValue):
                raise ValueError(f"{name} is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "appid": self.appid,
            "installed": self.installed.to_dict(),
            "build_id": self.build_id.to_dict(),
            "size_on_disk_bytes": self.size_on_disk_bytes.to_dict(),
            "manifest_source_modified_at": self.manifest_source_modified_at.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LocalOperationBatch:
    generated_at: datetime
    items: tuple[LocalOperationItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "generated_at", _timestamp(self.generated_at, name="generated_at")
        )
        if not isinstance(self.items, tuple) or len(self.items) > MAX_ITEMS:
            raise ValueError("operation items must be a bounded tuple")
        if any(not isinstance(item, LocalOperationItem) for item in self.items):
            raise ValueError("operation items contain an invalid item")
        appids = tuple(item.appid for item in self.items)
        if appids != tuple(sorted(set(appids))):
            raise ValueError("operation items must have unique ordered AppIDs")

    @property
    def schema(self) -> str:
        return SCHEMA

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "generated_at": _json_time(self.generated_at),
            "items": [item.to_dict() for item in self.items],
            "unsupported_capabilities": {
                capability: {
                    "availability": "unavailable",
                    "reason": "adapter_not_implemented",
                }
                for capability in UNSUPPORTED_CAPABILITIES
            },
        }


def observe_local_operations(
    *,
    requested_appids: tuple[int, ...],
    installed_facts: tuple[PromotedInstalledFact, ...],
    generated_at: datetime,
    latest_attempt: InstalledAttempt | None = None,
) -> LocalOperationBatch:
    """Reconstruct operational state from an already-read cache snapshot.

    A missing requested fact is not treated as proof of absence.  Callers may
    supply an explicit ``absent`` fact only when the selected machine's complete
    promoted projection establishes that negative membership.
    """

    now = _timestamp(generated_at, name="generated_at")
    if not isinstance(requested_appids, tuple) or len(requested_appids) > MAX_ITEMS:
        raise ValueError("requested_appids must be a bounded tuple")
    requested = tuple(sorted(_appid(value) for value in requested_appids))
    if len(set(requested)) != len(requested):
        raise ValueError("requested_appids must be unique")
    if not isinstance(installed_facts, tuple) or len(installed_facts) > MAX_ITEMS:
        raise ValueError("installed_facts must be a bounded tuple")
    requested_set = set(requested)
    facts: dict[int, PromotedInstalledFact] = {}
    for fact in installed_facts:
        if not isinstance(fact, PromotedInstalledFact):
            raise ValueError("installed_facts contains an invalid fact")
        if fact.appid not in requested_set or fact.appid in facts:
            raise ValueError("installed_facts has an unexpected or duplicate AppID")
        facts[fact.appid] = fact
    if latest_attempt is not None and not isinstance(latest_attempt, InstalledAttempt):
        raise ValueError("latest_attempt is invalid")
    return LocalOperationBatch(
        generated_at=now,
        items=tuple(
            _operation_item(appid, facts.get(appid), now, latest_attempt)
            for appid in requested
        ),
    )


def _operation_item(
    appid: int,
    fact: PromotedInstalledFact | None,
    now: datetime,
    latest_attempt: InstalledAttempt | None,
) -> LocalOperationItem:
    if fact is None:
        return LocalOperationItem(
            appid,
            InstalledState(
                "unknown",
                "unknown",
                None,
                (),
                "no_promoted_fact",
                "no_promoted_fact",
            ),
            _unknown("no_promoted_fact"),
            _unknown("no_promoted_fact"),
            _unknown("no_promoted_fact"),
        )

    observed = _parse_timestamp(fact.observed_at)
    freshness, freshness_reason = _freshness(
        observed=observed,
        now=now,
        promoted_sync_run_id=fact.promoted_sync_run_id,
        latest_attempt=latest_attempt,
    )
    installed = InstalledState(
        fact.presence,
        freshness,
        observed,
        fact.evidence_ids,
        fact.unknown_reason,
        freshness_reason,
    )
    if fact.presence != "present":
        reason = (
            "not_installed"
            if fact.presence == "absent"
            else "installed_presence_unknown"
        )
        return LocalOperationItem(
            appid,
            installed,
            _unknown(reason),
            _unknown(reason),
            _unknown(reason),
        )
    manifest_time = _parse_timestamp(fact.manifest_source_modified_at)
    if manifest_time is None:
        manifest_value = _unknown("not_observed")
    elif manifest_time > now:
        manifest_value = _unknown("source_time_in_future")
    else:
        manifest_value = KnownValue("known", _json_time(manifest_time))
    return LocalOperationItem(
        appid,
        installed,
        _known_or_unknown(fact.build_id, "not_observed"),
        _known_or_unknown(fact.size_on_disk_bytes, "not_observed"),
        manifest_value,
    )


def _freshness(
    *,
    observed: datetime | None,
    now: datetime,
    promoted_sync_run_id: EvidenceId | None,
    latest_attempt: InstalledAttempt | None,
) -> tuple[Freshness, str | None]:
    if observed is None:
        return "unknown", "observation_time_unparseable_or_missing"
    age = now - observed
    if age < timedelta(0):
        return "unknown", "observation_time_in_future"
    base: Freshness = "fresh" if age <= FRESH_FOR else "stale"
    if latest_attempt is None or latest_attempt.status == "complete":
        return base, None
    attempted = _parse_timestamp(latest_attempt.attempted_at)
    if attempted is None:
        return "unknown", "latest_attempt_time_unparseable_or_missing"
    if attempted > now:
        return "unknown", "latest_attempt_time_in_future"
    if attempted > observed:
        return "stale", "newer_incomplete_attempt"
    if attempted < observed:
        return base, None
    if (
        latest_attempt.attempt_id == promoted_sync_run_id
        and latest_attempt.attempt_id is not None
    ):
        return base, None
    if (
        isinstance(latest_attempt.attempt_id, int)
        and not isinstance(latest_attempt.attempt_id, bool)
        and isinstance(promoted_sync_run_id, int)
        and not isinstance(promoted_sync_run_id, bool)
        and latest_attempt.attempt_id <= promoted_sync_run_id
    ):
        return base, None
    return "stale", "ambiguous_equal_time_incomplete_attempt"


def _unknown(reason: str) -> KnownValue:
    return KnownValue("unknown", None, reason)


def _known_or_unknown(value: str | int | None, reason: str) -> KnownValue:
    return _unknown(reason) if value is None else KnownValue("known", value)


__all__ = [
    "FRESH_FOR",
    "SCHEMA",
    "UNSUPPORTED_CAPABILITIES",
    "InstalledAttempt",
    "InstalledState",
    "KnownValue",
    "LocalOperationBatch",
    "LocalOperationItem",
    "PromotedInstalledFact",
    "observe_local_operations",
]

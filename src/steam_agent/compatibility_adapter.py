"""Typed storage-to-assessment adapter for the cache-only M5 query boundary."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from steam_agent.compatibility import (
    CompatibilityTarget,
    FeatureRequirement,
    GateOverride,
)
from steam_agent.compatibility_query import (
    CompatibilityQueryResult,
    LocalObservation,
    SystemSnapshot,
    reconstruct_compatibility,
    compatibility_declared_facts,
)
from steam_agent.contracts import format_timestamp
from steam_agent.storage import CompatibilitySnapshot, SyncRun


def assess_compatibility_snapshot(
    snapshot: CompatibilitySnapshot,
    *,
    target: CompatibilityTarget,
    requirements: tuple[FeatureRequirement, ...] = (),
    overrides: tuple[GateOverride, ...] = (),
) -> CompatibilityQueryResult:
    """Adapt one atomic typed snapshot without performing I/O or retaining prose."""

    generated_at = _time(snapshot.as_of)
    requested = tuple(item.appid for item in snapshot.requested)
    system = _system(snapshot, target)
    declared = {
        "items": tuple(
            {
                "appid": subject.appid,
                "facts": subject.current.facts,
                "observed_at": subject.current.observed_at,
                "promoted_sync_run_id": subject.current.promoted_sync_run_id,
                "projection_identity": _declared_projection_identity(
                    subject.current.facts,
                    subject.current.promoted_sync_run_id,
                ),
            }
            for subject in snapshot.declared_apps.subjects
            if subject.current is not None
        ),
        "latest_demand": tuple(
            asdict(subject.latest_demand)
            for subject in snapshot.declared_apps.subjects
        ),
    }
    installed = (
        _installed_observations(snapshot, requested)
        if target.kind == "machine"
        else None
    )
    return reconstruct_compatibility(
        requested,
        target=target,
        generated_at=generated_at,
        system=system,
        declared_snapshot=declared,
        installed=installed,
        owned=_owned_observations(snapshot, requested),
        requirements=requirements,
        overrides=overrides,
    )


def compatibility_query_data(
    query: CompatibilityQueryResult, *, explain: bool
) -> dict[str, Any]:
    """Return only normalized, agent-safe fields for the process envelope."""

    results: list[dict[str, Any]] = []
    for assessment in query.assessment.results:
        item = _json_safe(asdict(assessment))
        if not explain:
            # Reasons and uncertainty remain visible; detailed gate provenance is
            # intentionally opt-in to keep the default answer compact.
            item.pop("gates", None)
            item.pop("likely_good_experience_original", None)
            item.pop("runtime_risks", None)
            item.pop("accessibility", None)
            item.pop("input", None)
            item.pop("language", None)
        results.append(item)
    return {
        "schema": query.assessment.schema,
        "target": _json_safe(asdict(query.assessment.target)),
        "requested_appids": list(query.assessment.requested_appids),
        "results": results,
        "references": [
            {
                "appid": appid,
                "items": [_json_safe(asdict(reference)) for reference in references],
            }
            for appid, references in query.references
        ],
        "source_gaps": [_json_safe(asdict(item)) for item in query.completeness.subjects],
        "source_completeness": {
            "missing_capabilities": list(query.completeness.missing_capabilities),
            "stale_capabilities": list(query.completeness.stale_capabilities),
        },
        "explain": explain,
    }


def _system(
    snapshot: CompatibilitySnapshot, target: CompatibilityTarget
) -> SystemSnapshot | None:
    if target.kind != "machine":
        return None
    source = snapshot.system_profile
    current = source.current
    latest = source.latest
    return SystemSnapshot(
        target_key=snapshot.machine_id,
        profile=None if current is None else current.profile,
        observed_at=None if current is None else _time(current.observed_at),
        snapshot_id=None if current is None else current.evidence_id,
        latest_attempt_at=_run_time(latest),
        latest_attempt_status=None if latest is None else latest.status,
        latest_attempt_id=None if latest is None else latest.id,
        promoted_sync_run_id=(
            None if current is None else current.promoted_sync_run_id
        ),
    )


def _installed_observations(
    snapshot: CompatibilitySnapshot, requested: tuple[int, ...]
) -> Mapping[int, LocalObservation] | None:
    source = snapshot.installed
    latest_complete = source.latest_complete
    if latest_complete is None and not source.games:
        return None
    current = {item.appid: item for item in source.games}
    result: dict[int, LocalObservation] = {}
    for appid in requested:
        item = current.get(appid)
        if item is not None:
            result[appid] = LocalObservation(
                "present",
                _time(item.observed_at),
                "installed_projection",
                item.evidence_id,
                _run_time(source.latest),
                None if source.latest is None else source.latest.status,
                item.promoted_sync_run_id,
                None if source.latest is None else source.latest.id,
            )
        elif latest_complete is not None:
            result[appid] = LocalObservation(
                "absent",
                _run_time(latest_complete),
                "installed_projection",
                latest_complete.id,
                _run_time(source.latest),
                None if source.latest is None else source.latest.status,
                latest_complete.id,
                None if source.latest is None else source.latest.id,
            )
    return result or None


def _owned_observations(
    snapshot: CompatibilitySnapshot, requested: tuple[int, ...]
) -> Mapping[int, LocalObservation] | None:
    source = snapshot.owned
    latest_complete = source.latest_complete
    if latest_complete is None and not source.games:
        return None
    current = {item.appid: item for item in source.games}
    result: dict[int, LocalObservation] = {}
    for appid in requested:
        item = current.get(appid)
        if item is not None:
            result[appid] = LocalObservation(
                "present",
                _time(item.observed_at),
                "visible_owned",
                item.evidence_id,
                _run_time(source.latest),
                None if source.latest is None else source.latest.status,
                item.promoted_sync_run_id,
                None if source.latest is None else source.latest.id,
            )
        elif latest_complete is not None:
            # The builder deliberately turns visible-owned absence into unknown,
            # never a non-ownership claim.
            result[appid] = LocalObservation(
                "absent",
                _run_time(latest_complete),
                "visible_owned",
                latest_complete.id,
                _run_time(source.latest),
                None if source.latest is None else source.latest.status,
                latest_complete.id,
                None if source.latest is None else source.latest.id,
            )
    return result or None


def _run_time(run: SyncRun | None) -> datetime | None:
    if run is None:
        return None
    return _time(run.completed_at or run.started_at)


def _declared_projection_identity(
    facts: Mapping[str, Any], promoted_sync_run_id: str | int | None
) -> str:
    """Bind derived evidence to exact normalized content without exposing it."""

    canonical = json.dumps(
        {
            "facts": compatibility_declared_facts(facts),
            "promoted_sync_run_id": promoted_sync_run_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"declared-projection:{hashlib.sha256(canonical).hexdigest()[:24]}"


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("snapshot timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


__all__ = ["assess_compatibility_snapshot", "compatibility_query_data"]

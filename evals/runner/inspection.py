"""Strict matrix inspection, compatibility checks, and vector aggregation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from statistics import median
import sys
from typing import Any, Sequence

from evals.runner import matrix, run_state


_LAYERS = ("agent_turns", "tool_policy", "oracle", "claims", "privacy")
RESULTS_ROOT = matrix.RESULTS_ROOT


class InspectionError(RuntimeError):
    """Retained matrix evidence failed structural or compatibility validation."""


@dataclass(frozen=True, slots=True)
class Observation:
    matrix_id: str
    work_item: run_state.MatrixWorkItem
    completion: run_state.MatrixCompletion
    child_manifest: run_state.RunManifest
    report: dict[str, Any]
    summary: dict[str, Any]
    compatibility: tuple[tuple[str, Any], ...]
    compatibility_sha256: str
    attempt_started_at: str | None = None


@dataclass(frozen=True, slots=True)
class UnavailableWorkItem:
    work_item: run_state.MatrixWorkItem
    completion: run_state.MatrixCompletion

    def to_dict(self) -> dict[str, Any]:
        assert self.completion.unavailable_reason is not None
        return {
            "work_item": self.work_item.to_dict(),
            "attempt_id": self.completion.attempt_id,
            "reason": self.completion.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class MatrixInspection:
    matrix_dir: Path
    manifest: run_state.MatrixManifest
    manifest_sha256: str
    structurally_complete: bool
    eligible: bool
    observations: tuple[Observation, ...]
    unavailable_work_items: tuple[UnavailableWorkItem, ...]
    orphan_attempt_ids: tuple[str, ...]
    orphan_attempt_hashes: tuple[
        tuple[str, tuple[tuple[str, str], ...]], ...
    ] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "steam-agent-eval-matrix-inspection/0.1",
            "matrix_id": self.manifest.matrix_id,
            "manifest_sha256": self.manifest_sha256,
            "state": self.manifest.state.value,
            "structurally_complete": self.structurally_complete,
            "eligible": self.eligible,
            "planned_work_items": len(self.manifest.work_items),
            "completed_work_items": len(self.manifest.completions),
            "accounted_work_items": (
                len(self.observations) + len(self.unavailable_work_items)
            ),
            "observed_work_items": len(self.observations),
            "unavailable_work_items": [
                item.to_dict() for item in self.unavailable_work_items
            ],
            "excluded_scenario_ids": list(self.manifest.excluded_scenario_ids),
            "orphan_attempt_ids": list(self.orphan_attempt_ids),
            "orphan_attempts": [
                {
                    "attempt_id": attempt_id,
                    "artifact_hashes": [
                        {"name": name, "sha256": digest}
                        for name, digest in hashes
                    ],
                }
                for attempt_id, hashes in self.orphan_attempt_hashes
            ],
            "compatibility_keys": sorted(
                {item.compatibility_sha256 for item in self.observations}
            ),
        }


def _private_regular(path: Path) -> None:
    try:
        item_stat = path.lstat()
    except OSError:
        raise InspectionError("matrix artifact is unavailable") from None
    if not stat.S_ISREG(item_stat.st_mode) or stat.S_IMODE(item_stat.st_mode) != 0o600:
        raise InspectionError("matrix artifact is not a private regular file")


def _private_regular_bytes(path: Path, *, max_bytes: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > max_bytes
        ):
            raise InspectionError("matrix artifact is not a private regular file")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, max_bytes + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise InspectionError("matrix artifact exceeds inspection limits")
        after = os.fstat(descriptor)
        path_stat = path.lstat()
        signature = lambda item: (  # noqa: E731
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if (
            size != before.st_size
            or signature(before) != signature(after)
            or signature(after) != signature(path_stat)
        ):
            raise InspectionError("matrix artifact changed during inspection")
        return b"".join(chunks)
    except InspectionError:
        raise
    except OSError:
        raise InspectionError("matrix artifact is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _private_directory(path: Path) -> None:
    try:
        item_stat = path.lstat()
    except OSError:
        raise InspectionError("matrix directory is unavailable") from None
    if not stat.S_ISDIR(item_stat.st_mode) or stat.S_IMODE(item_stat.st_mode) != 0o700:
        raise InspectionError("matrix directory is not private")


def _private_results_boundary(path: Path) -> Path:
    if ".." in Path(path).parts:
        raise InspectionError("matrix results root boundary is invalid")
    absolute = Path(os.path.abspath(path))
    chain = (*reversed(absolute.parents), absolute)
    try:
        for item in chain:
            item_stat = item.lstat()
            if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISDIR(item_stat.st_mode):
                raise InspectionError("matrix results root boundary is invalid")
        _private_directory(absolute)
        return absolute
    except InspectionError:
        raise
    except OSError:
        raise InspectionError("matrix results root boundary is unavailable") from None


def _strict_object(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> dict[str, Any]:
    value = matrix._read_strict_json(path, max_bytes=max_bytes)  # noqa: SLF001
    if not isinstance(value, dict):
        raise InspectionError("matrix artifact is not an object")
    return value


def _passed_value(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise InspectionError("report layer outcome is invalid")


def deterministic_layer_value(report: dict[str, Any], layer: str) -> bool | None:
    """Return one canonical deterministic layer value from an inspected report."""

    metric = report["metrics"][layer]
    field = "deterministic_passed" if layer == "claims" else "passed"
    return _passed_value(metric.get(field))


def _deterministic_layer_value(report: dict[str, Any], layer: str) -> bool | None:
    return deterministic_layer_value(report, layer)


def _compatibility(
    matrix_manifest: run_state.MatrixManifest,
    work_item: run_state.MatrixWorkItem,
    child_manifest: run_state.RunManifest,
    report: dict[str, Any],
    *,
    timeout_seconds: float,
) -> tuple[tuple[tuple[str, Any], ...], str]:
    scenario = next(
        item
        for item in matrix_manifest.inputs.scenarios
        if item.scenario_id == work_item.scenario_id
    )
    generator = report["generator"]
    ordered_selected_scenario_inventory = tuple(
        item.to_dict() for item in matrix_manifest.inputs.scenarios
    )
    fields: dict[str, Any] = {
        "commit": matrix_manifest.inputs.commit,
        "campaign_sha256": matrix_manifest.campaign_sha256,
        "matrix_source_digest": matrix_manifest.inputs.source_digest,
        "matrix_harness_digest": matrix_manifest.inputs.harness_digest,
        "ordered_selected_scenario_inventory": ordered_selected_scenario_inventory,
        "deterministic_preflight_attestation": (
            matrix_manifest.preflight_attestation.to_dict()
        ),
        "child_snapshot_digest": child_manifest.source_digest,
        "scenario_id": scenario.scenario_id,
        "scenario_sha256": scenario.source_sha256,
        "scenario_schema_version": scenario.schema_version,
        "scenario_schema_sha256": scenario.schema_sha256,
        "track": work_item.track,
        "instructions_version": generator.get("instructions_version"),
        "control_set_version": child_manifest.control_set_version,
        "tool_versions": tuple(child_manifest.tool_versions),
        "report_schema": report.get("artifact_schema_version"),
        "timeout_seconds": timeout_seconds,
    }
    canonical = matrix._canonical_json_bytes(fields)  # noqa: SLF001
    return tuple(sorted(fields.items())), hashlib.sha256(canonical).hexdigest()


def _verify_observation_chronology(
    manifest: run_state.MatrixManifest,
    observations: Sequence[Observation],
) -> None:
    by_work_item = {
        item.work_item.work_item_id: item for item in observations
    }
    try:
        campaign_started = run_state._parse_time(manifest.started_at)  # noqa: SLF001
        campaign_updated = run_state._parse_time(manifest.updated_at)  # noqa: SLF001
        previous_completion = campaign_started
        for completion in manifest.completions:
            completed = run_state._parse_time(completion.completed_at)  # noqa: SLF001
            if completed < previous_completion or completed > campaign_updated:
                raise InspectionError("matrix observation chronology is invalid")
            if completion.outcome == "observed":
                observation = by_work_item.get(completion.work_item_id)
                if observation is None:
                    raise InspectionError(
                        "matrix observation chronology evidence is unavailable"
                    )
                child = observation.child_manifest
                attempt_started = run_state._parse_time(  # noqa: SLF001
                    observation.attempt_started_at
                )
                child_started = run_state._parse_time(child.started_at)  # noqa: SLF001
                child_finished = run_state._parse_time(child.finished_at)  # noqa: SLF001
                if not (
                    campaign_started
                    < attempt_started
                    < child_started
                    < child_finished
                    < completed
                    and previous_completion < attempt_started
                ):
                    raise InspectionError(
                        "matrix observation chronology is invalid"
                    )
            previous_completion = completed
    except run_state.ManifestStateError:
        raise InspectionError("matrix observation chronology is invalid") from None


def inspect_matrix(
    matrix_dir: Path,
    *,
    results_root: Path | None = None,
) -> MatrixInspection:
    requested_matrix_dir = Path(matrix_dir)
    _private_directory(requested_matrix_dir)
    boundary = Path(results_root) if results_root is not None else RESULTS_ROOT
    boundary = _private_results_boundary(boundary)
    try:
        matrix_dir = requested_matrix_dir.resolve(strict=True)
        results_root = boundary.resolve(strict=True)
    except (OSError, RuntimeError):
        raise InspectionError("matrix results root is unavailable") from None
    try:
        matrix_dir.relative_to(results_root)
    except ValueError:
        raise InspectionError("matrix directory is outside the results root") from None
    for name in ("config.json",):
        _private_regular(matrix_dir / name)
    try:
        manifest_bytes = _private_regular_bytes(
            matrix_dir / "manifest.json",
            max_bytes=run_state.MATRIX_MANIFEST_MAX_BYTES,
        )
        manifest = run_state.MatrixManifest.from_dict(
            matrix._strict_json_loads(manifest_bytes.decode("utf-8"))  # noqa: SLF001
        )
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_bytes != run_state._strict_json_bytes(manifest.to_dict()):  # noqa: SLF001
            raise InspectionError("matrix manifest is not canonical")
    except InspectionError:
        raise
    except (
        TypeError,
        UnicodeError,
        ValueError,
        run_state.ManifestStateError,
    ):
        raise InspectionError("matrix manifest is invalid") from None
    try:
        config = matrix.load_config(
            matrix_dir / "config.json", validate_calibrated_assets=False
        )
        matrix.validate_retained_calibrated_assets(matrix_dir, config)
        matrix.validate_retained_preflight_evidence(
            matrix_dir,
            manifest.inputs,
            manifest.preflight_attestation,
        )
        matrix._verify_matrix_layout(matrix_dir, manifest)  # noqa: SLF001
    except matrix.MatrixError as error:
        raise InspectionError(str(error)) from None
    if (
        manifest.matrix_id != matrix_dir.name
        or config.sha256 != manifest.config_sha256
        or config.campaign != manifest.campaign
        or config.campaign.sha256 != manifest.campaign_sha256
        or matrix.plan_sha256(manifest.work_items) != manifest.plan_sha256
        or matrix.resolve_plan(config, manifest.inputs) != manifest.work_items
    ):
        raise InspectionError("matrix plan provenance is invalid")

    observations: list[Observation] = []
    unavailable_work_items: list[UnavailableWorkItem] = []
    committed_attempts: set[tuple[str, str]] = set()
    official_attempt_starts: dict[str, str] = {}
    for work_item, completion in zip(
        manifest.work_items, manifest.completions, strict=False
    ):
        attempt_dir = (
            matrix_dir / "work" / work_item.work_item_id / completion.attempt_id
        )
        _private_directory(attempt_dir)
        _private_regular(attempt_dir / "started.json")
        _private_regular(attempt_dir / "result.json")
        try:
            attempt_started_at = matrix.validate_attempt_start(
                attempt_dir / "started.json", work_item, completion
            )
        except matrix.MatrixError as error:
            raise InspectionError(str(error)) from None
        result = _strict_object(attempt_dir / "result.json")
        if set(result) != {"schema", "completion"} or (
            result.get("schema") != "steam-agent-eval-matrix-attempt-result/0.1"
            or result.get("completion") != completion.to_dict()
        ):
            raise InspectionError("matrix attempt result is invalid")
        committed_attempts.add((work_item.work_item_id, completion.attempt_id))
        official_attempt_starts[work_item.work_item_id] = attempt_started_at
        if completion.outcome == "unavailable":
            unavailable_work_items.append(
                UnavailableWorkItem(work_item=work_item, completion=completion)
            )
            continue

        assert completion.child_run_id is not None
        assert completion.child_exit_code is not None
        child_dir = results_root / completion.child_run_id
        child = matrix.ChildResult(completion.child_exit_code, child_dir)
        try:
            validated_child = matrix.validate_child_result(
                child,
                work_item,
                manifest,
                results_root=results_root,
            )
        except matrix.MatrixError as error:
            raise InspectionError(str(error)) from None
        if (
            validated_child.child_run_id != completion.child_run_id
            or validated_child.artifact_hashes != completion.artifact_hashes
        ):
            raise InspectionError("committed child artifact hash changed")
        child_manifest = validated_child.manifest
        report = validated_child.report
        summary = validated_child.summary
        metrics = report.get("metrics")
        if not isinstance(metrics, dict) or any(
            not isinstance(metrics.get(layer), dict) for layer in _LAYERS
        ):
            raise InspectionError("child metric vector is invalid")
        for layer in _LAYERS:
            _passed_value(metrics[layer].get("passed"))
        _passed_value(metrics["claims"].get("deterministic_passed"))
        compatibility, digest = _compatibility(
            manifest,
            work_item,
            child_manifest,
            report,
            timeout_seconds=config.timeout_seconds,
        )
        observations.append(
            Observation(
                matrix_id=manifest.matrix_id,
                work_item=work_item,
                completion=completion,
                child_manifest=child_manifest,
                report=report,
                summary=summary,
                compatibility=compatibility,
                compatibility_sha256=digest,
                attempt_started_at=attempt_started_at,
            )
        )

    orphan_attempts: list[str] = []
    orphan_attempt_hashes: list[
        tuple[str, tuple[tuple[str, str], ...]]
    ] = []
    disqualifying_orphan_attempts: set[str] = set()
    official_completions = {
        item.work_item_id: item for item in manifest.completions
    }
    work_items_by_id = {item.work_item_id: item for item in manifest.work_items}
    official_child_run_ids = {
        item.child_run_id
        for item in manifest.completions
        if item.child_run_id is not None
    }
    orphan_child_run_ids: set[str] = set()
    try:
        campaign_started = run_state._parse_time(manifest.started_at)  # noqa: SLF001
    except run_state.ManifestStateError:
        raise InspectionError("matrix attempt history chronology is invalid") from None
    work_root = matrix_dir / "work"
    if work_root.exists():
        _private_directory(work_root)
        expected_work_ids = {item.work_item_id for item in manifest.work_items}
        for item_root in sorted(work_root.iterdir()):
            _private_directory(item_root)
            if item_root.name not in expected_work_ids:
                raise InspectionError("matrix work directory is unexpected")
            for attempt_dir in sorted(item_root.iterdir()):
                _private_directory(attempt_dir)
                try:
                    if matrix._validate_attempt_staging_directory(  # noqa: SLF001
                        attempt_dir
                    ):
                        continue
                except matrix.MatrixError as error:
                    raise InspectionError(str(error)) from None
                try:
                    validated_attempt = matrix.validate_attempt_artifacts(
                        attempt_dir,
                        work_item_id=item_root.name,
                    )
                except matrix.MatrixError as error:
                    raise InspectionError(str(error)) from None
                if (item_root.name, attempt_dir.name) not in committed_attempts:
                    orphan_id = f"{item_root.name}/{attempt_dir.name}"
                    orphan_attempts.append(orphan_id)
                    orphan_attempt_hashes.append(
                        (orphan_id, validated_attempt.artifact_hashes)
                    )
                    orphan_completion = validated_attempt.completion
                    orphan_child_run_id = (
                        orphan_completion.child_run_id
                        if orphan_completion is not None
                        else None
                    )
                    if (
                        orphan_completion is not None
                        and orphan_completion.outcome == "observed"
                    ):
                        assert orphan_completion.child_exit_code is not None
                        assert orphan_child_run_id is not None
                        orphan_child = matrix.ChildResult(
                            orphan_completion.child_exit_code,
                            results_root / orphan_child_run_id,
                        )
                        try:
                            validated_child = matrix.validate_child_result(
                                orphan_child,
                                work_items_by_id[item_root.name],
                                manifest,
                                results_root=results_root,
                            )
                        except matrix.MatrixError as error:
                            raise InspectionError(str(error)) from None
                        if (
                            validated_child.child_run_id != orphan_child_run_id
                            or validated_child.artifact_hashes
                            != orphan_completion.artifact_hashes
                        ):
                            raise InspectionError(
                                "orphan child artifact hash changed"
                            )
                    if orphan_child_run_id is not None:
                        if (
                            orphan_child_run_id in official_child_run_ids
                            or orphan_child_run_id in orphan_child_run_ids
                        ):
                            raise InspectionError(
                                "matrix attempt history duplicates child evidence"
                            )
                        orphan_child_run_ids.add(orphan_child_run_id)
                    official = official_completions.get(item_root.name)
                    official_started_at = official_attempt_starts.get(item_root.name)
                    is_prior_retry = False
                    if official is not None and official_started_at is not None:
                        try:
                            orphan_started = run_state._parse_time(  # noqa: SLF001
                                validated_attempt.started_at
                            )
                            official_started = run_state._parse_time(  # noqa: SLF001
                                official_started_at
                            )
                            orphan_finished_before_retry = (
                                orphan_completion is None
                                or run_state._parse_time(  # noqa: SLF001
                                    orphan_completion.completed_at
                                )
                                < official_started
                            )
                        except run_state.ManifestStateError:
                            raise InspectionError(
                                "matrix attempt history chronology is invalid"
                            ) from None
                        is_prior_retry = (
                            validated_attempt.attempt_id < official.attempt_id
                            and campaign_started < orphan_started
                            and orphan_started < official_started
                            and orphan_finished_before_retry
                        )
                        if (
                            validated_attempt.attempt_id < official.attempt_id
                            and not is_prior_retry
                        ):
                            raise InspectionError(
                                "matrix attempt history chronology is invalid"
                            )
                    if not is_prior_retry:
                        disqualifying_orphan_attempts.add(orphan_id)

    _verify_observation_chronology(manifest, observations)
    _compatibility_cell_signatures(observations)
    structurally_complete = manifest.state is run_state.MatrixState.COMPLETED and len(
        observations
    ) + len(unavailable_work_items) == len(manifest.work_items)
    eligible = (
        structurally_complete
        and not unavailable_work_items
        and not disqualifying_orphan_attempts
    )
    return MatrixInspection(
        matrix_dir=matrix_dir,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        structurally_complete=structurally_complete,
        eligible=eligible,
        observations=tuple(observations),
        unavailable_work_items=tuple(unavailable_work_items),
        orphan_attempt_ids=tuple(orphan_attempts),
        orphan_attempt_hashes=tuple(orphan_attempt_hashes),
    )


def _outcome(report: dict[str, Any]) -> bool | None:
    values = [_deterministic_layer_value(report, layer) for layer in _LAYERS]
    if False in values:
        return False
    if None in values:
        return None
    return True


def _counts(values: Sequence[bool | None]) -> dict[str, int]:
    return {
        "true": sum(value is True for value in values),
        "false": sum(value is False for value in values),
        "null": sum(value is None for value in values),
    }


def _operational(observations: Sequence[Observation]) -> dict[str, Any]:
    durations: list[float] = []
    command_counts: list[int] = []
    for observation in observations:
        operational = observation.report.get("operational")
        if not isinstance(operational, dict):
            raise InspectionError("operational vector is invalid")
        duration = operational.get("duration_seconds")
        commands = operational.get("command_executions")
        if (
            not isinstance(duration, int | float)
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or duration < 0
            or not isinstance(commands, int)
            or isinstance(commands, bool)
            or commands < 0
        ):
            raise InspectionError("operational vector is invalid")
        durations.append(float(duration))
        command_counts.append(commands)
    return {
        "duration_seconds": {
            "median": median(durations),
            "minimum": min(durations),
            "maximum": max(durations),
        },
        "command_executions": {
            "median": median(command_counts),
            "minimum": min(command_counts),
            "maximum": max(command_counts),
        },
    }


def operational_vector(observations: Sequence[Observation]) -> dict[str, Any]:
    """Aggregate validated operational measurements without a quality score."""

    return _operational(observations)


_CompatibilityCell = tuple[str | None, str | None, str, str]


def _compatibility_cell_signatures(
    observations: Sequence[Observation],
) -> dict[_CompatibilityCell, tuple[str, int]]:
    cells: dict[_CompatibilityCell, list[Observation]] = {}
    for observation in observations:
        item = observation.work_item
        key = (
            item.route.model,
            item.route.reasoning_effort,
            item.track,
            item.scenario_id,
        )
        cells.setdefault(key, []).append(observation)
    signatures: dict[_CompatibilityCell, tuple[str, int]] = {}
    for key, items in cells.items():
        digests = {item.compatibility_sha256 for item in items}
        if len(digests) != 1:
            raise InspectionError(
                "matrix compatibility differs within a replicate cell"
            )
        signatures[key] = (next(iter(digests)), len(items))
    return signatures


def aggregate_observations(observations: Sequence[Observation]) -> dict[str, Any]:
    compatibility_cells = _compatibility_cell_signatures(observations)
    cells: dict[tuple[str | None, str | None, str, str], list[Observation]] = {}
    for observation in observations:
        item = observation.work_item
        key = (
            item.route.model,
            item.route.reasoning_effort,
            item.track,
            item.scenario_id,
        )
        cells.setdefault(key, []).append(observation)
    rendered_cells: list[dict[str, Any]] = []
    for key in sorted(
        cells,
        key=lambda item: (
            item[0] or "",
            item[1] or "",
            item[2],
            item[3],
        ),
    ):
        model, effort, track, scenario_id = key
        items = cells[key]
        layer_values = {
            layer: [_deterministic_layer_value(item.report, layer) for item in items]
            for layer in _LAYERS
        }
        outcomes = [_outcome(item.report) for item in items]
        rendered_cells.append(
            {
                "route": {"model": model, "reasoning_effort": effort},
                "track": track,
                "scenario_id": scenario_id,
                "compatibility_sha256": compatibility_cells[key][0],
                "observations": len(items),
                "scenario_outcomes": _counts(outcomes),
                "layers": {
                    layer: _counts(values) for layer, values in layer_values.items()
                },
                "deterministic_false_layers": {
                    layer: sum(value is False for value in values)
                    for layer, values in layer_values.items()
                },
                "replicate_consistency": {
                    "scenario_outcome": len(set(outcomes)) == 1,
                    "layers": {
                        layer: len(set(values)) == 1
                        for layer, values in layer_values.items()
                    },
                },
                "operational": _operational(items),
            }
        )
    return {
        "schema": "steam-agent-eval-matrix-vector/0.1",
        "observations": len(observations),
        "cells": rendered_cells,
    }


def compare_matrices(matrix_dirs: Sequence[Path]) -> dict[str, Any]:
    if len(matrix_dirs) < 1:
        raise InspectionError("comparison requires at least one matrix")
    resolved_dirs = [Path(path).resolve() for path in matrix_dirs]
    if len(set(resolved_dirs)) != len(resolved_dirs):
        raise InspectionError("matrix comparison contains a duplicate directory")
    inspections = [inspect_matrix(path) for path in matrix_dirs]
    for item in inspections:
        _verify_observation_chronology(item.manifest, item.observations)
    matrix_ids = [item.manifest.matrix_id for item in inspections]
    if len(set(matrix_ids)) != len(matrix_ids):
        raise InspectionError("matrix comparison contains a duplicate matrix ID")
    child_run_ids = [
        observation.completion.child_run_id
        for item in inspections
        for observation in item.observations
    ]
    if len(set(child_run_ids)) != len(child_run_ids):
        raise InspectionError("matrix comparison reuses observed child evidence")
    if any(not item.structurally_complete for item in inspections):
        raise InspectionError("comparison requires structurally complete matrices")
    unavailable_by_matrix = {
        item.manifest.matrix_id: [
            unavailable.to_dict() for unavailable in item.unavailable_work_items
        ]
        for item in inspections
    }
    orphan_attempt_ids_by_matrix = {
        item.manifest.matrix_id: list(item.orphan_attempt_ids) for item in inspections
    }
    orphan_attempts_by_matrix = {
        item.manifest.matrix_id: [
            {
                "attempt_id": attempt_id,
                "artifact_hashes": [
                    {"name": name, "sha256": digest} for name, digest in hashes
                ],
            }
            for attempt_id, hashes in item.orphan_attempt_hashes
        ]
        for item in inspections
    }
    if any(not item.eligible for item in inspections):
        return {
            "schema": "steam-agent-eval-matrix-comparison/0.1",
            "matrix_ids": [item.manifest.matrix_id for item in inspections],
            "eligible": False,
            "compatibility_keys": [],
            "excluded_scenario_ids_by_matrix": {
                item.manifest.matrix_id: list(item.manifest.excluded_scenario_ids)
                for item in inspections
            },
            "unavailable_work_items_by_matrix": unavailable_by_matrix,
            "orphan_attempt_ids_by_matrix": orphan_attempt_ids_by_matrix,
            "orphan_attempts_by_matrix": orphan_attempts_by_matrix,
            "vector": None,
        }
    key_sets = [
        {item.compatibility_sha256 for item in inspection.observations}
        for inspection in inspections
    ]
    if any(keys != key_sets[0] for keys in key_sets[1:]):
        raise InspectionError("matrix comparison inputs are incompatible")
    compatibility_cells: dict[_CompatibilityCell, tuple[str, int]] = {}
    for item in inspections:
        for key, signature in _compatibility_cell_signatures(
            item.observations
        ).items():
            existing = compatibility_cells.get(key)
            if existing is not None and existing != signature:
                raise InspectionError("matrix comparison inputs are incompatible")
            compatibility_cells[key] = signature
    observations = [
        observation
        for inspection in inspections
        for observation in inspection.observations
    ]
    return {
        "schema": "steam-agent-eval-matrix-comparison/0.1",
        "matrix_ids": [item.manifest.matrix_id for item in inspections],
        "eligible": True,
        "compatibility_keys": sorted(key_sets[0]),
        "excluded_scenario_ids_by_matrix": {
            item.manifest.matrix_id: list(item.manifest.excluded_scenario_ids)
            for item in inspections
        },
        "unavailable_work_items_by_matrix": unavailable_by_matrix,
        "orphan_attempt_ids_by_matrix": orphan_attempt_ids_by_matrix,
        "orphan_attempts_by_matrix": orphan_attempts_by_matrix,
        "vector": aggregate_observations(observations),
    }


def inspect_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner inspect")
    parser.add_argument("matrix_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = inspect_matrix(args.matrix_dir).to_dict()
    except (InspectionError, matrix.MatrixError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def compare_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner compare")
    parser.add_argument("matrix_dirs", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        result = compare_matrices(args.matrix_dirs)
    except (InspectionError, matrix.MatrixError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0

"""Privacy-safe preparation and import workflow for qualitative review.

The runner deliberately does not invoke a model judge.  It prepares blinded
cases, wraps externally produced verdicts in the existing hash-bound judgment
contract, and derives agreement adjudications mechanically.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from typing import Any, Sequence

from evals.runner import inspection, judge, matrix, run_state


_CASE_SCHEMA = "steam-agent-eval-review-case/0.1"
_VERDICTS_SCHEMA = "steam-agent-eval-review-verdicts/0.1"
_LEDGER_SCHEMA = "steam-agent-eval-review-ledger/0.1"
_OPERATION_SCHEMA = "steam-agent-eval-review-operation/0.1"
_MAX_CASES = 1024
_MAX_ATTEMPTS = 3
_MAX_DURATION_MS = 24 * 60 * 60 * 1000
_ISOLATION_ATTESTATION = "codex-0.146-restricted-profile-v1"
_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_SAFE_WORK_ITEM = re.compile(r"w-[0-9]{6}-[0-9a-f]{16}\Z", re.ASCII)


class ReviewError(RuntimeError):
    """A review package or operation is unsafe, stale, or inconsistent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return matrix._canonical_json_bytes(value)  # noqa: SLF001


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_json(
    path: Path,
    *,
    schema_name: str | None = None,
    require_private: bool = False,
) -> dict[str, Any]:
    try:
        item_stat = path.lstat()
        if not stat.S_ISREG(item_stat.st_mode) or (
            require_private and stat.S_IMODE(item_stat.st_mode) != 0o600
        ):
            raise ReviewError("qualitative review input is not a regular file")
        document = matrix._read_strict_json(  # noqa: SLF001
            path, max_bytes=_MAX_DOCUMENT_BYTES
        )
    except (OSError, matrix.MatrixError):
        raise ReviewError("qualitative review input is invalid") from None
    if not isinstance(document, dict):
        raise ReviewError("qualitative review input is invalid")
    if schema_name is not None:
        try:
            return judge._validate_schema(document, schema_name)  # noqa: SLF001
        except judge.JudgmentError as error:
            raise ReviewError(str(error)) from None
    return document


def _write_json(path: Path, value: Any) -> None:
    try:
        run_state.atomic_publish_private_bytes(path, _canonical_bytes(value))
    except (OSError, FileExistsError, run_state.ManifestStateError):
        raise ReviewError("qualitative review artifact is unavailable") from None


def _private_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    except OSError:
        raise ReviewError("qualitative review directory is unavailable") from None


def _asset_text(matrix_dir: Path, name: str, expected_sha256: str) -> str:
    path = matrix_dir / "calibration" / name
    try:
        item_stat = path.lstat()
        content = path.read_bytes()
    except OSError:
        raise ReviewError(
            "qualitative review calibration asset is unavailable"
        ) from None
    if (
        not stat.S_ISREG(item_stat.st_mode)
        or len(content) > _MAX_DOCUMENT_BYTES
        or hashlib.sha256(content).hexdigest() != expected_sha256
    ):
        raise ReviewError("qualitative review calibration asset does not match")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise ReviewError("qualitative review calibration asset is invalid") from None


def _asset_json(matrix_dir: Path, name: str, expected_sha256: str) -> dict[str, Any]:
    text = _asset_text(matrix_dir, name, expected_sha256)
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        raise ReviewError("qualitative review calibration asset is invalid") from None
    if not isinstance(value, dict):
        raise ReviewError("qualitative review calibration asset is invalid")
    return value


def _target_for(
    target_index: judge._TargetIndex,  # noqa: SLF001
    work_item_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    observation = target_index.observations.get(work_item_id)
    if observation is None:
        raise ReviewError("qualitative review target is unavailable")
    scenario = target_index.scenarios.get(observation.work_item.scenario_id)
    if scenario is None:
        raise ReviewError("qualitative review scenario is unavailable")
    try:
        report_sha256 = dict(observation.completion.artifact_hashes)["report.json"]
        projection = judge._qualitative_projection(  # noqa: SLF001
            observation,
            scenario,
            campaign=target_index.inspection_result.manifest.campaign,
        )
    except (KeyError, judge.JudgmentError) as error:
        raise ReviewError(str(error)) from None
    target = {
        "matrix_id": target_index.inspection_result.manifest.matrix_id,
        "work_item_id": work_item_id,
        "report_sha256": report_sha256,
        "scenario_sha256": scenario.source_sha256,
        "rubric_sha256": scenario.rubric_sha256,
        "projection_sha256": _sha256(projection),
    }
    try:
        judge._target_observation(target_index, target)  # noqa: SLF001
    except judge.JudgmentError as error:
        raise ReviewError(str(error)) from None
    return target, projection


def _case_document(
    matrix_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    work_item_id: str,
) -> dict[str, Any]:
    campaign = target_index.inspection_result.manifest.campaign
    target, projection = _target_for(target_index, work_item_id)
    response_schema = _read_json(judge.SCHEMA_ROOT / "review-verdicts-0.1.json")
    return {
        "schema": _CASE_SCHEMA,
        "execution": {
            "model_input": "this_document_verbatim",
            "criterion_coverage": "every_projection_criterion_exactly_once",
            "external_context": "forbidden",
            "response_schema": {
                "schema": _VERDICTS_SCHEMA,
                "sha256": _sha256(response_schema),
            },
        },
        "target": target,
        "prompt": {
            "version": campaign.prompt_version,
            "sha256": campaign.prompt_sha256,
            "text": _asset_text(
                matrix_dir,
                "matrix-judge-prompt-0.1.md",
                campaign.prompt_sha256,
            ),
        },
        "parser": {
            "version": campaign.parser_version,
            "sha256": campaign.parser_sha256,
            "document": _asset_json(
                matrix_dir,
                "matrix-parser-0.1.json",
                campaign.parser_sha256,
            ),
        },
        "presentation": {"blinded_label": "candidate-A", "order": 0},
        "projection": projection,
    }


def _validate_review_root(
    review_dir: Path, result: inspection.MatrixInspection
) -> dict[str, Any]:
    try:
        item_stat = review_dir.lstat()
    except OSError:
        raise ReviewError("qualitative review directory is unavailable") from None
    if not stat.S_ISDIR(item_stat.st_mode) or stat.S_IMODE(item_stat.st_mode) != 0o700:
        raise ReviewError("qualitative review directory is not private")
    allowed = {
        "cases",
        "operations",
        "ledger.json",
        "matrix.lock",
        "response-schema.json",
    }
    if {item.name for item in review_dir.iterdir()} - allowed:
        raise ReviewError("qualitative review directory contains unexpected nodes")
    for name in ("cases", "operations"):
        path = review_dir / name
        try:
            directory_stat = path.lstat()
        except OSError:
            raise ReviewError("qualitative review directory is unavailable") from None
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise ReviewError("qualitative review directory is not private")
    ledger = _read_json(review_dir / "ledger.json", require_private=True)
    required = {
        "schema",
        "matrix_id",
        "manifest_sha256",
        "prepared_at",
        "policy",
        "response_schema",
        "cases",
    }
    if set(ledger) != required or ledger.get("schema") != _LEDGER_SCHEMA:
        raise ReviewError("qualitative review ledger is invalid")
    if (
        ledger.get("matrix_id") != result.manifest.matrix_id
        or ledger.get("manifest_sha256") != result.manifest_sha256
        or ledger.get("policy")
        != {
            "maximum_attempts_per_judgment": _MAX_ATTEMPTS,
            "model_invocation": "external",
            "usage_accounting": "unavailable",
        }
    ):
        raise ReviewError("qualitative review ledger does not match matrix")
    response_schema = _read_json(
        review_dir / "response-schema.json", require_private=True
    )
    expected_response_schema = _read_json(
        judge.SCHEMA_ROOT / "review-verdicts-0.1.json"
    )
    if (
        ledger.get("response_schema")
        != {
            "path": "response-schema.json",
            "sha256": _sha256(response_schema),
        }
        or response_schema != expected_response_schema
    ):
        raise ReviewError("qualitative review response schema is invalid")
    cases = ledger.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= _MAX_CASES:
        raise ReviewError("qualitative review ledger is invalid")
    expected_work_items = {item.work_item_id for item in result.manifest.work_items}
    seen_work_items: set[str] = set()
    for item in cases:
        work_item_id = item.get("work_item_id") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or set(item) != {"work_item_id", "path", "sha256"}
            or not isinstance(work_item_id, str)
            or _SAFE_WORK_ITEM.fullmatch(work_item_id) is None
            or item.get("path") != f"cases/{work_item_id}.json"
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
            or work_item_id in seen_work_items
        ):
            raise ReviewError("qualitative review ledger is invalid")
        seen_work_items.add(work_item_id)
    if seen_work_items != expected_work_items:
        raise ReviewError("qualitative review ledger does not cover matrix")
    if {item.name for item in (review_dir / "cases").iterdir()} != {
        f"{work_item_id}.json" for work_item_id in expected_work_items
    }:
        raise ReviewError("qualitative review case directory is invalid")
    operation_names = {item.name for item in (review_dir / "operations").iterdir()}
    if len(operation_names) > len(cases) * (len(result.manifest.campaign.judges) + 1):
        raise ReviewError("qualitative review operation ledger exceeds limits")
    valid_operation_names = {
        f"judgment-{work_item_id}-{judge_config.identifier}.json"
        for work_item_id in expected_work_items
        for judge_config in result.manifest.campaign.judges
    } | {
        f"adjudication-{work_item_id}-agreement.json"
        for work_item_id in expected_work_items
    }
    if not operation_names <= valid_operation_names:
        raise ReviewError("qualitative review operation ledger is invalid")
    for name in operation_names:
        _read_json(
            review_dir / "operations" / name,
            require_private=True,
        )
    return ledger


def prepare(matrix_dir: Path, review_dir: Path) -> dict[str, Any]:
    """Publish one immutable route-blind case package for a benchmark."""

    matrix_dir = Path(matrix_dir).resolve()
    review_dir = Path(review_dir).resolve()
    try:
        if review_dir.is_relative_to(matrix_dir):
            raise ReviewError("qualitative review directory must be outside matrix")
        target_index = judge._target_index(matrix_dir)  # noqa: SLF001
    except (judge.JudgmentError, inspection.InspectionError) as error:
        raise ReviewError(str(error)) from None
    result = target_index.inspection_result
    if (
        result.manifest.campaign.campaign_kind != "benchmark"
        or not result.structurally_complete
        or not result.manifest.work_items
        or len(result.manifest.work_items) > _MAX_CASES
    ):
        raise ReviewError("qualitative review requires a complete benchmark")
    if review_dir.exists():
        raise ReviewError("qualitative review directory already exists")
    parent = review_dir.parent
    if not parent.is_dir():
        raise ReviewError("qualitative review parent directory is unavailable")
    staging = Path(tempfile.mkdtemp(prefix=f".{review_dir.name}-", dir=parent))
    try:
        staging.chmod(0o700)
        cases_dir = staging / "cases"
        operations_dir = staging / "operations"
        _private_dir(cases_dir)
        _private_dir(operations_dir)
        response_schema = _read_json(judge.SCHEMA_ROOT / "review-verdicts-0.1.json")
        _write_json(staging / "response-schema.json", response_schema)
        case_entries: list[dict[str, Any]] = []
        for work_item in result.manifest.work_items:
            document = _case_document(matrix_dir, target_index, work_item.work_item_id)
            try:
                judge._validate_schema(document, "review-case-0.1.json")  # noqa: SLF001
            except judge.JudgmentError as error:
                raise ReviewError(str(error)) from None
            filename = f"{work_item.work_item_id}.json"
            _write_json(cases_dir / filename, document)
            case_entries.append(
                {
                    "work_item_id": work_item.work_item_id,
                    "path": f"cases/{filename}",
                    "sha256": _sha256(document),
                }
            )
        ledger = {
            "schema": _LEDGER_SCHEMA,
            "matrix_id": result.manifest.matrix_id,
            "manifest_sha256": result.manifest_sha256,
            "prepared_at": _now(),
            "policy": {
                "maximum_attempts_per_judgment": _MAX_ATTEMPTS,
                "model_invocation": "external",
                "usage_accounting": "unavailable",
            },
            "response_schema": {
                "path": "response-schema.json",
                "sha256": _sha256(response_schema),
            },
            "cases": case_entries,
        }
        _write_json(staging / "ledger.json", ledger)
        os.replace(staging, review_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "matrix_id": result.manifest.matrix_id,
        "cases": len(result.manifest.work_items),
    }


def _case_entry(ledger: dict[str, Any], work_item_id: str) -> dict[str, Any]:
    matches = [
        item
        for item in ledger["cases"]
        if isinstance(item, dict) and item.get("work_item_id") == work_item_id
    ]
    if len(matches) != 1:
        raise ReviewError("qualitative review case is unavailable")
    item = matches[0]
    if set(item) != {"work_item_id", "path", "sha256"}:
        raise ReviewError("qualitative review ledger is invalid")
    return item


def _load_bound_case(
    matrix_dir: Path,
    review_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    ledger: dict[str, Any],
    work_item_id: str,
) -> dict[str, Any]:
    if _SAFE_WORK_ITEM.fullmatch(work_item_id) is None:
        raise ReviewError("qualitative review work item is invalid")
    entry = _case_entry(ledger, work_item_id)
    if entry["path"] != f"cases/{work_item_id}.json":
        raise ReviewError("qualitative review case path is invalid")
    case_path = review_dir / entry["path"]
    document = _read_json(
        case_path,
        schema_name="review-case-0.1.json",
        require_private=True,
    )
    expected = _case_document(matrix_dir, target_index, work_item_id)
    if document != expected or _sha256(document) != entry["sha256"]:
        raise ReviewError("qualitative review case does not match matrix")
    return document


def _operation_path(
    review_dir: Path, kind: str, work_item_id: str, suffix: str
) -> Path:
    return review_dir / "operations" / f"{kind}-{work_item_id}-{suffix}.json"


def _publish_operation(path: Path, document: dict[str, Any]) -> None:
    if path.exists():
        retained = _read_json(path, require_private=True)
        if retained != document:
            raise ReviewError("qualitative review operation already exists")
        return
    _write_json(path, document)


def _validate_judgment_operation(
    operation: dict[str, Any],
    *,
    case: dict[str, Any],
    judge_identifier: str,
) -> dict[str, Any]:
    required = {
        "schema",
        "kind",
        "matrix_id",
        "work_item_id",
        "judge_identifier",
        "attempt_count",
        "duration_ms",
        "usage",
        "isolation_attestation",
        "case_sha256",
        "artifact_sha256",
        "artifact",
        "recorded_at",
    }
    artifact = operation.get("artifact")
    artifact_judge = artifact.get("judge") if isinstance(artifact, dict) else None
    if (
        set(operation) != required
        or operation.get("schema") != _OPERATION_SCHEMA
        or operation.get("kind") != "judgment_import"
        or operation.get("matrix_id") != case["target"]["matrix_id"]
        or operation.get("work_item_id") != case["target"]["work_item_id"]
        or operation.get("judge_identifier") != judge_identifier
        or not isinstance(operation.get("attempt_count"), int)
        or isinstance(operation.get("attempt_count"), bool)
        or not 1 <= operation["attempt_count"] <= _MAX_ATTEMPTS
        or not isinstance(operation.get("duration_ms"), int)
        or isinstance(operation.get("duration_ms"), bool)
        or not 0 <= operation["duration_ms"] <= _MAX_DURATION_MS
        or operation.get("usage") != {"state": "unavailable"}
        or operation.get("isolation_attestation") != _ISOLATION_ATTESTATION
        or operation.get("case_sha256") != _sha256(case)
        or not isinstance(artifact, dict)
        or artifact.get("target") != case["target"]
        or not isinstance(artifact_judge, dict)
        or artifact_judge.get("identifier") != judge_identifier
        or operation.get("artifact_sha256") != _sha256(artifact)
    ):
        raise ReviewError("qualitative review operation is invalid")
    return artifact


def _validate_adjudication_operation(
    operation: dict[str, Any], *, case: dict[str, Any]
) -> dict[str, Any]:
    required = {
        "schema",
        "kind",
        "matrix_id",
        "work_item_id",
        "case_sha256",
        "artifact_sha256",
        "artifact",
        "recorded_at",
    }
    artifact = operation.get("artifact")
    if (
        set(operation) != required
        or operation.get("schema") != _OPERATION_SCHEMA
        or operation.get("kind") != "adjudication_import"
        or operation.get("matrix_id") != case["target"]["matrix_id"]
        or operation.get("work_item_id") != case["target"]["work_item_id"]
        or operation.get("case_sha256") != _sha256(case)
        or not isinstance(artifact, dict)
        or artifact.get("target") != case["target"]
        or operation.get("artifact_sha256") != _sha256(artifact)
    ):
        raise ReviewError("qualitative review operation is invalid")
    return artifact


def _import_document_locked(
    matrix_dir: Path, kind: str, document: dict[str, Any]
) -> tuple[Path, str]:
    """Import a validated artifact while the caller holds the matrix lock."""

    with tempfile.TemporaryDirectory(prefix="steam-agent-review-import-") as name:
        root = Path(name)
        root.chmod(0o700)
        source = root / f"{kind}.json"
        _write_json(source, document)
        if kind == "judgment":
            return judge._import_judgment_locked(matrix_dir, source)  # noqa: SLF001
        return judge._import_adjudication_locked(matrix_dir, source)  # noqa: SLF001


def _retained_target(
    path: Path,
    document: dict[str, Any],
    digest: str,
    *,
    kind: str,
) -> tuple[Path, str] | None:
    """Return an exact private retained target, or ``None`` when absent."""

    try:
        item_stat = path.lstat()
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise ReviewError(f"retained {kind} is unavailable") from None
    if (
        not stat.S_ISREG(item_stat.st_mode)
        or stat.S_IMODE(item_stat.st_mode) != 0o600
        or content != _canonical_bytes(document)
        or hashlib.sha256(content).hexdigest() != digest
    ):
        raise ReviewError(f"retained {kind} does not match review operation")
    return path, digest


def _reject_existing_target(path: Path, *, kind: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ReviewError(f"qualitative {kind} target is unavailable") from None
    raise ReviewError(f"qualitative {kind} target already exists")


def assemble_judgment(
    matrix_dir: Path,
    review_dir: Path,
    work_item_id: str,
    verdicts_path: Path,
    *,
    judge_identifier: str,
    attempt_count: int,
    duration_ms: int,
    isolation_attestation: str,
) -> dict[str, Any]:
    """Validate external verdicts, assemble judgment 0.1, and import it."""

    if (
        not 1 <= attempt_count <= _MAX_ATTEMPTS
        or not 0 <= duration_ms <= _MAX_DURATION_MS
        or isolation_attestation != _ISOLATION_ATTESTATION
    ):
        raise ReviewError("qualitative review operational measurement is invalid")
    matrix_dir = Path(matrix_dir).resolve()
    review_dir = Path(review_dir).resolve()
    with matrix.MatrixLock(review_dir):
        with matrix.MatrixLock(matrix_dir):
            try:
                judge._reject_finalized_screen(matrix_dir)  # noqa: SLF001
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except judge.JudgmentError as error:
                raise ReviewError(str(error)) from None
            ledger = _validate_review_root(review_dir, target_index.inspection_result)
            case = _load_bound_case(
                matrix_dir, review_dir, target_index, ledger, work_item_id
            )
            campaign = target_index.inspection_result.manifest.campaign
            judges = [
                item for item in campaign.judges if item.identifier == judge_identifier
            ]
            if len(judges) != 1:
                raise ReviewError("qualitative review judge is not configured")
            operation_path = _operation_path(
                review_dir, "judgment", work_item_id, judge_identifier
            )

            # The durable operation is the recovery source. Resuming must not
            # depend on the disposable external response still existing.
            if operation_path.exists():
                operation = _read_json(operation_path, require_private=True)
                document = _validate_judgment_operation(
                    operation,
                    case=case,
                    judge_identifier=judge_identifier,
                )
                judge._validate_judgment_document(  # noqa: SLF001
                    target_index, document
                )
                digest = operation["artifact_sha256"]
                target = matrix_dir / "judgments" / f"{document['judgment_id']}.json"
                retained = _retained_target(target, document, digest, kind="judgment")
                if retained is None:
                    retained = _import_document_locked(matrix_dir, "judgment", document)
                return {"path": retained[0].name, "sha256": retained[1]}

            verdict_document = _read_json(
                Path(verdicts_path), schema_name="review-verdicts-0.1.json"
            )
            expected_response_target = {
                "work_item_id": case["target"]["work_item_id"],
                "projection_sha256": case["target"]["projection_sha256"],
            }
            if verdict_document["target"] != expected_response_target:
                raise ReviewError("qualitative verdicts target a different case")
            criteria = [item["id"] for item in case["projection"]["criteria"]]
            verdict_map = judge._criterion_map(  # noqa: SLF001
                verdict_document["verdicts"], field="verdict"
            )
            if set(verdict_map) != set(criteria):
                raise ReviewError("qualitative verdicts do not cover the exact rubric")
            ordered_verdicts = sorted(
                verdict_document["verdicts"],
                key=lambda item: criteria.index(item["criterion_id"]),
            )
            document = {
                "schema": "steam-agent-eval-judgment/0.1",
                "judgment_id": f"judgment-{work_item_id}-{judge_identifier}",
                "target": case["target"],
                "judge": judges[0].to_dict(),
                "prompt": {
                    "version": campaign.prompt_version,
                    "sha256": campaign.prompt_sha256,
                },
                "parser": {
                    "version": campaign.parser_version,
                    "sha256": campaign.parser_sha256,
                },
                "presentation": case["presentation"],
                "verdicts": ordered_verdicts,
                "created_at": _now(),
            }
            judge._validate_judgment_document(  # noqa: SLF001
                target_index, document
            )
            artifact_sha256 = _sha256(document)
            target = matrix_dir / "judgments" / f"{document['judgment_id']}.json"
            _reject_existing_target(target, kind="judgment")
            operation = {
                "schema": _OPERATION_SCHEMA,
                "kind": "judgment_import",
                "matrix_id": case["target"]["matrix_id"],
                "work_item_id": work_item_id,
                "judge_identifier": judge_identifier,
                "attempt_count": attempt_count,
                "duration_ms": duration_ms,
                "usage": {"state": "unavailable"},
                "isolation_attestation": isolation_attestation,
                "case_sha256": _sha256(case),
                "artifact_sha256": artifact_sha256,
                "artifact": document,
                "recorded_at": _now(),
            }
            _publish_operation(operation_path, operation)
            path, digest = _import_document_locked(matrix_dir, "judgment", document)
            return {"path": path.name, "sha256": digest}


def _existing_operation_artifact(
    path: Path,
    matrix_dir: Path,
    *,
    case: dict[str, Any],
    target_index: judge._TargetIndex,  # noqa: SLF001
    retained: dict[str, dict[str, Any]],
) -> tuple[Path, str] | None:
    if not path.exists():
        return None
    operation = _read_json(path, require_private=True)
    artifact = _validate_adjudication_operation(operation, case=case)
    judge._validate_adjudication_document(  # noqa: SLF001
        matrix_dir, target_index, artifact, retained=retained
    )
    digest = operation["artifact_sha256"]
    target = matrix_dir / "adjudications" / f"{artifact.get('adjudication_id')}.json"
    retained_target = _retained_target(target, artifact, digest, kind="adjudication")
    if retained_target is not None:
        return retained_target
    return _import_document_locked(matrix_dir, "adjudication", artifact)


def resolve_agreement(matrix_dir: Path, review_dir: Path) -> dict[str, Any]:
    """Import one mechanical agreement adjudication for every complete roster."""

    matrix_dir = Path(matrix_dir).resolve()
    review_dir = Path(review_dir).resolve()
    imported = 0
    retained_count = 0
    with matrix.MatrixLock(review_dir):
        with matrix.MatrixLock(matrix_dir):
            try:
                judge._reject_finalized_screen(matrix_dir)  # noqa: SLF001
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except judge.JudgmentError as error:
                raise ReviewError(str(error)) from None
            ledger = _validate_review_root(review_dir, target_index.inspection_result)
            retained = judge._retained_judgments(  # noqa: SLF001
                matrix_dir, target_index
            )
            campaign = target_index.inspection_result.manifest.campaign
            for entry in ledger["cases"]:
                work_item_id = entry["work_item_id"]
                case = _load_bound_case(
                    matrix_dir, review_dir, target_index, ledger, work_item_id
                )
                operation_path = _operation_path(
                    review_dir, "adjudication", work_item_id, "agreement"
                )
                existing = _existing_operation_artifact(
                    operation_path,
                    matrix_dir,
                    case=case,
                    target_index=target_index,
                    retained=retained,
                )
                if existing is not None:
                    retained_count += 1
                    continue
                matching = [
                    (digest, document)
                    for digest, document in retained.items()
                    if document["target"] == case["target"]
                ]
                configured = {item.identifier: item for item in campaign.judges}
                by_judge: dict[str, tuple[str, dict[str, Any]]] = {}
                for digest, document in matching:
                    identifier = document["judge"]["identifier"]
                    if identifier not in configured or identifier in by_judge:
                        raise ReviewError("qualitative judgment roster is ambiguous")
                    by_judge[identifier] = (digest, document)
                if set(by_judge) != set(configured):
                    raise ReviewError(
                        f"qualitative judgment roster is incomplete for {work_item_id}"
                    )
                for judge_config in campaign.judges:
                    operation = _read_json(
                        _operation_path(
                            review_dir,
                            "judgment",
                            work_item_id,
                            judge_config.identifier,
                        ),
                        require_private=True,
                    )
                    operation_artifact = _validate_judgment_operation(
                        operation,
                        case=case,
                        judge_identifier=judge_config.identifier,
                    )
                    digest, retained_artifact = by_judge[judge_config.identifier]
                    if (
                        operation.get("artifact_sha256") != digest
                        or operation_artifact != retained_artifact
                    ):
                        raise ReviewError(
                            "qualitative judgment does not match operation ledger"
                        )
                hashes = [by_judge[item.identifier][0] for item in campaign.judges]
                verdict_maps = [
                    judge._criterion_map(  # noqa: SLF001
                        by_judge[item.identifier][1]["verdicts"], field="verdict"
                    )
                    for item in campaign.judges
                ]
                criteria = [item["id"] for item in case["projection"]["criteria"]]
                outcomes = []
                for criterion_id in criteria:
                    values = {item[criterion_id] for item in verdict_maps}
                    outcome = (
                        next(iter(values))
                        if len(values) == 1 and "uncertain" not in values
                        else "unresolved"
                    )
                    outcomes.append({"criterion_id": criterion_id, "outcome": outcome})
                document = {
                    "schema": "steam-agent-eval-adjudication/0.1",
                    "adjudication_id": f"adjudication-{work_item_id}",
                    "target": case["target"],
                    "method": campaign.adjudication_method,
                    "adjudicator": campaign.adjudicator,
                    "judgment_sha256s": hashes,
                    "outcomes": outcomes,
                    "created_at": _now(),
                }
                judge._validate_adjudication_document(  # noqa: SLF001
                    matrix_dir,
                    target_index,
                    document,
                    retained=retained,
                )
                target = (
                    matrix_dir / "adjudications" / f"{document['adjudication_id']}.json"
                )
                _reject_existing_target(target, kind="adjudication")
                operation = {
                    "schema": _OPERATION_SCHEMA,
                    "kind": "adjudication_import",
                    "matrix_id": case["target"]["matrix_id"],
                    "work_item_id": work_item_id,
                    "case_sha256": _sha256(case),
                    "artifact_sha256": _sha256(document),
                    "artifact": document,
                    "recorded_at": _now(),
                }
                _publish_operation(operation_path, operation)
                _import_document_locked(matrix_dir, "adjudication", document)
                imported += 1
    return {"imported": imported, "retained": retained_count}


def review_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner review")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("matrix_dir", type=Path)
    prepare_parser.add_argument("review_dir", type=Path)
    assemble_parser = subparsers.add_parser("assemble")
    assemble_parser.add_argument("matrix_dir", type=Path)
    assemble_parser.add_argument("review_dir", type=Path)
    assemble_parser.add_argument("work_item_id")
    assemble_parser.add_argument("verdicts", type=Path)
    assemble_parser.add_argument("--judge", required=True)
    assemble_parser.add_argument("--attempt-count", required=True, type=int)
    assemble_parser.add_argument("--duration-ms", required=True, type=int)
    assemble_parser.add_argument(
        "--isolation-attestation",
        required=True,
        choices=(_ISOLATION_ATTESTATION,),
    )
    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("matrix_dir", type=Path)
    resolve_parser.add_argument("review_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.matrix_dir, args.review_dir)
        elif args.command == "assemble":
            result = assemble_judgment(
                args.matrix_dir,
                args.review_dir,
                args.work_item_id,
                args.verdicts,
                judge_identifier=args.judge,
                attempt_count=args.attempt_count,
                duration_ms=args.duration_ms,
                isolation_attestation=args.isolation_attestation,
            )
        else:
            result = resolve_agreement(args.matrix_dir, args.review_dir)
    except (
        ReviewError,
        judge.JudgmentError,
        inspection.InspectionError,
        matrix.MatrixError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("qualitative review filesystem operation failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0

"""Strict imported judgment and adjudication artifacts.

These artifacts assess only the opt-in qualitative rubric.  They are hash-bound
to an immutable generator report and never mutate or replace its deterministic
layer vector.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any, Sequence

from jsonschema import Draft202012Validator, FormatChecker

from evals.runner import grade, inspection, matrix, run_state


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = ROOT / "evals" / "schema"
_MAX_ARTIFACT_BYTES = 1024 * 1024
_MAX_PROJECTION_BYTES = 16 * 1024 * 1024
_MAX_SELECTED_EVIDENCE_BYTES = 1024 * 1024
_FORBIDDEN_PROJECTION_MARKERS = (
    '"metrics"',
    '"passed"',
    "agent_turns",
    "effective_model",
    "reasoning_effort",
    "requested_model",
    "requested_route_confirmed",
    "tool_policy",
)
_FORBIDDEN_METADATA_IDENTITIES = frozenset(
    {
        "agent_turns",
        "claims",
        "failed",
        "metrics",
        "oracle",
        "passed",
        "privacy",
        "reasoning_effort",
        "requested_model",
        "requested_route_confirmed",
        "tool_policy",
    }
)
_REASONING_EFFORT_IDENTITIES = frozenset({"low", "medium", "high", "xhigh"})
_FIXED_ROUTE_IDENTITIES = frozenset({"sol", "terra", "luna"})
_ROUTE_CONTEXT_IDENTITIES = frozenset(
    {"candidate", "effort", "generator", "model", "reasoning", "route"}
)
_DETERMINISTIC_LAYER_IDENTITIES = frozenset(
    {"claims", "metrics", "oracle", "privacy", "tool"}
)
_DETERMINISTIC_OUTCOME_IDENTITIES = frozenset(
    {
        "fail",
        "failed",
        "failing",
        "fails",
        "false",
        "failure",
        "null",
        "pass",
        "passed",
        "passes",
        "passing",
        "success",
        "succeeded",
        "true",
        "unknown",
        "unresolved",
    }
)
_BOUND_RATIONALE_IDENTITIES = (
    ("agent", "turns"),
    ("deterministic", "passed"),
    ("effective", "model"),
    ("reasoning", "effort"),
    ("requested", "model"),
    ("requested", "route", "confirmed"),
    ("tool", "policy"),
)
_IDENTITY_TOKEN = re.compile(r"[A-Za-z0-9]+", re.ASCII)


class JudgmentError(RuntimeError):
    """A qualitative artifact is invalid, unbound, or internally inconsistent."""


def _identity_tokens(value: str) -> tuple[str, ...]:
    return tuple(token.casefold() for token in _IDENTITY_TOKEN.findall(value))


def _contains_token_sequence(
    tokens: tuple[str, ...], sequence: tuple[str, ...]
) -> bool:
    width = len(sequence)
    return any(
        tokens[index : index + width] == sequence
        for index in range(len(tokens) - width + 1)
    )


def _validate_schema(document: Any, schema_name: str) -> dict[str, Any]:
    schema = matrix._read_strict_json(  # noqa: SLF001
        SCHEMA_ROOT / schema_name, max_bytes=_MAX_ARTIFACT_BYTES
    )
    if not isinstance(document, dict):
        raise JudgmentError("qualitative artifact is not an object")
    try:
        Draft202012Validator.check_schema(schema)
        valid = Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).is_valid(document)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise JudgmentError("qualitative artifact schema is invalid")
    return document


def _read_import(path: Path, schema_name: str) -> tuple[dict[str, Any], bytes]:
    try:
        item_stat = Path(path).lstat()
        if (
            not stat.S_ISREG(item_stat.st_mode)
            or item_stat.st_size > _MAX_ARTIFACT_BYTES
        ):
            raise JudgmentError("qualitative artifact input is invalid")
        document = matrix._read_strict_json(  # noqa: SLF001
            Path(path), max_bytes=_MAX_ARTIFACT_BYTES
        )
    except (OSError, matrix.MatrixError):
        raise JudgmentError("qualitative artifact input is invalid") from None
    validated = _validate_schema(document, schema_name)
    return validated, matrix._canonical_json_bytes(validated)  # noqa: SLF001


def _captured_cli_document(observation: inspection.Observation) -> dict[str, Any]:
    documents = observation.report.get("required_cli_documents")
    diagnostics = observation.report.get("diagnostics")
    capture = (
        diagnostics.get("evidence_capture")
        if isinstance(diagnostics, dict)
        else None
    )
    if isinstance(documents, list) and len(documents) > 1:
        raise JudgmentError("qualitative selected evidence is ambiguous")
    if (
        not isinstance(documents, list)
        or len(documents) != 1
        or not isinstance(documents[0], dict)
        or not isinstance(capture, dict)
        or capture.get("state") != "captured"
        or capture.get("successful_candidates") != 1
    ):
        raise JudgmentError("qualitative selected evidence is unavailable")
    return documents[0]


def _selected_evidence(document: dict[str, Any], path: str) -> dict[str, Any]:
    try:
        values, plural = grade.select_path(document, path)
    except (IndexError, KeyError, TypeError, ValueError):
        raise JudgmentError("qualitative selected evidence is unavailable") from None
    if not plural and len(values) != 1:
        raise JudgmentError("qualitative selected evidence is ambiguous")
    evidence = (
        {"cardinality": "many", "values": values}
        if plural
        else {"cardinality": "one", "value": values[0]}
    )
    try:
        content = matrix._canonical_json_bytes(evidence)  # noqa: SLF001
    except matrix.MatrixError:
        raise JudgmentError("qualitative selected evidence is invalid") from None
    if len(content) > _MAX_SELECTED_EVIDENCE_BYTES:
        raise JudgmentError("qualitative selected evidence exceeds safety limits")
    return evidence


def _conditional_selected_evidence(
    observation: inspection.Observation, path: str
) -> dict[str, Any]:
    try:
        document = _captured_cli_document(observation)
    except JudgmentError as error:
        if str(error) != "qualitative selected evidence is unavailable":
            raise
        return {"cardinality": "zero", "state": "capture_unavailable"}
    try:
        values, plural = grade.select_path(document, path)
    except (IndexError, KeyError, TypeError):
        return {"cardinality": "zero", "state": "path_unavailable"}
    except ValueError:
        raise JudgmentError("qualitative selected evidence is invalid") from None
    if not plural and len(values) != 1:
        raise JudgmentError("qualitative selected evidence is ambiguous")
    if not values:
        evidence: dict[str, Any] = {
            "cardinality": "zero",
            "state": "empty_selection",
            "values": [],
        }
    elif len(values) == 1:
        evidence = {"cardinality": "one", "value": values[0]}
    else:
        evidence = {"cardinality": "many", "values": values}
    try:
        content = matrix._canonical_json_bytes(evidence)  # noqa: SLF001
    except matrix.MatrixError:
        raise JudgmentError("qualitative selected evidence is invalid") from None
    if len(content) > _MAX_SELECTED_EVIDENCE_BYTES:
        raise JudgmentError("qualitative selected evidence exceeds safety limits")
    return evidence


def _qualitative_projection(
    observation: inspection.Observation,
    scenario: run_state.MatrixScenario,
) -> dict[str, Any]:
    answers = observation.report.get("qualitative_review_answers")
    if not isinstance(answers, list) or not answers or len(answers) > 128:
        raise JudgmentError("qualitative projection is unavailable")
    expected_turn = -1
    texts: list[str] = []
    for item in answers:
        if not isinstance(item, dict) or set(item) != {"turn", "text"}:
            raise JudgmentError("qualitative projection contains grading metadata")
        turn = item["turn"]
        text = item["text"]
        if (
            not isinstance(turn, int)
            or isinstance(turn, bool)
            or turn <= expected_turn
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise JudgmentError("qualitative projection is invalid")
        expected_turn = turn
        texts.append(text)
    must_mention = tuple(
        item
        for item in scenario.qualitative_criteria
        if item.source == "fact_rubric.must_mention"
    )
    captured_document = (
        _captured_cli_document(observation) if must_mention else None
    )
    projected_criteria: list[dict[str, Any]] = []
    for criterion in scenario.qualitative_criteria:
        projected = criterion.to_dict()
        if criterion.source == "fact_rubric.must_mention":
            assert criterion.evidence_path is not None
            assert captured_document is not None
            projected["selected_evidence"] = _selected_evidence(
                captured_document, criterion.evidence_path
            )
        elif criterion.source == "fact_rubric.support_if_claimed":
            assert criterion.evidence_path is not None
            projected["selected_evidence"] = _conditional_selected_evidence(
                observation, criterion.evidence_path
            )
        projected_criteria.append(projected)
    projection = {
        "schema": "steam-agent-eval-qualitative-projection/0.1",
        "criteria": projected_criteria,
        "answers": answers,
    }
    content = matrix._canonical_json_bytes(projection)  # noqa: SLF001
    rendered = content.decode("ascii")
    if len(content) > _MAX_PROJECTION_BYTES:
        raise JudgmentError("qualitative projection exceeds safety limits")
    combined = "\n".join(texts)
    folded = combined.casefold()
    candidate_model = observation.work_item.route.model
    if (
        "eval_canary_" in rendered.casefold()
        or grade.find_private_host_paths(rendered)
        or grade.find_private_host_paths(combined)
        or any(marker in folded for marker in _FORBIDDEN_PROJECTION_MARKERS)
        or (
            candidate_model is not None
            and len(candidate_model) >= 3
            and candidate_model.casefold() in folded
        )
    ):
        raise JudgmentError("qualitative projection contains prohibited material")
    return projection


def _projection_digest(
    observation: inspection.Observation,
    scenario: run_state.MatrixScenario,
) -> str:
    projection = _qualitative_projection(observation, scenario)
    return hashlib.sha256(
        matrix._canonical_json_bytes(projection)  # noqa: SLF001
    ).hexdigest()


def _reject_unsafe_metadata(
    document: dict[str, Any], observation: inspection.Observation
) -> None:
    rendered = matrix._canonical_json_bytes(document).decode("ascii")  # noqa: SLF001
    if "eval_canary_" in rendered.casefold() or grade.find_private_host_paths(rendered):
        raise JudgmentError("qualitative metadata contains private material")
    candidate_model = observation.work_item.route.model
    protected_model = candidate_model.casefold() if candidate_model is not None else None

    def reject_rationale(value: str) -> None:
        folded = value.casefold()
        token_sequence = _identity_tokens(value)
        tokens = frozenset(token_sequence)
        model_is_present = (
            protected_model is not None
            and re.search(
                rf"(?<![A-Za-z0-9]){re.escape(protected_model)}(?![A-Za-z0-9])",
                folded,
            )
            is not None
        )
        route_alias_is_present = bool(tokens & _FIXED_ROUTE_IDENTITIES)
        effort_is_bound = bool(
            tokens & _REASONING_EFFORT_IDENTITIES
            and tokens & _ROUTE_CONTEXT_IDENTITIES
        )
        if (
            model_is_present
            or route_alias_is_present
            or "xhigh" in tokens
            or effort_is_bound
        ):
            raise JudgmentError(
                "qualitative rationale contains candidate route material"
            )
        if any(
            _contains_token_sequence(token_sequence, identity)
            for identity in _BOUND_RATIONALE_IDENTITIES
        ) or (
            tokens & _DETERMINISTIC_LAYER_IDENTITIES
            and tokens & _DETERMINISTIC_OUTCOME_IDENTITIES
        ):
            raise JudgmentError(
                "qualitative rationale contains deterministic outcome material"
            )

    def reject_identity(value: Any) -> None:
        if isinstance(value, str):
            folded = value.casefold()
            tokens = frozenset(_identity_tokens(value))
            if (
                protected_model is not None
                and protected_model in folded
            ) or tokens & (
                _REASONING_EFFORT_IDENTITIES | _FIXED_ROUTE_IDENTITIES
            ):
                raise JudgmentError(
                    "qualitative metadata contains candidate route material"
                )
            return
        if isinstance(value, dict):
            for item in value.values():
                reject_identity(item)
        elif isinstance(value, list):
            for item in value:
                reject_identity(item)

    reject_identity(document.get("judgment_id"))
    for section_name in ("target", "prompt", "parser", "presentation"):
        section = document.get(section_name)
        if section is not None:
            reject_identity(section)
    judge_metadata = document.get("judge")
    if isinstance(judge_metadata, dict):
        for key, value in judge_metadata.items():
            if key not in {"model", "reasoning_effort"}:
                reject_identity(value)
    verdicts = document.get("verdicts")
    if isinstance(verdicts, list):
        for verdict in verdicts:
            if isinstance(verdict, dict) and isinstance(
                verdict.get("rationale"), str
            ):
                reject_rationale(verdict["rationale"])
    for section_name in ("judge", "prompt", "parser"):
        section = document.get(section_name)
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            if key in {
                "model",
                "reasoning_effort",
                "sha256",
                "settings_sha256",
            } or not isinstance(value, str):
                continue
            identity = value.casefold().replace("-", "_")
            if identity in _FORBIDDEN_METADATA_IDENTITIES:
                raise JudgmentError(
                    "qualitative metadata contains deterministic outcome material"
                )


def _target_observation(
    matrix_dir: Path, target: dict[str, Any]
) -> tuple[inspection.MatrixInspection, inspection.Observation, run_state.MatrixScenario]:
    try:
        result = inspection.inspect_matrix(matrix_dir)
    except inspection.InspectionError as error:
        raise JudgmentError(str(error)) from None
    if target.get("matrix_id") != result.manifest.matrix_id:
        raise JudgmentError("qualitative target matrix does not match")
    observation = next(
        (
            item
            for item in result.observations
            if item.work_item.work_item_id == target.get("work_item_id")
        ),
        None,
    )
    if observation is None:
        raise JudgmentError("qualitative target work item is unavailable")
    metrics = observation.report.get("metrics")
    privacy = metrics.get("privacy") if isinstance(metrics, dict) else None
    if not isinstance(privacy, dict) or privacy.get("passed") is not True:
        raise JudgmentError("qualitative target is not privacy-cleared")
    scenario = next(
        item
        for item in result.manifest.inputs.scenarios
        if item.scenario_id == observation.work_item.scenario_id
    )
    report_hash = dict(observation.completion.artifact_hashes)["report.json"]
    projection_hash = _projection_digest(observation, scenario)
    if (
        target.get("report_sha256") != report_hash
        or target.get("scenario_sha256") != scenario.source_sha256
        or target.get("rubric_sha256") != scenario.rubric_sha256
        or target.get("projection_sha256") != projection_hash
    ):
        raise JudgmentError("qualitative target digest does not match")
    return result, observation, scenario


def _criterion_map(items: Any, *, field: str) -> dict[str, str]:
    if not isinstance(items, list):
        raise JudgmentError("qualitative criteria are invalid")
    result: dict[str, str] = {}
    for item in items:
        criterion_id = item.get("criterion_id") if isinstance(item, dict) else None
        value = item.get(field) if isinstance(item, dict) else None
        if not isinstance(criterion_id, str) or not isinstance(value, str):
            raise JudgmentError("qualitative criteria are invalid")
        if field == "verdict":
            rationale = item.get("rationale")
            if not isinstance(rationale, str) or not 1 <= len(rationale.split()) <= 12:
                raise JudgmentError("judgment rationale is invalid")
        if criterion_id in result:
            raise JudgmentError("qualitative criterion is duplicated")
        result[criterion_id] = value
    return result


def _configured_judge(document: dict[str, Any]) -> run_state.MatrixJudgeConfiguration:
    try:
        return run_state.MatrixJudgeConfiguration.from_dict(document["judge"])
    except (KeyError, run_state.ManifestStateError):
        raise JudgmentError("judgment judge configuration is invalid") from None


def _validate_judgment_policy(
    campaign: run_state.MatrixCampaign, document: dict[str, Any]
) -> run_state.MatrixJudgeConfiguration:
    configured = _configured_judge(document)
    if (
        configured not in campaign.judges
        or document["prompt"]
        != {"version": campaign.prompt_version, "sha256": campaign.prompt_sha256}
        or document["parser"]
        != {"version": campaign.parser_version, "sha256": campaign.parser_sha256}
    ):
        raise JudgmentError("judgment does not match campaign judge policy")
    return configured


def _requires_calibrated_policy(
    campaign: run_state.MatrixCampaign,
    scenario: run_state.MatrixScenario,
) -> bool:
    return campaign.campaign_kind == "qualification" or any(
        item.source == "fact_rubric.criteria.hard_fail"
        for item in scenario.qualitative_criteria
    )


def _validate_adjudication_policy(
    campaign: run_state.MatrixCampaign,
    document: dict[str, Any],
    judgments: Sequence[dict[str, Any]],
) -> None:
    configured = tuple(_configured_judge(item) for item in judgments)
    if (
        document["method"] != campaign.adjudication_method
        or document["adjudicator"] != campaign.adjudicator
        or len(configured) != len(campaign.judges)
        or len(set(configured)) != len(configured)
        or set(configured) != set(campaign.judges)
    ):
        raise JudgmentError("adjudication does not match campaign judge policy")


def _publish_artifact(
    matrix_dir: Path,
    *,
    collection: str,
    artifact_id: str,
    content: bytes,
) -> tuple[Path, str]:
    root = Path(matrix_dir) / collection
    if not root.exists():
        try:
            root.mkdir(mode=0o700)
            root.chmod(0o700)
        except OSError:
            raise JudgmentError("qualitative artifact directory is unavailable") from None
    path = root / f"{artifact_id}.json"
    try:
        run_state.atomic_publish_private_bytes(path, content)
    except (OSError, FileExistsError, run_state.ManifestStateError):
        raise JudgmentError("qualitative artifact already exists or is unavailable") from None
    return path, hashlib.sha256(content).hexdigest()


def _reject_finalized_screen(matrix_dir: Path) -> None:
    try:
        (Path(matrix_dir) / "acceptance.json").lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise JudgmentError("finalized screen state is invalid") from None
    raise JudgmentError("finalized screen cannot accept qualitative artifacts")


def _import_judgment_locked(matrix_dir: Path, source: Path) -> tuple[Path, str]:
    document, content = _read_import(source, "judgment-0.1.json")
    result, observation, scenario = _target_observation(
        matrix_dir, document["target"]
    )
    _reject_unsafe_metadata(document, observation)
    verdicts = _criterion_map(document["verdicts"], field="verdict")
    if set(verdicts) != set(scenario.criterion_ids):
        raise JudgmentError("judgment does not cover the exact rubric")
    if _requires_calibrated_policy(result.manifest.campaign, scenario):
        _validate_judgment_policy(result.manifest.campaign, document)
    return _publish_artifact(
        matrix_dir,
        collection="judgments",
        artifact_id=document["judgment_id"],
        content=content,
    )


def import_judgment(matrix_dir: Path, source: Path) -> tuple[Path, str]:
    try:
        with matrix.MatrixLock(Path(matrix_dir)):
            _reject_finalized_screen(matrix_dir)
            return _import_judgment_locked(matrix_dir, source)
    except matrix.MatrixError as error:
        raise JudgmentError(str(error)) from None


def _retained_judgments(matrix_dir: Path) -> dict[str, dict[str, Any]]:
    root = Path(matrix_dir) / "judgments"
    if not root.is_dir():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.json")):
        try:
            item_stat = path.lstat()
            if (
                not stat.S_ISREG(item_stat.st_mode)
                or stat.S_IMODE(item_stat.st_mode) != 0o600
            ):
                raise JudgmentError("retained judgment is not private")
            document = matrix._read_strict_json(  # noqa: SLF001
                path, max_bytes=_MAX_ARTIFACT_BYTES
            )
        except (OSError, matrix.MatrixError):
            raise JudgmentError("retained judgment is invalid") from None
        validated = _validate_schema(document, "judgment-0.1.json")
        content = matrix._canonical_json_bytes(validated)  # noqa: SLF001
        if path.read_bytes() != content:
            raise JudgmentError("retained judgment is not canonical")
        inspection_result, observation, scenario = _target_observation(
            matrix_dir, validated["target"]
        )
        _reject_unsafe_metadata(validated, observation)
        verdicts = _criterion_map(validated["verdicts"], field="verdict")
        if set(verdicts) != set(scenario.criterion_ids):
            raise JudgmentError("retained judgment does not cover the exact rubric")
        if _requires_calibrated_policy(inspection_result.manifest.campaign, scenario):
            _validate_judgment_policy(inspection_result.manifest.campaign, validated)
        result[hashlib.sha256(content).hexdigest()] = validated
    return result


def _import_adjudication_locked(matrix_dir: Path, source: Path) -> tuple[Path, str]:
    document, content = _read_import(source, "adjudication-0.1.json")
    inspection_result, observation, scenario = _target_observation(
        matrix_dir, document["target"]
    )
    _reject_unsafe_metadata(document, observation)
    outcomes = _criterion_map(document["outcomes"], field="outcome")
    if set(outcomes) != set(scenario.criterion_ids):
        raise JudgmentError("adjudication does not cover the exact rubric")
    retained = _retained_judgments(matrix_dir)
    hashes = document["judgment_sha256s"]
    if any(digest not in retained for digest in hashes):
        raise JudgmentError("adjudication references an unavailable judgment")
    target = document["target"]
    expected_judgment_target = {
        "matrix_id": target["matrix_id"],
        "work_item_id": target["work_item_id"],
        "report_sha256": target["report_sha256"],
        "scenario_sha256": target["scenario_sha256"],
        "rubric_sha256": target["rubric_sha256"],
        "projection_sha256": target["projection_sha256"],
    }
    judgment_maps: list[dict[str, str]] = []
    for digest in hashes:
        judgment = retained[digest]
        judgment_target = {
            key: judgment["target"][key] for key in expected_judgment_target
        }
        if judgment_target != expected_judgment_target:
            raise JudgmentError("adjudication judgments target different reports")
        judgment_maps.append(_criterion_map(judgment["verdicts"], field="verdict"))
    if _requires_calibrated_policy(inspection_result.manifest.campaign, scenario):
        _validate_adjudication_policy(
            inspection_result.manifest.campaign,
            document,
            [retained[digest] for digest in hashes],
        )
    if document["method"] == "agreement":
        if len(judgment_maps) < 2:
            raise JudgmentError("agreement requires at least two judgments")
        for criterion_id, outcome in outcomes.items():
            verdicts = {item[criterion_id] for item in judgment_maps}
            expected = (
                next(iter(verdicts))
                if len(verdicts) == 1 and "uncertain" not in verdicts
                else "unresolved"
            )
            if outcome != expected:
                raise JudgmentError("agreement outcome does not match judgments")
    return _publish_artifact(
        matrix_dir,
        collection="adjudications",
        artifact_id=document["adjudication_id"],
        content=content,
    )


def import_adjudication(matrix_dir: Path, source: Path) -> tuple[Path, str]:
    try:
        with matrix.MatrixLock(Path(matrix_dir)):
            _reject_finalized_screen(matrix_dir)
            return _import_adjudication_locked(matrix_dir, source)
    except matrix.MatrixError as error:
        raise JudgmentError(str(error)) from None


def deterministic_failures(observation: inspection.Observation) -> tuple[str, ...]:
    """Return immutable false layers; qualitative artifacts cannot clear them."""

    return tuple(
        layer
        for layer in ("agent_turns", "tool_policy", "oracle", "claims", "privacy")
        if observation.report["metrics"][layer]["passed"] is False
    )


def judge_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner adjudicate")
    parser.add_argument("kind", choices=("judgment", "adjudication"))
    parser.add_argument("matrix_dir", type=Path)
    parser.add_argument("source", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.kind == "judgment":
            path, digest = import_judgment(args.matrix_dir, args.source)
        else:
            path, digest = import_adjudication(args.matrix_dir, args.source)
    except (JudgmentError, inspection.InspectionError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"path": path.name, "sha256": digest}, sort_keys=True))
    return 0

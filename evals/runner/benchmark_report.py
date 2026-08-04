"""Read-only diagnostic reports for benchmark matrix campaigns."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from evals.runner import acceptance, inspection, matrix, run_state


_LAYERS = ("agent_turns", "tool_policy", "oracle", "claims", "privacy")


class BenchmarkReportError(RuntimeError):
    """Benchmark evidence cannot produce a strict diagnostic report."""


def _work_identity(item: run_state.MatrixWorkItem) -> dict[str, Any]:
    return {
        "work_item_id": item.work_item_id,
        "scenario_id": item.scenario_id,
        "track": item.track,
        "route": item.route.to_dict(),
        "replicate": item.replicate,
    }


def benchmark_report(matrix_dir: Path) -> dict[str, Any]:
    """Validate and render separate deterministic and qualitative vectors."""

    try:
        result = inspection.inspect_matrix(Path(matrix_dir))
    except inspection.InspectionError as error:
        raise BenchmarkReportError(str(error)) from None
    manifest = result.manifest
    if manifest.campaign.campaign_kind != "benchmark":
        raise BenchmarkReportError(
            "diagnostic report requires a benchmark campaign"
        )
    try:
        qualitative_evidence = acceptance.load_qualitative_outcomes(
            result,
            require_all_judgments_adjudicated=False,
        )
    except acceptance.AcceptanceError as error:
        raise BenchmarkReportError(str(error)) from None

    observations = {
        item.work_item.work_item_id: item for item in result.observations
    }
    completions = {
        item.work_item_id: item for item in manifest.completions
    }
    scenarios = {
        item.scenario_id: item for item in manifest.inputs.scenarios
    }
    qualitative = qualitative_evidence.outcome_map
    deterministic_vector: list[dict[str, Any]] = []
    qualitative_vector: list[dict[str, Any]] = []
    for work_item in manifest.work_items:
        identity = _work_identity(work_item)
        completion = completions.get(work_item.work_item_id)
        observation = observations.get(work_item.work_item_id)
        if observation is not None:
            state = "observed"
            layers = {
                layer: inspection.deterministic_layer_value(
                    observation.report, layer
                )
                for layer in _LAYERS
            }
            unavailable_reason = None
        elif completion is not None and completion.outcome == "unavailable":
            state = "unavailable"
            layers = None
            unavailable_reason = completion.unavailable_reason
        else:
            state = "unaccounted"
            layers = None
            unavailable_reason = None
        deterministic_vector.append(
            {
                **identity,
                "state": state,
                "unavailable_reason": unavailable_reason,
                "layers": layers,
            }
        )

        scenario = scenarios[work_item.scenario_id]
        adjudicated = qualitative.get(work_item.work_item_id, {})
        qualitative_vector.append(
            {
                **identity,
                "criteria": [
                    {
                        "criterion_id": criterion.criterion_id,
                        "source": criterion.source,
                        "outcome": adjudicated.get(
                            criterion.criterion_id, "unreviewed"
                        ),
                    }
                    for criterion in scenario.qualitative_criteria
                ],
            }
        )

    deterministic_counts = {
        layer: {
            "true": sum(
                item["layers"] is not None and item["layers"][layer] is True
                for item in deterministic_vector
            ),
            "false": sum(
                item["layers"] is not None and item["layers"][layer] is False
                for item in deterministic_vector
            ),
            "null": sum(
                item["layers"] is not None and item["layers"][layer] is None
                for item in deterministic_vector
            ),
        }
        for layer in _LAYERS
    }
    qualitative_counts = {
        outcome: sum(
            criterion["outcome"] == outcome
            for item in qualitative_vector
            for criterion in item["criteria"]
        )
        for outcome in ("pass", "fail", "unresolved", "unreviewed")
    }
    reviewed_work_items = len(qualitative)
    try:
        operational = (
            inspection.operational_vector(result.observations)
            if result.observations
            else None
        )
    except inspection.InspectionError as error:
        raise BenchmarkReportError(str(error)) from None

    return {
        "schema": "steam-agent-eval-benchmark-report/0.1",
        "campaign_kind": "benchmark",
        "matrix_id": manifest.matrix_id,
        "manifest_sha256": result.manifest_sha256,
        "config_sha256": manifest.config_sha256,
        "campaign_sha256": manifest.campaign_sha256,
        "plan_sha256": manifest.plan_sha256,
        "state": manifest.state.value,
        "structurally_complete": result.structurally_complete,
        "excluded_scenario_ids": list(manifest.excluded_scenario_ids),
        "qualitative_evidence_sha256": qualitative_evidence.sha256,
        "aggregates": {
            "deterministic": deterministic_counts,
            "deterministic_work_items": {
                state: sum(item["state"] == state for item in deterministic_vector)
                for state in ("observed", "unavailable", "unaccounted")
            },
            "operational": operational,
            "qualitative": qualitative_counts,
            "qualitative_work_items": {
                "reviewed": reviewed_work_items,
                "unreviewed": len(manifest.work_items) - reviewed_work_items,
            },
        },
        "deterministic": deterministic_vector,
        "qualitative": qualitative_vector,
    }


def report_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner report")
    parser.add_argument("matrix_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = benchmark_report(args.matrix_dir)
    except (
        BenchmarkReportError,
        inspection.InspectionError,
        matrix.MatrixError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0

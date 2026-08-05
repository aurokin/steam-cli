from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import acceptance, controls, inspection, matrix, run_state  # noqa: E402


NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
LAYERS = ("agent_turns", "tool_policy", "oracle", "claims", "privacy")


@pytest.fixture(autouse=True)
def _canonical_results_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inspection, "RESULTS_ROOT", tmp_path / "results")


def _inputs() -> run_state.MatrixInputs:
    return run_state.MatrixInputs(
        commit="1" * 40,
        source_digest="2" * 64,
        harness_digest="3" * 64,
        scenarios=(
            run_state.MatrixScenario(
                scenario_id="m7-z99",
                source_sha256="a" * 64,
                child_source_digest="f" * 64,
                schema_version="steam-agent-eval:0.3",
                schema_sha256="b" * 64,
                execution_support="live",
                rubric_sha256="c" * 64,
                criterion_ids=("quality",),
                qualitative_criteria=(
                    run_state.MatrixQualitativeCriterion(
                        "quality", "judged_answer_rubric", "Be useful.", None
                    ),
                ),
                turn_count=1,
            ),
        ),
        tool_versions=(("codex", "0.146.0"), ("python", "3.13")),
    )


def _write_config(
    path: Path, *, model: str, timeout: int, replicates: int = 1
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "steam-agent-eval-matrix/0.1",
                "campaign_kind": "screen",
                "selection_policy": {
                    "version": "fixed-ordered-scenarios/0.1",
                    "mode": "fixed_ordered",
                },
                "acceptance_policy": {
                    "version": "fixed-corpus/0.1",
                    "hard_layers": list(LAYERS),
                    "required_tracks": ["discovery"],
                    "replicates": replicates,
                    "qualitative_rule": "fact_hard_safety_resolved_pass",
                },
                "judge_policy": {
                    "version": "blinded-qualitative/0.1",
                    "judgment_schema": "steam-agent-eval-judgment/0.1",
                    "adjudication_schema": "steam-agent-eval-adjudication/0.1",
                    "prompt_version": "matrix-judge/0.1",
                    "parser_version": "matrix-parser/0.1",
                    "prompt_sha256": "671449c1329475b3753ffe30a017ad60152603efe6def833872eff8c428deec7",
                    "parser_sha256": "658a8acdf97c7d681c2b78e68c853b73fe010c49631595c7f69f67575931be49",
                    "judges": [
                        item.to_dict()
                        for item in run_state.CALIBRATED_JUDGE_CONFIGURATIONS
                    ],
                    "adjudication": {
                        "method": run_state.CALIBRATED_ADJUDICATION_METHOD,
                        "adjudicator": run_state.CALIBRATED_ADJUDICATOR,
                    },
                },
                "screen_provenance": None,
                "models": [model],
                "efforts": ["high"],
                "tracks": ["discovery"],
                "scenario_ids": ["m7-z99"],
                "replicates": replicates,
                "timeout_seconds": timeout,
                "schedule": "route-interleaved-v1",
            }
        )
    )
    return path


def _child(
    results_root: Path,
    inputs: run_state.MatrixInputs,
    *,
    duration: float,
) -> matrix.ChildExecutor:
    def execute(item: run_state.MatrixWorkItem, _timeout: float) -> matrix.ChildResult:
        run_id = f"child-{len(list(results_root.glob('child-*'))) + 1:06d}"
        run_dir = results_root / run_id
        run_dir.mkdir(mode=0o700)
        scenario_dir = run_dir / item.scenario_id
        scenario_dir.mkdir(mode=0o700)
        frozen = run_state.FrozenScenario.create(
            source_name="m7/m7-z99.json",
            original_bytes=b"{}",
            document={"id": item.scenario_id},
        )
        child_started = datetime.now(timezone.utc)
        initial = run_state.RunManifest.create(
            run_id=run_id,
            commit=inputs.commit,
            source_digest=inputs.scenarios[0].child_source_digest,
            cleanliness="clean",
            track=item.track,
            control_set_version="steam-agent-eval-controls/0.1",
            scenarios=[frozen],
            requested_routes=[
                run_state.RequestedRoute(item.route.model, item.route.reasoning_effort)
            ],
            tool_versions=dict(inputs.tool_versions),
            started_at=child_started,
        )
        running = run_state.RunManifest(
            run_id=initial.run_id,
            state=run_state.RunState.RUNNING,
            revision=2,
            commit=initial.commit,
            source_digest=initial.source_digest,
            cleanliness=initial.cleanliness,
            track=initial.track,
            control_set_version=initial.control_set_version,
            controls_passed=True,
            terminal_reason=None,
            scenario_ids=initial.scenario_ids,
            completed_scenario_ids=(),
            fixture_hashes=((item.scenario_id, "a" * 64),),
            requested_routes=initial.requested_routes,
            tool_versions=initial.tool_versions,
            started_at=initial.started_at,
            updated_at=initial.updated_at,
            finished_at=None,
        )
        completed = running.transition(
            run_state.RunState.COMPLETED,
            at=child_started + timedelta(microseconds=1),
            completed_scenario_ids=[item.scenario_id],
        )
        metrics: dict[str, dict[str, Any]] = {
            layer: {"passed": False if layer == "tool_policy" else True}
            for layer in LAYERS
        }
        report = {
            "artifact_schema_version": "steam-agent-eval-report/0.2",
            "scenario": item.scenario_id,
            "fixture_sha256": "a" * 64,
            "track": item.track,
            "generator": {
                "requested_model": item.route.model,
                "requested_reasoning_effort": item.route.reasoning_effort,
                "effective_model_by_turn": [item.route.model],
                "effective_reasoning_effort_by_turn": [item.route.reasoning_effort],
                "observed_models_by_turn": [[item.route.model]],
                "observed_reasoning_efforts_by_turn": [[item.route.reasoning_effort]],
                "requested_route_confirmed": True,
                "instructions_version": "agent-instructions/0.9",
            },
            "turns": [{}],
            "metrics": metrics,
            "operational": {
                "duration_seconds": duration,
                "command_executions": 2,
            },
        }
        run_state.atomic_publish_private_json(
            run_dir / "manifest.json", completed.to_dict()
        )
        run_state.atomic_publish_private_json(
            run_dir / "controls.json",
            controls.run_scripted_controls(lambda case: case.expected_layer_map()),
        )
        run_state.atomic_publish_private_json(scenario_dir / "report.json", report)
        run_state.atomic_publish_private_text(
            scenario_dir / "transcript.jsonl", '{"harness":"turn"}\n'
        )
        artifacts = {
            name: hashlib.sha256((scenario_dir / name).read_bytes()).hexdigest()
            for name in ("report.json", "transcript.jsonl")
        }
        run_state.atomic_publish_private_json(
            run_dir / "summary.json",
            [
                {
                    "scenario": item.scenario_id,
                    "passed": False,
                    "track": item.track,
                    "layers": {layer: metrics[layer]["passed"] for layer in LAYERS},
                    "artifacts": artifacts,
                }
            ],
        )
        return matrix.ChildResult(1, run_dir)

    return execute


def _completed_matrix(
    tmp_path: Path,
    *,
    name: str,
    model: str,
    timeout: int = 30,
    duration: float = 1.0,
    replicates: int = 1,
    unavailable_reason: str | None = None,
) -> Path:
    results = tmp_path / "results"
    config = _write_config(
        tmp_path / f"{name}.json",
        model=model,
        timeout=timeout,
        replicates=replicates,
    )
    child_executor = (
        (lambda _item, _timeout: matrix.ChildResult.unavailable(unavailable_reason))
        if unavailable_reason is not None
        else _child(results, _inputs(), duration=duration)
    )
    manifest = matrix.execute_matrix(
        config,
        results_root=results,
        input_collector=lambda _config: _inputs(),
        child_executor=child_executor,
    )
    return results / manifest.matrix_id


def test_inspection_revalidates_private_hash_bound_child_artifacts(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a", duration=1.5)
    result = inspection.inspect_matrix(matrix_dir)

    assert result.eligible is True
    assert result.structurally_complete is True
    assert result.orphan_attempt_ids == ()
    assert len(result.observations) == 1
    assert result.to_dict()["completed_work_items"] == 1
    assert result.to_dict()["observed_work_items"] == 1
    assert result.to_dict()["unavailable_work_items"] == []

    observation = result.observations[0]
    report = (
        matrix_dir.parent
        / observation.completion.child_run_id
        / observation.work_item.scenario_id
        / "report.json"
    )
    report.write_text("{}\n")
    with pytest.raises(inspection.InspectionError):
        inspection.inspect_matrix(matrix_dir)


@pytest.mark.parametrize("tamper", ("delete", "replace"))
def test_inspection_requires_hash_bound_controls_artifact(
    tmp_path: Path, tamper: str
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    inspected = inspection.inspect_matrix(matrix_dir)
    [observation] = inspected.observations
    assert "controls.json" in dict(observation.completion.artifact_hashes)
    assert observation.completion.child_run_id is not None
    controls_path = (
        matrix_dir.parent / observation.completion.child_run_id / "controls.json"
    )
    if tamper == "delete":
        controls_path.unlink()
    else:
        controls_path.write_text("{}\n")

    with pytest.raises(inspection.InspectionError, match="controls|artifact"):
        inspection.inspect_matrix(matrix_dir)


def test_inspection_uses_exact_validated_child_bundle_without_reopening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    real_validate = matrix.validate_child_result
    mutated = False

    def validate_then_mutate(
        child: matrix.ChildResult,
        work_item: run_state.MatrixWorkItem,
        manifest: run_state.MatrixManifest,
        *,
        results_root: Path = matrix.RESULTS_ROOT,
    ) -> matrix.ValidatedChildResult:
        nonlocal mutated
        validated = real_validate(
            child,
            work_item,
            manifest,
            results_root=results_root,
        )
        if not mutated:
            mutated = True
            assert child.run_dir is not None
            (child.run_dir / "manifest.json").write_text("{}\n")
            (child.run_dir / work_item.scenario_id / "report.json").write_text(
                "{}\n"
            )
            (child.run_dir / "summary.json").write_text("[null]\n")
        return validated

    monkeypatch.setattr(matrix, "validate_child_result", validate_then_mutate)

    result = inspection.inspect_matrix(matrix_dir)

    assert result.observations[0].child_manifest.run_id.startswith("child-")
    assert (
        result.observations[0].report["artifact_schema_version"]
        == "steam-agent-eval-report/0.2"
    )
    assert result.observations[0].summary["scenario"] == "m7-z99"
    with pytest.raises(inspection.InspectionError, match="child cohort manifest"):
        inspection.inspect_matrix(matrix_dir)


@pytest.mark.parametrize("staging_state", ("empty", "started"))
def test_inspection_audits_staging_without_counting_it_as_an_orphan(
    tmp_path: Path, staging_state: str
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    manifest = matrix.load_manifest(matrix_dir)
    [work_item] = manifest.work_items
    item_root = matrix_dir / "work" / work_item.work_item_id
    staging = item_root / ".attempt-init-attempt-000002-audit123"
    staging.mkdir(mode=0o700)
    if staging_state == "started":
        run_state.atomic_publish_private_json(
            staging / "started.json",
            {
                "schema": "steam-agent-eval-matrix-attempt/0.1",
                "attempt_id": "attempt-000002",
                "work_item_id": work_item.work_item_id,
                "started_at": manifest.finished_at,
            },
        )

    result = inspection.inspect_matrix(matrix_dir)

    assert result.eligible is True
    assert result.orphan_attempt_ids == ()


def test_inspection_rejects_a_symlink_matrix_root_before_resolution(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    linked = tmp_path / "linked-matrix"
    linked.symlink_to(matrix_dir, target_is_directory=True)

    with pytest.raises(inspection.InspectionError, match="not private"):
        inspection.inspect_matrix(linked)
    with pytest.raises(acceptance.AcceptanceError, match="not private"):
        acceptance._strict_inspection(linked)  # noqa: SLF001


def test_inspection_uses_the_canonical_results_boundary_by_default(
    tmp_path: Path,
) -> None:
    (tmp_path / "results").mkdir(mode=0o700)
    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    matrix_dir = _completed_matrix(outside_parent, name="outside", model="model-a")

    with pytest.raises(inspection.InspectionError, match="outside the results root"):
        inspection.inspect_matrix(matrix_dir)


@pytest.mark.parametrize("symlink", ("root", "ancestor"))
def test_inspection_rejects_symlinked_results_boundary_before_resolution(
    tmp_path: Path, symlink: str
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    matrix_dir = _completed_matrix(
        real_parent, name="inside", model="model-a"
    )
    real_results = real_parent / "results"
    if symlink == "root":
        boundary = tmp_path / "linked-results"
        boundary.symlink_to(real_results, target_is_directory=True)
    else:
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        boundary = linked_parent / "results"

    with pytest.raises(inspection.InspectionError, match="boundary"):
        inspection.inspect_matrix(matrix_dir, results_root=boundary)


def test_compare_emits_separate_layer_vectors_without_blended_score(
    tmp_path: Path,
) -> None:
    first = _completed_matrix(tmp_path, name="first", model="model-a", duration=1.0)
    second = _completed_matrix(tmp_path, name="second", model="model-b", duration=3.0)
    compared = inspection.compare_matrices([first, second])

    assert compared["eligible"] is True
    assert compared["unavailable_work_items_by_matrix"] == {
        first.name: [],
        second.name: [],
    }
    assert compared["orphan_attempt_ids_by_matrix"] == {
        first.name: [],
        second.name: [],
    }
    assert "score" not in json.dumps(compared)
    cells = compared["vector"]["cells"]
    assert len(cells) == 2
    assert all(cell["layers"]["tool_policy"]["false"] == 1 for cell in cells)
    assert all(cell["scenario_outcomes"]["false"] == 1 for cell in cells)
    assert {cell["route"]["model"] for cell in cells} == {"model-a", "model-b"}


def test_compare_rejects_incompatible_execution_settings(tmp_path: Path) -> None:
    first = _completed_matrix(tmp_path, name="first", model="model-a", timeout=30)
    second = _completed_matrix(tmp_path, name="second", model="model-b", timeout=60)
    with pytest.raises(inspection.InspectionError, match="incompatible"):
        inspection.compare_matrices([first, second])


def test_compare_rejects_same_live_scenario_set_in_different_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = _completed_matrix(tmp_path, name="first", model="model-a")
    second_dir = _completed_matrix(tmp_path, name="second", model="model-b")
    first = inspection.inspect_matrix(first_dir)
    second = inspection.inspect_matrix(second_dir)
    first_scenario = first.manifest.inputs.scenarios[0]
    second_scenario = replace(
        first_scenario,
        scenario_id="m7-z98",
        source_sha256="e" * 64,
        rubric_sha256="f" * 64,
    )
    first_manifest = replace(
        first.manifest,
        inputs=replace(
            first.manifest.inputs,
            scenarios=(first_scenario, second_scenario),
        ),
    )
    second_manifest = replace(
        second.manifest,
        inputs=replace(
            second.manifest.inputs,
            scenarios=(second_scenario, first_scenario),
        ),
    )
    first_compatibility, first_digest = inspection._compatibility(  # noqa: SLF001
        first_manifest,
        first.observations[0].work_item,
        first.observations[0].child_manifest,
        first.observations[0].report,
        timeout_seconds=30,
    )
    second_compatibility, second_digest = inspection._compatibility(  # noqa: SLF001
        second_manifest,
        second.observations[0].work_item,
        second.observations[0].child_manifest,
        second.observations[0].report,
        timeout_seconds=30,
    )
    first = replace(
        first,
        manifest=first_manifest,
        observations=(
            replace(
                first.observations[0],
                compatibility=first_compatibility,
                compatibility_sha256=first_digest,
            ),
        ),
    )
    second = replace(
        second,
        manifest=second_manifest,
        observations=(
            replace(
                second.observations[0],
                compatibility=second_compatibility,
                compatibility_sha256=second_digest,
            ),
        ),
    )
    results = iter((first, second))
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: next(results))

    with pytest.raises(inspection.InspectionError, match="incompatible"):
        inspection.compare_matrices([first_dir, second_dir])


@pytest.mark.parametrize("difference", ("selected_corpus", "preflight_evidence"))
def test_compare_rejects_different_deterministic_preflight_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, difference: str
) -> None:
    first_dir = _completed_matrix(tmp_path, name="first", model="model-a")
    second_dir = _completed_matrix(tmp_path, name="second", model="model-b")
    first = inspection.inspect_matrix(first_dir)
    second = inspection.inspect_matrix(second_dir)
    live = first.manifest.inputs.scenarios[0]
    deterministic = replace(
        live,
        scenario_id="m5-c03",
        source_sha256="4" * 64,
        child_source_digest="5" * 64,
        execution_support="deterministic_only",
        rubric_sha256="6" * 64,
    )

    def with_deterministic_corpus(
        result: inspection.MatrixInspection,
        scenario: run_state.MatrixScenario,
        *,
        evidence_digest: str,
    ) -> inspection.MatrixInspection:
        inputs = replace(result.manifest.inputs, scenarios=(live, scenario))
        manifest = replace(
            result.manifest,
            inputs=inputs,
            preflight_attestation=run_state.MatrixPreflightAttestation.for_inputs(
                inputs,
                evidence={
                    scenario.scenario_id: (
                        "domain_oracle",
                        "8" * 64,
                        evidence_digest,
                    )
                },
            ),
            excluded_scenario_ids=(scenario.scenario_id,),
        )
        observation = result.observations[0]
        compatibility, digest = inspection._compatibility(  # noqa: SLF001
            manifest,
            observation.work_item,
            observation.child_manifest,
            observation.report,
            timeout_seconds=30,
        )
        return replace(
            result,
            manifest=manifest,
            observations=(
                replace(
                    observation,
                    compatibility=compatibility,
                    compatibility_sha256=digest,
                ),
            ),
        )

    first = with_deterministic_corpus(
        first, deterministic, evidence_digest="9" * 64
    )
    second_scenario = (
        replace(deterministic, source_sha256="7" * 64)
        if difference == "selected_corpus"
        else deterministic
    )
    second = with_deterministic_corpus(
        second,
        second_scenario,
        evidence_digest=("a" * 64 if difference == "preflight_evidence" else "9" * 64),
    )
    first_fields = dict(first.observations[0].compatibility)
    assert len(first_fields["ordered_selected_scenario_inventory"]) == 2
    assert first_fields["deterministic_preflight_attestation"] == (
        first.manifest.preflight_attestation.to_dict()
    )
    results = iter((first, second))
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: next(results))

    with pytest.raises(inspection.InspectionError, match="incompatible"):
        inspection.compare_matrices((first_dir, second_dir))


def test_inspection_rejects_tampered_committed_attempt_start(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    completion = inspection.inspect_matrix(matrix_dir).manifest.completions[0]
    started_path = (
        matrix_dir
        / "work"
        / completion.work_item_id
        / completion.attempt_id
        / "started.json"
    )
    started = json.loads(started_path.read_text())
    started["started_at"] = "2026-08-02T11:59:00Z"
    started_path.write_text(json.dumps(started) + "\n")

    with pytest.raises(inspection.InspectionError, match="attempt start"):
        inspection.inspect_matrix(matrix_dir)


def test_inspection_exposes_the_hash_bound_attempt_start(tmp_path: Path) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")

    result = inspection.inspect_matrix(matrix_dir)
    observation = result.observations[0]
    started_path = (
        matrix_dir
        / "work"
        / observation.completion.work_item_id
        / observation.completion.attempt_id
        / "started.json"
    )

    assert (
        observation.attempt_started_at
        == json.loads(started_path.read_text())["started_at"]
    )
    assert result.manifest_sha256 == hashlib.sha256(
        (matrix_dir / "manifest.json").read_bytes()
    ).hexdigest()


def test_inspection_allows_private_operational_review_registry(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    run_state.atomic_publish_private_json(
        matrix_dir / "review-package.json", {"operational": True}
    )

    result = inspection.inspect_matrix(matrix_dir)

    assert result.manifest.matrix_id == matrix_dir.name


def test_attempt_validator_accepts_canonical_published_bytes(tmp_path: Path) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    manifest = matrix.load_manifest(matrix_dir)
    [completion] = manifest.completions
    attempt_dir = (
        matrix_dir
        / "work"
        / completion.work_item_id
        / completion.attempt_id
    )

    validated = matrix.validate_attempt_artifacts(
        attempt_dir, work_item_id=completion.work_item_id
    )

    assert validated.completion == completion
    assert dict(validated.artifact_hashes) == {
        name: hashlib.sha256((attempt_dir / name).read_bytes()).hexdigest()
        for name in ("result.json", "started.json")
    }


@pytest.mark.parametrize(
    ("tamper", "diagnostic"),
    (
        ("whitespace", "attempt result"),
        ("key-order", "attempt result"),
        ("duplicate-member", "attempt history"),
    ),
)
def test_official_result_requires_unique_canonical_json_bytes(
    tmp_path: Path, tamper: str, diagnostic: str
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    completion = matrix.load_manifest(matrix_dir).completions[0]
    result_path = (
        matrix_dir
        / "work"
        / completion.work_item_id
        / completion.attempt_id
        / "result.json"
    )
    result = json.loads(result_path.read_text())
    if tamper == "whitespace":
        replacement = result_path.read_bytes() + b"\n"
    elif tamper == "key-order":
        replacement = (
            json.dumps(
                {
                    "schema": result["schema"],
                    "completion": result["completion"],
                },
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=False,
            )
            + "\n"
        ).encode()
    else:
        replacement = (
            "{"
            f'"completion":{json.dumps(result["completion"])},'
            f'"schema":{json.dumps(result["schema"])},'
            f'"schema":{json.dumps(result["schema"])}'
            "}\n"
        ).encode()
    result_path.write_bytes(replacement)

    with pytest.raises(inspection.InspectionError, match=diagnostic):
        inspection.inspect_matrix(matrix_dir)


def test_official_start_requires_unique_canonical_json_bytes(tmp_path: Path) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    completion = matrix.load_manifest(matrix_dir).completions[0]
    started_path = (
        matrix_dir
        / "work"
        / completion.work_item_id
        / completion.attempt_id
        / "started.json"
    )
    started_path.write_bytes(started_path.read_bytes() + b"\n")

    with pytest.raises(inspection.InspectionError, match="attempt start"):
        inspection.inspect_matrix(matrix_dir)


def test_inspection_rejects_a_forged_manifest_revision(tmp_path: Path) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    manifest_path = matrix_dir / "manifest.json"
    document = json.loads(manifest_path.read_text())
    document["revision"] += 1
    manifest_path.write_text(json.dumps(document) + "\n")

    with pytest.raises(inspection.InspectionError, match="manifest is invalid"):
        inspection.inspect_matrix(matrix_dir)


@pytest.mark.parametrize("encoding", ("compact", "extra-whitespace"))
def test_inspection_requires_canonical_matrix_manifest_bytes(
    tmp_path: Path, encoding: str
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    manifest_path = matrix_dir / "manifest.json"
    document = json.loads(manifest_path.read_text())
    replacement = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":") if encoding == "compact" else None,
    ).encode()
    manifest_path.write_bytes(replacement + b"\n\n")

    with pytest.raises(inspection.InspectionError, match="not canonical"):
        inspection.inspect_matrix(matrix_dir)


def test_private_manifest_read_rejects_mutation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(b'{"state":"open"}\n')
    path.chmod(0o600)
    real_read = inspection.os.read
    changed = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        content = real_read(descriptor, size)
        if content and not changed:
            changed = True
            path.write_bytes(b'{"state":"done"}\n')
        return content

    monkeypatch.setattr(inspection.os, "read", mutate_after_read)

    with pytest.raises(inspection.InspectionError, match="changed"):
        inspection._private_regular_bytes(path, max_bytes=1024)  # noqa: SLF001


@pytest.mark.parametrize("invalid_digest", (None, 17))
def test_inspection_normalizes_malformed_manifest_digest_primitives(
    tmp_path: Path,
    invalid_digest: object,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    manifest_path = matrix_dir / "manifest.json"
    document = json.loads(manifest_path.read_text())
    document["inputs"]["source_digest"] = invalid_digest
    manifest_path.write_text(json.dumps(document) + "\n")

    with pytest.raises(
        inspection.InspectionError,
        match=r"^matrix manifest is invalid$",
    ) as error:
        inspection.inspect_matrix(matrix_dir)

    assert str(matrix_dir) not in str(error.value)


def test_compare_rejects_duplicate_matrix_directory(tmp_path: Path) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")

    with pytest.raises(inspection.InspectionError, match="duplicate directory"):
        inspection.compare_matrices([matrix_dir, matrix_dir])


def test_compare_rejects_stale_observation_chronology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    result = inspection.inspect_matrix(matrix_dir)
    observation = result.observations[0]
    stale = replace(
        result,
        observations=(
            replace(
                observation,
                child_manifest=replace(
                    observation.child_manifest,
                    started_at=result.manifest.started_at,
                ),
            ),
        ),
    )
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: stale)

    with pytest.raises(inspection.InspectionError, match="chronology"):
        inspection.compare_matrices([matrix_dir])


def test_compare_rejects_duplicate_matrix_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = _completed_matrix(tmp_path, name="first", model="model-a")
    second_dir = _completed_matrix(tmp_path, name="second", model="model-b")
    first = inspection.inspect_matrix(first_dir)
    second = inspection.inspect_matrix(second_dir)
    second = replace(
        second,
        manifest=replace(second.manifest, matrix_id=first.manifest.matrix_id),
    )
    results = iter((first, second))
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: next(results))

    with pytest.raises(inspection.InspectionError, match="duplicate matrix ID"):
        inspection.compare_matrices([first_dir, second_dir])


def test_compare_rejects_shared_observed_child_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = _completed_matrix(tmp_path, name="first", model="model-a")
    second_dir = _completed_matrix(tmp_path, name="second", model="model-b")
    first = inspection.inspect_matrix(first_dir)
    second = inspection.inspect_matrix(second_dir)
    first_child = first.observations[0].completion.child_run_id
    second_observation = replace(
        second.observations[0],
        completion=replace(
            second.observations[0].completion,
            child_run_id=first_child,
        ),
    )
    second = replace(second, observations=(second_observation,))
    results = iter((first, second))
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: next(results))

    with pytest.raises(inspection.InspectionError, match="reuses observed child"):
        inspection.compare_matrices([first_dir, second_dir])


def test_vector_uses_deterministic_claims_independently_of_qualitative_state(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    result = inspection.inspect_matrix(matrix_dir)
    report = result.observations[0].report
    report["metrics"]["tool_policy"]["passed"] = True
    report["metrics"]["claims"]["passed"] = None
    report["metrics"]["claims"]["deterministic_passed"] = True

    vector = inspection.aggregate_observations(result.observations)

    [cell] = vector["cells"]
    assert cell["layers"]["claims"] == {"true": 1, "false": 0, "null": 0}
    assert cell["scenario_outcomes"] == {"true": 1, "false": 0, "null": 0}
    assert cell["compatibility_sha256"] == result.observations[0].compatibility_sha256


def test_single_matrix_aggregation_rejects_mixed_compatibility_in_one_cell(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(
        tmp_path, name="mixed", model="model-a", replicates=2
    )
    result = inspection.inspect_matrix(matrix_dir)
    first, second = result.observations
    mismatched = replace(second, compatibility_sha256="0" * 64)

    with pytest.raises(inspection.InspectionError, match="replicate cell"):
        inspection.aggregate_observations((first, mismatched))


def test_inspection_rejects_mixed_compatibility_in_one_replicate_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = _completed_matrix(
        tmp_path, name="mixed", model="model-a", replicates=2
    )
    real_compatibility = inspection._compatibility  # noqa: SLF001
    calls = 0

    def alternating(*args: Any, **kwargs: Any) -> tuple[tuple[tuple[str, Any], ...], str]:
        nonlocal calls
        compatibility, digest = real_compatibility(*args, **kwargs)
        calls += 1
        return compatibility, digest if calls == 1 else "0" * 64

    monkeypatch.setattr(inspection, "_compatibility", alternating)

    with pytest.raises(inspection.InspectionError, match="replicate cell"):
        inspection.inspect_matrix(matrix_dir)


def test_comparison_preserves_overlapping_cell_replicate_multiplicity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = _completed_matrix(
        tmp_path, name="first", model="model-a", replicates=1
    )
    second_dir = _completed_matrix(
        tmp_path, name="second", model="model-a", replicates=2
    )
    first = inspection.inspect_matrix(first_dir)
    second = inspection.inspect_matrix(second_dir)
    reference = first.observations[0]
    second = replace(
        second,
        observations=tuple(
            replace(
                observation,
                compatibility=reference.compatibility,
                compatibility_sha256=reference.compatibility_sha256,
            )
            for observation in second.observations
        ),
    )
    results = iter((first, second))
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: next(results))

    with pytest.raises(inspection.InspectionError, match="incompatible"):
        inspection.compare_matrices((first_dir, second_dir))


def test_comparison_preserves_compatibility_digest_per_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = _completed_matrix(tmp_path, name="first", model="model-a")
    second_dir = _completed_matrix(tmp_path, name="second", model="model-a")
    first = inspection.inspect_matrix(first_dir)
    second = inspection.inspect_matrix(second_dir)

    def observation_for(
        base: inspection.Observation,
        *,
        model: str,
        digest: str,
        suffix: str,
    ) -> inspection.Observation:
        work_item = replace(
            base.work_item,
            work_item_id=f"w-{suffix}",
            route=run_state.MatrixRoute(model, "high"),
        )
        completion = replace(
            base.completion,
            work_item_id=work_item.work_item_id,
            child_run_id=f"child-{suffix}",
        )
        return replace(
            base,
            work_item=work_item,
            completion=completion,
            compatibility_sha256=digest,
        )

    first = replace(
        first,
        observations=(
            observation_for(
                first.observations[0], model="model-a", digest="a" * 64, suffix="a1"
            ),
            observation_for(
                first.observations[0], model="model-b", digest="b" * 64, suffix="b1"
            ),
        ),
    )
    second = replace(
        second,
        observations=(
            observation_for(
                second.observations[0], model="model-a", digest="b" * 64, suffix="a2"
            ),
            observation_for(
                second.observations[0], model="model-b", digest="a" * 64, suffix="b2"
            ),
        ),
    )
    results = iter((first, second))
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: next(results))
    monkeypatch.setattr(
        inspection, "_verify_observation_chronology", lambda _manifest, _items: None
    )

    with pytest.raises(inspection.InspectionError, match="incompatible"):
        inspection.compare_matrices((first_dir, second_dir))


def test_unavailable_completion_is_accounted_without_becoming_observation(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(
        tmp_path,
        name="unavailable",
        model="model-a",
        unavailable_reason="provider_route_unavailable",
    )

    result = inspection.inspect_matrix(matrix_dir)

    assert result.structurally_complete is True
    assert result.eligible is False
    assert result.observations == ()
    assert result.orphan_attempt_ids == ()
    assert len(result.unavailable_work_items) == 1
    rendered = result.to_dict()
    assert rendered["accounted_work_items"] == 1
    assert rendered["observed_work_items"] == 0
    assert rendered["unavailable_work_items"] == [
        {
            "work_item": result.manifest.work_items[0].to_dict(),
            "attempt_id": "attempt-000001",
            "reason": "provider_route_unavailable",
        }
    ]


def test_successful_retry_history_is_audited_without_disqualifying_matrix(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    config = _write_config(
        tmp_path / "retry.json", model="model-a", timeout=30
    )

    def fail_first_attempt(
        _item: run_state.MatrixWorkItem, _timeout: float
    ) -> matrix.ChildResult:
        raise RuntimeError("simulated first-attempt failure")

    with pytest.raises(matrix.MatrixError, match="failed structurally"):
        matrix.execute_matrix(
            config,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=fail_first_attempt,
        )

    [matrix_dir] = results_root.glob("matrix-*")
    completed = matrix.execute_matrix(
        config,
        matrix_id=matrix_dir.name,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=_child(results_root, _inputs(), duration=1.0),
    )
    result = inspection.inspect_matrix(matrix_dir)

    assert completed.completions[0].attempt_id == "attempt-000002"
    assert result.orphan_attempt_ids == (
        f"{completed.work_items[0].work_item_id}/attempt-000001",
    )
    assert result.eligible is True
    compared = inspection.compare_matrices((matrix_dir,))
    assert compared["eligible"] is True
    assert compared["vector"] is not None


@pytest.mark.parametrize("tamper", ("delete", "replace", "mutate"))
def test_observed_orphan_revalidates_its_distinct_child_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    results_root = tmp_path / "results"
    config = _write_config(tmp_path / "retry.json", model="model-a", timeout=30)
    original_persist = run_state.MatrixManifest.persist

    def fail_first_checkpoint(
        manifest: run_state.MatrixManifest, path: Path
    ) -> None:
        if manifest.revision == 1:
            raise OSError("simulated checkpoint failure")
        original_persist(manifest, path)

    monkeypatch.setattr(
        run_state.MatrixManifest, "persist", fail_first_checkpoint
    )
    with pytest.raises(matrix.MatrixError, match="failed structurally"):
        matrix.execute_matrix(
            config,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=_child(results_root, _inputs(), duration=1.0),
        )

    [matrix_dir] = results_root.glob("matrix-*")
    monkeypatch.setattr(run_state.MatrixManifest, "persist", original_persist)
    completed = matrix.execute_matrix(
        config,
        matrix_id=matrix_dir.name,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=_child(results_root, _inputs(), duration=1.0),
    )
    inspected = inspection.inspect_matrix(matrix_dir)
    assert inspected.eligible is True
    [orphan_id] = inspected.orphan_attempt_ids
    orphan_result = json.loads(
        (matrix_dir / "work" / orphan_id / "result.json").read_text()
    )
    orphan_child_id = orphan_result["completion"]["child_run_id"]
    assert orphan_child_id != completed.completions[0].child_run_id
    orphan_child = results_root / orphan_child_id
    report_path = orphan_child / "m7-z99" / "report.json"
    if tamper == "delete":
        report_path.unlink()
    elif tamper == "replace":
        official_child_id = completed.completions[0].child_run_id
        assert official_child_id is not None
        report_path.write_bytes(
            (results_root / official_child_id / "manifest.json").read_bytes()
        )
    else:
        report = json.loads(report_path.read_text())
        report["scenario"] = "m7-tampered"
        report_path.write_bytes(run_state._strict_json_bytes(report))  # noqa: SLF001

    with pytest.raises(inspection.InspectionError, match="child|artifact|report"):
        inspection.inspect_matrix(matrix_dir)


def test_orphan_completion_cannot_duplicate_official_child_evidence(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    inspected = inspection.inspect_matrix(matrix_dir)
    [official] = inspected.manifest.completions
    orphan_dir = (
        matrix_dir / "work" / official.work_item_id / "attempt-000002"
    )
    orphan_dir.mkdir(mode=0o700)
    started_path = orphan_dir / "started.json"
    run_state.atomic_publish_private_json(
        started_path,
        {
            "schema": "steam-agent-eval-matrix-attempt/0.1",
            "attempt_id": "attempt-000002",
            "work_item_id": official.work_item_id,
            "started_at": inspected.manifest.finished_at,
        },
    )
    duplicate = replace(
        official,
        attempt_id="attempt-000002",
        started_sha256=hashlib.sha256(started_path.read_bytes()).hexdigest(),
        completed_at=inspected.manifest.finished_at,
    )
    run_state.atomic_publish_private_json(
        orphan_dir / "result.json",
        {
            "schema": "steam-agent-eval-matrix-attempt-result/0.1",
            "completion": duplicate.to_dict(),
        },
    )

    with pytest.raises(inspection.InspectionError, match="duplicates child evidence"):
        inspection.inspect_matrix(matrix_dir)


def test_unavailable_attempt_result_remains_private_and_hash_bound(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(
        tmp_path,
        name="unavailable",
        model="model-a",
        unavailable_reason="provider_route_unavailable",
    )
    completion = inspection.inspect_matrix(matrix_dir).manifest.completions[0]
    attempt_dir = matrix_dir / "work" / completion.work_item_id / completion.attempt_id
    result_path = attempt_dir / "result.json"

    result_path.chmod(0o644)
    with pytest.raises(inspection.InspectionError, match="invalid"):
        inspection.inspect_matrix(matrix_dir)

    result_path.chmod(0o600)
    value = json.loads(result_path.read_text())
    value["completion"]["unavailable_reason"] = "route_removed"
    result_path.write_text(json.dumps(value) + "\n")
    result_path.chmod(0o600)
    with pytest.raises(inspection.InspectionError, match="attempt result"):
        inspection.inspect_matrix(matrix_dir)


def test_unavailable_attempt_is_committed_while_prior_attempt_is_orphaned(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(
        tmp_path,
        name="unavailable",
        model="model-a",
        unavailable_reason="provider_route_unavailable",
    )
    item = inspection.inspect_matrix(matrix_dir).manifest.work_items[0]
    orphan_dir = matrix_dir / "work" / item.work_item_id / "attempt-000002"
    orphan_dir.mkdir(mode=0o700)
    run_state.atomic_publish_private_json(
        orphan_dir / "started.json",
        {
            "schema": "steam-agent-eval-matrix-attempt/0.1",
            "attempt_id": "attempt-000002",
            "work_item_id": item.work_item_id,
            "started_at": "2026-08-02T12:00:00Z",
        },
    )

    result = inspection.inspect_matrix(matrix_dir)

    assert result.eligible is False
    orphan_id = f"{item.work_item_id}/attempt-000002"
    assert result.orphan_attempt_ids == (orphan_id,)
    assert result.orphan_attempt_hashes == (
        (
            orphan_id,
            (
                (
                    "started.json",
                    hashlib.sha256((orphan_dir / "started.json").read_bytes()).hexdigest(),
                ),
            ),
        ),
    )


def test_orphan_failure_is_exactly_validated_and_hashes_expose_rewrites(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    item = inspection.inspect_matrix(matrix_dir).manifest.work_items[0]
    orphan_id = f"{item.work_item_id}/attempt-000002"
    orphan_dir = matrix_dir / "work" / orphan_id
    orphan_dir.mkdir(mode=0o700)
    run_state.atomic_publish_private_json(
        orphan_dir / "started.json",
        {
            "schema": "steam-agent-eval-matrix-attempt/0.1",
            "attempt_id": "attempt-000002",
            "work_item_id": item.work_item_id,
            "started_at": "2026-08-02T12:00:00Z",
        },
    )
    failure_path = orphan_dir / "failure.json"
    run_state.atomic_publish_private_json(
        failure_path,
        {
            "schema": "steam-agent-eval-matrix-attempt-failure/0.1",
            "reason": "child_cohort_invalid",
            "error_type": "ValueError",
        },
    )

    before = inspection.inspect_matrix(matrix_dir)
    before_hashes = dict(before.orphan_attempt_hashes)[orphan_id]
    assert {name for name, _digest in before_hashes} == {
        "failure.json",
        "started.json",
    }

    failure = json.loads(failure_path.read_text())
    failure["error_type"] = "RuntimeError"
    failure_path.write_bytes(run_state._strict_json_bytes(failure))  # noqa: SLF001
    after = inspection.inspect_matrix(matrix_dir)
    assert dict(after.orphan_attempt_hashes)[orphan_id] != before_hashes

    failure_path.write_text(json.dumps(failure) + "\n")
    with pytest.raises(inspection.InspectionError, match="attempt history"):
        inspection.inspect_matrix(matrix_dir)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("attempt_id", "attempt-000003"),
        ("work_item_id", "w-999999-forged"),
        ("started_at", "not-a-time"),
    ),
)
def test_orphan_attempt_start_requires_exact_identity_and_time(
    tmp_path: Path, field: str, value: str
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    item = inspection.inspect_matrix(matrix_dir).manifest.work_items[0]
    orphan_dir = (
        matrix_dir / "work" / item.work_item_id / "attempt-000002"
    )
    orphan_dir.mkdir(mode=0o700)
    started = {
        "schema": "steam-agent-eval-matrix-attempt/0.1",
        "attempt_id": "attempt-000002",
        "work_item_id": item.work_item_id,
        "started_at": "2026-08-02T12:00:00Z",
    }
    started[field] = value
    run_state.atomic_publish_private_json(orphan_dir / "started.json", started)

    with pytest.raises(inspection.InspectionError, match="attempt start"):
        inspection.inspect_matrix(matrix_dir)


def test_orphan_result_is_hash_bound_exact_and_private(tmp_path: Path) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    item = inspection.inspect_matrix(matrix_dir).manifest.work_items[0]
    orphan_id = f"{item.work_item_id}/attempt-000002"
    orphan_dir = matrix_dir / "work" / orphan_id
    orphan_dir.mkdir(mode=0o700)
    started_path = orphan_dir / "started.json"
    run_state.atomic_publish_private_json(
        started_path,
        {
            "schema": "steam-agent-eval-matrix-attempt/0.1",
            "attempt_id": "attempt-000002",
            "work_item_id": item.work_item_id,
            "started_at": "2026-08-02T12:00:00Z",
        },
    )
    completion = run_state.MatrixCompletion(
        work_item_id=item.work_item_id,
        attempt_id="attempt-000002",
        started_sha256=hashlib.sha256(started_path.read_bytes()).hexdigest(),
        outcome="unavailable",
        unavailable_reason="provider_route_unavailable",
        child_run_id=None,
        child_exit_code=None,
        artifact_hashes=(),
        completed_at="2026-08-02T12:00:01Z",
    )
    result_path = orphan_dir / "result.json"
    run_state.atomic_publish_private_json(
        result_path,
        {
            "schema": "steam-agent-eval-matrix-attempt-result/0.1",
            "completion": completion.to_dict(),
        },
    )

    result = inspection.inspect_matrix(matrix_dir)
    assert {name for name, _digest in dict(result.orphan_attempt_hashes)[orphan_id]} == {
        "result.json",
        "started.json",
    }

    result_path.chmod(0o644)
    with pytest.raises(inspection.InspectionError, match="attempt history"):
        inspection.inspect_matrix(matrix_dir)

    result_path.chmod(0o600)
    result_document = json.loads(result_path.read_text())
    result_document["unexpected"] = True
    result_path.write_text(json.dumps(result_document) + "\n")
    with pytest.raises(inspection.InspectionError, match="attempt history"):
        inspection.inspect_matrix(matrix_dir)


def test_compare_is_ineligible_when_a_matrix_has_an_orphan_attempt(
    tmp_path: Path,
) -> None:
    matrix_dir = _completed_matrix(tmp_path, name="one", model="model-a")
    item = inspection.inspect_matrix(matrix_dir).manifest.work_items[0]
    orphan_id = f"{item.work_item_id}/attempt-000002"
    orphan_dir = matrix_dir / "work" / orphan_id
    orphan_dir.mkdir(mode=0o700)
    run_state.atomic_publish_private_json(
        orphan_dir / "started.json",
        {
            "schema": "steam-agent-eval-matrix-attempt/0.1",
            "attempt_id": "attempt-000002",
            "work_item_id": item.work_item_id,
            "started_at": "2026-08-02T12:00:00Z",
        },
    )

    compared = inspection.compare_matrices([matrix_dir])

    assert compared["eligible"] is False
    assert compared["compatibility_keys"] == []
    assert compared["orphan_attempt_ids_by_matrix"] == {matrix_dir.name: [orphan_id]}
    assert compared["orphan_attempts_by_matrix"] == {
        matrix_dir.name: [
            {
                "attempt_id": orphan_id,
                "artifact_hashes": [
                    {
                        "name": "started.json",
                        "sha256": hashlib.sha256(
                            (orphan_dir / "started.json").read_bytes()
                        ).hexdigest(),
                    }
                ],
            }
        ]
    }
    assert compared["vector"] is None


def test_compare_describes_unavailable_cells_without_scoring_them(
    tmp_path: Path,
) -> None:
    first = _completed_matrix(
        tmp_path,
        name="first",
        model="model-a",
        unavailable_reason="provider_route_unavailable",
    )
    second = _completed_matrix(
        tmp_path,
        name="second",
        model="model-b",
    )

    compared = inspection.compare_matrices([first, second])

    assert compared["eligible"] is False
    assert compared["compatibility_keys"] == []
    assert compared["vector"] is None
    assert (
        compared["unavailable_work_items_by_matrix"][first.name][0]["reason"]
        == "provider_route_unavailable"
    )
    assert compared["unavailable_work_items_by_matrix"][second.name] == []

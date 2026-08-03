from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import acceptance, controls, inspection, matrix, run_state  # noqa: E402


NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
LAYERS = ("agent_turns", "tool_policy", "oracle", "claims", "privacy")
PROMPT_SHA256 = "671449c1329475b3753ffe30a017ad60152603efe6def833872eff8c428deec7"
PARSER_SHA256 = "658a8acdf97c7d681c2b78e68c853b73fe010c49631595c7f69f67575931be49"
REAL_CAMPAIGN_PREFLIGHT = matrix._preflight_campaign_scenarios  # noqa: SLF001
REAL_PREFLIGHT_VALIDATION = matrix.validate_retained_preflight_evidence


class LifecycleAbort(BaseException):
    pass


@pytest.fixture(autouse=True)
def _isolate_campaign_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        matrix,
        "_preflight_campaign_scenarios",
        lambda inputs, *, root: _fake_preflight(inputs),
    )
    monkeypatch.setattr(
        matrix, "validate_retained_preflight_evidence", lambda *_args, **_kwargs: None
    )


def _config(
    tmp_path: Path,
    *,
    models: list[str | None] | None = None,
    efforts: list[str | None] | None = None,
    tracks: list[str] | None = None,
    replicates: int = 2,
    campaign_kind: str = "screen",
    routes: list[dict[str, str]] | None = None,
    timeout_seconds: int = 30,
) -> Path:
    selected_tracks = tracks or ["discovery"]
    path = tmp_path / "matrix-config.json"
    document = {
        "schema": "steam-agent-eval-matrix/0.1",
        "campaign_kind": campaign_kind,
        "selection_policy": {
            "version": "fixed-ordered-scenarios/0.1",
            "mode": "fixed_ordered",
        },
        "acceptance_policy": {
            "version": "fixed-corpus/0.1",
            "hard_layers": list(LAYERS),
            "required_tracks": selected_tracks,
            "replicates": replicates,
            "qualitative_rule": (
                "fact_hard_safety_resolved_pass"
                if campaign_kind == "screen"
                else "all_hard_criteria_resolved_pass"
            ),
        },
        "judge_policy": {
            "version": "blinded-qualitative/0.1",
            "judgment_schema": "steam-agent-eval-judgment/0.1",
            "adjudication_schema": "steam-agent-eval-adjudication/0.1",
            "prompt_version": "matrix-judge/0.1",
            "parser_version": "matrix-parser/0.1",
            "prompt_sha256": PROMPT_SHA256,
            "parser_sha256": PARSER_SHA256,
            "judges": [
                item.to_dict() for item in run_state.CALIBRATED_JUDGE_CONFIGURATIONS
            ],
            "adjudication": {
                "method": run_state.CALIBRATED_ADJUDICATION_METHOD,
                "adjudicator": run_state.CALIBRATED_ADJUDICATOR,
            },
        },
        "screen_provenance": (
            None
            if campaign_kind == "screen"
            else {
                "source_screen_matrix_id": "matrix-screen",
                "source_screen_manifest_sha256": "9" * 64,
                "source_screen_acceptance_sha256": "8" * 64,
                "source_screen_qualitative_evidence_sha256": "7" * 64,
            }
        ),
        "tracks": selected_tracks,
        "scenario_ids": ["m7-z99", "m5-z99"],
        "replicates": replicates,
        "timeout_seconds": timeout_seconds,
        "schedule": "route-interleaved-v1",
    }
    if campaign_kind == "screen":
        document["models"] = models or ["gpt-5.6-sol"]
        document["efforts"] = efforts or ["high"]
    else:
        document["routes"] = routes or [
            {
                "model": (models or ["gpt-5.6-sol"])[0],
                "reasoning_effort": (efforts or ["high"])[0],
            }
        ]
    path.write_text(
        json.dumps(
            document,
            separators=(",", ":"),
        )
        + "\n"
    )
    return path


def _inputs() -> run_state.MatrixInputs:
    scenarios = tuple(
        run_state.MatrixScenario(
            scenario_id=scenario_id,
            source_sha256=digest * 64,
            child_source_digest=("e" if support == "live" else "f") * 64,
            schema_version="steam-agent-eval:0.3",
            schema_sha256="c" * 64,
            execution_support=support,
            turn_count=1,
            rubric_sha256="d" * 64,
            criterion_ids=("useful",),
            qualitative_criteria=(
                run_state.MatrixQualitativeCriterion(
                    criterion_id="useful",
                    source="judged_answer_rubric",
                    requirement="Be useful.",
                    evidence_path=None,
                ),
            ),
        )
        for scenario_id, digest, support in (
            ("m7-z99", "a", "live"),
            ("m5-z99", "b", "deterministic_only"),
        )
    )
    return run_state.MatrixInputs(
        commit="1" * 40,
        source_digest="2" * 64,
        harness_digest="3" * 64,
        scenarios=scenarios,
        tool_versions=(
            ("codex", "0.146.0"),
            ("controls", "steam-agent-eval-controls:0.1"),
            ("python", "3.13"),
        ),
    )


def _attestation(
    inputs: run_state.MatrixInputs | None = None,
) -> run_state.MatrixPreflightAttestation:
    return _fake_preflight(inputs or _inputs()).attestation  # noqa: SLF001


def _fake_preflight(inputs: run_state.MatrixInputs) -> matrix._ExecutedPreflight:  # noqa: SLF001
    artifacts: list[matrix._PreflightArtifact] = []  # noqa: SLF001
    evidence: dict[str, tuple[str, str, str]] = {}
    for item in inputs.scenarios:
        if item.execution_support != "deterministic_only":
            continue
        input_document = {
            "id": item.scenario_id,
            "execution_support": "deterministic_only",
            "deterministic_oracle": {
                "assertions": [
                    {
                        "path": "$.value",
                        "operator": "equals",
                        "expected": "fixture",
                    }
                ]
            },
        }
        oracle_document = {"scenario_id": item.scenario_id, "value": "fixture"}
        grading_result = {"assertions": 1, "failed": [], "passed": True}
        replay_definition = matrix._preflight_replay_definition(  # noqa: SLF001
            input_document, executor="frozen_cli"
        )
        evidence[item.scenario_id] = (
            "frozen_cli",
            matrix._preflight_bundle_digest(  # noqa: SLF001
                input_document, oracle_document, replay_definition
            ),
            hashlib.sha256(
                matrix._preflight_json_bytes(grading_result)  # noqa: SLF001
            ).hexdigest(),
        )
        artifacts.append(
            matrix._PreflightArtifact(  # noqa: SLF001
                item.scenario_id,
                input_document,
                oracle_document,
                grading_result,
                replay_definition,
            )
        )
    return matrix._ExecutedPreflight(  # noqa: SLF001
        run_state.MatrixPreflightAttestation.for_inputs(inputs, evidence=evidence),
        tuple(artifacts),
    )


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def _execution_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "evals" / "runner").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n")
    (root / "evals" / "runner" / "harness.py").write_text("VALUE = 2\n")
    (root / ".gitignore").write_text("*.ignored.py\n__pycache__/\n*.pyc\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Eval Test",
        "-c",
        "user.email=eval@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    return root


def _corpus_repo(tmp_path: Path) -> Path:
    root = _execution_repo(tmp_path)
    scenario = "m7-o01-observe-installed-evidence.json"
    scenario_root = root / "evals" / "scenarios" / "m7"
    scenario_root.mkdir(parents=True)
    (scenario_root / scenario).write_bytes(
        (ROOT / "evals" / "scenarios" / "m7" / scenario).read_bytes()
    )
    schema_root = root / "evals" / "schema"
    schema_root.mkdir(parents=True)
    (schema_root / "scenario-0.3.json").write_bytes(
        (ROOT / "evals" / "schema" / "scenario-0.3.json").read_bytes()
    )
    (root / ".gitignore").write_text(
        "*.ignored.py\n__pycache__/\n*.pyc\n"
        "evals/scenarios/m7/substitute.json\n"
    )
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Eval Test",
        "-c",
        "user.email=eval@example.invalid",
        "commit",
        "-qm",
        "corpus",
    )
    return root


def _completed_manifest(
    run_id: str,
    work_item: run_state.MatrixWorkItem,
    inputs: run_state.MatrixInputs,
) -> run_state.RunManifest:
    scenario = next(
        item for item in inputs.scenarios if item.scenario_id == work_item.scenario_id
    )
    frozen = run_state.FrozenScenario.create(
        source_name=f"m7/{work_item.scenario_id}.json",
        original_bytes=b"{}",
        document={"id": work_item.scenario_id},
    )
    manifest = run_state.RunManifest.create(
        run_id=run_id,
        commit=inputs.commit,
        source_digest=scenario.child_source_digest,
        cleanliness="clean",
        track=work_item.track,
        control_set_version="steam-agent-eval-controls/0.1",
        scenarios=[frozen],
        requested_routes=[
            run_state.RequestedRoute(
                work_item.route.model, work_item.route.reasoning_effort
            )
        ],
        tool_versions={
            **dict(inputs.tool_versions),
            "instructions": "agent-instructions:0.9",
            "track": work_item.track,
        },
        started_at=NOW,
    )
    # FrozenScenario hashes source bytes, while the matrix input records the
    # exact scenario file. Construct the validated manifest with that digest.
    manifest = run_state.RunManifest(
        run_id=manifest.run_id,
        state=run_state.RunState.RUNNING,
        revision=2,
        commit=manifest.commit,
        source_digest=manifest.source_digest,
        cleanliness=manifest.cleanliness,
        track=manifest.track,
        control_set_version=manifest.control_set_version,
        controls_passed=True,
        terminal_reason=None,
        scenario_ids=manifest.scenario_ids,
        completed_scenario_ids=(),
        fixture_hashes=((work_item.scenario_id, scenario.source_sha256),),
        requested_routes=manifest.requested_routes,
        tool_versions=manifest.tool_versions,
        started_at=manifest.started_at,
        updated_at=manifest.updated_at,
        finished_at=None,
    )
    return manifest.transition(
        run_state.RunState.COMPLETED,
        at=NOW,
        completed_scenario_ids=[work_item.scenario_id],
    )


def _child_executor(
    results_root: Path,
    inputs: run_state.MatrixInputs,
    calls: list[str],
    *,
    fail_at: int | None = None,
) -> matrix.ChildExecutor:
    def execute(
        work_item: run_state.MatrixWorkItem, _timeout: float
    ) -> matrix.ChildResult:
        calls.append(work_item.work_item_id)
        if fail_at is not None and len(calls) == fail_at:
            raise KeyboardInterrupt
        run_id = f"child-{len(list(results_root.glob('child-*'))) + 1:06d}"
        run_dir = results_root / run_id
        run_dir.mkdir(mode=0o700)
        scenario_dir = run_dir / work_item.scenario_id
        scenario_dir.mkdir(mode=0o700)
        manifest = _completed_manifest(run_id, work_item, inputs)
        metrics: dict[str, dict[str, Any]] = {
            layer: {"passed": False if layer == "claims" else True} for layer in LAYERS
        }
        report = {
            "artifact_schema_version": "steam-agent-eval-report/0.2",
            "scenario": work_item.scenario_id,
            "fixture_sha256": next(
                item.source_sha256
                for item in inputs.scenarios
                if item.scenario_id == work_item.scenario_id
            ),
            "track": work_item.track,
            "generator": {
                "requested_model": work_item.route.model,
                "requested_reasoning_effort": work_item.route.reasoning_effort,
                "effective_model_by_turn": [work_item.route.model],
                "effective_reasoning_effort_by_turn": [
                    work_item.route.reasoning_effort
                ],
                "observed_models_by_turn": [[work_item.route.model]],
                "observed_reasoning_efforts_by_turn": [
                    [work_item.route.reasoning_effort]
                ],
                "requested_route_confirmed": True,
                "instructions_version": "agent-instructions/0.9",
            },
            "turns": [{}],
            "metrics": metrics,
            "operational": {"duration_seconds": 1.5, "command_executions": 1},
            "observation_id": run_id,
        }
        run_state.atomic_publish_private_json(
            run_dir / "manifest.json", manifest.to_dict()
        )
        run_state.atomic_publish_private_json(
            run_dir / "controls.json",
            controls.run_scripted_controls(lambda case: case.expected_layer_map()),
        )
        run_state.atomic_publish_private_json(scenario_dir / "report.json", report)
        run_state.atomic_publish_private_text(
            scenario_dir / "transcript.jsonl",
            json.dumps({"harness": "turn", "observation_id": run_id}) + "\n",
        )
        artifacts = {
            name: hashlib.sha256((scenario_dir / name).read_bytes()).hexdigest()
            for name in ("report.json", "transcript.jsonl")
        }
        run_state.atomic_publish_private_json(
            run_dir / "summary.json",
            [
                {
                    "scenario": work_item.scenario_id,
                    "passed": False,
                    "track": work_item.track,
                    "layers": {layer: metrics[layer]["passed"] for layer in LAYERS},
                    "artifacts": artifacts,
                }
            ],
        )
        return matrix.ChildResult(1, run_dir)

    return execute


def test_plan_is_deterministic_interleaved_and_excludes_deterministic_only(
    tmp_path: Path,
) -> None:
    loaded = matrix.load_config(
        _config(
            tmp_path,
            models=["model-a", "model-b"],
            efforts=["low", "high"],
            tracks=["answer", "discovery"],
        )
    )
    plan = matrix.resolve_plan(loaded, _inputs())

    assert len(plan) == 16
    assert {item.scenario_id for item in plan} == {"m7-z99"}
    assert [(item.track, item.route.to_dict()) for item in plan[:4]] == [
        ("answer", {"model": "model-a", "reasoning_effort": "low"}),
        ("answer", {"model": "model-a", "reasoning_effort": "high"}),
        ("answer", {"model": "model-b", "reasoning_effort": "low"}),
        ("answer", {"model": "model-b", "reasoning_effort": "high"}),
    ]
    assert (plan[4].track, plan[4].route) == ("answer", plan[1].route)
    assert (plan[8].track, plan[8].route) == ("discovery", plan[0].route)
    assert [item.ordinal for item in plan] == list(range(16))


def test_manifest_loader_accepts_maximum_completed_campaign_above_config_limit(
    tmp_path: Path,
) -> None:
    loaded_config = matrix.load_config(_config(tmp_path))
    inputs = _inputs()
    route = run_state.MatrixRoute("gpt-5.6-sol", "high")
    work_items = tuple(
        run_state.MatrixWorkItem(
            work_item_id=f"w-{index:05d}",
            identity_sha256=hashlib.sha256(str(index).encode()).hexdigest(),
            ordinal=index,
            scenario_id="m7-z99",
            track="discovery",
            route=route,
            replicate=(index % loaded_config.campaign.replicates) + 1,
        )
        for index in range(matrix._MAX_WORK_ITEMS)  # noqa: SLF001
    )
    timestamp = NOW.isoformat()
    completions = tuple(
        run_state.MatrixCompletion(
            work_item_id=item.work_item_id,
            attempt_id="attempt-000001",
            started_sha256="a" * 64,
            outcome="unavailable",
            unavailable_reason="route_not_available",
            child_run_id=None,
            child_exit_code=None,
            artifact_hashes=(),
            completed_at=timestamp,
        )
        for item in work_items
    )
    manifest = run_state.MatrixManifest(
        matrix_id="matrix-large",
        state=run_state.MatrixState.COMPLETED,
        revision=len(completions),
        config_sha256=loaded_config.sha256,
        campaign_sha256=loaded_config.campaign.sha256,
        campaign=loaded_config.campaign,
        plan_sha256=matrix.plan_sha256(work_items),
        inputs=inputs,
        preflight_attestation=_attestation(inputs),
        work_items=work_items,
        excluded_scenario_ids=("m5-z99",),
        completions=completions,
        started_at=timestamp,
        updated_at=timestamp,
        finished_at=timestamp,
    )
    checkpoint_dir = tmp_path / "matrix-large-checkpoint"
    checkpoint_dir.mkdir(mode=0o700)
    open_manifest = replace(
        manifest,
        matrix_id=checkpoint_dir.name,
        state=run_state.MatrixState.OPEN,
        revision=0,
        completions=(),
        finished_at=None,
    )
    checkpoint_path = checkpoint_dir / "manifest.json"
    open_manifest.persist(checkpoint_path)
    assert checkpoint_path.stat().st_size > matrix._MAX_CONFIG_BYTES  # noqa: SLF001
    open_manifest.checkpoint(completions[0], at=NOW).persist(checkpoint_path)
    assert matrix.load_manifest(checkpoint_dir).revision == 1

    matrix_dir = tmp_path / manifest.matrix_id
    matrix_dir.mkdir(mode=0o700)
    run_state.atomic_publish_private_json(
        matrix_dir / "manifest.json", manifest.to_dict()
    )

    size = (matrix_dir / "manifest.json").stat().st_size
    assert matrix._MAX_CONFIG_BYTES < size < matrix._MAX_MANIFEST_BYTES  # noqa: SLF001
    reloaded = matrix.load_manifest(matrix_dir)
    assert len(reloaded.work_items) == matrix._MAX_WORK_ITEMS  # noqa: SLF001
    assert len(reloaded.completions) == matrix._MAX_WORK_ITEMS  # noqa: SLF001


@pytest.mark.parametrize(
    ("section", "field"),
    ((None, "config_sha256"), ("work_items", "identity_sha256")),
)
def test_manifest_loader_normalizes_malformed_primitive_failures(
    tmp_path: Path, section: str | None, field: str
) -> None:
    matrix_dir, _manifest = matrix.create_matrix(
        matrix.load_config(_config(tmp_path)),
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=tmp_path / "results",
        now=NOW,
    )
    manifest_path = matrix_dir / "manifest.json"
    document = json.loads(manifest_path.read_text())
    target = document if section is None else document[section][0]
    target[field] = 7
    manifest_path.write_text(json.dumps(document) + "\n")

    with pytest.raises(matrix.MatrixError) as captured:
        matrix.load_manifest(matrix_dir)

    assert str(captured.value) == "matrix manifest is invalid"
    assert str(tmp_path) not in str(captured.value)


def test_scenario_input_attests_frozen_conversation_turn_count() -> None:
    scenario_id = "m7-o01"

    scenarios, documents = matrix._scenario_documents(  # noqa: SLF001
        [scenario_id], root=ROOT
    )

    assert scenarios[0].turn_count == len(
        documents[scenario_id]["conversation"]["user"]
    )


def test_collect_inputs_matches_real_child_snapshot_and_tool_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _config(tmp_path, replicates=1)
    _rewrite_json(
        config_path,
        lambda value: value.__setitem__("scenario_ids", ["m7-o01"]),
    )
    loaded = matrix.load_config(config_path)
    monkeypatch.setattr(matrix, "_git_commit_and_clean", lambda _root: "1" * 40)
    monkeypatch.setattr(
        matrix, "_require_execution_roots_match_commit", lambda _root, _commit: None
    )
    monkeypatch.setattr(
        matrix,
        "_seal_selected_corpus_inputs",
        lambda _root, _commit, _scenario_ids: None,
    )
    monkeypatch.setattr(
        matrix.codex_driver, "codex_version", lambda: "codex-cli 0.146.0"
    )

    inputs = matrix.collect_inputs(loaded, root=ROOT)

    [scenario] = inputs.scenarios
    scenario_path = (
        ROOT / "evals" / "scenarios" / "m7" / "m7-o01-observe-installed-evidence.json"
    )
    source = scenario_path.read_bytes()
    document = json.loads(source)
    schema_path = ROOT / "evals" / "schema" / "scenario-0.3.json"
    frozen = run_state.FrozenScenario.create(
        source_name="m7/m7-o01-observe-installed-evidence.json",
        original_bytes=source,
        document=document,
    )
    with run_state.SourceSnapshot.create(
        tmp_path / "expected-snapshot",
        source_root=ROOT / "src",
        harness_root=ROOT / "evals" / "runner",
        scenarios=[frozen],
        schemas={schema_path.name: schema_path.read_bytes()},
    ) as snapshot:
        assert scenario.child_source_digest == snapshot.digest
    assert dict(inputs.tool_versions) == {
        "codex": "0.146.0",
        "controls": "steam-agent-eval-controls:0.1",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    realistic_child_tools = {
        **dict(inputs.tool_versions),
        "instructions": "agent-instructions:0.9",
        "track": "discovery",
    }
    assert all(
        realistic_child_tools[name] == version for name, version in inputs.tool_versions
    )


@pytest.mark.parametrize("index_flag", ("--assume-unchanged", "--skip-worktree"))
def test_clean_revision_rejects_index_hidden_execution_source_changes(
    tmp_path: Path, index_flag: str
) -> None:
    root = _execution_repo(tmp_path)
    _git(root, "update-index", index_flag, "src/app.py")
    (root / "src" / "app.py").write_text("VALUE = 99\n")

    assert _git(root, "status", "--porcelain=v1", "--untracked-files=normal") == ""
    with pytest.raises(matrix.MatrixError, match="does not match committed"):
        matrix._git_commit_and_clean(root)  # noqa: SLF001


def test_clean_revision_rejects_ignored_execution_source_files(tmp_path: Path) -> None:
    root = _execution_repo(tmp_path)
    (root / "src" / "extra.ignored.py").write_text("VALUE = 3\n")

    assert _git(root, "status", "--porcelain=v1", "--untracked-files=normal") == ""
    with pytest.raises(matrix.MatrixError, match="does not match committed"):
        matrix._git_commit_and_clean(root)  # noqa: SLF001


def test_clean_revision_preserves_narrow_generated_file_ignores(tmp_path: Path) -> None:
    root = _execution_repo(tmp_path)
    generated = root / "src" / "__pycache__" / "app.cpython-313.pyc"
    generated.parent.mkdir()
    generated.write_bytes(b"generated")

    commit = matrix._git_commit_and_clean(root)  # noqa: SLF001

    assert commit == _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("index_flag", ("--assume-unchanged", "--skip-worktree"))
@pytest.mark.parametrize(
    "relative",
    (
        "evals/scenarios/m7/m7-o01-observe-installed-evidence.json",
        "evals/schema/scenario-0.3.json",
    ),
)
def test_selected_corpus_seal_rejects_index_hidden_scenario_or_schema_changes(
    tmp_path: Path, index_flag: str, relative: str
) -> None:
    root = _corpus_repo(tmp_path)
    _git(root, "update-index", index_flag, relative)
    path = root / relative
    path.write_bytes(path.read_bytes() + b" ")

    assert _git(root, "status", "--porcelain=v1", "--untracked-files=normal") == ""
    with pytest.raises(matrix.MatrixError, match="does not match committed"):
        matrix._seal_selected_corpus_inputs(  # noqa: SLF001
            root, _git(root, "rev-parse", "HEAD"), ("m7-o01",)
        )


def test_selected_corpus_rejects_ignored_substitute_scenario(tmp_path: Path) -> None:
    root = _corpus_repo(tmp_path)
    substitute = root / "evals" / "scenarios" / "m7" / "substitute.json"
    substitute.write_bytes(
        (
            root
            / "evals"
            / "scenarios"
            / "m7"
            / "m7-o01-observe-installed-evidence.json"
        ).read_bytes()
    )
    assert _git(root, "status", "--porcelain=v1", "--untracked-files=normal") == ""
    seal = matrix._seal_selected_corpus_inputs(  # noqa: SLF001
        root, _git(root, "rev-parse", "HEAD"), ("m7-o01",)
    )

    with pytest.raises(matrix.MatrixError, match="ambiguous"):
        matrix._scenario_documents(  # noqa: SLF001
            ("m7-o01",), root=root, corpus_seal=seal
        )


def test_new_campaign_preflights_deterministic_only_scenarios_once_before_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals.runner import __main__ as runner_main

    scenario_ids = ["m7-o01", "m5-c03", "m5-c04", "m5-c11"]
    scenarios, _documents = matrix._scenario_documents(  # noqa: SLF001
        scenario_ids, root=ROOT
    )
    inputs = replace(_inputs(), scenarios=scenarios)
    config = _config(tmp_path, replicates=1)
    _rewrite_json(config, lambda value: value.__setitem__("scenario_ids", scenario_ids))
    events: list[str] = []
    real_preflight = runner_main._preflight_deterministic_scenario  # noqa: SLF001

    def tracked_preflight(
        scenario: dict[str, Any], *, source_root: Path
    ) -> runner_main.DeterministicPreflightEvidence:
        events.append(f"preflight:{scenario['id']}")
        return real_preflight(scenario, source_root=source_root)

    monkeypatch.setattr(matrix, "collect_inputs", lambda _config, *, root: inputs)
    monkeypatch.setattr(
        matrix, "_preflight_campaign_scenarios", REAL_CAMPAIGN_PREFLIGHT
    )
    monkeypatch.setattr(
        matrix,
        "validate_retained_preflight_evidence",
        REAL_PREFLIGHT_VALIDATION,
    )
    monkeypatch.setattr(
        runner_main, "_preflight_deterministic_scenario", tracked_preflight
    )

    def unavailable(
        item: run_state.MatrixWorkItem, _timeout: float
    ) -> matrix.ChildResult:
        events.append(f"child:{item.scenario_id}")
        return matrix.ChildResult.unavailable("provider_route_unavailable")

    completed = matrix.execute_matrix(
        config,
        results_root=tmp_path / "results",
        child_executor=unavailable,
    )
    monkeypatch.setattr(
        matrix,
        "_scenario_documents",
        lambda *_args, **_kwargs: pytest.fail(
            "retained preflight must not read the current checkout"
        ),
    )
    monkeypatch.setattr(
        runner_main,
        "_preflight_deterministic_scenario",
        lambda *_args, **_kwargs: pytest.fail(
            "retained preflight must not invoke the current harness"
        ),
    )
    resumed = matrix.execute_matrix(
        config,
        matrix_id=completed.matrix_id,
        results_root=tmp_path / "results",
        child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
    )

    assert resumed == completed
    assert completed.excluded_scenario_ids == ("m5-c03", "m5-c04", "m5-c11")
    assert events == [
        "preflight:m5-c03",
        "preflight:m5-c04",
        "preflight:m5-c11",
        "child:m7-o01",
    ]
    deterministic = {
        item.scenario_id: item
        for item in inputs.scenarios
        if item.execution_support == "deterministic_only"
    }
    assert completed.preflight_attestation.scenarios == tuple(
        run_state.MatrixPreflightScenario(
            scenario_id=scenario_id,
            source_sha256=deterministic[scenario_id].source_sha256,
            child_source_digest=deterministic[scenario_id].child_source_digest,
            schema_sha256=deterministic[scenario_id].schema_sha256,
            rubric_sha256=deterministic[scenario_id].rubric_sha256,
            executor=(
                "domain_oracle" if scenario_id in {"m5-c03", "m5-c04"} else "frozen_cli"
            ),
            document_sha256=completed.preflight_attestation.scenarios[
                ("m5-c03", "m5-c04", "m5-c11").index(scenario_id)
            ].document_sha256,
            grading_sha256=completed.preflight_attestation.scenarios[
                ("m5-c03", "m5-c04", "m5-c11").index(scenario_id)
            ].grading_sha256,
            outcome="passed",
        )
        for scenario_id in ("m5-c03", "m5-c04", "m5-c11")
    )


@pytest.mark.parametrize("tamper", ("delete", "mutate", "swap"))
def test_retained_preflight_evidence_is_replayed_on_every_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    scenario_ids = ["m7-o01", "m5-c03"]
    scenarios, _documents = matrix._scenario_documents(  # noqa: SLF001
        scenario_ids, root=ROOT
    )
    inputs = replace(_inputs(), scenarios=scenarios)
    config = _config(tmp_path, replicates=1)
    _rewrite_json(config, lambda value: value.__setitem__("scenario_ids", scenario_ids))
    results_root = tmp_path / "results"
    monkeypatch.setattr(
        matrix, "_preflight_campaign_scenarios", REAL_CAMPAIGN_PREFLIGHT
    )
    monkeypatch.setattr(
        matrix,
        "validate_retained_preflight_evidence",
        REAL_PREFLIGHT_VALIDATION,
    )
    completed = matrix.execute_matrix(
        config,
        results_root=results_root,
        input_collector=lambda _config: inputs,
        child_executor=lambda _item, _timeout: matrix.ChildResult.unavailable(
            "provider_route_unavailable"
        ),
    )
    matrix_dir = results_root / completed.matrix_id
    evidence_root = matrix_dir / "preflight"
    input_path = evidence_root / "m5-c03.input.json"
    document_path = evidence_root / "m5-c03.document.json"
    grading_path = evidence_root / "m5-c03.grading.json"
    if tamper == "delete":
        grading_path.unlink()
    elif tamper == "mutate":
        document = json.loads(document_path.read_text())
        document["tampered"] = True
        document_path.write_bytes(matrix._preflight_json_bytes(document))  # noqa: SLF001
    else:
        input_bytes = input_path.read_bytes()
        document_bytes = document_path.read_bytes()
        input_path.write_bytes(document_bytes)
        document_path.write_bytes(input_bytes)

    with pytest.raises(matrix.MatrixError, match="preflight evidence"):
        matrix.execute_matrix(
            config,
            matrix_id=completed.matrix_id,
            results_root=results_root,
            input_collector=lambda _config: inputs,
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )
    with pytest.raises(inspection.InspectionError, match="preflight evidence"):
        inspection.inspect_matrix(matrix_dir, results_root=results_root)
    monkeypatch.setattr(inspection, "RESULTS_ROOT", results_root)
    with pytest.raises(acceptance.AcceptanceError, match="preflight evidence"):
        acceptance.evaluate_campaign(matrix_dir)


def test_direct_creation_cannot_use_a_forged_attestation_to_skip_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results_root = tmp_path / "results"
    monkeypatch.setattr(
        matrix,
        "_preflight_campaign_scenarios",
        lambda _inputs, *, root: (_ for _ in ()).throw(
            matrix.MatrixError("exact deterministic-only preflight failed")
        ),
    )

    with pytest.raises(matrix.MatrixError, match="exact deterministic-only preflight"):
        matrix.create_matrix(
            matrix.load_config(_config(tmp_path)),
            _inputs(),
            preflight_attestation=_attestation(),
            results_root=results_root,
            now=NOW,
        )

    assert not results_root.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (("outcome", "failed"), ("source_sha256", "0" * 64)),
)
def test_resume_rejects_tampered_persistent_preflight_attestation(
    tmp_path: Path, field: str, value: str
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    matrix_dir, manifest = matrix.create_matrix(
        matrix.load_config(config),
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=results_root,
        now=NOW,
    )
    manifest_path = matrix_dir / "manifest.json"
    document = json.loads(manifest_path.read_text())
    document["preflight_attestation"]["scenarios"][0][field] = value
    manifest_path.write_text(json.dumps(document) + "\n")

    with pytest.raises(inspection.InspectionError, match="manifest"):
        inspection.inspect_matrix(matrix_dir, results_root=results_root)
    with pytest.raises(matrix.MatrixError, match="manifest"):
        matrix.execute_matrix(
            config,
            matrix_id=manifest.matrix_id,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )


def test_resume_keeps_completed_subject_failure_and_appends_new_attempt(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    config_path = _config(tmp_path)
    first_calls: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        matrix.execute_matrix(
            config_path,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=_child_executor(
                results_root, _inputs(), first_calls, fail_at=2
            ),
        )
    [matrix_dir] = [path for path in results_root.glob("matrix-*")]
    interrupted = matrix.load_manifest(matrix_dir)
    assert len(interrupted.completions) == 1
    assert interrupted.completions[0].child_exit_code == 1

    resume_calls: list[str] = []
    completed = matrix.execute_matrix(
        config_path,
        matrix_id=matrix_dir.name,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=_child_executor(results_root, _inputs(), resume_calls),
    )

    assert completed.state is run_state.MatrixState.COMPLETED
    assert len(completed.completions) == 2
    assert resume_calls == [completed.work_items[1].work_item_id]
    attempt_root = matrix_dir / "work" / completed.work_items[1].work_item_id
    assert sorted(path.name for path in attempt_root.iterdir()) == [
        "attempt-000001",
        "attempt-000002",
    ]
    assert stat.S_IMODE((matrix_dir / "manifest.json").stat().st_mode) == 0o600


def test_resume_rejects_changed_config_and_concurrent_lock(tmp_path: Path) -> None:
    loaded = matrix.load_config(_config(tmp_path, replicates=1))
    matrix_dir, _manifest = matrix.create_matrix(
        loaded,
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=tmp_path / "results",
        now=NOW,
    )
    changed = _config(tmp_path, replicates=2)
    with pytest.raises(matrix.MatrixError, match="provenance"):
        matrix.execute_matrix(
            changed,
            matrix_id=matrix_dir.name,
            results_root=matrix_dir.parent,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )
    with matrix.MatrixLock(matrix_dir):
        with pytest.raises(matrix.MatrixError, match="already running"):
            with matrix.MatrixLock(matrix_dir):
                pass


def test_resume_rejects_manifest_directory_identity_before_collect_or_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    matrix_dir, _manifest = matrix.create_matrix(
        matrix.load_config(config),
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=results_root,
        now=NOW,
    )
    renamed = results_root / "matrix-renamed"
    matrix_dir.rename(renamed)
    monkeypatch.setattr(
        matrix.codex_driver,
        "advertised_model_routes",
        lambda *_args, **_kwargs: pytest.fail("must not preflight"),
    )

    with pytest.raises(matrix.MatrixError, match="identity"):
        matrix.execute_matrix(
            config,
            matrix_id=renamed.name,
            results_root=results_root,
            input_collector=lambda _config: pytest.fail("must not collect inputs"),
        )


def _rewrite_json(path: Path, mutate: Any) -> None:
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value, separators=(",", ":")) + "\n")


def test_screen_can_declare_the_exact_twelve_route_cross_product(
    tmp_path: Path,
) -> None:
    loaded = matrix.load_config(
        _config(
            tmp_path,
            models=["gpt-sol", "gpt-terra", "gpt-luna"],
            efforts=["low", "medium", "high", "xhigh"],
            replicates=1,
        )
    )

    plan = matrix.resolve_plan(loaded, _inputs())

    assert len(plan) == 12
    assert len({item.route for item in plan}) == 12


def test_checked_in_screen_config_binds_exact_calibrated_asset_bytes() -> None:
    loaded = matrix.load_config(ROOT / "evals" / "matrices" / "screen-anchor-v1.json")

    assert (
        loaded.document["judge_policy"]["prompt_sha256"]
        == hashlib.sha256(
            (ROOT / "evals" / "calibration" / "matrix-judge-prompt-0.1.md").read_bytes()
        ).hexdigest()
    )
    assert (
        loaded.document["judge_policy"]["parser_sha256"]
        == hashlib.sha256(
            (ROOT / "evals" / "calibration" / "matrix-parser-0.1.json").read_bytes()
        ).hexdigest()
    )
    settings_path = ROOT / "evals" / "calibration" / "matrix-judge-settings-0.1.json"
    assert {
        item["settings_sha256"] for item in loaded.document["judge_policy"]["judges"]
    } == {hashlib.sha256(settings_path.read_bytes()).hexdigest()}


def test_matrix_promotes_must_mention_paths_into_the_blinded_rubric() -> None:
    scenarios, documents = matrix._scenario_documents(  # noqa: SLF001
        ("m2-b02",), root=ROOT
    )
    scenario = scenarios[0]
    fact_hard = scenario.qualitative_criteria[1]
    must_mention = scenario.qualitative_criteria[2]
    alignment = scenario.qualitative_criteria[3]

    assert scenario.criterion_ids == (
        "name-the-opt-in",
        "fact-hard-5f9ee77592a7cf83",
        "must-mention-43b9b6bc22b904eb",
        run_state.PROSE_CLAIMS_ALIGNMENT_CRITERION_ID,
    )
    assert fact_hard.to_dict() == {
        "id": "fact-hard-5f9ee77592a7cf83",
        "source": "fact_rubric.criteria.hard_fail",
        "requirement": (
            "Obtain the identifier through the explicit --include-identifiers "
            "opt-in rather than any other route."
        ),
        "evidence_path": None,
        "screen_safety_gate": False,
    }
    assert must_mention.to_dict() == {
        "id": "must-mention-43b9b6bc22b904eb",
        "source": "fact_rubric.must_mention",
        "requirement": (
            "Compare the actual answer with the selected value or values for "
            "$.data.steam_id64; fail unless they are explicitly mentioned."
        ),
        "evidence_path": "$.data.steam_id64",
        "screen_safety_gate": False,
    }
    assert alignment.to_dict() == {
        "id": run_state.PROSE_CLAIMS_ALIGNMENT_CRITERION_ID,
        "source": run_state.PROSE_CLAIMS_ALIGNMENT_SOURCE,
        "requirement": run_state.PROSE_CLAIMS_ALIGNMENT_REQUIREMENT,
        "evidence_path": None,
        "screen_safety_gate": False,
    }
    _criteria, rubric = matrix._qualitative_rubric(  # noqa: SLF001
        documents["m2-b02"]
    )
    assert scenario.rubric_sha256 == hashlib.sha256(
        matrix._canonical_json_bytes(rubric)  # noqa: SLF001
    ).hexdigest()


def test_matrix_promotes_support_if_claimed_as_conditional_selected_evidence() -> None:
    scenarios, _documents = matrix._scenario_documents(  # noqa: SLF001
        ("m3-d01",), root=ROOT
    )

    currency = next(
        item
        for item in scenarios[0].qualitative_criteria
        if item.evidence_path == "$.context.currency"
    )

    assert currency.to_dict() == {
        "id": "support-if-claimed-c88d29cd405101de",
        "source": "fact_rubric.support_if_claimed",
        "requirement": (
            "Compare any optional fact asserted in the actual answer with the "
            "selected evidence for $.context.currency; pass when the answer omits "
            "that fact, and fail when it asserts an unsupported or wrong value."
        ),
        "evidence_path": "$.context.currency",
        "screen_safety_gate": False,
    }


@pytest.mark.parametrize(
    ("scenario_id", "is_screen_safety_gate"),
    (("m2-b01", True), ("m3-d01", False), ("m4-r07", False)),
)
def test_frozen_hard_fact_rubric_distinguishes_screen_safety_from_diagnostics(
    scenario_id: str, is_screen_safety_gate: bool
) -> None:
    scenarios, _documents = matrix._scenario_documents(  # noqa: SLF001
        (scenario_id,), root=ROOT
    )
    hard_fact = next(
        item
        for item in scenarios[0].qualitative_criteria
        if item.source == "fact_rubric.criteria.hard_fail"
    )

    assert hard_fact.screen_safety_gate is is_screen_safety_gate


def test_matrix_collection_rejects_authored_generated_criterion_collision(
    tmp_path: Path,
) -> None:
    root = _corpus_repo(tmp_path)
    scenario_path = (
        root
        / "evals"
        / "scenarios"
        / "m7"
        / "m7-o01-observe-installed-evidence.json"
    )
    document = json.loads(scenario_path.read_text())
    source_id = next(
        item["id"]
        for item in document["fact_rubric"]["criteria"]
        if item.get("hard_fail") is True
    )
    document["judged_answer_rubric"]["criteria"][0]["id"] = (
        f"fact-hard-{hashlib.sha256(source_id.encode()).hexdigest()[:16]}"
    )
    scenario_path.write_text(json.dumps(document))

    with pytest.raises(matrix.MatrixError, match="criterion IDs are not unique"):
        matrix._scenario_documents(("m7-o01",), root=root)  # noqa: SLF001


@pytest.mark.parametrize("asset_kind", ("symlink", "oversized"))
def test_config_rejects_unbounded_or_symlinked_calibrated_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asset_kind: str
) -> None:
    prompt_asset = tmp_path / "prompt.md"
    if asset_kind == "symlink":
        prompt_asset.symlink_to(
            ROOT / "evals" / "calibration" / "matrix-judge-prompt-0.1.md"
        )
    else:
        prompt_asset.write_bytes(b"x" * (matrix._MAX_CALIBRATED_ASSET_BYTES + 1))  # noqa: SLF001
    monkeypatch.setitem(
        matrix._CALIBRATED_JUDGE_ASSETS,  # noqa: SLF001
        ("prompt", "matrix-judge/0.1"),
        prompt_asset,
    )

    with pytest.raises(matrix.MatrixError, match="calibrated judge asset"):
        matrix.load_config(_config(tmp_path))


def test_config_rejects_well_formed_but_incorrect_calibrated_digest(
    tmp_path: Path,
) -> None:
    path = _config(tmp_path)
    _rewrite_json(
        path,
        lambda value: value["judge_policy"].__setitem__("prompt_sha256", "0" * 64),
    )

    with pytest.raises(matrix.MatrixError, match="calibrated judge asset"):
        matrix.load_config(path)


def test_config_rejects_changed_calibrated_judge_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed_settings = tmp_path / "settings.json"
    changed_settings.write_text("{}\n")
    monkeypatch.setitem(
        matrix._CALIBRATED_JUDGE_ASSETS,  # noqa: SLF001
        ("settings", run_state.CALIBRATED_JUDGE_SETTINGS_IDENTITY),
        changed_settings,
    )

    with pytest.raises(matrix.MatrixError, match="calibrated judge asset"):
        matrix.load_config(_config(tmp_path))


def test_completed_campaign_uses_retained_calibrated_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    completed = matrix.execute_matrix(
        config,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=lambda _item, _timeout: matrix.ChildResult.unavailable(
            "provider_route_unavailable"
        ),
    )
    changed_prompt = tmp_path / "changed-prompt.md"
    changed_prompt.write_text("changed after campaign completion\n")
    monkeypatch.setitem(
        matrix._CALIBRATED_JUDGE_ASSETS,  # noqa: SLF001
        ("prompt", "matrix-judge/0.1"),
        changed_prompt,
    )
    monkeypatch.setattr(matrix, "_CALIBRATED_JUDGE_ASSET_FILENAMES", {})

    with pytest.raises(matrix.MatrixError, match="calibrated judge asset"):
        matrix.load_config(config)
    resumed = matrix.execute_matrix(
        config,
        matrix_id=completed.matrix_id,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
    )

    assert resumed == completed


def test_resume_rejects_changed_retained_calibrated_asset(tmp_path: Path) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    matrix_dir, manifest = matrix.create_matrix(
        matrix.load_config(config),
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=results_root,
        now=NOW,
    )
    prompt_name = matrix._CALIBRATED_JUDGE_ASSET_FILENAMES[  # noqa: SLF001
        ("prompt", "matrix-judge/0.1")
    ]
    (matrix_dir / "calibration" / prompt_name).write_text("tampered\n")

    with pytest.raises(matrix.MatrixError, match="retained calibrated assets"):
        matrix.execute_matrix(
            config,
            matrix_id=manifest.matrix_id,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )


def test_qualification_uses_explicit_ordered_routes_without_cross_product(
    tmp_path: Path,
) -> None:
    routes = [
        {"model": "model-b", "reasoning_effort": "xhigh"},
        {"model": "model-a", "reasoning_effort": "low"},
        {"model": "model-b", "reasoning_effort": "medium"},
    ]
    loaded = matrix.load_config(
        _config(
            tmp_path,
            campaign_kind="qualification",
            routes=routes,
            replicates=1,
        )
    )

    plan = matrix.resolve_plan(loaded, _inputs())

    assert [item.route.to_dict() for item in plan] == routes


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.__setitem__(
            "routes", [{"model": "model-a", "reasoning_effort": "high"}]
        ),
        lambda value: value["judge_policy"].__setitem__("prompt_sha256", "bad"),
        lambda value: value["judge_policy"].pop("parser_sha256"),
    ),
)
def test_screen_rejects_qualification_routes_or_unbound_judge_assets(
    tmp_path: Path, mutate: Any
) -> None:
    path = _config(tmp_path)
    _rewrite_json(path, mutate)

    with pytest.raises(matrix.MatrixError, match="config"):
        matrix.load_config(path)


@pytest.mark.parametrize("field", ("models", "efforts"))
def test_qualification_rejects_screen_axes(tmp_path: Path, field: str) -> None:
    path = _config(tmp_path, campaign_kind="qualification")
    _rewrite_json(path, lambda value: value.__setitem__(field, ["model-a"]))

    with pytest.raises(matrix.MatrixError, match="config"):
        matrix.load_config(path)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.__setitem__("models", [None]),
        lambda value: value.__setitem__("efforts", [None]),
        lambda value: value["acceptance_policy"].__setitem__(
            "hard_layers", ["privacy"]
        ),
        lambda value: value["acceptance_policy"].__setitem__(
            "required_tracks", ["answer"]
        ),
        lambda value: value["acceptance_policy"].__setitem__("replicates", 99),
        lambda value: value["selection_policy"].__setitem__("version", "latest"),
    ),
)
def test_config_rejects_unpinned_routes_or_policy_axis_mismatch(
    tmp_path: Path, mutate: Any
) -> None:
    path = _config(tmp_path)
    _rewrite_json(path, mutate)

    with pytest.raises(matrix.MatrixError, match="config"):
        matrix.load_config(path)


def test_qualification_requires_hash_bound_source_screen_provenance(
    tmp_path: Path,
) -> None:
    qualification = matrix.load_config(_config(tmp_path, campaign_kind="qualification"))
    assert qualification.campaign.source_screen_manifest_sha256 == "9" * 64
    assert qualification.campaign.source_screen_matrix_id == "matrix-screen"
    assert qualification.campaign.source_screen_acceptance_sha256 == "8" * 64
    assert (
        qualification.campaign.source_screen_qualitative_evidence_sha256
        == "7" * 64
    )

    _rewrite_json(
        _config(tmp_path, campaign_kind="qualification"),
        lambda value: value.__setitem__("screen_provenance", None),
    )
    with pytest.raises(matrix.MatrixError, match="config"):
        matrix.load_config(tmp_path / "matrix-config.json")


@pytest.mark.parametrize(
    "field",
    (
        "source_screen_matrix_id",
        "source_screen_manifest_sha256",
        "source_screen_acceptance_sha256",
        "source_screen_qualitative_evidence_sha256",
    ),
)
def test_qualification_rejects_incomplete_finalized_screen_provenance(
    tmp_path: Path, field: str
) -> None:
    path = _config(tmp_path, campaign_kind="qualification")
    _rewrite_json(path, lambda value: value["screen_provenance"].pop(field))

    with pytest.raises(matrix.MatrixError, match="config"):
        matrix.load_config(path)


def test_qualification_cannot_start_before_screen_acceptance_is_finalized(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    results_root.mkdir(mode=0o700)
    (results_root / "matrix-screen").mkdir(mode=0o700)
    loaded = matrix.load_config(
        _config(
            tmp_path,
            campaign_kind="qualification",
            routes=[{"model": "model-a", "reasoning_effort": "high"}],
            replicates=1,
        )
    )

    with pytest.raises(matrix.MatrixError, match="source screen acceptance"):
        matrix.create_matrix(
            loaded,
            _inputs(),
            preflight_attestation=_attestation(),
            results_root=results_root,
            now=NOW,
        )

    assert {item.name for item in results_root.iterdir()} == {"matrix-screen"}


@pytest.mark.parametrize("changed", ("acceptance", "qualitative", "chronology"))
def test_qualification_source_verification_rejects_post_hoc_screen_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    content = b"finalized-screen-acceptance"
    path = _config(
        tmp_path,
        campaign_kind="qualification",
        routes=[{"model": "model-a", "reasoning_effort": "high"}],
        replicates=1,
    )

    def bind(value: dict[str, Any]) -> None:
        value["screen_provenance"]["source_screen_acceptance_sha256"] = (
            hashlib.sha256(content).hexdigest()
        )

    _rewrite_json(path, bind)
    loaded = matrix.load_config(path)
    results_root = tmp_path / "results"
    results_root.mkdir(mode=0o700)
    (results_root / "matrix-screen").mkdir(mode=0o700)
    decision = SimpleNamespace(
        survivors=(run_state.MatrixRoute("model-a", "high"),),
        qualitative_evidence_sha256="7" * 64,
        finalized_at="2026-08-02T11:30:00Z",
    )
    inspected = SimpleNamespace(
        manifest_sha256="9" * 64,
            manifest=SimpleNamespace(
                matrix_id="matrix-screen",
                finished_at="2026-08-02T11:00:00Z",
                acceptance_sha256=None,
            ),
    )
    current = [decision, content, inspected]
    monkeypatch.setattr(
        acceptance,
        "load_finalized_screen",
        lambda _path: tuple(current),
    )

    matrix._verify_qualification_source(  # noqa: SLF001
        loaded, results_root, started_at=NOW
    )
    if changed == "acceptance":
        current[1] = b"post-hoc-screen-acceptance"
    elif changed == "qualitative":
        current[0] = SimpleNamespace(
            survivors=decision.survivors,
            qualitative_evidence_sha256="6" * 64,
            finalized_at=decision.finalized_at,
        )
    else:
        current[0] = SimpleNamespace(
            survivors=decision.survivors,
            qualitative_evidence_sha256="7" * 64,
            finalized_at="2026-08-02T12:00:00Z",
        )

    with pytest.raises(matrix.MatrixError, match="does not match"):
        matrix._verify_qualification_source(  # noqa: SLF001
            loaded, results_root, started_at=NOW
        )


def test_unavailable_routes_are_accounted_once_and_complete_the_campaign(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path)
    calls: list[str] = []

    def unavailable(
        item: run_state.MatrixWorkItem, _timeout: float
    ) -> matrix.ChildResult:
        calls.append(item.work_item_id)
        return matrix.ChildResult.unavailable("provider_route_unavailable")

    completed = matrix.execute_matrix(
        config,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=unavailable,
    )
    assert completed.state is run_state.MatrixState.COMPLETED
    assert [item.outcome for item in completed.completions] == [
        "unavailable",
        "unavailable",
    ]
    assert all(not item.artifact_hashes for item in completed.completions)

    resumed = matrix.execute_matrix(
        config,
        matrix_id=completed.matrix_id,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=lambda _item, _timeout: pytest.fail("must not retry"),
    )
    assert resumed == completed
    assert calls == [item.work_item_id for item in completed.work_items]


def test_default_executor_preflights_once_and_caches_exact_route_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results_root = tmp_path / "results"
    config = _config(
        tmp_path,
        models=["model-a", "model-b"],
        efforts=["high"],
        replicates=2,
    )
    preflight_calls: list[tuple[tuple[str, str], ...]] = []
    child_calls: list[str] = []

    def advertised(
        routes: tuple[tuple[str, str], ...], *, timeout_seconds: float
    ) -> tuple[bool, ...]:
        assert timeout_seconds == matrix._ROUTE_PREFLIGHT_TIMEOUT_SECONDS  # noqa: SLF001
        preflight_calls.append(routes)
        return tuple(route[0] == "model-a" for route in routes)

    def child(
        item: run_state.MatrixWorkItem,
        timeout: float,
        *,
        turn_count: int,
        root: Path,
        results_root: Path,
    ) -> matrix.ChildResult:
        assert turn_count == 1
        del timeout, root, results_root
        child_calls.append(item.work_item_id)
        return matrix.ChildResult.unavailable("downstream_route_unavailable")

    monkeypatch.setattr(matrix.codex_driver, "advertised_model_routes", advertised)
    monkeypatch.setattr(matrix, "_run_child_subprocess", child)

    completed = matrix.execute_matrix(
        config,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
    )

    assert preflight_calls == [(("model-a", "high"), ("model-b", "high"))]
    assert child_calls == [
        item.work_item_id
        for item in completed.work_items
        if item.route.model == "model-a"
    ]
    assert [item.unavailable_reason for item in completed.completions].count(
        "route_not_available"
    ) == 2
    assert completed.state is run_state.MatrixState.COMPLETED


def test_default_executor_treats_catalog_protocol_errors_as_structural(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results_root = tmp_path / "results"

    def malformed(*args: object, **kwargs: object) -> tuple[bool, ...]:
        del args, kwargs
        raise matrix.codex_driver.CodexProtocolError("private catalog failure")

    monkeypatch.setattr(matrix.codex_driver, "advertised_model_routes", malformed)
    monkeypatch.setattr(
        matrix,
        "_run_child_subprocess",
        lambda *args, **kwargs: pytest.fail(
            "catalog failure must precede child launch"
        ),
    )

    with pytest.raises(
        matrix.MatrixError, match="preflight failed structurally"
    ) as error:
        matrix.execute_matrix(
            _config(tmp_path),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
        )

    assert "private catalog failure" not in str(error.value)
    [matrix_dir] = results_root.glob("matrix-*")
    assert not (matrix_dir / "work").exists()


def test_child_route_confirmation_cannot_hide_effective_route_mismatch(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])

    def rerouted(item: run_state.MatrixWorkItem, timeout: float) -> matrix.ChildResult:
        child = base(item, timeout)
        assert child.run_dir is not None
        report_path = child.run_dir / item.scenario_id / "report.json"
        report = json.loads(report_path.read_text())
        report["generator"]["effective_model_by_turn"] = ["different-model"]
        report_path.write_text(json.dumps(report) + "\n")
        return child

    with pytest.raises(matrix.MatrixError, match="report"):
        matrix.execute_matrix(
            _config(tmp_path),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=rerouted,
        )


def test_child_manifest_run_id_must_match_result_directory(tmp_path: Path) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])

    def mismatched(
        item: run_state.MatrixWorkItem, timeout: float
    ) -> matrix.ChildResult:
        child = base(item, timeout)
        assert child.run_dir is not None
        manifest_path = child.run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["run_id"] = "different-child"
        manifest_path.write_text(json.dumps(manifest) + "\n")
        return child

    with pytest.raises(matrix.MatrixError, match="identity.*directory"):
        matrix.execute_matrix(
            _config(tmp_path, replicates=1),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=mismatched,
        )


def test_child_manifest_malformed_primitive_is_bounded_matrix_error(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])

    def malformed(item: run_state.MatrixWorkItem, timeout: float) -> matrix.ChildResult:
        child = base(item, timeout)
        assert child.run_dir is not None
        manifest_path = child.run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source"]["digest"] = 7
        manifest_path.write_text(json.dumps(manifest) + "\n")
        return child

    with pytest.raises(matrix.MatrixError) as captured:
        matrix.execute_matrix(
            _config(tmp_path, replicates=1),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=malformed,
        )

    assert str(captured.value) == "child cohort manifest is invalid"
    assert str(tmp_path) not in str(captured.value)


def test_child_snapshot_must_match_campaign_source_digest(tmp_path: Path) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])

    def mismatched(
        item: run_state.MatrixWorkItem, timeout: float
    ) -> matrix.ChildResult:
        child = base(item, timeout)
        assert child.run_dir is not None
        manifest_path = child.run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source"]["digest"] = "f" * 64
        manifest_path.write_text(json.dumps(manifest) + "\n")
        return child

    with pytest.raises(matrix.MatrixError, match="snapshot.*campaign inputs"):
        matrix.execute_matrix(
            _config(tmp_path, replicates=1),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=mismatched,
        )


def test_child_controls_identity_must_match_campaign_inputs(tmp_path: Path) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])

    def mismatched(
        item: run_state.MatrixWorkItem, timeout: float
    ) -> matrix.ChildResult:
        child = base(item, timeout)
        assert child.run_dir is not None
        manifest_path = child.run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for tool in manifest["tool_versions"]:
            if tool["name"] == "controls":
                tool["version"] = "steam-agent-eval-controls:9.9"
        manifest_path.write_text(json.dumps(manifest) + "\n")
        return child

    with pytest.raises(matrix.MatrixError, match="tool versions"):
        matrix.execute_matrix(
            _config(tmp_path, replicates=1),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=mismatched,
        )


def test_child_route_history_must_cover_every_reported_conversation_turn(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    inputs = _inputs()
    inputs = replace(
        inputs,
        scenarios=(
            replace(inputs.scenarios[0], turn_count=2),
            inputs.scenarios[1],
        ),
    )
    base = _child_executor(results_root, inputs, [])

    def truncated(item: run_state.MatrixWorkItem, timeout: float) -> matrix.ChildResult:
        return base(item, timeout)

    with pytest.raises(matrix.MatrixError, match="report"):
        matrix.execute_matrix(
            _config(tmp_path, replicates=1),
            results_root=results_root,
            input_collector=lambda _config: inputs,
            child_executor=truncated,
        )


def test_child_scenario_artifact_directory_cannot_be_a_symlink(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])

    def linked(item: run_state.MatrixWorkItem, timeout: float) -> matrix.ChildResult:
        child = base(item, timeout)
        assert child.run_dir is not None
        scenario_dir = child.run_dir / item.scenario_id
        outside = tmp_path / "outside-scenario"
        scenario_dir.rename(outside)
        scenario_dir.symlink_to(outside, target_is_directory=True)
        return child

    with pytest.raises(matrix.MatrixError, match="invalid|private"):
        matrix.execute_matrix(
            _config(tmp_path, replicates=1),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=linked,
        )


@pytest.mark.parametrize("field", ("artifacts", "passed", "layers"))
def test_child_summary_cannot_fabricate_the_report_chain(
    tmp_path: Path, field: str
) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])

    def fabricated(
        item: run_state.MatrixWorkItem, timeout: float
    ) -> matrix.ChildResult:
        child = base(item, timeout)
        assert child.run_dir is not None
        summary_path = child.run_dir / "summary.json"
        summary = json.loads(summary_path.read_text())
        if field == "artifacts":
            summary[0][field]["report.json"] = "0" * 64
        elif field == "passed":
            summary[0][field] = True
        else:
            summary[0][field]["privacy"] = False
        summary_path.write_text(json.dumps(summary) + "\n")
        return child

    with pytest.raises(matrix.MatrixError, match="summary"):
        matrix.execute_matrix(
            _config(tmp_path),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=fabricated,
        )


def test_child_summary_entry_must_be_an_object(tmp_path: Path) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])

    def non_object(
        item: run_state.MatrixWorkItem, timeout: float
    ) -> matrix.ChildResult:
        child = base(item, timeout)
        assert child.run_dir is not None
        (child.run_dir / "summary.json").write_text("[null]\n")
        return child

    with pytest.raises(matrix.MatrixError, match="report"):
        matrix.execute_matrix(
            _config(tmp_path),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=non_object,
        )


def test_observed_child_run_ids_must_be_fresh(tmp_path: Path) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])
    retained: matrix.ChildResult | None = None

    def repeated(item: run_state.MatrixWorkItem, timeout: float) -> matrix.ChildResult:
        nonlocal retained
        if retained is None:
            retained = base(item, timeout)
        return retained

    with pytest.raises(matrix.MatrixError, match="not fresh"):
        matrix.execute_matrix(
            _config(tmp_path),
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=repeated,
        )

    [matrix_dir] = results_root.glob("matrix-*")
    assert len(matrix.load_manifest(matrix_dir).completions) == 1


@pytest.mark.parametrize("artifact_name", ("report.json", "transcript.jsonl"))
def test_distinct_child_runs_may_publish_identical_artifact_content(
    tmp_path: Path, artifact_name: str
) -> None:
    results_root = tmp_path / "results"
    base = _child_executor(results_root, _inputs(), [])
    retained_content: bytes | None = None

    def repeated_evidence(
        item: run_state.MatrixWorkItem, timeout: float
    ) -> matrix.ChildResult:
        nonlocal retained_content
        child = base(item, timeout)
        assert child.run_dir is not None
        artifact_path = child.run_dir / item.scenario_id / artifact_name
        if retained_content is None:
            retained_content = artifact_path.read_bytes()
            return child
        artifact_path.write_bytes(retained_content)
        summary_path = child.run_dir / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary[0]["artifacts"][artifact_name] = hashlib.sha256(
            retained_content
        ).hexdigest()
        summary_path.write_text(json.dumps(summary) + "\n")
        return child

    completed = matrix.execute_matrix(
        _config(tmp_path),
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=repeated_evidence,
    )

    assert completed.state is run_state.MatrixState.COMPLETED
    assert len({item.child_run_id for item in completed.completions}) == 2
    assert (
        len(
            {
                dict(item.artifact_hashes)[artifact_name]
                for item in completed.completions
            }
        )
        == 1
    )


@pytest.mark.parametrize("tamper", ("delete", "replace"))
def test_resume_requires_hash_bound_controls_artifact(
    tmp_path: Path, tamper: str
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    completed = matrix.execute_matrix(
        config,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=_child_executor(results_root, _inputs(), []),
    )
    [completion] = completed.completions
    assert "controls.json" in dict(completion.artifact_hashes)
    assert completion.child_run_id is not None
    controls_path = results_root / completion.child_run_id / "controls.json"
    if tamper == "delete":
        controls_path.unlink()
    else:
        controls_path.write_text("{}\n")

    with pytest.raises(matrix.MatrixError, match="controls|artifact"):
        matrix.execute_matrix(
            config,
            matrix_id=completed.matrix_id,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )


def test_result_orphan_survives_checkpoint_failure_and_resume_uses_new_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    original_persist = run_state.MatrixManifest.persist

    def fail_checkpoint(manifest: run_state.MatrixManifest, path: Path) -> None:
        if manifest.revision == 1:
            raise OSError("simulated checkpoint failure")
        original_persist(manifest, path)

    monkeypatch.setattr(run_state.MatrixManifest, "persist", fail_checkpoint)
    with pytest.raises(matrix.MatrixError, match="failed structurally"):
        matrix.execute_matrix(
            config,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=_child_executor(results_root, _inputs(), []),
        )

    [matrix_dir] = results_root.glob("matrix-*")
    work_item = matrix.load_manifest(matrix_dir).work_items[0]
    first_attempt = matrix_dir / "work" / work_item.work_item_id / "attempt-000001"
    assert {path.name for path in first_attempt.iterdir()} == {
        "started.json",
        "result.json",
    }

    monkeypatch.setattr(run_state.MatrixManifest, "persist", original_persist)
    completed = matrix.execute_matrix(
        config,
        matrix_id=matrix_dir.name,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=_child_executor(results_root, _inputs(), []),
    )

    assert completed.state is run_state.MatrixState.COMPLETED
    assert completed.completions[0].attempt_id == "attempt-000002"


@pytest.mark.parametrize("interruption", ("publish", "rename"))
def test_interrupted_attempt_initialization_resumes_without_consuming_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    original_publish = run_state.atomic_publish_private_json
    original_rename = matrix.os.rename

    if interruption == "publish":

        def interrupt_publish(path: Path, value: Any) -> None:
            if path.name == "started.json":
                raise KeyboardInterrupt
            original_publish(path, value)

        monkeypatch.setattr(run_state, "atomic_publish_private_json", interrupt_publish)
    else:

        def interrupt_rename(source: Path, destination: Path) -> None:
            if Path(source).name.startswith(".attempt-init-"):
                raise KeyboardInterrupt
            original_rename(source, destination)

        monkeypatch.setattr(matrix.os, "rename", interrupt_rename)

    with pytest.raises(KeyboardInterrupt):
        matrix.execute_matrix(
            config,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=_child_executor(results_root, _inputs(), []),
        )

    [matrix_dir] = results_root.glob("matrix-*")
    [item_root] = (matrix_dir / "work").iterdir()
    [staging] = item_root.iterdir()
    assert staging.name.startswith(".attempt-init-attempt-000001-")

    monkeypatch.setattr(run_state, "atomic_publish_private_json", original_publish)
    monkeypatch.setattr(matrix.os, "rename", original_rename)
    completed = matrix.execute_matrix(
        config,
        matrix_id=matrix_dir.name,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=_child_executor(results_root, _inputs(), []),
    )

    assert completed.completions[0].attempt_id == "attempt-000001"
    assert (item_root / "attempt-000001" / "started.json").is_file()


def test_staged_attempt_initialization_never_accepts_forged_result(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    matrix_dir, manifest = matrix.create_matrix(
        matrix.load_config(config),
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=results_root,
        now=NOW,
    )
    work_root = matrix_dir / "work"
    work_root.mkdir(mode=0o700)
    item_root = work_root / manifest.work_items[0].work_item_id
    item_root.mkdir(mode=0o700)
    staging = item_root / ".attempt-init-attempt-000001-forged123"
    staging.mkdir(mode=0o700)
    run_state.atomic_publish_private_json(staging / "result.json", {})

    with pytest.raises(matrix.MatrixError, match="initialization"):
        matrix.execute_matrix(
            config,
            matrix_id=manifest.matrix_id,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )


@pytest.mark.parametrize("turn_count", (1, 2))
def test_outer_child_timeout_scales_with_manifest_bound_turn_count_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, turn_count: int
) -> None:
    results_root = tmp_path / "evals" / "results"
    config = _config(tmp_path, replicates=1, timeout_seconds=2)
    inputs = _inputs()
    inputs = replace(
        inputs,
        scenarios=(
            replace(inputs.scenarios[0], turn_count=turn_count),
            inputs.scenarios[1],
        ),
    )
    observed_timeouts: list[float] = []

    class TimedOutProcess:
        pid = 1234
        returncode: int | None = None
        stdout = None
        stderr = None

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            observed_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(["child"], timeout)

    process = TimedOutProcess()

    class InertTracker:
        def __init__(self, _pid: int):
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(
        matrix.codex_driver,
        "advertised_model_routes",
        lambda _routes, *, timeout_seconds: (True,),
    )
    monkeypatch.setattr(matrix.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(matrix, "_ChildProcessTracker", InertTracker)
    monkeypatch.setattr(matrix, "_wait_for_child_bootstrap", lambda *_args: None)
    monkeypatch.setattr(matrix, "_continue_child_process", lambda *_args: None)
    monkeypatch.setattr(
        matrix,
        "_terminate_child_process_tree",
        lambda observed, **_kwargs: setattr(observed, "returncode", -15),
    )

    with pytest.raises(matrix.MatrixError, match="timed out"):
        matrix.execute_matrix(
            config,
            root=tmp_path,
            results_root=results_root,
            input_collector=lambda _config: inputs,
        )

    assert observed_timeouts == [
        2 * turn_count + matrix._CHILD_PROCESS_GRACE_SECONDS  # noqa: SLF001
    ]
    [matrix_dir] = results_root.glob("matrix-*")
    work_item = matrix.load_manifest(matrix_dir).work_items[0]
    first_attempt = matrix_dir / "work" / work_item.work_item_id / "attempt-000001"
    assert {path.name for path in first_attempt.iterdir()} == {
        "started.json",
        "failure.json",
    }

    completed = matrix.execute_matrix(
        config,
        matrix_id=matrix_dir.name,
        root=tmp_path,
        results_root=results_root,
        input_collector=lambda _config: inputs,
        child_executor=lambda _item, _timeout: matrix.ChildResult.unavailable(
            "provider_route_unavailable"
        ),
    )
    assert completed.completions[0].attempt_id == "attempt-000002"


@pytest.mark.parametrize(
    ("timeout_seconds", "turn_count"),
    ((1, 0), (1, True), (1, 65), (float("inf"), 1), (10**400, 2)),
)
def test_child_timeout_budget_rejects_unbounded_or_invalid_values(
    timeout_seconds: float, turn_count: int
) -> None:
    with pytest.raises(matrix.MatrixError, match="timeout budget"):
        matrix._child_timeout_budget(timeout_seconds, turn_count)  # noqa: SLF001


@pytest.mark.parametrize("error_type", (KeyboardInterrupt, SystemExit, LifecycleAbort))
def test_child_communicate_lifecycle_abort_cleans_tree_before_reraising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    class InterruptedProcess:
        pid = 1234
        returncode: int | None = None
        stdout = None
        stderr = None

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            del timeout
            raise error_type()

    process = InterruptedProcess()
    cleaned: list[int] = []

    class InertTracker:
        def __init__(self, _pid: int):
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(matrix.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(matrix, "_ChildProcessTracker", InertTracker)
    monkeypatch.setattr(matrix, "_wait_for_child_bootstrap", lambda *_args: None)
    monkeypatch.setattr(matrix, "_continue_child_process", lambda *_args: None)
    monkeypatch.setattr(
        matrix,
        "_terminate_child_process_tree",
        lambda observed, **_kwargs: cleaned.append(observed.pid),
    )
    work_item = matrix.resolve_plan(
        matrix.load_config(_config(tmp_path, replicates=1)), _inputs()
    )[0]

    with pytest.raises(error_type):
        matrix._run_child_subprocess(  # noqa: SLF001
            work_item,
            1,
            turn_count=1,
            root=tmp_path,
            results_root=tmp_path / "evals" / "results",
        )

    assert cleaned == [process.pid]


def test_timeout_cleanup_kills_detached_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_program = (
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    )
    leader_program = (
        "import subprocess,sys,time;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_program!r}],"
        "start_new_session=True);"
        "print(child.pid,flush=True);"
        "time.sleep(60)"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    child_pid = int(leader.stdout.readline())
    monkeypatch.setattr(matrix, "_CHILD_TERMINATION_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(matrix, "_CHILD_FORCE_KILL_SECONDS", 0.5)

    try:
        matrix._terminate_child_process_tree(leader)  # noqa: SLF001
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            table = matrix._process_table()  # noqa: SLF001
            if child_pid not in matrix._live_process_ids({child_pid}, table):  # noqa: SLF001
                break
            time.sleep(0.05)
        assert child_pid not in matrix._live_process_ids(  # noqa: SLF001
            {child_pid},
            matrix._process_table(),  # noqa: SLF001
        )
        assert leader.returncode is not None
    finally:
        for pid in (child_pid, leader.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("exit_code", (0, 1, 3))
def test_normal_child_completion_cleans_detached_descendant_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    results_root = tmp_path / "evals" / "results"
    results_root.mkdir(parents=True, mode=0o700)
    work_item = matrix.resolve_plan(
        matrix.load_config(_config(tmp_path, replicates=1)), _inputs()
    )[0]
    child_pid_path = tmp_path / "detached-child.pid"
    child_program = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(60)"
    )
    leader_program = (
        "import subprocess,sys,time;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_program!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,start_new_session=True);"
        f"open({str(child_pid_path)!r},'w').write(str(child.pid));"
        "print('reports: evals/results/run-normal-cleanup',file=sys.stderr,flush=True);"
        f"time.sleep(0.25);sys.exit({exit_code})"
    )
    real_popen = subprocess.Popen
    leader = real_popen(
        [sys.executable, "-c", leader_program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    def controlled_popen(
        argv: list[str], **kwargs: Any
    ) -> subprocess.Popen[str]:
        if argv[:3] == [sys.executable, "-c", matrix._CHILD_BOOTSTRAP]:  # noqa: SLF001
            return leader
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(matrix.subprocess, "Popen", controlled_popen)
    monkeypatch.setattr(matrix, "_wait_for_child_bootstrap", lambda *_args: None)
    monkeypatch.setattr(matrix, "_continue_child_process", lambda *_args: None)
    monkeypatch.setattr(matrix, "_CHILD_TERMINATION_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(matrix, "_CHILD_FORCE_KILL_SECONDS", 0.5)

    child_pid = -1
    try:
        result = matrix._run_child_subprocess(  # noqa: SLF001
            work_item,
            1,
            turn_count=1,
            root=tmp_path,
            results_root=results_root,
        )
        child_pid = int(child_pid_path.read_text())

        assert result.exit_code == exit_code
        assert result.run_dir == results_root / "run-normal-cleanup"
        assert child_pid not in matrix._live_process_ids(  # noqa: SLF001
            {child_pid}, matrix._process_table()  # noqa: SLF001
        )
    finally:
        for pid in (child_pid, leader.pid):
            if pid <= 0:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_child_cleanup_does_not_signal_a_same_second_reused_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CapturedTracker:
        def capture(self) -> None:
            pass

        def live_identities(self) -> set[tuple[int, str]]:
            return {(4242, "darwin:1785714000:100")}

    monkeypatch.setattr(
        matrix,
        "_process_table",
        lambda: {
            4242: matrix._ProcessRecord(  # noqa: SLF001
                parent_pid=1,
                process_group=4242,
                state="S",
                kernel_identity="darwin:1785714000:900",
            )
        },
    )
    signaled: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        matrix.os, "kill", lambda pid, sig: signaled.append((pid, sig))
    )

    matrix._signal_child_tree(  # noqa: SLF001
        CapturedTracker(),  # type: ignore[arg-type]
        signal.SIGKILL,
    )

    assert signaled == []


def test_tracker_snapshot_error_reaps_bootstrap_and_closes_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results_root = tmp_path / "evals" / "results"
    results_root.mkdir(parents=True)
    work_item = matrix.resolve_plan(
        matrix.load_config(_config(tmp_path, replicates=1)), _inputs()
    )[0]
    real_popen = subprocess.Popen
    real_killpg = os.killpg
    spawned: list[subprocess.Popen[str]] = []
    signaled_groups: list[tuple[int, signal.Signals]] = []

    def recording_popen(
        argv: list[str], **kwargs: Any
    ) -> subprocess.Popen[str]:
        process = real_popen(argv, **kwargs)
        spawned.append(process)
        return process

    def recording_killpg(pid: int, sig: signal.Signals) -> None:
        signaled_groups.append((pid, sig))
        real_killpg(pid, sig)

    def unavailable_process_table() -> dict[int, matrix._ProcessRecord]:  # noqa: SLF001
        raise matrix.MatrixError("matrix child process cleanup failed")

    monkeypatch.setattr(matrix.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(matrix, "_process_table", unavailable_process_table)
    monkeypatch.setattr(matrix.os, "killpg", recording_killpg)

    with pytest.raises(matrix.MatrixError, match="process cleanup"):
        matrix._run_child_subprocess(  # noqa: SLF001
            work_item,
            1,
            turn_count=1,
            root=tmp_path,
            results_root=results_root,
        )

    [process] = spawned
    assert signaled_groups == [(process.pid, signal.SIGKILL)]
    assert process.returncode is not None
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


def test_child_exiting_before_tracker_snapshot_is_reaped_and_pipes_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results_root = tmp_path / "evals" / "results"
    results_root.mkdir(parents=True)
    work_item = matrix.resolve_plan(
        matrix.load_config(_config(tmp_path, replicates=1)), _inputs()
    )[0]
    leader = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 2
    while leader.pid in matrix._live_process_ids(  # noqa: SLF001
        {leader.pid}, matrix._process_table()  # noqa: SLF001
    ):
        assert time.monotonic() < deadline
        time.sleep(0.01)

    class MissingSnapshotTracker:
        def __init__(self, _pid: int):
            raise matrix.MatrixError("matrix child process cleanup failed")

    real_killpg = os.killpg
    signaled_groups: list[tuple[int, signal.Signals]] = []

    def recording_killpg(pid: int, sig: signal.Signals) -> None:
        signaled_groups.append((pid, sig))
        try:
            real_killpg(pid, sig)
        except ProcessLookupError:
            pass

    monkeypatch.setattr(matrix.subprocess, "Popen", lambda *_args, **_kwargs: leader)
    monkeypatch.setattr(matrix, "_ChildProcessTracker", MissingSnapshotTracker)
    monkeypatch.setattr(matrix.os, "killpg", recording_killpg)

    with pytest.raises(matrix.MatrixError, match="process cleanup"):
        matrix._run_child_subprocess(  # noqa: SLF001
            work_item,
            1,
            turn_count=1,
            root=tmp_path,
            results_root=results_root,
        )

    assert signaled_groups == [(leader.pid, signal.SIGKILL)]
    assert leader.returncode is not None
    assert leader.stdout is not None and leader.stdout.closed
    assert leader.stderr is not None and leader.stderr.closed


@pytest.mark.parametrize("tamper", ("wrong-schema", "extra-key"))
def test_resume_strictly_validates_committed_attempt_result_shape(
    tmp_path: Path, tamper: str
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    completed = matrix.execute_matrix(
        config,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=lambda _item, _timeout: matrix.ChildResult.unavailable(
            "provider_route_unavailable"
        ),
    )
    [completion] = completed.completions
    result_path = (
        results_root
        / completed.matrix_id
        / "work"
        / completion.work_item_id
        / completion.attempt_id
        / "result.json"
    )
    result = json.loads(result_path.read_text())
    if tamper == "wrong-schema":
        result["schema"] = "steam-agent-eval-matrix-attempt-result/9.9"
    else:
        result["extra"] = True
    result_path.write_text(json.dumps(result) + "\n")

    with pytest.raises(matrix.MatrixError, match="attempt history"):
        matrix.execute_matrix(
            config,
            matrix_id=completed.matrix_id,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )


@pytest.mark.parametrize("tamper", ("valid-change", "duplicate-member"))
def test_resume_rejects_tampered_committed_attempt_start(
    tmp_path: Path, tamper: str
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    completed = matrix.execute_matrix(
        config,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=_child_executor(results_root, _inputs(), []),
    )
    completion = completed.completions[0]
    started_path = (
        results_root
        / completed.matrix_id
        / "work"
        / completion.work_item_id
        / completion.attempt_id
        / "started.json"
    )
    if tamper == "valid-change":
        started = json.loads(started_path.read_text())
        started["started_at"] = "2026-08-02T11:59:00Z"
        started_path.write_text(json.dumps(started) + "\n")
    else:
        started_path.write_text(
            '{"schema":"steam-agent-eval-matrix-attempt/0.1",'
            '"schema":"steam-agent-eval-matrix-attempt/0.1"}\n'
        )

    with pytest.raises(matrix.MatrixError, match="attempt start"):
        matrix.execute_matrix(
            config,
            matrix_id=completed.matrix_id,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )


def test_matrix_layout_allows_private_qualitative_artifact_directories(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    matrix_dir, manifest = matrix.create_matrix(
        matrix.load_config(config),
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=results_root,
        now=NOW,
    )
    for directory, filename in (
        ("judgments", "judgment-1.json"),
        ("adjudications", "adjudication-1.json"),
    ):
        root = matrix_dir / directory
        root.mkdir(mode=0o700)
        run_state.atomic_publish_private_json(root / filename, {})

    completed = matrix.execute_matrix(
        config,
        matrix_id=manifest.matrix_id,
        results_root=results_root,
        input_collector=lambda _config: _inputs(),
        child_executor=lambda _item, _timeout: matrix.ChildResult.unavailable(
            "provider_route_unavailable"
        ),
    )

    assert completed.state is run_state.MatrixState.COMPLETED


@pytest.mark.parametrize("directory", ("judgments", "adjudications"))
@pytest.mark.parametrize("unsafe", ("symlink", "unexpected-name", "directory"))
def test_matrix_layout_rejects_unsafe_qualitative_artifacts(
    tmp_path: Path, directory: str, unsafe: str
) -> None:
    results_root = tmp_path / "results"
    config = _config(tmp_path, replicates=1)
    matrix_dir, manifest = matrix.create_matrix(
        matrix.load_config(config),
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=results_root,
        now=NOW,
    )
    artifact_root = matrix_dir / directory
    artifact_root.mkdir(mode=0o700)
    artifact_path = artifact_root / (
        "unsafe name.json" if unsafe == "unexpected-name" else "artifact-1.json"
    )
    if unsafe == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_text("{}\n")
        artifact_path.symlink_to(outside)
    elif unsafe == "directory":
        artifact_path.mkdir(mode=0o700)
    else:
        run_state.atomic_publish_private_json(artifact_path, {})

    with pytest.raises(matrix.MatrixError, match="unexpected|invalid"):
        matrix.execute_matrix(
            config,
            matrix_id=manifest.matrix_id,
            results_root=results_root,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )


def test_results_root_and_matrix_containment_reject_symlinks_and_extra_nodes(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    results_link = tmp_path / "results-link"
    results_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(matrix.MatrixError, match="directory|root"):
        matrix.create_matrix(
            matrix.load_config(_config(tmp_path)),
            _inputs(),
            preflight_attestation=_attestation(),
            results_root=results_link,
        )

    results = tmp_path / "results"
    loaded = matrix.load_config(_config(tmp_path))
    matrix_dir, manifest = matrix.create_matrix(
        loaded,
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=results,
        now=NOW,
    )
    (matrix_dir / "unexpected").write_text("not part of the campaign")
    with pytest.raises(matrix.MatrixError, match="unexpected"):
        matrix.execute_matrix(
            tmp_path / "matrix-config.json",
            matrix_id=manifest.matrix_id,
            results_root=results,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )


@pytest.mark.parametrize("kind", ("work-symlink", "attempt-symlink", "extra-work"))
def test_work_and_attempt_layout_rejects_preexisting_unsafe_nodes(
    tmp_path: Path, kind: str
) -> None:
    results = tmp_path / "results"
    config = _config(tmp_path)
    matrix_dir, manifest = matrix.create_matrix(
        matrix.load_config(config),
        _inputs(),
        preflight_attestation=_attestation(),
        results_root=results,
        now=NOW,
    )
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if kind == "work-symlink":
        (matrix_dir / "work").symlink_to(outside, target_is_directory=True)
    else:
        work = matrix_dir / "work"
        work.mkdir(mode=0o700)
        if kind == "extra-work":
            (work / "unexpected").mkdir(mode=0o700)
        else:
            item_root = work / manifest.work_items[0].work_item_id
            item_root.mkdir(mode=0o700)
            (item_root / "attempt-000001").symlink_to(outside, target_is_directory=True)

    with pytest.raises(matrix.MatrixError, match="invalid|unexpected|not private"):
        matrix.execute_matrix(
            config,
            matrix_id=manifest.matrix_id,
            results_root=results,
            input_collector=lambda _config: _inputs(),
            child_executor=lambda _item, _timeout: pytest.fail("must not execute"),
        )

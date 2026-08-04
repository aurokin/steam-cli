from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import run_state  # noqa: E402
from evals.runner.run_state import (  # noqa: E402
    FrozenScenario,
    ManifestStateError,
    RequestedRoute,
    RunManifest,
    RunState,
    SnapshotIntegrityError,
    SourceSnapshot,
    TerminalReason,
    atomic_publish_private_json,
    atomic_publish_private_text,
    inventory_digest,
)


NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def _matrix_campaign() -> run_state.MatrixCampaign:
    return run_state.MatrixCampaign(
        campaign_kind="screen",
        selection_version="fixed-ordered-scenarios/0.1",
        selection_mode="fixed_ordered",
        acceptance_version="fixed-corpus/0.1",
        hard_layers=("agent_turns", "tool_policy", "oracle", "claims", "privacy"),
        required_tracks=("discovery",),
        replicates=2,
        qualitative_rule="fact_hard_safety_resolved_pass",
        judge_version="blinded-qualitative/0.1",
        judgment_schema="steam-agent-eval-judgment/0.1",
        adjudication_schema="steam-agent-eval-adjudication/0.1",
        prompt_version="matrix-judge/0.1",
        parser_version="matrix-parser/0.1",
        prompt_sha256="d" * 64,
        parser_sha256="e" * 64,
        judges=run_state.CALIBRATED_JUDGE_CONFIGURATIONS,
        adjudication_method=run_state.CALIBRATED_ADJUDICATION_METHOD,
        adjudicator=run_state.CALIBRATED_ADJUDICATOR,
        source_screen_manifest_sha256=None,
    )


def _scenario(
    scenario_id: str = "m7-z99", *, source_name: str | None = None
) -> FrozenScenario:
    source_name = source_name or f"m7/{scenario_id}.json"
    document = {"id": scenario_id, "conversation": {"user": ["question"]}}
    source = json.dumps(document, separators=(",", ":")).encode()
    return FrozenScenario.create(
        source_name=source_name,
        original_bytes=source,
        document=document,
    )


def _snapshot(tmp_path: Path) -> SourceSnapshot:
    source = tmp_path / "source"
    package = source / "steam_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VERSION = 'snapshot'\n")
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "grade.py").write_text("VERSION = 'snapshot'\n")
    return SourceSnapshot.create(
        tmp_path / "snapshot",
        source_root=source,
        harness_root=harness,
        scenarios=[_scenario()],
        schemas={"scenario-0.2.json": b'{"type":"object"}\n'},
    )


def _manifest(scenario: FrozenScenario | None = None) -> RunManifest:
    return RunManifest.create(
        run_id="20260801T120000Z",
        commit="a" * 40,
        source_digest="b" * 64,
        cleanliness="clean",
        track="legacy",
        control_set_version="steam-agent-eval-controls/0.1",
        scenarios=[scenario or _scenario()],
        requested_routes=[RequestedRoute("gpt-5.6-sol", "xhigh")],
        tool_versions={"codex": "0.146.0", "runner": "0.1"},
        started_at=NOW,
    )


def test_frozen_scenario_retains_exact_bytes_hash_and_immutable_document() -> None:
    scenario = _scenario()

    assert (
        scenario.sha256
        == __import__("hashlib").sha256(scenario.original_bytes).hexdigest()
    )
    with pytest.raises(TypeError):
        scenario.document["id"] = "m7-z98"
    mutable = scenario.mutable_document()
    mutable["id"] = "m7-z98"
    assert scenario.document["id"] == "m7-z99"


@pytest.mark.parametrize(
    "source_name",
    (
        "/private/scenario.json",
        "../scenario.json",
        "m7/../../scenario.json",
        "m7\\scenario.json",
        "m7/private name.json",
        "m7/scenario.json\n",
    ),
)
def test_frozen_scenario_rejects_private_or_unsafe_source_names(
    source_name: str,
) -> None:
    with pytest.raises(ValueError) as captured:
        _scenario(source_name=source_name)
    assert source_name not in str(captured.value)


def test_source_snapshot_copies_declared_inputs_seals_and_verifies(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    try:
        assert snapshot.verify() == snapshot.digest
        assert inventory_digest(snapshot.root) == snapshot.digest
        assert (
            snapshot.root / "evals/scenarios/m7/m7-z99.json"
        ).read_bytes() == _scenario().original_bytes
        assert stat.S_IMODE(snapshot.root.stat().st_mode) == 0o555
        for path in snapshot.root.rglob("*"):
            expected = 0o555 if path.is_dir() else 0o444
            assert stat.S_IMODE(path.stat().st_mode) == expected
        rendered_inventory = json.dumps(
            [entry.relative_name for entry in snapshot.inventory]
        )
        assert str(tmp_path) not in rendered_inventory
    finally:
        snapshot.cleanup()
    assert not snapshot.root.exists()


def test_source_snapshot_binds_and_seals_optional_project_skill(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_text("VALUE = 'source'\n")
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "grade.py").write_text("VALUE = 'harness'\n")
    skill = tmp_path / "skill"
    (skill / "agents").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: steam-agent\n---\n")
    (skill / "agents" / "openai.yaml").write_text(
        'interface:\n  display_name: "Steam Agent"\n'
    )

    without_skill = SourceSnapshot.create(
        tmp_path / "without-skill",
        source_root=source,
        harness_root=harness,
        scenarios=[_scenario()],
        schemas={"scenario-0.2.json": b"{}"},
    )
    with_skill = SourceSnapshot.create(
        tmp_path / "with-skill",
        source_root=source,
        harness_root=harness,
        skill_root=skill,
        scenarios=[_scenario()],
        schemas={"scenario-0.2.json": b"{}"},
    )
    try:
        copied = with_skill.root / "skill" / "steam-agent"
        assert (copied / "SKILL.md").read_bytes() == (skill / "SKILL.md").read_bytes()
        assert (copied / "agents" / "openai.yaml").read_bytes() == (
            skill / "agents" / "openai.yaml"
        ).read_bytes()
        assert with_skill.digest != without_skill.digest
        assert with_skill.verify() == with_skill.digest
        assert stat.S_IMODE(copied.stat().st_mode) == 0o555
        assert stat.S_IMODE((copied / "SKILL.md").stat().st_mode) == 0o444
    finally:
        with_skill.cleanup()
        without_skill.cleanup()


def test_source_snapshot_rejects_symlink_without_reading_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    harness = tmp_path / "harness"
    harness.mkdir()
    outside = tmp_path / "private-must-not-appear"
    outside.write_text("secret")
    (source / "alias.py").symlink_to(outside)

    with pytest.raises(SnapshotIntegrityError) as captured:
        SourceSnapshot.create(
            tmp_path / "snapshot",
            source_root=source,
            harness_root=harness,
            scenarios=[_scenario()],
            schemas={"scenario-0.2.json": b"{}"},
        )

    assert outside.name not in str(captured.value)
    assert outside.read_text() == "secret"
    assert not (tmp_path / "snapshot").exists()


def test_source_snapshot_verification_detects_content_and_mode_tampering(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    source_file = snapshot.root / "src/steam_agent/__init__.py"
    try:
        source_file.chmod(0o600)
        source_file.write_text("VERSION = 'contaminated'\n")
        with pytest.raises(SnapshotIntegrityError):
            snapshot.verify()
    finally:
        snapshot.cleanup()


def test_inventory_traversal_errors_do_not_expose_host_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = tmp_path / "private-source-name"
    monkeypatch.setattr(
        run_state,
        "_scan_inventory_unchecked",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError(str(private_path))),
    )

    with pytest.raises(SnapshotIntegrityError) as captured:
        inventory_digest(tmp_path)

    assert str(private_path) not in str(captured.value)


@pytest.mark.parametrize("tree_name", ("source", "harness"))
def test_source_snapshot_rejects_cross_file_copy_race(
    tree_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    harness = tmp_path / "harness"
    source.mkdir()
    harness.mkdir()
    target_root = source if tree_name == "source" else harness
    (target_root / "a.py").write_text("A = 'clean'\n")
    (target_root / "b.py").write_text("B = 'clean'\n")
    other_root = harness if tree_name == "source" else source
    (other_root / "stable.py").write_text("STABLE = True\n")
    original = run_state._read_regular_file_at  # noqa: SLF001
    raced = False
    first_file_reads = 0

    def race(directory_fd: int, name: str):
        nonlocal first_file_reads, raced
        if name == "a.py":
            first_file_reads += 1
        if name == "a.py" and first_file_reads == 2:
            raced = True
            (target_root / "b.py").write_text("B = 'transient'\n")
        result = original(directory_fd, name)
        if name == "b.py" and raced:
            (target_root / "b.py").write_text("B = 'clean'\n")
        return result

    monkeypatch.setattr(run_state, "_read_regular_file_at", race)
    with pytest.raises(SnapshotIntegrityError) as captured:
        SourceSnapshot.create(
            tmp_path / "snapshot",
            source_root=source,
            harness_root=harness,
            scenarios=[_scenario()],
            schemas={"scenario-0.2.json": b"{}"},
        )

    assert str(captured.value) == "source changed while being snapshotted"
    assert not (tmp_path / "snapshot").exists()


def test_atomic_publication_is_private_and_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    atomic_publish_private_json(path, {"state": "first"})

    assert json.loads(path.read_text()) == {"state": "first"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        atomic_publish_private_text(path, "second")
    assert json.loads(path.read_text()) == {"state": "first"}
    assert not any(
        item.name.startswith(".artifact.json.tmp-") for item in tmp_path.iterdir()
    )


def test_atomic_publication_rejects_existing_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("keep")
    target = tmp_path / "artifact"
    target.symlink_to(outside)

    with pytest.raises(FileExistsError):
        atomic_publish_private_text(target, "replace")

    assert target.is_symlink()
    assert outside.read_text() == "keep"


def test_manifest_transitions_are_monotonic_and_terminal() -> None:
    manifest = _manifest()
    controls = manifest.transition(RunState.CONTROLS, at=NOW + timedelta(seconds=1))
    running = controls.transition(
        RunState.RUNNING,
        at=NOW + timedelta(seconds=2),
        controls_passed=True,
    )
    completed = running.transition(
        RunState.COMPLETED,
        at=NOW + timedelta(seconds=3),
        completed_scenario_ids=["m7-z99"],
    )

    assert [
        manifest.revision,
        controls.revision,
        running.revision,
        completed.revision,
    ] == [
        0,
        1,
        2,
        3,
    ]
    assert completed.terminal is True
    assert completed.finished_at == completed.updated_at
    with pytest.raises(ManifestStateError):
        completed.transition(RunState.RUNNING, at=NOW + timedelta(seconds=4))
    with pytest.raises(ManifestStateError):
        manifest.transition(RunState.COMPLETED, at=NOW + timedelta(seconds=1))
    with pytest.raises(ManifestStateError):
        controls.transition(
            RunState.RUNNING,
            at=NOW - timedelta(seconds=1),
            controls_passed=True,
        )


def test_manifest_records_track_controls_and_ordered_running_checkpoints() -> None:
    scenarios = [_scenario("m7-z97"), _scenario("m7-z98"), _scenario("m7-z99")]
    manifest = RunManifest.create(
        run_id="20260801T120000Z",
        commit="a" * 40,
        source_digest="b" * 64,
        cleanliness="clean",
        track="discovery",
        control_set_version="steam-agent-eval-controls/0.1",
        scenarios=scenarios,
        requested_routes=[RequestedRoute("gpt-5.6-sol", "xhigh")],
        tool_versions={"codex": "0.146.0"},
        started_at=NOW,
    )
    controls = manifest.transition(RunState.CONTROLS, at=NOW)
    running = controls.transition(RunState.RUNNING, at=NOW, controls_passed=True)
    first = running.transition(
        RunState.RUNNING,
        at=NOW + timedelta(seconds=1),
        completed_scenario_ids=["m7-z97"],
    )
    second = first.transition(
        RunState.RUNNING,
        at=NOW + timedelta(seconds=2),
        completed_scenario_ids=["m7-z97", "m7-z98"],
    )
    completed = second.transition(
        RunState.COMPLETED,
        at=NOW + timedelta(seconds=3),
        completed_scenario_ids=["m7-z97", "m7-z98", "m7-z99"],
    )

    document = completed.to_dict()
    assert document["track"] == "discovery"
    assert document["control_set_version"] == "steam-agent-eval-controls/0.1"
    assert document["controls_passed"] is True
    assert document["completed_scenario_ids"] == ["m7-z97", "m7-z98", "m7-z99"]


def test_manifest_rejects_nonappend_completion_and_incomplete_success() -> None:
    scenarios = [_scenario("m7-z97"), _scenario("m7-z98")]
    manifest = RunManifest.create(
        run_id="20260801T120000Z",
        commit="a" * 40,
        source_digest="b" * 64,
        cleanliness="clean",
        track="answer",
        control_set_version="steam-agent-eval-controls/0.1",
        scenarios=scenarios,
        requested_routes=[RequestedRoute(None, None)],
        tool_versions={"runner": "0.1"},
        started_at=NOW,
    )
    controls = manifest.transition(RunState.CONTROLS, at=NOW)
    with pytest.raises(ManifestStateError):
        controls.transition(RunState.RUNNING, at=NOW, controls_passed=False)
    running = controls.transition(RunState.RUNNING, at=NOW, controls_passed=True)
    first = running.transition(
        RunState.RUNNING, at=NOW, completed_scenario_ids=["m7-z97"]
    )

    with pytest.raises(ManifestStateError):
        first.transition(
            RunState.RUNNING,
            at=NOW,
            completed_scenario_ids=["m7-z98"],
        )
    with pytest.raises(ManifestStateError):
        first.transition(RunState.RUNNING, at=NOW)
    with pytest.raises(ManifestStateError):
        first.transition(RunState.COMPLETED, at=NOW)

    interrupted = first.transition(
        RunState.INTERRUPTED,
        at=NOW,
        terminal_reason=TerminalReason.CANCELLED,
    )
    assert interrupted.completed_scenario_ids == ("m7-z97",)


@pytest.mark.parametrize(
    ("state", "reason"),
    (
        (RunState.FAILED, TerminalReason.SOURCE_NOT_CLEAN),
        (RunState.FAILED, TerminalReason.CONTROLS_FAILED),
        (RunState.FAILED, TerminalReason.PREFLIGHT_FAILED),
        (RunState.FAILED, TerminalReason.RUNNER_ERROR),
        (RunState.FAILED, TerminalReason.ARTIFACT_FAILURE),
        (RunState.INTERRUPTED, TerminalReason.CANCELLED),
        (RunState.CONTAMINATED, TerminalReason.SOURCE_CHANGED),
        (RunState.CONTAMINATED, TerminalReason.SNAPSHOT_INVALID),
    ),
)
def test_manifest_requires_bounded_reason_for_noncompletion(
    state: RunState, reason: TerminalReason
) -> None:
    terminal = _manifest().transition(state, at=NOW, terminal_reason=reason)
    assert terminal.terminal_reason is reason
    assert terminal.to_dict()["terminal_reason"] == reason.value

    with pytest.raises(ManifestStateError):
        _manifest().transition(state, at=NOW)


def test_manifest_rejects_reason_for_wrong_or_nonterminal_state() -> None:
    with pytest.raises(ManifestStateError):
        _manifest().transition(
            RunState.CONTROLS,
            at=NOW,
            terminal_reason=TerminalReason.RUNNER_ERROR,
        )
    with pytest.raises(ManifestStateError):
        _manifest().transition(
            RunState.FAILED,
            at=NOW,
            terminal_reason=TerminalReason.CANCELLED,
        )


def test_manifest_persists_dynamic_control_and_completion_checkpoints(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.json"
    initial = _manifest()
    controls = initial.transition(RunState.CONTROLS, at=NOW)
    running = controls.transition(RunState.RUNNING, at=NOW, controls_passed=True)
    checkpoint = running.transition(
        RunState.RUNNING, at=NOW, completed_scenario_ids=["m7-z99"]
    )

    for manifest in (initial, controls, running, checkpoint):
        manifest.persist(path)

    persisted = json.loads(path.read_text())
    assert persisted["revision"] == 3
    assert persisted["state"] == "running"
    assert persisted["controls_passed"] is True
    assert persisted["completed_scenario_ids"] == ["m7-z99"]


@pytest.mark.parametrize(
    "terminal",
    (RunState.FAILED, RunState.INTERRUPTED, RunState.CONTAMINATED),
)
def test_manifest_can_terminalize_safely_before_live_execution(
    terminal: RunState,
) -> None:
    reasons = {
        RunState.FAILED: TerminalReason.RUNNER_ERROR,
        RunState.INTERRUPTED: TerminalReason.CANCELLED,
        RunState.CONTAMINATED: TerminalReason.SOURCE_CHANGED,
    }
    terminal_manifest = _manifest().transition(
        terminal, at=NOW, terminal_reason=reasons[terminal]
    )
    assert terminal_manifest.state is terminal
    assert terminal_manifest.terminal is True


def test_manifest_persistence_atomically_advances_one_run(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    initial = _manifest()
    initial.persist(path)
    controls = initial.transition(RunState.CONTROLS, at=NOW + timedelta(seconds=1))
    controls.persist(path)

    document = json.loads(path.read_text())
    assert document["schema"] == "steam-agent-eval-run/0.1"
    assert document["revision"] == 1
    assert document["state"] == "controls"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ManifestStateError):
        initial.persist(path)
    assert not any(
        item.name.startswith(".manifest.json.tmp-") for item in tmp_path.iterdir()
    )


def test_manifest_persistence_rejects_provenance_change(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    initial = _manifest()
    initial.persist(path)
    controls = initial.transition(RunState.CONTROLS, at=NOW + timedelta(seconds=1))
    contaminated_history = replace(controls, source_digest="c" * 64)

    with pytest.raises(ManifestStateError):
        contaminated_history.persist(path)

    assert json.loads(path.read_text())["source"]["digest"] == "b" * 64


def test_manifest_contains_only_bounded_provenance_not_source_content() -> None:
    private_value = "/Users/private/person"
    scenario = FrozenScenario.create(
        source_name="m7/m7-z99.json",
        original_bytes=json.dumps(
            {"id": "m7-z99", "privacy_canaries": {"path": private_value}}
        ).encode(),
        document={"id": "m7-z99", "privacy_canaries": {"path": private_value}},
    )
    rendered = json.dumps(_manifest(scenario).to_dict(), sort_keys=True)

    assert private_value not in rendered
    assert scenario.source_name not in rendered
    assert scenario.sha256 in rendered


def test_manifest_rejects_path_bearing_identity_fields() -> None:
    with pytest.raises(ManifestStateError):
        RunManifest.create(
            run_id="../private",
            commit="a" * 40,
            source_digest="b" * 64,
            cleanliness="clean",
            track="legacy",
            control_set_version="steam-agent-eval-controls/0.1",
            scenarios=[_scenario()],
            requested_routes=[RequestedRoute(None, None)],
            tool_versions={},
            started_at=NOW,
        )


@pytest.mark.skipif(os.name != "posix", reason="snapshot permissions are POSIX-only")
def test_snapshot_context_manager_cleans_read_only_tree(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    root = snapshot.root
    with snapshot:
        assert root.exists()
    assert not root.exists()


def _matrix_manifest() -> run_state.MatrixManifest:
    inputs = run_state.MatrixInputs(
        commit="a" * 40,
        source_digest="b" * 64,
        harness_digest="c" * 64,
        scenarios=(
            run_state.MatrixScenario(
                scenario_id="m7-z99",
                source_sha256="d" * 64,
                child_source_digest="1" * 64,
                schema_version="steam-agent-eval:0.3",
                schema_sha256="e" * 64,
                execution_support="live",
                turn_count=2,
                rubric_sha256="f" * 64,
                criterion_ids=("grounded-answer",),
                qualitative_criteria=(
                    run_state.MatrixQualitativeCriterion(
                        "grounded-answer",
                        "judged_answer_rubric",
                        "Ground the answer.",
                        None,
                    ),
                ),
            ),
        ),
        tool_versions=(("codex", "0.146.0"),),
    )
    route = run_state.MatrixRoute("gpt-5.6-sol", "xhigh")
    work_items = tuple(
        run_state.MatrixWorkItem(
            work_item_id=f"w-00000{index}-{'1' * 16}",
            identity_sha256=str(index + 1) * 64,
            ordinal=index,
            scenario_id="m7-z99",
            track="discovery",
            route=route,
            replicate=index + 1,
        )
        for index in range(2)
    )
    return run_state.MatrixManifest.create(
        matrix_id="matrix-20260802T120000Z",
        config_sha256="2" * 64,
        campaign=_matrix_campaign(),
        plan_sha256="3" * 64,
        inputs=inputs,
        work_items=work_items,
        excluded_scenario_ids=(),
        started_at=NOW,
    )


def _matrix_completion(work_item_id: str, attempt: int) -> run_state.MatrixCompletion:
    return run_state.MatrixCompletion(
        work_item_id=work_item_id,
        attempt_id=f"attempt-{attempt:06d}",
        started_sha256="0" * 64,
        outcome="observed",
        unavailable_reason=None,
        child_run_id=f"20260802T12000{attempt}Z",
        child_exit_code=1,
        artifact_hashes=tuple(
            sorted(
                (name, str(index) * 64)
                for index, name in enumerate(
                    (
                        "controls.json",
                        "manifest.json",
                        "report.json",
                        "summary.json",
                        "transcript.jsonl",
                    ),
                    start=4,
                )
            )
        ),
        completed_at=(NOW + timedelta(seconds=attempt)).isoformat(),
    )


def test_matrix_manifest_checkpoints_exact_work_prefix_and_round_trips(
    tmp_path: Path,
) -> None:
    initial = _matrix_manifest()
    path = tmp_path / "manifest.json"
    initial.persist(path)
    first = initial.checkpoint(
        _matrix_completion(initial.work_items[0].work_item_id, 1),
        at=NOW + timedelta(seconds=1),
    )
    first.persist(path)
    completed = first.checkpoint(
        _matrix_completion(initial.work_items[1].work_item_id, 2),
        at=NOW + timedelta(seconds=2),
    )
    completed.persist(path)

    loaded = run_state.MatrixManifest.from_dict(json.loads(path.read_text()))
    assert loaded == completed
    assert loaded.state is run_state.MatrixState.COMPLETED
    assert loaded.inputs.scenarios[0].turn_count == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ManifestStateError):
        loaded.checkpoint(
            _matrix_completion(initial.work_items[0].work_item_id, 3), at=NOW
        )


def test_matrix_acceptance_checkpoint_is_exact_and_irreversible(
    tmp_path: Path,
) -> None:
    initial = _matrix_manifest()
    path = tmp_path / "manifest.json"
    initial.persist(path)
    first = initial.checkpoint(
        _matrix_completion(initial.work_items[0].work_item_id, 1),
        at=NOW + timedelta(seconds=1),
    )
    first.persist(path)
    completed = first.checkpoint(
        _matrix_completion(initial.work_items[1].work_item_id, 2),
        at=NOW + timedelta(seconds=2),
    )
    completed.persist(path)
    source_digest = completed.acceptance_source_sha256
    assert "acceptance_sha256" not in completed.to_dict()
    assert "acceptance_finalized_at" not in completed.to_dict()
    bound = completed.bind_acceptance(
        "7" * 64,
        finalized_at=(NOW + timedelta(seconds=3)).isoformat(),
    )
    bound.persist(path)

    assert bound.to_dict()["acceptance_sha256"] == "7" * 64
    assert bound.to_dict()["acceptance_finalized_at"] == (
        NOW + timedelta(seconds=3)
    ).isoformat()
    assert run_state.MatrixManifest.from_dict(json.loads(path.read_text())) == bound
    assert bound.acceptance_source_sha256 == source_digest
    frozen_bytes = path.read_bytes()
    for changed in (
        completed,
        replace(bound, acceptance_sha256="8" * 64),
        replace(
            bound,
            acceptance_finalized_at=(NOW + timedelta(seconds=4)).isoformat(),
        ),
    ):
        with pytest.raises(ManifestStateError, match="history"):
            changed.persist(path)
        assert path.read_bytes() == frozen_bytes


def test_matrix_manifest_revision_exactly_counts_committed_completions() -> None:
    initial = _matrix_manifest()

    with pytest.raises(ManifestStateError, match="revision does not match"):
        replace(initial, revision=1)

    forged = initial.to_dict()
    forged["revision"] = 1
    with pytest.raises(ManifestStateError, match="revision does not match"):
        run_state.MatrixManifest.from_dict(forged)


@pytest.mark.parametrize("turn_count", (0, -1, True, 1.5, 65))
def test_matrix_scenario_requires_a_positive_integer_turn_count(
    turn_count: object,
) -> None:
    scenario = _matrix_manifest().inputs.scenarios[0]

    with pytest.raises(ManifestStateError, match="turn count"):
        replace(scenario, turn_count=turn_count)


def test_matrix_scenario_accepts_executor_maximum_turn_count() -> None:
    scenario = _matrix_manifest().inputs.scenarios[0]

    assert replace(scenario, turn_count=64).turn_count == 64


@pytest.mark.parametrize("field", ("turn_count", "child_source_digest"))
def test_matrix_scenario_strict_parse_requires_bound_fields(field: str) -> None:
    document = _matrix_manifest().inputs.scenarios[0].to_dict()
    document.pop(field)

    with pytest.raises(ManifestStateError, match="scenario"):
        run_state.MatrixScenario.from_dict(document)


def test_matrix_manifest_rejects_out_of_order_or_changed_history(
    tmp_path: Path,
) -> None:
    initial = _matrix_manifest()
    with pytest.raises(ManifestStateError):
        initial.checkpoint(
            _matrix_completion(initial.work_items[1].work_item_id, 1), at=NOW
        )

    path = tmp_path / "manifest.json"
    initial.persist(path)
    changed = replace(initial, config_sha256="9" * 64).checkpoint(
        _matrix_completion(initial.work_items[0].work_item_id, 1), at=NOW
    )
    with pytest.raises(ManifestStateError):
        changed.persist(path)


def test_matrix_manifest_hash_binds_normalized_campaign_policy() -> None:
    manifest = _matrix_manifest()
    document = manifest.to_dict()
    document["campaign"]["acceptance_policy"]["replicates"] = 3

    with pytest.raises(ManifestStateError, match="campaign digest"):
        run_state.MatrixManifest.from_dict(document)


def test_matrix_campaign_rejects_an_arbitrary_self_consistent_judge() -> None:
    arbitrary = replace(
        run_state.CALIBRATED_JUDGE_CONFIGURATIONS[0], model="gpt-5.6-terra"
    )

    with pytest.raises(ManifestStateError, match="calibrated judge policy"):
        replace(
            _matrix_campaign(),
            judges=(arbitrary, *run_state.CALIBRATED_JUDGE_CONFIGURATIONS[1:]),
        )


@pytest.mark.parametrize(
    ("section", "extra"),
    (
        ("selection_policy", "unexpected_selection_field"),
        ("acceptance_policy", "unexpected_acceptance_field"),
        ("judge_policy", "unexpected_judge_field"),
    ),
)
def test_matrix_manifest_rejects_unknown_nested_campaign_fields(
    section: str, extra: str
) -> None:
    document = _matrix_manifest().to_dict()
    document["campaign"][section][extra] = True

    with pytest.raises(ManifestStateError, match="campaign policy"):
        run_state.MatrixManifest.from_dict(document)


def test_unavailable_matrix_completion_is_typed_and_round_trips() -> None:
    completion = run_state.MatrixCompletion(
        work_item_id="w-000000-1111111111111111",
        attempt_id="attempt-000001",
        started_sha256="0" * 64,
        outcome="unavailable",
        unavailable_reason="provider_route_unavailable",
        child_run_id=None,
        child_exit_code=None,
        artifact_hashes=(),
        completed_at=NOW.isoformat(),
    )

    assert run_state.MatrixCompletion.from_dict(completion.to_dict()) == completion


@pytest.mark.parametrize("started_sha256", ("", "0" * 63, "g" * 64, None))
def test_matrix_completion_requires_attempt_start_digest(
    started_sha256: object,
) -> None:
    completion = _matrix_completion("w-000000-1111111111111111", 1)

    with pytest.raises(ManifestStateError, match="start digest"):
        replace(completion, started_sha256=started_sha256)


def test_matrix_completion_strict_parse_requires_attempt_start_digest() -> None:
    completion = _matrix_completion("w-000000-1111111111111111", 1).to_dict()
    completion.pop("started_sha256")

    with pytest.raises(ManifestStateError, match="completion"):
        run_state.MatrixCompletion.from_dict(completion)


@pytest.mark.parametrize(
    "overrides",
    (
        {"unavailable_reason": None},
        {"child_run_id": "child-000001"},
        {"child_exit_code": 1},
        {"artifact_hashes": (("report.json", "a" * 64),)},
    ),
)
def test_unavailable_matrix_completion_rejects_observation_fields(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "work_item_id": "w-000000-1111111111111111",
        "attempt_id": "attempt-000001",
        "started_sha256": "0" * 64,
        "outcome": "unavailable",
        "unavailable_reason": "provider_route_unavailable",
        "child_run_id": None,
        "child_exit_code": None,
        "artifact_hashes": (),
        "completed_at": NOW.isoformat(),
    }
    values.update(overrides)

    with pytest.raises(ManifestStateError, match="unavailable"):
        run_state.MatrixCompletion(**values)  # type: ignore[arg-type]

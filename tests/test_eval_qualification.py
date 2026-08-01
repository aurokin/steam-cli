from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import __main__ as runner  # noqa: E402
from evals.runner import codex_driver  # noqa: E402
from evals.runner import controls  # noqa: E402


REQUIREMENT = {
    "command": "steam-agent operations observe",
    "arguments": ["--machine", "synthetic-machine"],
}
COMMAND = (
    "./bin/steam-agent --data-dir steam-agent-data --format json "
    "operations observe --machine synthetic-machine"
)


def _turn(*results: dict[str, object]) -> dict[str, object]:
    return {
        "index": 0,
        "_command_results": list(results),
        "_command_completion_sequences": list(range(len(results))),
    }


def _result(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "command": COMMAND,
        "status": "completed",
        "exit_code": 0,
        "output": '{"data":{"state":"ready"}}',
        "output_source": "aggregate",
        "output_delta_count": 0,
    }
    value.update(overrides)
    return value


def test_typed_evidence_capture_distinguishes_transport_states() -> None:
    not_applicable = runner._capture_required_document([], [])  # noqa: SLF001
    missing = runner._capture_required_document(  # noqa: SLF001
        [_turn()], [REQUIREMENT]
    )
    failed = runner._capture_required_document(  # noqa: SLF001
        [_turn(_result(exit_code=1))], [REQUIREMENT]
    )
    duplicate = runner._capture_required_document(  # noqa: SLF001
        [_turn(_result(), _result(output_source="deltas", output_delta_count=2))],
        [REQUIREMENT],
    )
    absent = runner._capture_required_document(  # noqa: SLF001
        [_turn(_result(output=None, output_source="absent"))], [REQUIREMENT]
    )
    invalid = runner._capture_required_document(  # noqa: SLF001
        [_turn(_result(output="not-json"))], [REQUIREMENT]
    )
    captured = runner._capture_required_document(  # noqa: SLF001
        [_turn(_result(output_source="deltas", output_delta_count=3))], [REQUIREMENT]
    )

    assert not_applicable.state == "not_applicable"
    assert missing.state == "command_missing"
    assert failed.state == "command_failed"
    assert duplicate.state == "multiple_candidates"
    assert duplicate.successful_candidates == 2
    assert absent.state == "output_absent"
    assert invalid.state == "invalid_json"
    assert captured.state == "captured"
    assert captured.source == "deltas"
    assert captured.document == {"data": {"state": "ready"}}


def test_answer_track_discloses_only_the_exact_command_manifest() -> None:
    appendix = runner._track_instruction_appendix(  # noqa: SLF001
        "answer", [REQUIREMENT]
    )
    assert runner._canonical_required_command(REQUIREMENT) in appendix  # noqa: SLF001
    assert "$.data" not in appendix
    assert "ready" not in appendix
    assert runner._track_instruction_appendix("discovery", [REQUIREMENT]) == ""  # noqa: SLF001


def test_legacy_track_preserves_instruction_bytes() -> None:
    rendered = runner.DEVELOPER_INSTRUCTIONS.format(
        steam_agent="./bin/steam-agent",
        data_dir="steam-agent-data",
        machine="synthetic-machine",
        account="synthetic",
        track_appendix="",
    )

    assert len(rendered) == 1888
    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        "5299615928a61c1658142a479ad576b116d37eacb5f82bacccfccb008d1372b4"
    )


def test_selected_input_check_detects_schema_drift(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "module.py").write_text("VALUE = 1\n")
    harness_root = tmp_path / "runner"
    harness_root.mkdir()
    (harness_root / "grade.py").write_text("VALUE = 1\n")
    schema_path = tmp_path / "scenario.json"
    schema_path.write_text('{"version": 1}\n')
    source_digest = runner.run_state.inventory_digest(source_root)
    harness_digest = runner.run_state.inventory_digest(harness_root)
    schema_digest = hashlib.sha256(schema_path.read_bytes()).hexdigest()

    assert runner._selected_inputs_unchanged(  # noqa: SLF001
        [],
        source_root=source_root,
        source_digest=source_digest,
        harness_root=harness_root,
        harness_digest=harness_digest,
        schema_path=schema_path,
        schema_digest=schema_digest,
    )

    schema_path.write_text('{"version": 2}\n')
    assert not runner._selected_inputs_unchanged(  # noqa: SLF001
        [],
        source_root=source_root,
        source_digest=source_digest,
        harness_root=harness_root,
        harness_digest=harness_digest,
        schema_path=schema_path,
        schema_digest=schema_digest,
    )


def test_snapshot_source_replaces_live_source_in_permission_profile(
    tmp_path: Path,
) -> None:
    snapshot_source = tmp_path / "snapshot" / "src"
    snapshot_source.mkdir(parents=True)

    roots = codex_driver._permission_read_roots(  # noqa: SLF001
        source_root=snapshot_source
    )

    assert snapshot_source.resolve() in roots
    assert (ROOT / "src").resolve() not in roots


def test_requested_route_requires_pre_activity_attestation_and_no_reroute() -> None:
    exact = codex_driver.AgentTranscript(
        effective_model="gpt-5.6-sol",
        effective_reasoning_effort="high",
        observed_models=["gpt-5.6-sol"],
        observed_reasoning_efforts=["high"],
        pre_activity_models=["gpt-5.6-sol"],
        pre_activity_reasoning_efforts=["high"],
    )
    away_and_back = codex_driver.AgentTranscript(
        effective_model="gpt-5.6-sol",
        effective_reasoning_effort="high",
        observed_models=["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-sol"],
        observed_reasoning_efforts=["high"],
        pre_activity_models=["gpt-5.6-sol"],
        pre_activity_reasoning_efforts=["high"],
    )
    late_effort = codex_driver.AgentTranscript(
        effective_model="gpt-5.6-sol",
        effective_reasoning_effort="high",
        observed_models=["gpt-5.6-sol"],
        observed_reasoning_efforts=["high"],
        pre_activity_models=["gpt-5.6-sol"],
        pre_activity_reasoning_efforts=[],
    )

    assert runner._requested_route_confirmed(  # noqa: SLF001
        [exact], model="gpt-5.6-sol", effort="high"
    )
    assert not runner._requested_route_confirmed(  # noqa: SLF001
        [away_and_back], model="gpt-5.6-sol", effort="high"
    )
    assert not runner._requested_route_confirmed(  # noqa: SLF001
        [late_effort], model="gpt-5.6-sol", effort="high"
    )


def test_diagnostics_are_additive_and_do_not_choose_a_cause() -> None:
    metrics = {
        "agent_turns": {"passed": False},
        "tool_policy": {
            "passed": False,
            "violations": [{"reason": "cache_only_boundary"}],
        },
        "oracle": {"passed": False},
        "claims": {"passed": False},
        "privacy": {"passed": False},
    }
    conditions = runner._observed_diagnostic_conditions(  # noqa: SLF001
        metrics, runner.EvidenceCapture(state="output_absent")
    )
    assert conditions == [
        "unsafe_activity",
        "privacy_failure",
        "agent_turn_incomplete",
        "required_evidence_unusable",
        "tool_policy_failure",
        "oracle_failure",
        "claims_failure",
    ]


def test_scripted_controls_use_integrated_runner_layers() -> None:
    result = controls.run_scripted_controls(runner._evaluate_scripted_control)  # noqa: SLF001

    assert result["passed"] is True
    assert len(result["controls"]) == 8


def test_controls_only_writes_a_completed_private_cohort(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "RESULTS_ROOT", tmp_path / "evals" / "results")
    monkeypatch.setattr(
        runner, "_source_revision_and_cleanliness", lambda: ("a" * 40, "clean")
    )

    assert runner.main(["--controls", "--track", "answer"]) == 0

    [run_dir] = (tmp_path / "evals" / "results").iterdir()
    manifest_path = run_dir / "manifest.json"
    controls_path = run_dir / "controls.json"
    summary_path = run_dir / "summary.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["state"] == "completed"
    assert manifest["terminal_reason"] is None
    assert manifest["source"]["snapshot"] == "sealed"
    assert manifest["tool_versions"]
    assert json.loads(controls_path.read_text())["passed"] is True
    assert json.loads(summary_path.read_text()) == {
        "controls": True,
        "track": "answer",
    }
    for path in (manifest_path, controls_path, summary_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_dirty_source_terminalizes_before_controls(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "RESULTS_ROOT", tmp_path / "evals" / "results")
    monkeypatch.setattr(
        runner, "_source_revision_and_cleanliness", lambda: ("a" * 40, "dirty")
    )
    monkeypatch.setattr(
        controls,
        "run_scripted_controls",
        lambda *args: (_ for _ in ()).throw(AssertionError("controls ran")),
    )

    assert runner.main(["--controls"]) == 1

    [run_dir] = (tmp_path / "evals" / "results").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "failed"
    assert manifest["terminal_reason"] == "source_not_clean"
    assert not (run_dir / "controls.json").exists()


def test_control_exception_terminalizes_structural_failure(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "RESULTS_ROOT", tmp_path / "evals" / "results")
    monkeypatch.setattr(
        runner, "_source_revision_and_cleanliness", lambda: ("a" * 40, "clean")
    )
    monkeypatch.setattr(
        controls,
        "run_scripted_controls",
        lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic")),
    )

    assert runner.main(["--controls"]) == 1

    [run_dir] = (tmp_path / "evals" / "results").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "failed"
    assert manifest["terminal_reason"] == "controls_failed"


def test_final_source_drift_contaminates_controls_cohort(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "RESULTS_ROOT", tmp_path / "evals" / "results")
    monkeypatch.setattr(
        runner, "_source_revision_and_cleanliness", lambda: ("a" * 40, "clean")
    )
    monkeypatch.setattr(runner, "_selected_inputs_unchanged", lambda *a, **k: False)

    assert runner.main(["--controls"]) == 1

    [run_dir] = (tmp_path / "evals" / "results").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "contaminated"
    assert manifest["terminal_reason"] == "source_changed"


def test_controls_finalization_interrupt_is_checkpointed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "RESULTS_ROOT", tmp_path / "evals" / "results")
    monkeypatch.setattr(
        runner, "_source_revision_and_cleanliness", lambda: ("a" * 40, "clean")
    )
    original_publish = runner.run_state.atomic_publish_private_json

    def interrupt_summary(path: Path, value: object) -> None:
        if path.name == "summary.json":
            raise KeyboardInterrupt
        original_publish(path, value)

    monkeypatch.setattr(
        runner.run_state, "atomic_publish_private_json", interrupt_summary
    )

    with pytest.raises(KeyboardInterrupt):
        runner.main(["--controls"])

    [run_dir] = (tmp_path / "evals" / "results").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "interrupted"
    assert manifest["terminal_reason"] == "cancelled"

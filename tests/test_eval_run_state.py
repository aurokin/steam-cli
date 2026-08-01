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

    assert scenario.sha256 == __import__("hashlib").sha256(
        scenario.original_bytes
    ).hexdigest()
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
    assert not any(item.name.startswith(".artifact.json.tmp-") for item in tmp_path.iterdir())


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

    assert [manifest.revision, controls.revision, running.revision, completed.revision] == [
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
    running = controls.transition(
        RunState.RUNNING, at=NOW, controls_passed=True
    )
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
    running = controls.transition(
        RunState.RUNNING, at=NOW, controls_passed=True
    )
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
    running = controls.transition(
        RunState.RUNNING, at=NOW, controls_passed=True
    )
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
    assert not any(item.name.startswith(".manifest.json.tmp-") for item in tmp_path.iterdir())


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

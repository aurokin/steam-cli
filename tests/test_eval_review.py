from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import codex_driver, review, run_state  # noqa: E402


WORK_ITEM_ID = "w-000000-0123456789abcdef"
PROMPT_VERSION = "matrix-judge/0.1"
PROMPT_SHA256 = "671449c1329475b3753ffe30a017ad60152603efe6def833872eff8c428deec7"
PARSER_VERSION = "matrix-parser/0.1"
PARSER_SHA256 = "658a8acdf97c7d681c2b78e68c853b73fe010c49631595c7f69f67575931be49"
TARGET = {
    "matrix_id": "matrix-test",
    "work_item_id": WORK_ITEM_ID,
    "report_sha256": "1" * 64,
    "scenario_sha256": "2" * 64,
    "rubric_sha256": "3" * 64,
    "projection_sha256": "4" * 64,
}
PROJECTION = {
    "schema": "steam-agent-eval-qualitative-projection/0.2",
    "criteria": [
        {
            "id": "clear",
            "source": "judged_answer_rubric",
            "requirement": "Answer clearly.",
            "evidence_path": None,
            "screen_safety_gate": False,
        },
        {
            "id": "aligned",
            "source": "generated.prose_claims_sidecar_alignment",
            "requirement": "Align prose and claims.",
            "evidence_path": None,
            "screen_safety_gate": False,
        },
    ],
    "answers": [{"turn": 0, "text": "A direct answer."}],
    "claims_sidecars": [{"turn": 0, "claims": [], "declined": False}],
}


def _campaign() -> SimpleNamespace:
    return SimpleNamespace(
        campaign_kind="benchmark",
        judges=run_state.CALIBRATED_JUDGE_CONFIGURATIONS,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=PROMPT_SHA256,
        parser_version=PARSER_VERSION,
        parser_sha256=PARSER_SHA256,
        adjudication_method=run_state.CALIBRATED_ADJUDICATION_METHOD,
        adjudicator=run_state.CALIBRATED_ADJUDICATOR,
    )


def _result() -> SimpleNamespace:
    manifest = SimpleNamespace(
        matrix_id="matrix-test",
        campaign=_campaign(),
        work_items=(SimpleNamespace(work_item_id=WORK_ITEM_ID),),
    )
    return SimpleNamespace(
        manifest=manifest,
        manifest_sha256="a" * 64,
        structurally_complete=True,
    )


def _case(judge_identifier: str = "judge-1") -> dict[str, object]:
    return {
        "schema": review._CASE_SCHEMA,  # noqa: SLF001
        "execution": {
            "model_input": "this_document_verbatim",
            "criterion_coverage": "every_projection_criterion_exactly_once",
            "external_context": "forbidden",
            "response_schema": {
                "schema": review._VERDICTS_SCHEMA,  # noqa: SLF001
                "sha256": "5" * 64,
            },
            "invocation": review._invocation_binding(  # noqa: SLF001
                TARGET, judge_identifier
            ),
        },
        "target": TARGET,
        "prompt": {
            "version": PROMPT_VERSION,
            "sha256": PROMPT_SHA256,
            "text": "Judge this case.",
        },
        "parser": {
            "version": PARSER_VERSION,
            "sha256": PARSER_SHA256,
            "document": {},
        },
        "presentation": {"blinded_label": "candidate-A", "order": 0},
        "projection": PROJECTION,
    }


def _review_root(path: Path) -> dict[str, object]:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    (path / "operations").mkdir(mode=0o700)
    ledger = {
        "cases": [
            {
                "work_item_id": WORK_ITEM_ID,
                "judge_identifier": configured.identifier,
                "path": f"cases/{WORK_ITEM_ID}-{configured.identifier}.json",
                "sha256": review._sha256(  # noqa: SLF001
                    _case(configured.identifier)
                ),
            }
            for configured in run_state.CALIBRATED_JUDGE_CONFIGURATIONS
        ]
    }
    return ledger


def _matrix_root(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _verdict_document(judge_identifier: str = "judge-1") -> dict[str, object]:
    return {
        "schema": review._VERDICTS_SCHEMA,  # noqa: SLF001
        "target": {
            "work_item_id": WORK_ITEM_ID,
            "projection_sha256": TARGET["projection_sha256"],
        },
        "invocation": review._invocation_binding(  # noqa: SLF001
            TARGET, judge_identifier
        ),
        "verdicts": [
            {
                "criterion_id": "aligned",
                "verdict": "pass",
                "rationale": "Claims align.",
            },
            {
                "criterion_id": "clear",
                "verdict": "pass",
                "rationale": "Answer is clear.",
            },
        ],
    }


def _write_event_log(path: Path, events: list[dict[str, object]]) -> None:
    path.write_bytes(
        b"".join(
            review._canonical_bytes(event) for event in events  # noqa: SLF001
        )
    )
    path.chmod(0o600)


def _valid_event_log() -> list[dict[str, object]]:
    return [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item-1", "type": "reasoning", "text": ""},
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item-2",
                "type": "agent_message",
                "text": "structured verdict",
            },
        },
        {"type": "turn.completed", "usage": {}},
    ]


def test_case_document_is_the_exact_route_blind_model_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    root = ROOT / "evals" / "calibration"
    for name in ("matrix-judge-prompt-0.1.md", "matrix-parser-0.1.json"):
        (calibration / name).write_bytes((root / name).read_bytes())
    index = SimpleNamespace(
        inspection_result=SimpleNamespace(
            manifest=SimpleNamespace(campaign=_campaign())
        )
    )
    monkeypatch.setattr(review, "_target_for", lambda *_args: (TARGET, PROJECTION))

    document = review._case_document(  # noqa: SLF001
        tmp_path, index, WORK_ITEM_ID, "judge-1"
    )

    encoded = review._canonical_bytes(document)  # noqa: SLF001
    assert review.judge._validate_schema(document, "review-case-0.1.json") == document  # noqa: SLF001
    assert b"gpt-5.6-sol" not in encoded
    assert b'"route"' not in encoded
    assert b'"metrics"' not in encoded
    assert document["projection"] == PROJECTION
    assert document["execution"]["model_input"] == "this_document_verbatim"
    assert document["execution"]["invocation"] == review._invocation_binding(  # noqa: SLF001
        TARGET, "judge-1"
    )
    assert document["prompt"]["text"].startswith("# Matrix qualitative judge")


def test_calibration_asset_rejects_symlink(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    target = calibration / "target.txt"
    target.write_text("calibration")
    (calibration / "asset.txt").symlink_to(target)

    with pytest.raises(review.ReviewError, match="asset is unavailable"):
        review._asset_text(  # noqa: SLF001
            tmp_path,
            "asset.txt",
            "0" * 64,
        )


def test_calibration_asset_rejects_preflight_oversize_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    path = calibration / "asset.txt"
    path.touch()
    os.truncate(path, review._MAX_DOCUMENT_BYTES + 1)  # noqa: SLF001

    monkeypatch.setattr(
        review.os,
        "read",
        lambda *_args: pytest.fail("oversized calibration must not be read"),
    )
    with pytest.raises(review.ReviewError, match="asset is unavailable"):
        review._asset_text(tmp_path, "asset.txt", "0" * 64)  # noqa: SLF001


def test_calibration_asset_rejects_fifo_without_hanging(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    path = calibration / "asset.txt"
    os.mkfifo(path, mode=0o600)
    script = """
from pathlib import Path
import sys
from evals.runner import review

try:
    review._asset_text(Path(sys.argv[1]), "asset.txt", "0" * 64)
except review.ReviewError as error:
    raise SystemExit(0 if "asset is unavailable" in str(error) else 2)
raise SystemExit(1)
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr


def test_prepare_publishes_private_cases_schema_and_bounded_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _result()
    index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: index)
    monkeypatch.setattr(
        review,
        "_case_document",
        lambda _matrix, _index, _work_item, judge_identifier: _case(
            judge_identifier
        ),
    )
    monkeypatch.setattr(review.judge, "_validate_schema", lambda value, _name: value)
    matrix_dir = tmp_path / "matrix"
    matrix_dir.mkdir()
    review_dir = tmp_path / "prepared"

    output = review.prepare(matrix_dir, review_dir)

    assert output == {"matrix_id": "matrix-test", "cases": 3}
    assert str(review_dir) not in review._canonical_bytes(output).decode()  # noqa: SLF001
    assert stat.S_IMODE(review_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((review_dir / "cases").stat().st_mode) == 0o700
    assert stat.S_IMODE((review_dir / "operations").stat().st_mode) == 0o700
    assert stat.S_IMODE((review_dir / "ledger.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((review_dir / "response-schema.json").stat().st_mode) == 0o600
    ledger = review._read_json(  # noqa: SLF001
        review_dir / "ledger.json", require_private=True
    )
    assert ledger["policy"] == {
        "maximum_attempts_per_judgment": 3,
        "model_invocation": "external",
        "usage_accounting": "unavailable",
    }
    assert {
        (item["judge_identifier"], item["path"])
        for item in ledger["cases"]
    } == {
        (
            configured.identifier,
            f"cases/{WORK_ITEM_ID}-{configured.identifier}.json",
        )
        for configured in run_state.CALIBRATED_JUDGE_CONFIGURATIONS
    }
    assert len(
        {
            review._read_json(  # noqa: SLF001
                review_dir / item["path"], require_private=True
            )["execution"]["invocation"]["binding_sha256"]
            for item in ledger["cases"]
        }
    ) == 3


def test_review_cli_redacts_filesystem_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = tmp_path / "private-account" / "review"
    monkeypatch.setattr(
        review,
        "prepare",
        lambda *_args: (_ for _ in ()).throw(OSError(str(private_path))),
    )

    assert (
        review.review_cli(["prepare", str(tmp_path / "matrix"), str(private_path)]) == 1
    )
    captured = capsys.readouterr()
    assert str(private_path) not in captured.err
    assert captured.err == "qualitative review filesystem operation failed\n"


@pytest.mark.parametrize("command", ["assemble", "resolve"])
@pytest.mark.parametrize("root_alias", ["same", "symlink"])
def test_review_operations_reject_overlapping_roots_without_hanging(
    tmp_path: Path, command: str, root_alias: str
) -> None:
    matrix_dir = tmp_path / "matrix"
    matrix_dir.mkdir(mode=0o700)
    review_dir = matrix_dir
    if root_alias == "symlink":
        review_dir = tmp_path / "review-alias"
        review_dir.symlink_to(matrix_dir, target_is_directory=True)
    script = """
from pathlib import Path
import sys
from evals.runner import review

matrix_dir = Path(sys.argv[2])
review_dir = Path(sys.argv[3])
try:
    if sys.argv[1] == "assemble":
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            "w-000000-0123456789abcdef",
            Path("missing-verdicts.json"),
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=1,
            isolation_attestation=review._ISOLATION_ATTESTATION,
        )
    else:
        review.resolve_agreement(matrix_dir, review_dir)
except review.ReviewError as error:
    raise SystemExit(0 if "must be outside matrix" in str(error) else 2)
raise SystemExit(1)
"""

    result = subprocess.run(
        [sys.executable, "-c", script, command, str(matrix_dir), str(review_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr
    assert not (matrix_dir / "matrix.lock").exists()


@pytest.mark.parametrize("command", ["prepare", "assemble", "resolve"])
def test_review_commands_reject_case_alias_without_hanging(
    tmp_path: Path, command: str
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS case-alias regression")
    matrix_dir = tmp_path / "CaseMatrix"
    matrix_dir.mkdir(mode=0o700)
    alias_dir = tmp_path / "casematrix"
    if not alias_dir.exists() or not os.path.samefile(matrix_dir, alias_dir):
        pytest.skip("filesystem is case-sensitive")
    review_dir = alias_dir / "prepared"
    script = """
from pathlib import Path
import sys
from evals.runner import review

matrix_dir = Path(sys.argv[2])
review_dir = Path(sys.argv[3])
try:
    if sys.argv[1] == "prepare":
        review.prepare(matrix_dir, review_dir)
    elif sys.argv[1] == "assemble":
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            "w-000000-0123456789abcdef",
            Path("missing-verdicts.json"),
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=1,
            isolation_attestation=review._ISOLATION_ATTESTATION,
        )
    else:
        review.resolve_agreement(matrix_dir, review_dir)
except review.ReviewError as error:
    raise SystemExit(0 if "must be outside matrix" in str(error) else 2)
raise SystemExit(1)
"""

    result = subprocess.run(
        [sys.executable, "-c", script, command, str(matrix_dir), str(review_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )

    assert result.returncode == 0, result.stderr
    assert not review_dir.exists()
    assert not (matrix_dir / "matrix.lock").exists()


@pytest.mark.parametrize("command", ["prepare", "assemble", "resolve"])
def test_review_commands_reject_mocked_parent_identity_before_locks_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    matrix_dir = _matrix_root(tmp_path / "matrix")
    review_parent = tmp_path / "review-parent"
    review_parent.mkdir(mode=0o700)
    review_dir = review_parent / "prepared"
    matrix_root = matrix_dir.resolve()
    review_root = review_dir.resolve()
    identity_calls: list[Path] = []

    def filesystem_identity(path: Path) -> tuple[int, int] | None:
        identity_calls.append(path)
        if path in {matrix_root, review_root.parent}:
            return (7, 11)
        return None

    def unexpected_target_index(_path: Path) -> None:
        pytest.fail("matrix inspection must follow root separation")

    class UnexpectedLock:
        def __init__(self, _path: Path) -> None:
            pytest.fail("lock acquisition must follow root separation")

    monkeypatch.setattr(review, "_filesystem_identity", filesystem_identity)
    monkeypatch.setattr(review.judge, "_target_index", unexpected_target_index)
    monkeypatch.setattr(review.matrix, "MatrixLock", UnexpectedLock)

    with pytest.raises(review.ReviewError, match="must be outside matrix"):
        if command == "prepare":
            review.prepare(matrix_dir, review_dir)
        elif command == "assemble":
            review.assemble_judgment(
                matrix_dir,
                review_dir,
                WORK_ITEM_ID,
                tmp_path / "missing-verdicts.json",
                judge_identifier="judge-1",
                attempt_count=1,
                duration_ms=1,
                isolation_attestation=review._ISOLATION_ATTESTATION,
            )
        else:
            review.resolve_agreement(matrix_dir, review_dir)

    assert identity_calls == [matrix_root, review_root, review_root.parent]
    assert not review_dir.exists()
    assert list(review_parent.iterdir()) == []
    assert not (matrix_dir / "matrix.lock").exists()


def test_review_root_rejects_non_string_work_item_without_type_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _result()
    index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: index)
    monkeypatch.setattr(review, "_case_document", lambda *_args: _case())
    monkeypatch.setattr(review.judge, "_validate_schema", lambda value, _name: value)
    matrix_dir = _matrix_root(tmp_path / "matrix")
    review_dir = tmp_path / "prepared"
    review.prepare(matrix_dir, review_dir)
    ledger_path = review_dir / "ledger.json"
    ledger = review._read_json(ledger_path, require_private=True)  # noqa: SLF001
    ledger["cases"][0]["work_item_id"] = 7
    ledger_path.write_bytes(review._canonical_bytes(ledger))  # noqa: SLF001

    with pytest.raises(review.ReviewError, match="ledger is invalid"):
        review._validate_review_root(review_dir, result)  # noqa: SLF001


def test_bound_case_rejects_semantically_equal_noncanonical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _result()
    index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: index)
    monkeypatch.setattr(review, "_case_document", lambda *_args: _case())
    monkeypatch.setattr(review.judge, "_validate_schema", lambda value, _name: value)
    matrix_dir = _matrix_root(tmp_path / "matrix")
    review_dir = tmp_path / "prepared"
    review.prepare(matrix_dir, review_dir)
    case_path = review_dir / "cases" / f"{WORK_ITEM_ID}-judge-1.json"
    case_path.write_text(json.dumps(_case(), indent=2))
    case_path.chmod(0o600)
    ledger = review._read_json(  # noqa: SLF001
        review_dir / "ledger.json", require_private=True
    )

    with pytest.raises(review.ReviewError, match="not canonical"):
        review._load_bound_case(  # noqa: SLF001
            matrix_dir, review_dir, index, ledger, WORK_ITEM_ID, "judge-1"
        )


def test_json_reader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.fifo"
    os.mkfifo(path, mode=0o600)
    script = """
from pathlib import Path
import sys
from evals.runner import review

try:
    review._read_json(Path(sys.argv[1]), require_private=True)
except review.ReviewError as error:
    raise SystemExit(0 if "not a regular file" in str(error) else 2)
raise SystemExit(1)
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr


def test_json_reader_rejects_preflight_oversize_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "verdicts.json"
    path.touch(mode=0o600)
    os.truncate(path, review._MAX_DOCUMENT_BYTES + 1)  # noqa: SLF001

    def unexpected_read(_descriptor: int, _size: int) -> bytes:
        raise AssertionError("oversized input must be rejected before read")

    monkeypatch.setattr(review.os, "read", unexpected_read)
    with pytest.raises(review.ReviewError, match="input is invalid"):
        review._read_json(path, require_private=True)  # noqa: SLF001


def test_json_reader_caps_file_growth_after_fstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "verdicts.json"
    path.touch(mode=0o600)
    actual_fstat = os.fstat
    bytes_returned = 0

    def stale_fstat(descriptor: int) -> SimpleNamespace:
        item_stat = actual_fstat(descriptor)
        return SimpleNamespace(st_mode=item_stat.st_mode, st_size=0)

    def growing_read(_descriptor: int, size: int) -> bytes:
        nonlocal bytes_returned
        bytes_returned += size
        return b"x" * size

    monkeypatch.setattr(review.os, "fstat", stale_fstat)
    monkeypatch.setattr(review.os, "read", growing_read)
    with pytest.raises(review.ReviewError, match="input is invalid"):
        review._read_json(path, require_private=True)  # noqa: SLF001
    assert bytes_returned == review._MAX_DOCUMENT_BYTES + 1  # noqa: SLF001


def test_event_log_accepts_only_reasoning_and_one_agent_message(
    tmp_path: Path,
) -> None:
    path = tmp_path / "codex.stdout"
    events = _valid_event_log()
    _write_event_log(path, events)

    assert review.check_event_log(path) == {
        "events": len(events),
        "agent_messages": 1,
    }


@pytest.mark.parametrize("item_type", ["command_execution", "file_change", "tool_call"])
def test_event_log_rejects_any_tool_use(tmp_path: Path, item_type: str) -> None:
    path = tmp_path / "codex.stdout"
    events = _valid_event_log()
    events.insert(
        -1,
        {
            "type": "item.completed",
            "item": {"id": "tool-1", "type": item_type},
        },
    )
    _write_event_log(path, events)

    with pytest.raises(review.ReviewError, match="contains tool use"):
        review.check_event_log(path)


@pytest.mark.parametrize("mutation", ["failed", "missing_message", "two_messages"])
def test_event_log_rejects_failed_or_ambiguous_completion(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "codex.stdout"
    events = _valid_event_log()
    if mutation == "failed":
        events[-1] = {"type": "turn.failed", "error": {}}
    elif mutation == "missing_message":
        events.pop(-2)
    else:
        events.insert(-1, events[-2])
    _write_event_log(path, events)

    with pytest.raises(review.ReviewError, match="event log is invalid"):
        review.check_event_log(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "concatenated",
        "duplicate_thread",
        "duplicate_turn_start",
        "duplicate_turn_complete",
        "out_of_order",
        "after_completion",
    ],
)
def test_event_log_requires_one_strictly_ordered_invocation(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "codex.stdout"
    events = _valid_event_log()
    if mutation == "concatenated":
        events += _valid_event_log()
    elif mutation == "duplicate_thread":
        events.insert(1, events[0])
    elif mutation == "duplicate_turn_start":
        events.insert(2, events[1])
    elif mutation == "duplicate_turn_complete":
        events.insert(-1, events[-1])
    elif mutation == "out_of_order":
        events[0], events[1] = events[1], events[0]
    else:
        events.append(
            {
                "type": "item.completed",
                "item": {"id": "late", "type": "reasoning"},
            }
        )
    _write_event_log(path, events)

    with pytest.raises(review.ReviewError, match="event log is invalid"):
        review.check_event_log(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "incomplete",
        "changed_type",
        "changed_id",
        "updated_without_start",
        "updated_after_completion",
    ],
)
def test_event_log_requires_stable_completed_item_lifecycles(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "codex.stdout"
    events = _valid_event_log()
    if mutation == "incomplete":
        events.insert(
            -1,
            {
                "type": "item.started",
                "item": {"id": "active", "type": "reasoning"},
            },
        )
    elif mutation == "changed_type":
        events[2:3] = [
            {
                "type": "item.started",
                "item": {"id": "item-1", "type": "reasoning"},
            },
            {
                "type": "item.completed",
                "item": {"id": "item-1", "type": "agent_message"},
            },
        ]
    elif mutation == "changed_id":
        events[2:3] = [
            {
                "type": "item.started",
                "item": {"id": "first-id", "type": "reasoning"},
            },
            {
                "type": "item.completed",
                "item": {"id": "second-id", "type": "reasoning"},
            },
        ]
    elif mutation == "updated_without_start":
        events.insert(
            2,
            {
                "type": "item.updated",
                "item": {"id": "unknown", "type": "reasoning"},
            },
        )
    else:
        events.insert(
            -1,
            {
                "type": "item.updated",
                "item": {"id": "item-1", "type": "reasoning"},
            },
        )
    _write_event_log(path, events)

    with pytest.raises(review.ReviewError, match="event log is invalid"):
        review.check_event_log(path)


def test_event_log_rejects_public_file(tmp_path: Path) -> None:
    path = tmp_path / "codex.stdout"
    _write_event_log(path, _valid_event_log())
    path.chmod(0o644)

    with pytest.raises(review.ReviewError, match="not a regular file"):
        review.check_event_log(path)


def test_event_log_rejects_malformed_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "codex.stdout"
    path.write_text('{"type":"thread.started"}\n{"type":')
    path.chmod(0o600)

    with pytest.raises(review.ReviewError, match="event log is invalid"):
        review.check_event_log(path)


def test_check_events_cli_emits_only_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "private-account" / "codex.stdout"
    path.parent.mkdir()
    _write_event_log(path, _valid_event_log())

    assert review.review_cli(["check-events", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == '{"agent_messages":1,"events":5}\n'
    assert str(path) not in captured.out


def test_native_codex_preflight_accepts_exact_native_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"native")
    executable.chmod(0o700)
    observed: dict[str, object] = {}

    def version(args: list[str], **kwargs: object) -> SimpleNamespace:
        observed["args"] = args
        observed["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.146.0\n")

    monkeypatch.setattr(review.subprocess, "run", version)

    assert review.preflight_native_codex(executable) == {
        "payload": "native",
        "version": "codex-cli 0.146.0",
    }
    assert observed["args"] == [str(executable.resolve()), "--version"]
    assert observed["env"] == {"PATH": os.defpath, "LANG": "C.UTF-8"}


@pytest.mark.parametrize(
    "payload",
    [b"#!/usr/bin/env node\n", b"#!/bin/sh\n", b"not executable payload"],
)
def test_native_codex_preflight_rejects_script_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(payload)
    executable.chmod(0o700)
    monkeypatch.setattr(
        review.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("script payload must not execute"),
    )

    with pytest.raises(review.ReviewError, match="native Codex 0.146"):
        review.preflight_native_codex(executable)


def test_native_codex_preflight_rejects_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"native-one")
    executable.chmod(0o700)

    def swapped_version(args: list[str], **_kwargs: object) -> SimpleNamespace:
        path = Path(args[0])
        path.unlink()
        path.write_bytes(b"\xcf\xfa\xed\xfe" + b"native-two")
        path.chmod(0o700)
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.146.0\n")

    monkeypatch.setattr(review.subprocess, "run", swapped_version)

    with pytest.raises(review.ReviewError, match="native Codex 0.146"):
        review.preflight_native_codex(executable)


def test_native_codex_preflight_rejects_symlink_to_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "personal-native"
    target.write_bytes(b"\xcf\xfa\xed\xfe" + b"native")
    target.chmod(0o700)
    executable = tmp_path / "codex"
    executable.symlink_to(target)
    monkeypatch.setattr(
        review.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("symlink payload must not execute"),
    )

    with pytest.raises(review.ReviewError, match="native Codex 0.146"):
        review.preflight_native_codex(executable)


def test_judgment_operation_rejects_non_object_judge() -> None:
    artifact = {"target": TARGET, "judge": []}
    operation = {
        "schema": review._OPERATION_SCHEMA,  # noqa: SLF001
        "kind": "judgment_import",
        "matrix_id": TARGET["matrix_id"],
        "work_item_id": WORK_ITEM_ID,
        "judge_identifier": "judge-1",
        "attempt_count": 1,
        "duration_ms": 1,
        "usage": {"state": "unavailable"},
        "isolation_attestation": review._ISOLATION_ATTESTATION,  # noqa: SLF001
        "case_sha256": review._sha256(_case()),  # noqa: SLF001
        "artifact_sha256": review._sha256(artifact),  # noqa: SLF001
        "artifact": artifact,
        "recorded_at": "2026-08-04T12:00:00Z",
    }

    with pytest.raises(review.ReviewError, match="operation is invalid"):
        review._validate_judgment_operation(  # noqa: SLF001
            operation, case=_case(), judge_identifier="judge-1"
        )


@pytest.mark.parametrize(
    "recorded_at",
    [None, 7, "", "not-a-timestamp", "2026-08-04T12:00:00"],
)
def test_operation_validators_reject_invalid_recorded_at(recorded_at: object) -> None:
    judgment_artifact = {
        "target": TARGET,
        "judge": run_state.CALIBRATED_JUDGE_CONFIGURATIONS[0].to_dict(),
    }
    judgment_operation = {
        "schema": review._OPERATION_SCHEMA,  # noqa: SLF001
        "kind": "judgment_import",
        "matrix_id": TARGET["matrix_id"],
        "work_item_id": WORK_ITEM_ID,
        "judge_identifier": "judge-1",
        "attempt_count": 1,
        "duration_ms": 1,
        "usage": {"state": "unavailable"},
        "isolation_attestation": review._ISOLATION_ATTESTATION,  # noqa: SLF001
        "case_sha256": review._sha256(_case()),  # noqa: SLF001
        "artifact_sha256": review._sha256(judgment_artifact),  # noqa: SLF001
        "artifact": judgment_artifact,
        "recorded_at": recorded_at,
    }
    adjudication_artifact = {"target": TARGET}
    adjudication_operation = {
        "schema": review._OPERATION_SCHEMA,  # noqa: SLF001
        "kind": "adjudication_import",
        "matrix_id": TARGET["matrix_id"],
        "work_item_id": WORK_ITEM_ID,
        "case_sha256": review._sha256(_case()),  # noqa: SLF001
        "artifact_sha256": review._sha256(adjudication_artifact),  # noqa: SLF001
        "artifact": adjudication_artifact,
        "recorded_at": recorded_at,
    }

    with pytest.raises(review.ReviewError, match="operation is invalid"):
        review._validate_judgment_operation(  # noqa: SLF001
            judgment_operation, case=_case(), judge_identifier="judge-1"
        )
    with pytest.raises(review.ReviewError, match="operation is invalid"):
        review._validate_adjudication_operation(  # noqa: SLF001
            adjudication_operation, case=_case()
        )


def test_operation_validators_accept_timezone_aware_recorded_at() -> None:
    artifact = {"target": TARGET}
    operation = {
        "schema": review._OPERATION_SCHEMA,  # noqa: SLF001
        "kind": "adjudication_import",
        "matrix_id": TARGET["matrix_id"],
        "work_item_id": WORK_ITEM_ID,
        "case_sha256": review._sha256(_case()),  # noqa: SLF001
        "artifact_sha256": review._sha256(artifact),  # noqa: SLF001
        "artifact": artifact,
        "recorded_at": "2026-08-04T12:00:00-06:00",
    }

    assert review._validate_adjudication_operation(  # noqa: SLF001
        operation, case=_case()
    ) == artifact


def test_restricted_judge_profile_has_no_host_or_runtime_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    rules = codex_driver._permission_filesystem_rules(  # noqa: SLF001
        include_runtime_roots=False
    )
    assert rules == {
        ":root": "deny",
        ":minimal": "read",
        ":tmpdir": "deny",
        ":slash_tmp": "deny",
    }
    args = codex_driver._app_server_process_args(  # noqa: SLF001
        "codex",
        workspace,
        include_runtime_roots=False,
        approval_policy="never",
    )
    overrides = [args[index + 1] for index, item in enumerate(args) if item == "-c"]
    assert 'default_permissions="steam-agent-eval"' in overrides
    assert 'approval_policy="never"' in overrides
    assert (
        "permissions.steam-agent-eval="
        + codex_driver._permission_profile_toml(  # noqa: SLF001
            include_runtime_roots=False
        )
        in overrides
    )
    assert 'shell_environment_policy.inherit="core"' in overrides
    assert (
        "shell_environment_policy.include_only="
        + json.dumps(
            list(codex_driver._SHELL_ENV_INCLUDE_ONLY),  # noqa: SLF001
            separators=(",", ":"),
        )
        in overrides
    )


def test_documented_judge_profile_matches_runner_configuration() -> None:
    documentation = (ROOT / "evals" / "README.md").read_text()
    profile = (
        f"permissions.{codex_driver._PERMISSION_PROFILE}="  # noqa: SLF001
        + codex_driver._permission_profile_toml(  # noqa: SLF001
            include_runtime_roots=False
        )
    )
    assert profile in documentation
    assert 'approval_policy="never"' in documentation
    assert "umask 077" in documentation
    assert 'JUDGE_ROOT="$(mktemp -d /tmp/steam-agent-judge.XXXXXX)"' in documentation
    assert 'test "$(stat -f \'%Lp\' "$JUDGE_ROOT")" = 700' in documentation
    assert 'CODEX_BIN="$JUDGE_ROOT/codex"' in documentation
    assert 'cp -c "$SOURCE_CODEX_BIN" "$CODEX_BIN"' in documentation
    assert 'review preflight-codex "$CODEX_BIN"' in documentation
    assert "npm/JavaScript launchers" in documentation
    assert "symlinks, and other scripts fail closed" in documentation
    assert "Never reuse that root or" in documentation
    assert '"$SOURCE_REVIEW_ROOT/cases/WORK_ITEM_ID-judge-1.json"' in documentation
    assert '--output-schema "$SCHEMA_PATH"' in documentation
    assert '- < "$CASE_PATH"' in documentation
    assert 'install -m 600 /dev/null "$VERDICT_PATH"' in documentation
    assert 'test "$(stat -f \'%Lp\' "$VERDICT_PATH")" = 600' in documentation
    assert '>"$STDOUT_LOG" 2>"$STDERR_LOG"' in documentation
    assert 'HOME="$JUDGE_ROOT/workspace"' in documentation
    for feature in review._HOST_ISOLATION_DISABLED_FEATURES:  # noqa: SLF001
        assert f"--disable {feature}" in documentation
    assert "--config 'agents.enabled=false'" in documentation
    assert "--config 'tools.update_plan.enabled=false'" in documentation
    assert (
        "--config 'tools.experimental_request_user_input.enabled=false'"
        in documentation
    )
    assert 'exec --json --ephemeral' in documentation
    assert 'review check-events "$STDOUT_LOG"' in documentation
    assert "tool-free" not in documentation
    invocation = documentation.split(
        "Then invoke the judge with the isolated environment:", maxsplit=1
    )[1].split("Import the result", maxsplit=1)[0]
    assert "SOURCE_REVIEW_ROOT" not in invocation
    assert "SOURCE_CODEX_BIN" not in invocation
    assert "/operator-owned" not in invocation
    assert "/Users/" not in invocation
    assert "PATH=/usr/bin:/bin" in invocation
    assert "--sandbox read-only" not in documentation
    assert review._ISOLATION_ATTESTATION in documentation  # noqa: SLF001


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("codex") is None,
    reason="requires the pinned macOS Codex sandbox",
)
def test_restricted_judge_profile_live_denies_auth_and_host_tmp(
    tmp_path: Path,
) -> None:
    executable = shutil.which("codex")
    assert executable is not None
    version = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        version.returncode != 0
        or version.stdout.strip() != codex_driver._REQUIRED_CODEX_VERSION  # noqa: SLF001
    ):
        pytest.skip("requires the pinned Codex version")
    isolated_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    isolated_home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    auth = isolated_home / "auth.json"
    auth.write_text('{"token":"synthetic"}')
    auth.chmod(0o600)
    host_secret = tmp_path / "host-secret"
    host_secret.write_text("private")
    host_secret.chmod(0o600)
    profile_override = (
        f"permissions.{codex_driver._PERMISSION_PROFILE}="  # noqa: SLF001
        + codex_driver._permission_profile_toml(  # noqa: SLF001
            include_runtime_roots=False
        )
    )
    environment = {
        "CODEX_HOME": str(isolated_home),
        "HOME": str(workspace),
        "TMPDIR": str(isolated_home),
        "PATH": os.defpath,
    }
    result = subprocess.run(
        [
            executable,
            "sandbox",
            "-C",
            str(workspace),
            "-P",
            codex_driver._PERMISSION_PROFILE,  # noqa: SLF001
            "-c",
            "default_permissions=" + json.dumps(codex_driver._PERMISSION_PROFILE),  # noqa: SLF001
            "-c",
            profile_override,
            "--",
            "/bin/sh",
            "-c",
            'test ! -r "$CODEX_HOME/auth.json" && test ! -r "$1" && touch allowed',
            "judge-canary",
            str(host_secret),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (workspace / "allowed").is_file()


@pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="requires the pinned Codex App Server",
)
def test_no_shell_judge_profile_live_config_preflight(tmp_path: Path) -> None:
    executable = shutil.which("codex")
    assert executable is not None
    version = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        version.returncode != 0
        or version.stdout.strip() != codex_driver._REQUIRED_CODEX_VERSION  # noqa: SLF001
    ):
        pytest.skip("requires the pinned Codex version")
    isolated_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    isolated_home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    process_args = codex_driver._app_server_process_args(  # noqa: SLF001
        executable,
        workspace,
        include_runtime_roots=False,
        approval_policy="never",
    )
    for feature in review._HOST_ISOLATION_DISABLED_FEATURES:  # noqa: SLF001
        process_args.extend(("--disable", feature))
    process_args.extend(
        (
            "-c",
            "agents.enabled=false",
            "-c",
            "tools.update_plan.enabled=false",
            "-c",
            "tools.experimental_request_user_input.enabled=false",
        )
    )
    process = subprocess.Popen(
        process_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=workspace,
        env=codex_driver._app_server_environment(  # noqa: SLF001
            isolated_home, workspace
        ),
        start_new_session=True,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        session = codex_driver._Session(  # noqa: SLF001
            process.stdin, process.stdout, 30
        )
        session.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "steam-agent-review-test",
                    "title": "Steam Agent qualitative review test",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        session.notify("initialized", {})
        codex_driver._validate_external_tool_boundary(  # noqa: SLF001
            session,
            str(workspace),
            include_runtime_roots=False,
            approval_policy="never",
        )
        response = session.request(
            "config/read", {"cwd": str(workspace), "includeLayers": False}
        )
        config = response.get("config")
        assert isinstance(config, dict)
        features = config.get("features")
        agents = config.get("agents")
        assert isinstance(features, dict)
        assert isinstance(agents, dict)
        assert all(
            features.get(feature) is False
            for feature in review._HOST_ISOLATION_DISABLED_FEATURES  # noqa: SLF001
        )
        assert agents.get("enabled") is False
        # Codex 0.146 omits these two accepted tool settings from config/read.
        # Strict-config initialization proves syntax support, not effective
        # model-visible inventory; source/wire evidence supplies that boundary.
    finally:
        codex_driver._terminate_process_group(process)  # noqa: SLF001


def test_assemble_wraps_external_verdicts_and_records_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
    matrix_dir = _matrix_root(tmp_path / "matrix")
    result = _result()
    index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: index)
    monkeypatch.setattr(review, "_validate_review_root", lambda *_args: ledger)
    monkeypatch.setattr(review, "_load_bound_case", lambda *_args: _case())
    monkeypatch.setattr(
        review.judge, "_validate_judgment_document", lambda *_args: None
    )
    imported: list[dict[str, object]] = []

    def fake_import(_matrix: Path, kind: str, document: dict[str, object]):
        imported.append(document)
        return Path(f"{kind}-retained.json"), review._sha256(document)  # noqa: SLF001

    monkeypatch.setattr(review, "_import_document_locked", fake_import)
    verdicts = tmp_path / "verdicts.json"
    review._write_json(verdicts, _verdict_document())  # noqa: SLF001

    output = review.assemble_judgment(
        matrix_dir,
        review_dir,
        WORK_ITEM_ID,
        verdicts,
        judge_identifier="judge-1",
        attempt_count=2,
        duration_ms=1234,
        isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
    )

    assert output["path"] == "judgment-retained.json"
    assert [item["criterion_id"] for item in imported[0]["verdicts"]] == [
        "clear",
        "aligned",
    ]
    operation = review._read_json(  # noqa: SLF001
        review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-1.json",
        require_private=True,
    )
    assert operation["attempt_count"] == 2
    assert operation["duration_ms"] == 1234
    assert operation["usage"] == {"state": "unavailable"}
    assert operation["isolation_attestation"] == review._ISOLATION_ATTESTATION  # noqa: SLF001
    assert operation["case_sha256"] == review._sha256(_case())  # noqa: SLF001


def _mock_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
    matrix_dir = _matrix_root(tmp_path / "matrix")
    index = SimpleNamespace(inspection_result=_result())
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: index)
    monkeypatch.setattr(review, "_validate_review_root", lambda *_args: ledger)
    monkeypatch.setattr(
        review,
        "_load_bound_case",
        lambda _matrix, _review, _index, _ledger, _work_item, judge_identifier: (
            _case(judge_identifier)
        ),
    )
    monkeypatch.setattr(
        review.judge, "_validate_judgment_document", lambda *_args: None
    )
    return matrix_dir, review_dir


def test_assemble_rejects_verdicts_bound_to_another_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir, review_dir = _mock_assembly(tmp_path, monkeypatch)
    verdicts = _verdict_document()
    verdicts["target"] = {
        "work_item_id": "w-000001-fedcba9876543210",
        "projection_sha256": "6" * 64,
    }
    verdicts_path = tmp_path / "verdicts.json"
    review._write_json(verdicts_path, verdicts)  # noqa: SLF001

    with pytest.raises(review.ReviewError, match="different case"):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            verdicts_path,
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=1,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )
    assert not (
        review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-1.json"
    ).exists()


def test_assemble_rejects_verdicts_bound_to_another_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir, review_dir = _mock_assembly(tmp_path, monkeypatch)
    verdicts_path = tmp_path / "judge-1-verdicts.json"
    review._write_json(  # noqa: SLF001
        verdicts_path, _verdict_document("judge-1")
    )

    with pytest.raises(review.ReviewError, match="different invocation"):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            verdicts_path,
            judge_identifier="judge-2",
            attempt_count=1,
            duration_ms=1,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )
    assert not (
        review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-2.json"
    ).exists()


def test_assemble_rejects_unconfigured_judge_before_resolving_operation_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir, review_dir = _mock_assembly(tmp_path, monkeypatch)
    escaped = review_dir / "escaped.json"
    review._write_json(escaped, {"private": True})  # noqa: SLF001

    with pytest.raises(review.ReviewError, match="judge is not configured"):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            tmp_path / "missing-verdicts.json",
            judge_identifier="../escaped",
            attempt_count=1,
            duration_ms=1,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )
    assert review._read_json(escaped) == {"private": True}  # noqa: SLF001


def test_assemble_resumes_from_operation_after_verdict_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir, review_dir = _mock_assembly(tmp_path, monkeypatch)
    verdicts_path = tmp_path / "verdicts.json"
    review._write_json(verdicts_path, _verdict_document())  # noqa: SLF001
    calls = 0

    def interrupted_import(
        _matrix: Path, kind: str, document: dict[str, object]
    ) -> tuple[Path, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated interruption")
        return Path(f"{kind}-retained.json"), review._sha256(document)  # noqa: SLF001

    monkeypatch.setattr(review, "_import_document_locked", interrupted_import)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            verdicts_path,
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=10,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )
    operation_path = review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-1.json"
    assert operation_path.is_file()
    verdicts_path.unlink()

    output = review.assemble_judgment(
        matrix_dir,
        review_dir,
        WORK_ITEM_ID,
        verdicts_path,
        judge_identifier="judge-1",
        attempt_count=1,
        duration_ms=10,
        isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
    )
    assert output["path"] == "judgment-retained.json"
    assert calls == 2


@pytest.mark.parametrize("retained_state", ["mismatch", "duplicate"])
def test_assemble_resume_requires_one_exact_semantic_judgment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retained_state: str,
) -> None:
    matrix_dir, review_dir = _mock_assembly(tmp_path, monkeypatch)
    verdicts_path = tmp_path / "verdicts.json"
    review._write_json(verdicts_path, _verdict_document())  # noqa: SLF001
    monkeypatch.setattr(
        review,
        "_import_document_locked",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("simulated interruption")),
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            verdicts_path,
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=10,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )
    operation = review._read_json(  # noqa: SLF001
        review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-1.json",
        require_private=True,
    )
    retained_document = json.loads(json.dumps(operation["artifact"]))
    if retained_state == "mismatch":
        retained_document["created_at"] = "2026-08-04T12:00:00Z"
    digest = review._sha256(retained_document)  # noqa: SLF001
    monkeypatch.setattr(
        review.judge,
        "_retained_judgments",
        lambda *_args: {digest: retained_document},
    )
    retained_files = [(Path("alternate-name.json"), digest, retained_document)]
    if retained_state == "duplicate":
        retained_files.append((Path("duplicate-name.json"), digest, retained_document))
    monkeypatch.setattr(
        review,
        "_retained_judgment_files",
        lambda *_args: retained_files,
    )
    verdicts_path.unlink()

    with pytest.raises(
        review.ReviewError,
        match="does not match review operation|roster is ambiguous",
    ):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            verdicts_path,
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=10,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )


def test_assemble_detects_target_conflict_before_publishing_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir, review_dir = _mock_assembly(tmp_path, monkeypatch)
    monkeypatch.setattr(review.judge, "_retained_judgments", lambda *_args: {})
    monkeypatch.setattr(review, "_retained_judgment_files", lambda *_args: [])
    verdicts_path = tmp_path / "verdicts.json"
    review._write_json(verdicts_path, _verdict_document())  # noqa: SLF001
    judgments = matrix_dir / "judgments"
    judgments.mkdir(mode=0o700)
    target = judgments / f"judgment-{WORK_ITEM_ID}-judge-1.json"
    target.write_text("{}")
    target.chmod(0o600)

    with pytest.raises(review.ReviewError, match="target already exists"):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            verdicts_path,
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=1,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )
    assert not (
        review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-1.json"
    ).exists()


def test_assemble_rejects_more_than_initial_plus_two_retries(tmp_path: Path) -> None:
    with pytest.raises(review.ReviewError, match="operational measurement"):
        review.assemble_judgment(
            tmp_path / "matrix",
            tmp_path / "review",
            WORK_ITEM_ID,
            tmp_path / "verdicts.json",
            judge_identifier="judge-1",
            attempt_count=4,
            duration_ms=1,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )


@pytest.mark.parametrize(
    ("attempt_count", "duration_ms"),
    [(True, 1), (1, False), (1.0, 1), (1, 1.0)],
)
def test_assemble_rejects_non_integer_operational_measurements(
    tmp_path: Path, attempt_count: object, duration_ms: object
) -> None:
    with pytest.raises(review.ReviewError, match="operational measurement"):
        review.assemble_judgment(
            tmp_path / "matrix",
            tmp_path / "review",
            WORK_ITEM_ID,
            tmp_path / "verdicts.json",
            judge_identifier="judge-1",
            attempt_count=attempt_count,  # type: ignore[arg-type]
            duration_ms=duration_ms,  # type: ignore[arg-type]
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )


def test_assemble_rejects_public_external_verdict_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir, review_dir = _mock_assembly(tmp_path, monkeypatch)
    verdicts_path = tmp_path / "verdicts.json"
    verdicts_path.write_bytes(  # deliberately bypass the private writer
        review._canonical_bytes(_verdict_document())  # noqa: SLF001
    )
    verdicts_path.chmod(0o644)

    with pytest.raises(review.ReviewError, match="not a regular file"):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            verdicts_path,
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=1,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )


def test_assemble_rejects_semantic_same_target_judge_before_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir, review_dir = _mock_assembly(tmp_path, monkeypatch)
    retained_document = {
        "target": TARGET,
        "judge": run_state.CALIBRATED_JUDGE_CONFIGURATIONS[0].to_dict(),
    }
    digest = review._sha256(retained_document)  # noqa: SLF001
    monkeypatch.setattr(
        review.judge,
        "_retained_judgments",
        lambda *_args: {digest: retained_document},
    )
    monkeypatch.setattr(
        review,
        "_retained_judgment_files",
        lambda *_args: [(Path("alternate-name.json"), digest, retained_document)],
    )

    with pytest.raises(review.ReviewError, match="already exists for judge"):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            tmp_path / "missing-verdicts.json",
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=1,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )
    assert not (
        review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-1.json"
    ).exists()


def test_policy_invalid_response_does_not_poison_corrected_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
    matrix_dir = _matrix_root(tmp_path / "matrix")
    result = _result()
    target_index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: target_index)
    monkeypatch.setattr(review, "_validate_review_root", lambda *_args: ledger)
    monkeypatch.setattr(review, "_load_bound_case", lambda *_args: _case())

    def validate(_index: object, document: dict[str, object]) -> None:
        if document["verdicts"][0]["rationale"] == "Claims passed.":
            raise review.judge.JudgmentError(
                "qualitative rationale contains deterministic outcome material"
            )

    monkeypatch.setattr(review.judge, "_validate_judgment_document", validate)
    monkeypatch.setattr(
        review,
        "_import_document_locked",
        lambda _matrix, kind, document: (
            Path(f"{kind}-retained.json"),
            review._sha256(document),  # noqa: SLF001
        ),
    )
    invalid = tmp_path / "invalid.json"
    corrected = tmp_path / "corrected.json"
    for path, rationale in (
        (invalid, "Claims passed."),
        (corrected, "Answer is clear."),
    ):
        review._write_json(  # noqa: SLF001
            path,
            {
                "schema": review._VERDICTS_SCHEMA,  # noqa: SLF001
                "target": {
                    "work_item_id": WORK_ITEM_ID,
                    "projection_sha256": TARGET["projection_sha256"],
                },
                "invocation": review._invocation_binding(  # noqa: SLF001
                    TARGET, "judge-1"
                ),
                "verdicts": [
                    {
                        "criterion_id": "clear",
                        "verdict": "pass",
                        "rationale": rationale,
                    },
                    {
                        "criterion_id": "aligned",
                        "verdict": "pass",
                        "rationale": "Claims align.",
                    },
                ],
            },
        )

    with pytest.raises(review.judge.JudgmentError, match="deterministic outcome"):
        review.assemble_judgment(
            matrix_dir,
            review_dir,
            WORK_ITEM_ID,
            invalid,
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=100,
            isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
        )
    operation = review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-1.json"
    assert not operation.exists()

    output = review.assemble_judgment(
        matrix_dir,
        review_dir,
        WORK_ITEM_ID,
        corrected,
        judge_identifier="judge-1",
        attempt_count=2,
        duration_ms=200,
        isolation_attestation=review._ISOLATION_ATTESTATION,  # noqa: SLF001
    )
    assert output["path"] == "judgment-retained.json"
    assert operation.exists()


def _mock_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
    matrix_dir = _matrix_root(tmp_path / "matrix")
    target_index = SimpleNamespace(inspection_result=_result())
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: target_index)
    monkeypatch.setattr(review, "_validate_review_root", lambda *_args: ledger)
    monkeypatch.setattr(
        review,
        "_load_bound_case",
        lambda _matrix, _review, _index, _ledger, _work_item, judge_identifier: (
            _case(judge_identifier)
        ),
    )
    judgments = {}
    for configured in run_state.CALIBRATED_JUDGE_CONFIGURATIONS:
        document = {
            "target": TARGET,
            "judge": configured.to_dict(),
            "verdicts": [
                {
                    "criterion_id": "clear",
                    "verdict": "pass",
                    "rationale": "Clear verdict.",
                },
                {
                    "criterion_id": "aligned",
                    "verdict": "pass",
                    "rationale": "Claims align.",
                },
            ],
        }
        digest = review._sha256(document)  # noqa: SLF001
        judgments[digest] = document
        operation = {
            "schema": review._OPERATION_SCHEMA,  # noqa: SLF001
            "kind": "judgment_import",
            "matrix_id": TARGET["matrix_id"],
            "work_item_id": WORK_ITEM_ID,
            "judge_identifier": configured.identifier,
            "attempt_count": 1,
            "duration_ms": 100,
            "usage": {"state": "unavailable"},
            "isolation_attestation": review._ISOLATION_ATTESTATION,  # noqa: SLF001
            "case_sha256": review._sha256(  # noqa: SLF001
                _case(configured.identifier)
            ),
            "artifact_sha256": digest,
            "artifact": document,
            "recorded_at": "2026-08-04T12:00:00Z",
        }
        review._write_json(  # noqa: SLF001
            review_dir
            / "operations"
            / f"judgment-{WORK_ITEM_ID}-{configured.identifier}.json",
            operation,
        )
    monkeypatch.setattr(review.judge, "_retained_judgments", lambda *_args: judgments)
    monkeypatch.setattr(
        review,
        "_retained_judgment_files",
        lambda *_args: [
            (Path(f"{digest}.json"), digest, document)
            for digest, document in judgments.items()
        ],
    )
    monkeypatch.setattr(
        review.judge, "_validate_adjudication_document", lambda *_args, **_kwargs: None
    )
    return matrix_dir, review_dir


def test_resolve_detects_target_conflict_before_publishing_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir, review_dir = _mock_resolution(tmp_path, monkeypatch)
    monkeypatch.setattr(review, "_retained_adjudication_files", lambda *_args: [])
    adjudications = matrix_dir / "adjudications"
    adjudications.mkdir(mode=0o700)
    target = adjudications / f"adjudication-{WORK_ITEM_ID}.json"
    target.write_text("{}")
    target.chmod(0o600)

    with pytest.raises(review.ReviewError, match="target already exists"):
        review.resolve_agreement(matrix_dir, review_dir)
    assert not (
        review_dir / "operations" / f"adjudication-{WORK_ITEM_ID}-agreement.json"
    ).exists()


def test_resolve_preflights_every_roster_before_first_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_work_item = "w-000001-fedcba9876543210"
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
    matrix_dir = _matrix_root(tmp_path / "matrix")
    campaign = _campaign()
    result = SimpleNamespace(
        manifest=SimpleNamespace(
            matrix_id="matrix-test",
            campaign=campaign,
            work_items=(
                SimpleNamespace(work_item_id=WORK_ITEM_ID),
                SimpleNamespace(work_item_id=second_work_item),
            ),
        )
    )
    target_index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: target_index)
    monkeypatch.setattr(review, "_validate_review_root", lambda *_args: ledger)

    def bound_case(
        _matrix: Path,
        _review: Path,
        _index: object,
        _ledger: object,
        work_item_id: str,
        judge_identifier: str,
    ) -> dict[str, object]:
        case = json.loads(json.dumps(_case(judge_identifier)))
        target = dict(TARGET, work_item_id=work_item_id)
        case["target"] = target
        case["execution"]["invocation"] = review._invocation_binding(  # noqa: SLF001
            target, judge_identifier
        )
        return case

    monkeypatch.setattr(review, "_load_bound_case", bound_case)
    monkeypatch.setattr(review.judge, "_retained_judgments", lambda *_args: {})
    monkeypatch.setattr(review, "_retained_judgment_files", lambda *_args: [])
    monkeypatch.setattr(review, "_retained_adjudication_files", lambda *_args: [])

    def roster(
        _review: Path,
        *,
        cases_by_judge: dict[str, dict[str, object]],
        campaign: object,
        files: object,
    ) -> dict[str, tuple[str, dict[str, object]]]:
        del files
        case = next(iter(cases_by_judge.values()))
        if case["target"]["work_item_id"] == second_work_item:
            raise review.ReviewError("later qualitative roster is invalid")
        return {
            configured.identifier: (
                configured.identifier[-1] * 64,
                {
                    "target": case["target"],
                    "judge": configured.to_dict(),
                    "verdicts": [
                        {
                            "criterion_id": criterion["id"],
                            "verdict": "pass",
                            "rationale": "Valid roster.",
                        }
                        for criterion in case["projection"]["criteria"]
                    ],
                },
            )
            for configured in campaign.judges
        }

    monkeypatch.setattr(review, "_bound_judgment_roster", roster)
    monkeypatch.setattr(
        review.judge, "_validate_adjudication_document", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        review,
        "_publish_operation",
        lambda *_args: pytest.fail("preflight failure must precede operation append"),
    )
    monkeypatch.setattr(
        review,
        "_import_document_locked",
        lambda *_args: pytest.fail("preflight failure must precede artifact append"),
    )

    with pytest.raises(review.ReviewError, match="later qualitative roster"):
        review.resolve_agreement(matrix_dir, review_dir)
    assert list((review_dir / "operations").iterdir()) == []
    assert not (matrix_dir / "adjudications").exists()


def test_resolve_resumes_adjudication_from_operation_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir, review_dir = _mock_resolution(tmp_path, monkeypatch)
    calls = 0

    def interrupted_import(
        _matrix: Path, kind: str, document: dict[str, object]
    ) -> tuple[Path, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated interruption")
        return Path(f"{kind}-retained.json"), review._sha256(document)  # noqa: SLF001

    monkeypatch.setattr(review, "_import_document_locked", interrupted_import)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        review.resolve_agreement(matrix_dir, review_dir)
    operation_path = (
        review_dir / "operations" / f"adjudication-{WORK_ITEM_ID}-agreement.json"
    )
    assert operation_path.is_file()

    assert review.resolve_agreement(matrix_dir, review_dir) == {
        "imported": 0,
        "retained": 1,
    }
    assert calls == 2


def test_resolve_repreflights_all_rosters_before_partial_phase_two_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_work_item = "w-000001-fedcba9876543210"
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
    matrix_dir = _matrix_root(tmp_path / "matrix")
    campaign = _campaign()
    result = SimpleNamespace(
        manifest=SimpleNamespace(
            matrix_id="matrix-test",
            campaign=campaign,
            work_items=(
                SimpleNamespace(work_item_id=WORK_ITEM_ID),
                SimpleNamespace(work_item_id=second_work_item),
            ),
        )
    )
    target_index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: target_index)
    monkeypatch.setattr(review, "_validate_review_root", lambda *_args: ledger)

    def bound_case(
        _matrix: Path,
        _review: Path,
        _index: object,
        _ledger: object,
        work_item_id: str,
        judge_identifier: str,
    ) -> dict[str, object]:
        case = json.loads(json.dumps(_case(judge_identifier)))
        target = dict(TARGET, work_item_id=work_item_id)
        case["target"] = target
        case["execution"]["invocation"] = review._invocation_binding(  # noqa: SLF001
            target, judge_identifier
        )
        return case

    monkeypatch.setattr(review, "_load_bound_case", bound_case)
    monkeypatch.setattr(review.judge, "_retained_judgments", lambda *_args: {})
    monkeypatch.setattr(review, "_retained_judgment_files", lambda *_args: [])
    retained_adjudications: list[tuple[Path, str, dict[str, object]]] = []
    monkeypatch.setattr(
        review,
        "_retained_adjudication_files",
        lambda *_args: list(retained_adjudications),
    )
    roster_calls: list[str] = []

    def roster(
        _review: Path,
        *,
        cases_by_judge: dict[str, dict[str, object]],
        campaign: object,
        files: object,
    ) -> dict[str, tuple[str, dict[str, object]]]:
        del files
        case = next(iter(cases_by_judge.values()))
        roster_calls.append(case["target"]["work_item_id"])
        return {
            configured.identifier: (
                configured.identifier[-1] * 64,
                {
                    "target": case["target"],
                    "judge": configured.to_dict(),
                    "verdicts": [
                        {
                            "criterion_id": criterion["id"],
                            "verdict": "pass",
                            "rationale": "Valid roster.",
                        }
                        for criterion in case["projection"]["criteria"]
                    ],
                },
            )
            for configured in campaign.judges
        }

    monkeypatch.setattr(review, "_bound_judgment_roster", roster)
    monkeypatch.setattr(
        review.judge, "_validate_adjudication_document", lambda *_args, **_kwargs: None
    )
    import_calls = 0

    def crash_on_second_import(
        matrix_root: Path, kind: str, document: dict[str, object]
    ) -> tuple[Path, str]:
        nonlocal import_calls
        import_calls += 1
        if import_calls == 2:
            raise RuntimeError("simulated second import crash")
        artifact_root = matrix_root / "adjudications"
        artifact_root.mkdir(mode=0o700, exist_ok=True)
        path = artifact_root / f"{document['adjudication_id']}.json"
        review._write_json(path, document)  # noqa: SLF001
        digest = review._sha256(document)  # noqa: SLF001
        retained_adjudications.append((path, digest, document))
        assert kind == "adjudication"
        return path, digest

    monkeypatch.setattr(review, "_import_document_locked", crash_on_second_import)

    with pytest.raises(RuntimeError, match="second import crash"):
        review.resolve_agreement(matrix_dir, review_dir)
    assert roster_calls == [WORK_ITEM_ID, second_work_item]
    assert len(list((review_dir / "operations").glob("adjudication-*.json"))) == 2
    assert len(retained_adjudications) == 1

    assert review.resolve_agreement(matrix_dir, review_dir) == {
        "imported": 0,
        "retained": 2,
    }
    assert roster_calls == [
        WORK_ITEM_ID,
        second_work_item,
        WORK_ITEM_ID,
        second_work_item,
    ]
    assert len(retained_adjudications) == 2
    assert import_calls == 3


@pytest.mark.parametrize("mutation", ["missing", "altered"])
def test_adjudication_resume_requires_bound_judgment_operation_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    matrix_dir, review_dir = _mock_resolution(tmp_path, monkeypatch)
    calls = 0

    def interrupted_import(
        _matrix: Path, _kind: str, _document: dict[str, object]
    ) -> tuple[Path, str]:
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(review, "_import_document_locked", interrupted_import)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        review.resolve_agreement(matrix_dir, review_dir)
    judgment_operation = (
        review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-2.json"
    )
    if mutation == "missing":
        judgment_operation.unlink()
    else:
        operation = review._read_json(  # noqa: SLF001
            judgment_operation, require_private=True
        )
        operation["artifact_sha256"] = "f" * 64
        judgment_operation.write_bytes(  # deliberately replace retained bytes
            review._canonical_bytes(operation)  # noqa: SLF001
        )
        judgment_operation.chmod(0o600)

    with pytest.raises(review.ReviewError):
        review.resolve_agreement(matrix_dir, review_dir)
    assert calls == 1


def test_resolve_mechanically_preserves_disagreement_as_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
    matrix_dir = _matrix_root(tmp_path / "matrix")
    result = _result()
    index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: index)
    monkeypatch.setattr(review, "_validate_review_root", lambda *_args: ledger)
    monkeypatch.setattr(
        review,
        "_load_bound_case",
        lambda _matrix, _review, _index, _ledger, _work_item, judge_identifier: (
            _case(judge_identifier)
        ),
    )
    judgments = {}
    for index_value, configured in enumerate(run_state.CALIBRATED_JUDGE_CONFIGURATIONS):
        clear = "fail" if index_value == 2 else "pass"
        document = {
            "target": TARGET,
            "judge": configured.to_dict(),
            "verdicts": [
                {
                    "criterion_id": "clear",
                    "verdict": clear,
                    "rationale": "Clear verdict.",
                },
                {
                    "criterion_id": "aligned",
                    "verdict": "pass",
                    "rationale": "Claims align.",
                },
            ],
        }
        digest = review._sha256(document)  # noqa: SLF001
        judgments[digest] = document
        operation = {
            "schema": review._OPERATION_SCHEMA,  # noqa: SLF001
            "kind": "judgment_import",
            "matrix_id": TARGET["matrix_id"],
            "work_item_id": WORK_ITEM_ID,
            "judge_identifier": configured.identifier,
            "attempt_count": 1,
            "duration_ms": 100,
            "usage": {"state": "unavailable"},
            "isolation_attestation": review._ISOLATION_ATTESTATION,  # noqa: SLF001
            "case_sha256": review._sha256(  # noqa: SLF001
                _case(configured.identifier)
            ),
            "artifact_sha256": digest,
            "artifact": document,
            "recorded_at": "2026-08-04T12:00:00Z",
        }
        review._write_json(  # noqa: SLF001
            review_dir
            / "operations"
            / f"judgment-{WORK_ITEM_ID}-{configured.identifier}.json",
            operation,
        )
    monkeypatch.setattr(review.judge, "_retained_judgments", lambda *_args: judgments)
    monkeypatch.setattr(
        review,
        "_retained_judgment_files",
        lambda *_args: [
            (Path(f"{digest}.json"), digest, document)
            for digest, document in judgments.items()
        ],
    )
    monkeypatch.setattr(
        review.judge, "_validate_adjudication_document", lambda *_args, **_kwargs: None
    )
    imported: list[dict[str, object]] = []

    def fake_import(_matrix: Path, _kind: str, document: dict[str, object]):
        imported.append(document)
        return Path("adjudication-retained.json"), review._sha256(document)  # noqa: SLF001

    monkeypatch.setattr(review, "_import_document_locked", fake_import)

    output = review.resolve_agreement(matrix_dir, review_dir)

    assert output == {"imported": 1, "retained": 0}
    assert imported[0]["outcomes"] == [
        {"criterion_id": "clear", "outcome": "unresolved"},
        {"criterion_id": "aligned", "outcome": "pass"},
    ]

from __future__ import annotations

from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import review, run_state  # noqa: E402


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


def _case() -> dict[str, object]:
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
                "path": f"cases/{WORK_ITEM_ID}.json",
                "sha256": review._sha256(_case()),  # noqa: SLF001
            }
        ]
    }
    return ledger


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

    document = review._case_document(tmp_path, index, WORK_ITEM_ID)  # noqa: SLF001

    encoded = review._canonical_bytes(document)  # noqa: SLF001
    assert review.judge._validate_schema(document, "review-case-0.1.json") == document  # noqa: SLF001
    assert b"gpt-5.6-sol" not in encoded
    assert b'"route"' not in encoded
    assert b'"metrics"' not in encoded
    assert document["projection"] == PROJECTION
    assert document["execution"]["model_input"] == "this_document_verbatim"
    assert document["prompt"]["text"].startswith("# Matrix qualitative judge")


def test_prepare_publishes_private_cases_schema_and_bounded_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _result()
    index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: index)
    monkeypatch.setattr(review, "_case_document", lambda *_args: _case())
    monkeypatch.setattr(review.judge, "_validate_schema", lambda value, _name: value)
    matrix_dir = tmp_path / "matrix"
    matrix_dir.mkdir()
    review_dir = tmp_path / "prepared"

    output = review.prepare(matrix_dir, review_dir)

    assert output["cases"] == 1
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


def test_assemble_wraps_external_verdicts_and_records_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
    result = _result()
    index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: index)
    monkeypatch.setattr(review, "_validate_review_root", lambda *_args: ledger)
    monkeypatch.setattr(review, "_load_bound_case", lambda *_args: _case())
    monkeypatch.setattr(review.judge, "_validate_judgment_document", lambda *_args: None)
    imported: list[dict[str, object]] = []

    def fake_import(_matrix: Path, kind: str, document: dict[str, object]):
        imported.append(document)
        return Path(f"{kind}-retained.json"), review._sha256(document)  # noqa: SLF001

    monkeypatch.setattr(review, "_import_document", fake_import)
    verdicts = tmp_path / "verdicts.json"
    review._write_json(  # noqa: SLF001
        verdicts,
        {
            "schema": review._VERDICTS_SCHEMA,  # noqa: SLF001
            "verdicts": [
                {"criterion_id": "aligned", "verdict": "pass", "rationale": "Claims align."},
                {"criterion_id": "clear", "verdict": "pass", "rationale": "Answer is clear."},
            ],
        },
    )

    output = review.assemble_judgment(
        tmp_path / "matrix",
        review_dir,
        WORK_ITEM_ID,
        verdicts,
        judge_identifier="judge-1",
        attempt_count=2,
        duration_ms=1234,
        isolation_attestation="isolated-home-no-skills",
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
    assert operation["isolation_attestation"] == "isolated-home-no-skills"
    assert operation["case_sha256"] == review._sha256(_case())  # noqa: SLF001


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
            isolation_attestation="isolated-home-no-skills",
        )


def test_policy_invalid_response_does_not_poison_corrected_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
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
        "_import_document",
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
                "verdicts": [
                    {"criterion_id": "clear", "verdict": "pass", "rationale": rationale},
                    {"criterion_id": "aligned", "verdict": "pass", "rationale": "Claims align."},
                ],
            },
        )

    with pytest.raises(review.judge.JudgmentError, match="deterministic outcome"):
        review.assemble_judgment(
            tmp_path / "matrix",
            review_dir,
            WORK_ITEM_ID,
            invalid,
            judge_identifier="judge-1",
            attempt_count=1,
            duration_ms=100,
            isolation_attestation="isolated-home-no-skills",
        )
    operation = (
        review_dir / "operations" / f"judgment-{WORK_ITEM_ID}-judge-1.json"
    )
    assert not operation.exists()

    output = review.assemble_judgment(
        tmp_path / "matrix",
        review_dir,
        WORK_ITEM_ID,
        corrected,
        judge_identifier="judge-1",
        attempt_count=2,
        duration_ms=200,
        isolation_attestation="isolated-home-no-skills",
    )
    assert output["path"] == "judgment-retained.json"
    assert operation.exists()


def test_resolve_mechanically_preserves_disagreement_as_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_dir = tmp_path / "prepared"
    ledger = _review_root(review_dir)
    result = _result()
    index = SimpleNamespace(inspection_result=result)
    monkeypatch.setattr(review.judge, "_target_index", lambda _path: index)
    monkeypatch.setattr(review, "_validate_review_root", lambda *_args: ledger)
    monkeypatch.setattr(review, "_load_bound_case", lambda *_args: _case())
    judgments = {}
    for index_value, configured in enumerate(run_state.CALIBRATED_JUDGE_CONFIGURATIONS):
        clear = "fail" if index_value == 2 else "pass"
        document = {
            "target": TARGET,
            "judge": configured.to_dict(),
            "verdicts": [
                {"criterion_id": "clear", "verdict": clear, "rationale": "Clear verdict."},
                {"criterion_id": "aligned", "verdict": "pass", "rationale": "Claims align."},
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
            "isolation_attestation": "isolated-home-no-skills",
            "case_sha256": review._sha256(_case()),  # noqa: SLF001
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
        review.judge, "_validate_adjudication_document", lambda *_args, **_kwargs: None
    )
    imported: list[dict[str, object]] = []

    def fake_import(_matrix: Path, _kind: str, document: dict[str, object]):
        imported.append(document)
        return Path("adjudication-retained.json"), review._sha256(document)  # noqa: SLF001

    monkeypatch.setattr(review, "_import_document", fake_import)

    output = review.resolve_agreement(tmp_path / "matrix", review_dir)

    assert output == {"imported": 1, "retained": 0}
    assert imported[0]["outcomes"] == [
        {"criterion_id": "clear", "outcome": "unresolved"},
        {"criterion_id": "aligned", "outcome": "pass"},
    ]

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


def _matrix_root(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _verdict_document() -> dict[str, object]:
    return {
        "schema": review._VERDICTS_SCHEMA,  # noqa: SLF001
        "target": {
            "work_item_id": WORK_ITEM_ID,
            "projection_sha256": TARGET["projection_sha256"],
        },
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

    assert output == {"matrix_id": "matrix-test", "cases": 1}
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
    case_path = review_dir / "cases" / f"{WORK_ITEM_ID}.json"
    case_path.write_text(json.dumps(_case(), indent=2))
    case_path.chmod(0o600)
    ledger = review._read_json(  # noqa: SLF001
        review_dir / "ledger.json", require_private=True
    )

    with pytest.raises(review.ReviewError, match="not canonical"):
        review._load_bound_case(  # noqa: SLF001
            matrix_dir, review_dir, index, ledger, WORK_ITEM_ID
        )


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
def test_restricted_judge_profile_live_config_preflight(tmp_path: Path) -> None:
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
    process = subprocess.Popen(
        codex_driver._app_server_process_args(  # noqa: SLF001
            executable,
            workspace,
            include_runtime_roots=False,
            approval_policy="never",
        ),
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
    monkeypatch.setattr(review, "_load_bound_case", lambda *_args: _case())
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
    monkeypatch.setattr(review, "_load_bound_case", lambda *_args: _case())
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
    monkeypatch.setattr(review, "_load_bound_case", lambda *_args: _case())
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

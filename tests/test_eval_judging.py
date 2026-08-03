from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
import sys
import time
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import grade, inspection, judge, matrix, run_state  # noqa: E402


NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def test_verified_judge_calibration_labels_bind_the_complete_case_set() -> None:
    cases_path = ROOT / "evals" / "calibration" / "judge-v1-cases.json"
    labels_path = ROOT / "evals" / "calibration" / "judge-v1-labels.json"
    results_path = ROOT / "evals" / "calibration" / "judge-v1-results.json"
    settings_path = (
        ROOT / "evals" / "calibration" / "matrix-judge-settings-0.1.json"
    )
    prompt_path = ROOT / "evals" / "calibration" / "matrix-judge-prompt-0.1.md"
    parser_path = ROOT / "evals" / "calibration" / "matrix-parser-0.1.json"
    cases_bytes = cases_path.read_bytes()
    cases = json.loads(cases_bytes)
    labels = json.loads(labels_path.read_text())
    results = json.loads(results_path.read_text())

    assert labels["cases_sha256"] == hashlib.sha256(cases_bytes).hexdigest()
    assert set(labels["labels"]) == {item["id"] for item in cases["cases"]}
    assert set(labels["labels"].values()) == {"pass", "fail"}
    scenario_documents: dict[str, dict[str, Any]] = {}
    for path in (ROOT / "evals" / "scenarios").glob("*/*.json"):
        document = json.loads(path.read_text())
        scenario_documents[document["id"]] = document
    promoted_scenarios, _documents = matrix._scenario_documents(  # noqa: SLF001
        tuple(sorted({case["scenario_id"] for case in cases["cases"]})),
        root=ROOT,
    )
    promoted_by_scenario = {
        scenario.scenario_id: {
            item.criterion_id: item for item in scenario.qualitative_criteria
        }
        for scenario in promoted_scenarios
    }
    pair_labels: dict[tuple[str, str], list[str]] = {}
    for case in cases["cases"]:
        criterion = promoted_by_scenario[case["scenario_id"]][case["criterion_id"]]
        assert case["requirement"] == criterion.requirement
        if "source" in case:
            assert case["source"] == criterion.source
            assert case["evidence_path"] == criterion.evidence_path
        else:
            criteria = {
                item["id"]: item["requirement"]
                for item in scenario_documents[case["scenario_id"]][
                    "judged_answer_rubric"
                ]["criteria"]
            }
            assert case["requirement"] == criteria[case["criterion_id"]]
        pair_labels.setdefault(
            (case["scenario_id"], case["criterion_id"]), []
        ).append(labels["labels"][case["id"]])
    assert all(set(values) == {"fail", "pass"} for values in pair_labels.values())
    assert results["case_set_sha256"] == labels["cases_sha256"]
    assert results["prompt_version"] == "matrix-judge/0.1"
    assert results["prompt_sha256"] == hashlib.sha256(
        prompt_path.read_bytes()
    ).hexdigest()
    assert results["parser_version"] == "matrix-parser/0.1"
    assert results["parser_sha256"] == hashlib.sha256(
        parser_path.read_bytes()
    ).hexdigest()
    assert results["judge_settings_sha256"] == hashlib.sha256(
        settings_path.read_bytes()
    ).hexdigest()
    assert results["status"] == "verified"
    assert results["reviewed_at"] == "2026-08-03T01:49:40Z"
    assert len(results["reviewers"]) == 3
    expected_ids = list(labels["labels"])
    expected_verdicts = [labels["labels"][case_id] for case_id in expected_ids]
    for reviewer in results["reviewers"]:
        assert reviewer["model"] == "gpt-5.6-sol"
        assert reviewer["effort"] == "xhigh"
        assert [item["id"] for item in reviewer["results"]] == expected_ids
        assert [item["verdict"] for item in reviewer["results"]] == expected_verdicts
        assert all(
            len(item["rationale"].split()) <= 12
            for item in reviewer["results"]
        )


def test_judge_calibration_covers_required_defect_classes() -> None:
    calibration_root = ROOT / "evals" / "calibration"
    cases = json.loads((calibration_root / "judge-v1-cases.json").read_text())[
        "cases"
    ]
    labels = json.loads(
        (calibration_root / "judge-v1-labels.json").read_text()
    )["labels"]
    cases_by_id = {case["id"]: case for case in cases}
    required_pairs = {
        "invented_price": ("c24", "c25"),
        "unknown_stated_as_free": ("c26", "c27"),
        "verbose_but_incomplete": ("c28", "c29"),
        "prose_to_sidecar_alignment": ("c30", "c31"),
    }

    for pass_id, fail_id in required_pairs.values():
        assert labels[pass_id] == "pass"
        assert labels[fail_id] == "fail"

    for case_id in ("c24", "c25"):
        case = cases_by_id[case_id]
        assert case["source"] == "fact_rubric.must_mention"
        assert case["evidence_path"] == "$.data.items[0].deal.current_offer"
        assert case["selected_evidence"] == {"cardinality": "one", "value": None}
    for case_id in ("c26", "c27"):
        case = cases_by_id[case_id]
        assert case["source"] == "fact_rubric.criteria.hard_fail"
        assert case["evidence_path"] is None
    for case_id in ("c28", "c29"):
        case = cases_by_id[case_id]
        assert case["source"] == "judged_answer_rubric"
        assert case["evidence_path"] is None
    for case_id in ("c30", "c31"):
        case = cases_by_id[case_id]
        assert case["source"] == "generated.prose_claims_sidecar_alignment"
        assert case["evidence_path"] is None
        projection = case["candidate_projection"]
        assert set(projection) == {"answers", "claims_sidecars"}
        assert projection["answers"][0]["turn"] == 0
        assert projection["claims_sidecars"][0]["turn"] == 0
        assert set(projection["claims_sidecars"][0]) == {
            "turn",
            "claims",
            "declined",
        }


def test_judge_calibration_pairs_cover_every_screen_safety_gate() -> None:
    calibration_root = ROOT / "evals" / "calibration"
    cases = json.loads((calibration_root / "judge-v1-cases.json").read_text())[
        "cases"
    ]
    labels = json.loads(
        (calibration_root / "judge-v1-labels.json").read_text()
    )["labels"]
    scenario_ids = tuple(
        sorted(
            json.loads(path.read_text())["id"]
            for path in (ROOT / "evals" / "scenarios").glob("*/*.json")
        )
    )
    promoted, _documents = matrix._scenario_documents(  # noqa: SLF001
        scenario_ids, root=ROOT
    )
    safety_criteria = {
        (scenario.scenario_id, criterion.criterion_id)
        for scenario in promoted
        for criterion in scenario.qualitative_criteria
        if criterion.screen_safety_gate
    }
    calibrated: dict[tuple[str, str], set[str]] = {}
    for case in cases:
        identity = (case["scenario_id"], case["criterion_id"])
        if identity in safety_criteria:
            assert case["source"] == "fact_rubric.criteria.hard_fail"
            assert case["evidence_path"] is None
            calibrated.setdefault(identity, set()).add(labels[case["id"]])

    assert set(calibrated) == safety_criteria
    assert all(verdicts == {"pass", "fail"} for verdicts in calibrated.values())


def _campaign(kind: str = "screen") -> run_state.MatrixCampaign:
    return run_state.MatrixCampaign(
        campaign_kind=kind,
        selection_version="fixed-ordered-scenarios/0.1",
        selection_mode="fixed_ordered",
        acceptance_version="fixed-corpus/0.1",
        hard_layers=("agent_turns", "tool_policy", "oracle", "claims", "privacy"),
        required_tracks=("discovery",),
        replicates=1,
        qualitative_rule=(
            "fact_hard_safety_resolved_pass"
            if kind == "screen"
            else "all_hard_criteria_resolved_pass"
        ),
        judge_version="blinded-qualitative/0.1",
        judgment_schema="steam-agent-eval-judgment/0.1",
        adjudication_schema="steam-agent-eval-adjudication/0.1",
        prompt_version="judge-prompt/0.1",
        parser_version="manual-import/0.1",
        prompt_sha256="d" * 64,
        parser_sha256="e" * 64,
        judges=run_state.CALIBRATED_JUDGE_CONFIGURATIONS,
        adjudication_method=run_state.CALIBRATED_ADJUDICATION_METHOD,
        adjudicator=run_state.CALIBRATED_ADJUDICATOR,
        source_screen_manifest_sha256=None if kind == "screen" else "9" * 64,
        source_screen_matrix_id=None if kind == "screen" else "matrix-screen",
        source_screen_acceptance_sha256=None if kind == "screen" else "8" * 64,
        source_screen_qualitative_evidence_sha256=(
            None if kind == "screen" else "7" * 64
        ),
    )


def _inspection(
    matrix_dir: Path, *, campaign_kind: str = "screen"
) -> inspection.MatrixInspection:
    scenario = run_state.MatrixScenario(
        scenario_id="m7-z99",
        source_sha256="a" * 64,
        child_source_digest="f" * 64,
        schema_version="steam-agent-eval:0.3",
        schema_sha256="b" * 64,
        execution_support="live",
        rubric_sha256="c" * 64,
        criterion_ids=("clear", "actionable"),
        qualitative_criteria=(
            run_state.MatrixQualitativeCriterion(
                "clear", "judged_answer_rubric", "Be clear.", None
            ),
            run_state.MatrixQualitativeCriterion(
                "actionable", "judged_answer_rubric", "Be actionable.", None
            ),
        ),
        turn_count=1,
    )
    inputs = run_state.MatrixInputs(
        commit="1" * 40,
        source_digest="2" * 64,
        harness_digest="3" * 64,
        scenarios=(scenario,),
        tool_versions=(("codex", "0.146.0"),),
    )
    work = run_state.MatrixWorkItem(
        work_item_id="w-000000-1234567890abcdef",
        identity_sha256="4" * 64,
        ordinal=0,
        scenario_id="m7-z99",
        track="discovery",
        route=run_state.MatrixRoute("model-a", "high"),
        replicate=1,
    )
    manifest = run_state.MatrixManifest.create(
        matrix_id=matrix_dir.name,
        config_sha256="5" * 64,
        plan_sha256="6" * 64,
        campaign=_campaign(campaign_kind),
        inputs=inputs,
        work_items=(work,),
        excluded_scenario_ids=(),
        started_at=NOW,
    )
    completion = run_state.MatrixCompletion(
        work_item_id=work.work_item_id,
        attempt_id="attempt-000001",
        started_sha256="0" * 64,
        outcome="observed",
        unavailable_reason=None,
        child_run_id="child-000001",
        child_exit_code=1,
        artifact_hashes=tuple(
            sorted(
                {
                    "controls.json": "6" * 64,
                    "manifest.json": "7" * 64,
                    "report.json": "8" * 64,
                    "summary.json": "9" * 64,
                    "transcript.jsonl": "a" * 64,
                }.items()
            )
        ),
        completed_at=NOW.isoformat(),
    )
    manifest = manifest.checkpoint(completion, at=NOW)
    frozen = run_state.FrozenScenario.create(
        source_name="m7/m7-z99.json",
        original_bytes=b"{}",
        document={"id": "m7-z99"},
    )
    child_initial = run_state.RunManifest.create(
        run_id="child-000001",
        commit=inputs.commit,
        source_digest="b" * 64,
        cleanliness="clean",
        track="discovery",
        control_set_version="steam-agent-eval-controls/0.1",
        scenarios=[frozen],
        requested_routes=[run_state.RequestedRoute("model-a", "high")],
        tool_versions={"codex": "0.146.0"},
        started_at=NOW,
    )
    child = run_state.RunManifest(
        run_id=child_initial.run_id,
        state=run_state.RunState.COMPLETED,
        revision=3,
        commit=child_initial.commit,
        source_digest=child_initial.source_digest,
        cleanliness=child_initial.cleanliness,
        track=child_initial.track,
        control_set_version=child_initial.control_set_version,
        controls_passed=True,
        terminal_reason=None,
        scenario_ids=child_initial.scenario_ids,
        completed_scenario_ids=child_initial.scenario_ids,
        fixture_hashes=child_initial.fixture_hashes,
        requested_routes=child_initial.requested_routes,
        tool_versions=child_initial.tool_versions,
        started_at=child_initial.started_at,
        updated_at=child_initial.updated_at,
        finished_at=child_initial.updated_at,
    )
    metrics: dict[str, dict[str, Any]] = {
        layer: {"passed": False if layer == "claims" else True}
        for layer in ("agent_turns", "tool_policy", "oracle", "claims", "privacy")
    }
    observation = inspection.Observation(
        matrix_id=manifest.matrix_id,
        work_item=work,
        completion=completion,
        child_manifest=child,
        report={
            "metrics": metrics,
            "qualitative_review_answers": [
                {"turn": 0, "text": "A clear, grounded answer for the user."}
            ],
            "qualitative_review_claims_sidecars": [
                {"turn": 0, "claims": [], "declined": False}
            ],
        },
        summary={},
        compatibility=(),
        compatibility_sha256="d" * 64,
    )
    return inspection.MatrixInspection(
        matrix_dir=matrix_dir,
        manifest=manifest,
        manifest_sha256="8" * 64,
        structurally_complete=True,
        eligible=True,
        observations=(observation,),
        unavailable_work_items=(),
        orphan_attempt_ids=(),
    )


def _judgment(
    result: inspection.MatrixInspection,
    *,
    judgment_id: str,
    clear: str,
    actionable: str,
) -> dict[str, Any]:
    observation = result.observations[0]
    scenario = result.manifest.inputs.scenarios[0]
    campaign = result.manifest.campaign
    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, scenario, campaign=campaign
    )
    return {
        "schema": "steam-agent-eval-judgment/0.1",
        "judgment_id": judgment_id,
        "target": {
            "matrix_id": result.manifest.matrix_id,
            "work_item_id": observation.work_item.work_item_id,
            "report_sha256": dict(observation.completion.artifact_hashes)[
                "report.json"
            ],
            "scenario_sha256": scenario.source_sha256,
            "rubric_sha256": scenario.rubric_sha256,
            "projection_sha256": __import__("hashlib").sha256(
                matrix._canonical_json_bytes(projection)  # noqa: SLF001
            ).hexdigest(),
        },
        "judge": campaign.judges[0].to_dict(),
        "prompt": {
            "version": campaign.prompt_version,
            "sha256": campaign.prompt_sha256,
        },
        "parser": {
            "version": campaign.parser_version,
            "sha256": campaign.parser_sha256,
        },
        "presentation": {"blinded_label": "candidate-A", "order": 0},
        "verdicts": [
            {
                "criterion_id": "clear",
                "verdict": clear,
                "rationale": "The answer is clear.",
            },
            {
                "criterion_id": "actionable",
                "verdict": actionable,
                "rationale": "The answer is actionable.",
            },
        ],
        "created_at": "2026-08-02T12:00:00Z",
    }


def _configured_judgment(
    result: inspection.MatrixInspection,
    *,
    judgment_id: str,
    judge_index: int,
    clear: str = "pass",
    actionable: str = "pass",
) -> dict[str, Any]:
    document = _judgment(
        result,
        judgment_id=judgment_id,
        clear=clear,
        actionable=actionable,
    )
    campaign = result.manifest.campaign
    document["judge"] = campaign.judges[judge_index].to_dict()
    document["prompt"] = {
        "version": campaign.prompt_version,
        "sha256": campaign.prompt_sha256,
    }
    document["parser"] = {
        "version": campaign.parser_version,
        "sha256": campaign.parser_sha256,
    }
    return document


def _with_must_mention(
    result: inspection.MatrixInspection,
) -> tuple[inspection.MatrixInspection, run_state.MatrixQualitativeCriterion]:
    scenario = result.manifest.inputs.scenarios[0]
    criterion = run_state.matrix_qualitative_criteria(
        (), ("$.data.steam_id64",)
    )[0]
    scenario = replace(
        scenario,
        rubric_sha256="9" * 64,
        criterion_ids=(*scenario.criterion_ids, criterion.criterion_id),
        qualitative_criteria=(*scenario.qualitative_criteria, criterion),
    )
    manifest = replace(
        result.manifest,
        inputs=replace(result.manifest.inputs, scenarios=(scenario,)),
    )
    for observation in result.observations:
        observation.report["required_cli_documents"] = [
            {"data": {"steam_id64": "76561198000000001"}}
        ]
        observation.report.setdefault("diagnostics", {})["evidence_capture"] = {
            "state": "captured",
            "source": "aggregate",
            "matching_attempts": 1,
            "successful_candidates": 1,
            "turn": 0,
            "completion_sequence": 0,
        }
    return replace(result, manifest=manifest), criterion


def _with_observation_track(
    result: inspection.MatrixInspection, track: str
) -> inspection.MatrixInspection:
    observation = result.observations[0]
    return replace(
        result,
        observations=(
            replace(
                observation,
                work_item=replace(observation.work_item, track=track),
            ),
        ),
    )


def _with_prose_claims_alignment(
    result: inspection.MatrixInspection, *, scenario_id: str = "m2-b02"
) -> inspection.MatrixInspection:
    scenario = result.manifest.inputs.scenarios[0]
    criterion = run_state.MatrixQualitativeCriterion(
        run_state.PROSE_CLAIMS_ALIGNMENT_CRITERION_ID,
        run_state.PROSE_CLAIMS_ALIGNMENT_SOURCE,
        run_state.PROSE_CLAIMS_ALIGNMENT_REQUIREMENT,
        None,
    )
    scenario = replace(
        scenario,
        scenario_id=scenario_id,
        rubric_sha256="7" * 64,
        criterion_ids=(*scenario.criterion_ids, criterion.criterion_id),
        qualitative_criteria=(*scenario.qualitative_criteria, criterion),
    )
    work = replace(
        result.manifest.work_items[0], scenario_id=scenario_id
    )
    manifest = replace(
        result.manifest,
        inputs=replace(result.manifest.inputs, scenarios=(scenario,)),
        work_items=(work,),
    )
    observation = replace(result.observations[0], work_item=work)
    return replace(result, manifest=manifest, observations=(observation,))


def test_imported_judgments_are_blinded_hash_bound_and_do_not_override_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    source = tmp_path / "judgment.json"
    source.write_text(
        json.dumps(
            _judgment(
                result,
                judgment_id="judgment-1",
                clear="pass",
                actionable="fail",
            )
        )
    )

    retained, digest = judge.import_judgment(matrix_dir, source)

    assert len(digest) == 64
    assert stat.S_IMODE(retained.stat().st_mode) == 0o600
    document = retained.read_text()
    assert "model-a" not in document
    assert [
        item["rationale"] for item in json.loads(document)["verdicts"]
    ] == ["The answer is clear.", "The answer is actionable."]
    assert judge.deterministic_failures(result.observations[0]) == ("claims",)

    tampered = _judgment(
        result,
        judgment_id="judgment-2",
        clear="pass",
        actionable="pass",
    )
    tampered["target"]["report_sha256"] = "f" * 64
    source.write_text(json.dumps(tampered))
    with pytest.raises(judge.JudgmentError, match="digest"):
        judge.import_judgment(matrix_dir, source)

    tampered = _judgment(
        result,
        judgment_id="judgment-3",
        clear="pass",
        actionable="pass",
    )
    tampered["target"]["projection_sha256"] = "0" * 64
    source.write_text(json.dumps(tampered))
    with pytest.raises(judge.JudgmentError, match="digest"):
        judge.import_judgment(matrix_dir, source)

    sidecar_bound = _judgment(
        result,
        judgment_id="judgment-4",
        clear="pass",
        actionable="pass",
    )
    result.observations[0].report["qualitative_review_claims_sidecars"][0][
        "claims"
    ] = [{"path": "$.data.state", "value": "owned"}]
    source.write_text(json.dumps(sidecar_bound))
    with pytest.raises(judge.JudgmentError, match="digest"):
        judge.import_judgment(matrix_dir, source)


def test_blinded_projection_binds_must_mention_requirements_to_actual_answers(
    tmp_path: Path,
) -> None:
    result, criterion = _with_must_mention(_inspection(tmp_path))
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {
            "turn": 0,
            "text": (
                "SteamID64 76561198000000002 was returned only through explicit "
                "opt-in."
            ),
        }
    ]
    scenario = result.manifest.inputs.scenarios[0]

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, scenario
    )

    assert projection["answers"] == observation.report["qualitative_review_answers"]
    projected_must_mention = next(
        item for item in projection["criteria"] if item["id"] == criterion.criterion_id
    )
    assert {
        key: projected_must_mention[key] for key in criterion.to_dict()
    } == criterion.to_dict()
    assert projected_must_mention["selected_evidence"] == {
        "cardinality": "one",
        "value": "76561198000000001",
    }
    assert all(
        "selected_evidence" not in item
        for item in projection["criteria"][:-1]
    )
    assert "76561198000000002" in projection["answers"][0]["text"]
    assert projection["claims_sidecars"] == [
        {"turn": 0, "claims": [], "declined": False}
    ]
    changed_criterion = replace(
        criterion, requirement=f"{criterion.requirement} Confirm explicitly."
    )
    changed_scenario = replace(
        scenario,
        qualitative_criteria=(
            *scenario.qualitative_criteria[:-1],
            changed_criterion,
        ),
    )
    assert judge._projection_digest(  # noqa: SLF001
        observation, changed_scenario
    ) != judge._projection_digest(observation, scenario)  # noqa: SLF001


def test_multiturn_projection_rejects_an_omitted_unsafe_answer_turn(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    scenario = replace(result.manifest.inputs.scenarios[0], turn_count=2)
    result = replace(
        result,
        manifest=replace(
            result.manifest,
            inputs=replace(result.manifest.inputs, scenarios=(scenario,)),
        ),
    )
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": "The first answer is safe."}
    ]
    observation.report["qualitative_review_claims_sidecars"] = [
        {"turn": 0, "claims": [], "declined": False},
        {"turn": 1, "claims": [], "declined": False},
    ]

    with pytest.raises(judge.JudgmentError, match="projection is unavailable"):
        judge._qualitative_projection(observation, scenario)  # noqa: SLF001


@pytest.mark.parametrize(
    ("answers", "sidecars"),
    (
        (
            [
                {"turn": 0, "text": "First."},
                {"turn": 0, "text": "Duplicate."},
            ],
            [
                {"turn": 0, "claims": [], "declined": False},
                {"turn": 1, "claims": [], "declined": False},
            ],
        ),
        (
            [
                {"turn": 0, "text": "First."},
                {"turn": 1, "text": "Second."},
                {"turn": 2, "text": "Extra."},
            ],
            [
                {"turn": 0, "claims": [], "declined": False},
                {"turn": 1, "claims": [], "declined": False},
                {"turn": 2, "claims": [], "declined": False},
            ],
        ),
    ),
)
def test_projection_rejects_duplicate_or_extra_turns(
    tmp_path: Path,
    answers: list[dict[str, Any]],
    sidecars: list[dict[str, Any]],
) -> None:
    result = _inspection(tmp_path)
    scenario = replace(result.manifest.inputs.scenarios[0], turn_count=2)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = answers
    observation.report["qualitative_review_claims_sidecars"] = sidecars

    with pytest.raises(judge.JudgmentError, match="projection"):
        judge._qualitative_projection(observation, scenario)  # noqa: SLF001


@pytest.mark.parametrize(
    ("answer", "claims", "expected_alignment"),
    (
        (
            "SteamID64 76561198000000001 was returned through explicit opt-in.",
            [{"path": "$.data.steam_id64", "value": "76561198000000001"}],
            "pass",
        ),
        (
            "SteamID64 76561198000000001 was returned, and the account owns it.",
            [{"path": "$.data.steam_id64", "value": "76561198000000001"}],
            "fail",
        ),
    ),
)
def test_m2_b02_projection_binds_prose_to_exact_same_turn_sidecar(
    tmp_path: Path,
    answer: str,
    claims: list[dict[str, Any]],
    expected_alignment: str,
) -> None:
    result = _with_prose_claims_alignment(_inspection(tmp_path))
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": answer}
    ]
    observation.report["qualitative_review_claims_sidecars"] = [
        {"turn": 0, "claims": claims, "declined": False}
    ]
    scenario = result.manifest.inputs.scenarios[0]

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, scenario
    )

    alignment = next(
        item
        for item in projection["criteria"]
        if item["id"] == run_state.PROSE_CLAIMS_ALIGNMENT_CRITERION_ID
    )
    assert alignment["source"] == run_state.PROSE_CLAIMS_ALIGNMENT_SOURCE
    assert alignment["requirement"] == run_state.PROSE_CLAIMS_ALIGNMENT_REQUIREMENT
    assert projection["answers"] == [{"turn": 0, "text": answer}]
    assert projection["claims_sidecars"] == [
        {"turn": 0, "claims": claims, "declined": False}
    ]
    # The expected verdict documents the calibrated judgment boundary: the
    # second prose claim is absent from the exact sidecar.
    assert expected_alignment == (
        "fail" if "owns it" in answer else "pass"
    )


def test_claims_sidecar_tamper_changes_projection_provenance(tmp_path: Path) -> None:
    result = _with_prose_claims_alignment(_inspection(tmp_path))
    observation = result.observations[0]
    scenario = result.manifest.inputs.scenarios[0]
    original = judge._projection_digest(observation, scenario)  # noqa: SLF001

    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.data.steam_id64", "value": "76561198000000001"}
    ]

    assert judge._projection_digest(  # noqa: SLF001
        observation, scenario
    ) != original


def test_claims_sidecar_projection_rejects_candidate_route_material(
    tmp_path: Path,
) -> None:
    result = _with_prose_claims_alignment(_inspection(tmp_path))
    observation = result.observations[0]
    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.data.note", "value": "model-a"}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


def test_selected_evidence_preserves_unknown_false_and_empty_states(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    scenario = result.manifest.inputs.scenarios[0]
    promoted = run_state.matrix_qualitative_criteria(
        (),
        (
            "$.data.unknown",
            "$.data.false_value",
            "$.data.empty_value",
            "$.data.items[*]",
        ),
    )
    scenario = replace(
        scenario,
        criterion_ids=(*scenario.criterion_ids, *(item.criterion_id for item in promoted)),
        qualitative_criteria=(*scenario.qualitative_criteria, *promoted),
    )
    observation = result.observations[0]
    observation.report["required_cli_documents"] = [
        {
            "data": {
                "unknown": "unknown",
                "false_value": False,
                "empty_value": [],
                "items": [],
            }
        }
    ]
    observation.report.setdefault("diagnostics", {})["evidence_capture"] = {
        "state": "captured",
        "successful_candidates": 1,
    }

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, scenario
    )
    selected = {
        item["evidence_path"]: item["selected_evidence"]
        for item in projection["criteria"]
        if item["source"] == "fact_rubric.must_mention"
    }

    assert selected == {
        "$.data.unknown": {"cardinality": "one", "value": "unknown"},
        "$.data.false_value": {"cardinality": "one", "value": False},
        "$.data.empty_value": {"cardinality": "one", "value": []},
        "$.data.items[*]": {"cardinality": "many", "values": []},
    }


@pytest.mark.parametrize(
    "value",
    (
        None,
        False,
        123,
        -4.5,
        "quote \" slash \\ newline\n snowman \u2603 rocket \U0001f680",
        ["a", 1, None],
        {"z": "last", "a": [True, False]},
    ),
)
def test_selected_evidence_size_matches_canonical_ascii_json(value: Any) -> None:
    expected = len(matrix._canonical_json_bytes(value)) - 1  # noqa: SLF001

    assert judge._bounded_canonical_json_size(value, expected) == expected  # noqa: SLF001
    if expected > 0:
        assert judge._bounded_canonical_json_size(value, expected - 1) is None  # noqa: SLF001


def test_schema_selected_evidence_maximum_fits_aggregate_projection_budget() -> None:
    schema = json.loads(
        (ROOT / "evals" / "schema" / "scenario-0.3.json").read_text()
    )
    rubric = schema["properties"]["fact_rubric"]["properties"]
    path_count = sum(
        rubric[field]["maxItems"]
        for field in ("must_mention", "support_if_claimed")
    )

    assert path_count * judge._MAX_SELECTED_EVIDENCE_BYTES <= (  # noqa: SLF001
        judge._MAX_SELECTED_EVIDENCE_TOTAL_BYTES  # noqa: SLF001
    )
    assert judge._MAX_SELECTED_EVIDENCE_TOTAL_BYTES < judge._MAX_PROJECTION_BYTES  # noqa: SLF001


def _overlapping_evidence_document(depth: int, value: str) -> tuple[dict, list[str]]:
    document: dict[str, Any] = {"value": value}
    paths = ["$.data"]
    suffix = ""
    for index in reversed(range(depth - 1)):
        document = {f"level_{index}": document}
    current: Any = document
    for index in range(depth - 1):
        suffix += f".level_{index}"
        paths.append(f"$.data{suffix}")
        current = current[f"level_{index}"]
    return {"data": document}, paths


def test_schema_maximum_overlapping_evidence_paths_remain_representable(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    scenario = result.manifest.inputs.scenarios[0]
    document, paths = _overlapping_evidence_document(12, "x" * 300_000)
    promoted = run_state.matrix_qualitative_criteria(
        (), tuple(paths[:6]), support_if_claimed=tuple(paths[6:])
    )
    scenario = replace(
        scenario,
        criterion_ids=(*scenario.criterion_ids, *(item.criterion_id for item in promoted)),
        qualitative_criteria=(*scenario.qualitative_criteria, *promoted),
    )
    observation = result.observations[0]
    observation.report["required_cli_documents"] = [document]
    observation.report.setdefault("diagnostics", {})["evidence_capture"] = {
        "state": "captured",
        "successful_candidates": 1,
    }

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, scenario
    )
    evidence = [
        item["selected_evidence"]
        for item in projection["criteria"]
        if "selected_evidence" in item
    ]

    assert len(evidence) == 12
    assert len(matrix._canonical_json_bytes(projection)) < judge._MAX_PROJECTION_BYTES  # noqa: SLF001


def test_aggregate_evidence_budget_rejects_before_projection_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _inspection(tmp_path)
    scenario = result.manifest.inputs.scenarios[0]
    document, paths = _overlapping_evidence_document(17, "x" * 500_000)
    promoted = run_state.matrix_qualitative_criteria((), tuple(paths))
    scenario = replace(
        scenario,
        criterion_ids=(*scenario.criterion_ids, *(item.criterion_id for item in promoted)),
        qualitative_criteria=(*scenario.qualitative_criteria, *promoted),
    )
    observation = result.observations[0]
    observation.report["required_cli_documents"] = [document]
    observation.report.setdefault("diagnostics", {})["evidence_capture"] = {
        "state": "captured",
        "successful_candidates": 1,
    }

    def unexpected_copy(_value: Any) -> bytes:
        pytest.fail("projection serialized before aggregate evidence rejection")

    monkeypatch.setattr(matrix, "_canonical_json_bytes", unexpected_copy)
    with pytest.raises(judge.JudgmentError, match="aggregate safety limits"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, scenario
        )


def test_conditional_selected_evidence_represents_zero_one_and_many(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    scenario = result.manifest.inputs.scenarios[0]
    promoted = run_state.matrix_qualitative_criteria(
        (),
        (),
        support_if_claimed=(
            "$.context.currency",
            "$.context.tags[*]",
            "$.context.none[*]",
            "$.context.unknown",
            "$.context.false_value",
            "$.context.empty_value",
            "$.context.absent",
        ),
    )
    scenario = replace(
        scenario,
        criterion_ids=(*scenario.criterion_ids, *(item.criterion_id for item in promoted)),
        qualitative_criteria=(*scenario.qualitative_criteria, *promoted),
    )
    observation = result.observations[0]
    observation.report["required_cli_documents"] = [
        {
            "context": {
                "currency": "USD",
                "tags": ["official", "fresh"],
                "none": [],
                "unknown": "unknown",
                "false_value": False,
                "empty_value": [],
            }
        }
    ]
    observation.report.setdefault("diagnostics", {})["evidence_capture"] = {
        "state": "captured",
        "successful_candidates": 1,
    }

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, scenario
    )
    selected = {
        item["evidence_path"]: item["selected_evidence"]
        for item in projection["criteria"]
        if item["source"] == "fact_rubric.support_if_claimed"
    }

    assert selected == {
        "$.context.currency": {"cardinality": "one", "value": "USD"},
        "$.context.tags[*]": {
            "cardinality": "many",
            "values": ["official", "fresh"],
        },
        "$.context.none[*]": {
            "cardinality": "zero",
            "state": "empty_selection",
            "values": [],
        },
        "$.context.unknown": {"cardinality": "one", "value": "unknown"},
        "$.context.false_value": {"cardinality": "one", "value": False},
        "$.context.empty_value": {"cardinality": "one", "value": []},
        "$.context.absent": {
            "cardinality": "zero",
            "state": "path_unavailable",
        },
    }


def test_conditional_selected_evidence_allows_an_unavailable_capture(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    scenario = result.manifest.inputs.scenarios[0]
    promoted = run_state.matrix_qualitative_criteria(
        (), (), support_if_claimed=("$.context.currency",)
    )
    scenario = replace(
        scenario,
        criterion_ids=(*scenario.criterion_ids, promoted[0].criterion_id),
        qualitative_criteria=(*scenario.qualitative_criteria, promoted[0]),
    )

    projection = judge._qualitative_projection(  # noqa: SLF001
        result.observations[0], scenario
    )

    assert projection["criteria"][-1]["selected_evidence"] == {
        "cardinality": "zero",
        "state": "capture_unavailable",
    }


@pytest.mark.parametrize(
    ("documents", "capture", "expected"),
    (
        (
            [
                {
                    "omitted": "unsafe-trace-content",
                    "sha256": "a" * 64,
                    "length": 128,
                }
            ],
            {
                "state": "captured",
                "successful_candidates": 1,
            },
            {"cardinality": "zero", "state": "path_unavailable"},
        ),
        (
            [],
            {
                "state": "multiple_candidates",
                "successful_candidates": 2,
            },
            {"cardinality": "zero", "state": "capture_unavailable"},
        ),
    ),
)
def test_screen_answer_projects_unavailable_must_mention_as_zero_evidence(
    tmp_path: Path,
    documents: list[dict[str, Any]],
    capture: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    result, criterion = _with_must_mention(_inspection(tmp_path))
    result = _with_observation_track(result, "answer")
    observation = result.observations[0]
    observation.report["required_cli_documents"] = documents
    observation.report.setdefault("diagnostics", {})["evidence_capture"] = capture

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation,
        result.manifest.inputs.scenarios[0],
        campaign=result.manifest.campaign,
    )

    projected = next(
        item for item in projection["criteria"] if item["id"] == criterion.criterion_id
    )
    assert projected["selected_evidence"] == expected


@pytest.mark.parametrize(
    ("campaign_kind", "track", "documents", "capture"),
    (
        (
            "screen",
            "discovery",
            [],
            {"state": "multiple_candidates", "successful_candidates": 2},
        ),
        (
            "screen",
            "discovery",
            [
                {
                    "omitted": "unsafe-trace-content",
                    "sha256": "a" * 64,
                    "length": 128,
                }
            ],
            {"state": "captured", "successful_candidates": 1},
        ),
        (
            "qualification",
            "discovery",
            [],
            {"state": "multiple_candidates", "successful_candidates": 2},
        ),
        (
            "qualification",
            "discovery",
            [
                {
                    "omitted": "unsafe-trace-content",
                    "sha256": "a" * 64,
                    "length": 128,
                }
            ],
            {"state": "captured", "successful_candidates": 1},
        ),
    ),
)
def test_discovery_and_qualification_require_exact_must_mention_evidence(
    tmp_path: Path,
    campaign_kind: str,
    track: str,
    documents: list[dict[str, Any]],
    capture: dict[str, Any],
) -> None:
    result, _criterion = _with_must_mention(
        _inspection(tmp_path, campaign_kind=campaign_kind)
    )
    result = _with_observation_track(result, track)
    observation = result.observations[0]
    observation.report["required_cli_documents"] = documents
    observation.report.setdefault("diagnostics", {})["evidence_capture"] = capture

    with pytest.raises(judge.JudgmentError, match="selected evidence is unavailable"):
        judge._qualitative_projection(  # noqa: SLF001
            observation,
            result.manifest.inputs.scenarios[0],
            campaign=result.manifest.campaign,
        )


def test_screen_answer_keeps_exact_must_mention_evidence(tmp_path: Path) -> None:
    result, criterion = _with_must_mention(_inspection(tmp_path))
    result = _with_observation_track(result, "answer")

    projection = judge._qualitative_projection(  # noqa: SLF001
        result.observations[0],
        result.manifest.inputs.scenarios[0],
        campaign=result.manifest.campaign,
    )

    projected = next(
        item for item in projection["criteria"] if item["id"] == criterion.criterion_id
    )
    assert projected["selected_evidence"] == {
        "cardinality": "one",
        "value": "76561198000000001",
    }


@pytest.mark.parametrize(
    ("documents", "expected"),
    (
        (
            [
                {"data": {"steam_id64": "76561198000000001"}},
                {"data": {"steam_id64": "76561198000000001"}},
            ],
            "ambiguous",
        ),
        ([{"data": {"steam_id64": "/Users/private/Steam"}}], "prohibited"),
        ([{"data": {"steam_id64": float("nan")}}], "invalid"),
        ([{"data": {"steam_id64": "x" * (1024 * 1024)}}], "exceeds safety limits"),
    ),
)
def test_screen_answer_unavailable_exception_still_rejects_unsafe_evidence(
    tmp_path: Path,
    documents: list[dict[str, Any]],
    expected: str,
) -> None:
    result, _criterion = _with_must_mention(_inspection(tmp_path))
    result = _with_observation_track(result, "answer")
    observation = result.observations[0]
    observation.report["required_cli_documents"] = documents

    with pytest.raises(judge.JudgmentError, match=expected):
        judge._qualitative_projection(  # noqa: SLF001
            observation,
            result.manifest.inputs.scenarios[0],
            campaign=result.manifest.campaign,
        )


def test_screen_answer_judgment_binds_unavailable_must_mention_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result, criterion = _with_must_mention(_inspection(matrix_dir))
    result = _with_observation_track(result, "answer")
    observation = result.observations[0]
    observation.report["required_cli_documents"] = []
    observation.report.setdefault("diagnostics", {})["evidence_capture"] = {
        "state": "multiple_candidates",
        "successful_candidates": 2,
    }
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _configured_judgment(
        result, judgment_id="judgment-screen-answer-unavailable", judge_index=0
    )
    document["verdicts"].append(
        {
            "criterion_id": criterion.criterion_id,
            "verdict": "uncertain",
            "rationale": "Required evidence is unavailable.",
        }
    )
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    retained, _digest = judge.import_judgment(matrix_dir, source)

    assert retained.name == "judgment-screen-answer-unavailable.json"


@pytest.mark.parametrize(
    ("documents", "expected"),
    (
        ([], "unavailable"),
        (
            [
                {"data": {"steam_id64": "76561198000000001"}},
                {"data": {"steam_id64": "76561198000000001"}},
            ],
            "ambiguous",
        ),
        ([{"data": {}}], "unavailable"),
        (
            [{"data": {"steam_id64": "EVAL_CANARY_STEAMID64_PRIVATE"}}],
            "prohibited",
        ),
        ([{"data": {"steam_id64": "/Users/private/Steam"}}], "prohibited"),
        ([{"data": {"steam_id64": float("nan")}}], "invalid"),
        (
            [{"data": {"steam_id64": "x" * (1024 * 1024)}}],
            "exceeds safety limits",
        ),
    ),
)
def test_selected_evidence_fails_closed_on_unsafe_or_ambiguous_inputs(
    tmp_path: Path,
    documents: list[dict[str, Any]],
    expected: str,
) -> None:
    result, _criterion = _with_must_mention(_inspection(tmp_path))
    observation = result.observations[0]
    observation.report["required_cli_documents"] = documents

    with pytest.raises(judge.JudgmentError, match=expected):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


def test_qualification_judgment_cannot_omit_a_must_mention_criterion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result, criterion = _with_must_mention(
        _inspection(matrix_dir, campaign_kind="qualification")
    )
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _configured_judgment(
        result, judgment_id="judgment-missing-mention", judge_index=0
    )
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    assert criterion.criterion_id not in {
        item["criterion_id"] for item in document["verdicts"]
    }
    with pytest.raises(judge.JudgmentError, match="exact rubric"):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize(
    "rationale",
    (
        None,
        "one two three four five six seven eight nine ten eleven twelve thirteen",
    ),
)
def test_judgment_requires_a_parser_conforming_rationale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rationale: str | None,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-rationale",
        clear="pass",
        actionable="pass",
    )
    if rationale is None:
        document["verdicts"][0].pop("rationale")
    else:
        document["verdicts"][0]["rationale"] = rationale
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match="schema"):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize(
    ("rationale", "expected"),
    (
        ("Candidate model-a produced this answer.", "candidate route"),
        ("The model-a/high route produced this answer.", "candidate route"),
        ("The SOL route produced this answer.", "candidate route"),
        ("This used xhigh.", "candidate route"),
        ("The candidate used high effort.", "candidate route"),
        ("Oracle passed.", "deterministic outcome"),
        ("Claims failed.", "deterministic outcome"),
        ("The tool_policy result was pass.", "deterministic outcome"),
        ("Privacy was false.", "deterministic outcome"),
        ("The agent_turns layer passed.", "deterministic outcome"),
    ),
)
def test_judgment_rejects_route_or_deterministic_leaks_in_rationales(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rationale: str,
    expected: str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-rationale-leak",
        clear="pass",
        actionable="pass",
    )
    document["verdicts"][0]["rationale"] = rationale
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match=expected):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize(
    "rationale",
    (
        "High confidence follows from the answer's proportional uncertainty.",
        "Medium detail is sufficient for this criterion.",
        "The answer avoids false certainty.",
        "Privacy guidance is clear and actionable.",
        "The tool explanation is concise.",
        "The answer is solid and directly supported.",
        "The lunar recommendation follows the stated criterion.",
        "The agent failed to explain the criterion.",
        "The claim passes because evidence is explicit.",
    ),
)
def test_judgment_allows_ordinary_criterion_rationales(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rationale: str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-benign-rationale",
        clear="pass",
        actionable="pass",
    )
    document["verdicts"][0]["rationale"] = rationale
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    retained, _digest = judge.import_judgment(matrix_dir, source)

    assert json.loads(retained.read_text())["verdicts"][0]["rationale"] == rationale


@pytest.mark.parametrize(
    "projection",
    [
        [{"turn": 0, "text": "EVAL_CANARY_CREDENTIAL_SECRET"}],
        [{"turn": 0, "text": "Read /Users/private/Steam/config.vdf"}],
        [{"turn": 0, "text": "Candidate model-a produced this answer."}],
        [{"turn": 0, "text": "Answer", "passed": True}],
    ],
)
def test_judgment_rejects_private_route_or_grading_projection_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    projection: list[dict[str, Any]],
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-unsafe",
        clear="pass",
        actionable="pass",
    )
    result.observations[0].report["qualitative_review_answers"] = projection
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match="projection"):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize("privacy_value", [False, None, "missing"])
def test_judgment_rejects_unjudgeable_privacy_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    privacy_value: bool | None | str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    privacy = result.observations[0].report["metrics"]["privacy"]
    if privacy_value == "missing":
        privacy.pop("passed")
    else:
        privacy["passed"] = privacy_value
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-unjudgeable",
        clear="pass",
        actionable="pass",
    )
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match="privacy-cleared"):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("judge", "identifier", "EVAL_CANARY_CREDENTIAL_SECRET"),
        ("judge", "settings_identity", "model-a-route"),
        ("prompt", "version", "tool-policy"),
        ("parser", "version", "/Users/private/parser"),
    ],
)
def test_judgment_rejects_private_route_or_outcome_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-unsafe-metadata",
        clear="pass",
        actionable="pass",
    )
    document[section][field] = value
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize(
    "rationale",
    (
        "Read /Users/private/Steam/config.vdf",
        r"Read \u002fUsers\u002fprivate\u002fSteam\u002fconfig.vdf",
        r"Read C:\\Users\\private\\Steam\\config.vdf",
        "Read file:%2FUsers%2Fprivate%2FSteam%2Fconfig.vdf",
    ),
)
def test_judgment_structural_privacy_scan_rejects_escaped_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rationale: str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-private-rationale",
        clear="pass",
        actionable="pass",
    )
    document["verdicts"][0]["rationale"] = rationale
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match="private material"):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    (
        ("_MAX_PRIVATE_SCAN_STRINGS", 1),
        ("_MAX_PRIVATE_SCAN_CHARACTERS", 3),
    ),
)
def test_structural_privacy_scan_fails_closed_at_aggregate_bounds(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
) -> None:
    monkeypatch.setattr(judge, limit_name, limit)

    assert judge._contains_private_material({"safe": "value"})  # noqa: SLF001


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("presentation", "blinded_label", "candidate-XHIGH"),
        ("judge", "settings_identity", "high"),
        ("judge", "identifier", "reviewer-xhigh"),
    ],
)
def test_judgment_rejects_candidate_reasoning_effort_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-effort-metadata",
        clear="pass",
        actionable="pass",
    )
    document[section][field] = value
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match="candidate route"):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("presentation", "blinded_label", "candidate-SOL"),
        ("presentation", "blinded_label", "candidate-TERRA"),
        ("presentation", "blinded_label", "candidate-LUNA"),
        ("judge", "settings_identity", "review-route-sol"),
        ("judge", "identifier", "terra"),
        ("parser", "version", "luna/0.1"),
    ],
)
def test_judgment_rejects_fixed_route_alias_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-route-alias",
        clear="pass",
        actionable="pass",
    )
    document[section][field] = value
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match="candidate route"):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("presentation", "blinded_label", "candidate-HIGHLIGHT"),
        ("judge", "settings_identity", "highlight-calibration"),
        ("judge", "identifier", "reviewer-lowdown"),
        ("prompt", "version", "mediumship/0.1"),
        ("parser", "version", "xhighway/0.1"),
    ],
)
def test_reasoning_effort_blinding_avoids_substring_false_positives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-effort-substring",
        clear="pass",
        actionable="pass",
    )
    document[section][field] = value
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    retained, _digest = judge.import_judgment(matrix_dir, source)

    assert retained.is_file()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("presentation", "blinded_label", "candidate-A"),
        ("presentation", "blinded_label", "candidate-SOLAR"),
        ("presentation", "blinded_label", "candidate-TERRAFORM"),
        ("presentation", "blinded_label", "candidate-LUNAR"),
        ("judge", "identifier", "console-reviewer"),
        ("judge", "settings_identity", "terrain-calibration"),
        ("parser", "version", "lunatic/0.1"),
    ],
)
def test_fixed_route_alias_blinding_avoids_substring_false_positives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-route-substring",
        clear="pass",
        actionable="pass",
    )
    document[section][field] = value
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    retained, _digest = judge.import_judgment(matrix_dir, source)

    assert retained.is_file()


def test_reasoning_effort_words_remain_allowed_in_candidate_answer_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    result.observations[0].report["qualitative_review_answers"] = [
        {
            "turn": 0,
            "text": "Use low settings for high frame rates on medium hardware.",
        }
    ]
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-effort-prose",
        clear="pass",
        actionable="pass",
    )
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    retained, _digest = judge.import_judgment(matrix_dir, source)

    assert retained.is_file()


@pytest.mark.parametrize(
    "disclosure",
    (
        "The candidate route used xhigh reasoning.",
        "The generator reasoning effort was low.",
        "The candidate route effort was medium.",
        "The generator reasoning effort was high.",
        "This answer came from SOL.",
        "This answer came from Terra.",
        "This answer came from Luna.",
        "The full candidate model was model-a.",
    ),
)
def test_candidate_answer_projection_rejects_route_and_effort_disclosures(
    tmp_path: Path, disclosure: str
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": disclosure}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


@pytest.mark.parametrize(
    "disclosure",
    (
        "xhigh",
        "candidate route: low",
        "reasoning effort: medium",
        "generator effort: high",
        "sol",
        "terra",
        "luna",
        "model-a",
    ),
)
def test_candidate_sidecar_projection_rejects_route_and_effort_disclosures(
    tmp_path: Path, disclosure: str
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.data.note", "value": disclosure}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


@pytest.mark.parametrize(
    "content",
    (
        "This candidate has a high discount.",
        "This candidate has a low price.",
        "Candidate high discounts remain attractive.",
        "The candidate offers high frame rates.",
        "Model low settings preserve battery life.",
        "Use low settings for high frame rates on medium hardware.",
        "Solar terrain looks lunar in highlights.",
        "A model-aware answer is solid.",
    ),
)
def test_candidate_projection_route_tokens_avoid_ordinary_word_false_positives(
    tmp_path: Path, content: str
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": content}
    ]
    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.data.note", "value": content}
    ]

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, result.manifest.inputs.scenarios[0]
    )

    assert projection["answers"][0]["text"] == content


def test_candidate_sidecar_route_context_and_effort_in_separate_claims_pass(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.data.category", "value": "model"},
        {"path": "$.data.discount", "value": "high"},
        {"path": "$.data.price", "value": "low"},
    ]

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, result.manifest.inputs.scenarios[0]
    )

    assert len(projection["claims_sidecars"][0]["claims"]) == 3


@pytest.mark.parametrize(
    "disclosure",
    (
        "model.high",
        "reasoning;high",
    ),
)
def test_candidate_sidecar_keeps_delimited_value_as_one_route_association(
    tmp_path: Path, disclosure: str
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.data.note", "value": disclosure}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


def test_candidate_sidecar_keeps_route_path_and_effort_value_associated(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.candidate.reasoning_effort.level", "value": "high"}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


def test_candidate_sidecar_keeps_model_path_and_effort_value_associated(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.data.model.score", "value": "high"}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


def test_explicit_reasoning_effort_beats_ordinary_noun_exemption(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": "Reasoning effort: high setting"}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


def test_natural_reasoning_effort_level_disclosure_is_rejected(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {
            "turn": 0,
            "text": "The candidate uses a reasoning effort level of high.",
        }
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


@pytest.mark.parametrize(
    "disclosure",
    (
        "The reasoning effort was configured by the evaluator to high.",
        "High was the selected value for this candidate's reasoning effort.",
    ),
)
def test_long_distance_reasoning_effort_disclosures_are_rejected(
    tmp_path: Path, disclosure: str
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": disclosure}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


@pytest.mark.parametrize(
    "disclosure",
    (
        "The effort setting chosen after reviewing all available evidence in "
        "detail was high.",
        "The effort settings selected after a lengthy review were medium.",
        "The effort-setting selected after a lengthy review was low.",
    ),
)
def test_effort_setting_compound_disclosures_are_rejected(
    tmp_path: Path, disclosure: str
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": disclosure}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


def test_distant_generic_effort_and_level_are_allowed_in_ordinary_prose(
    tmp_path: Path,
) -> None:
    content = (
        "Performance was high after reviewing all available evidence in detail, "
        "an effort that took hours."
    )
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": content}
    ]

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, result.manifest.inputs.scenarios[0]
    )

    assert projection["answers"][0]["text"] == content


def test_atomic_sidecar_allows_distant_generic_effort_and_level(
    tmp_path: Path,
) -> None:
    content = (
        "Performance was high after reviewing all available evidence in detail, "
        "an effort that took hours."
    )
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.data.note", "value": content}
    ]

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, result.manifest.inputs.scenarios[0]
    )

    assert projection["claims_sidecars"][0]["claims"][0]["value"] == content


@pytest.mark.parametrize(
    "content",
    (
        "Low settings reduced rendering cost; the migration effort took hours.",
        "The effort setting was documented. Performance was high.",
    ),
)
def test_effort_setting_context_does_not_cross_prose_boundaries(
    tmp_path: Path, content: str
) -> None:
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": content}
    ]

    projection = judge._qualitative_projection(  # noqa: SLF001
        observation, result.manifest.inputs.scenarios[0]
    )

    assert projection["answers"][0]["text"] == content


def test_atomic_sidecar_preserves_effort_setting_and_level_association(
    tmp_path: Path,
) -> None:
    content = "The effort setting was documented. Performance was high."
    result = _inspection(tmp_path)
    observation = result.observations[0]
    observation.report["qualitative_review_claims_sidecars"][0]["claims"] = [
        {"path": "$.data.note", "value": content}
    ]

    with pytest.raises(judge.JudgmentError, match="prohibited material"):
        judge._qualitative_projection(  # noqa: SLF001
            observation, result.manifest.inputs.scenarios[0]
        )


def test_candidate_route_context_and_effort_in_separate_answer_turns_pass(
    tmp_path: Path,
) -> None:
    result = _inspection(tmp_path)
    scenario = replace(result.manifest.inputs.scenarios[0], turn_count=2)
    observation = result.observations[0]
    observation.report["qualitative_review_answers"] = [
        {"turn": 0, "text": "This candidate is promising."},
        {"turn": 1, "text": "It has a high discount."},
    ]
    observation.report["qualitative_review_claims_sidecars"] = [
        {"turn": 0, "claims": [], "declined": False},
        {"turn": 1, "claims": [], "declined": False},
    ]

    projection = judge._qualitative_projection(observation, scenario)  # noqa: SLF001

    assert len(projection["answers"]) == 2


@pytest.mark.parametrize("judge_model", ("candidate-XHIGH", "SOL", "gpt-5.6-terra"))
def test_judge_model_field_may_name_its_own_model_or_route_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, judge_model: str
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-judge-model",
        clear="pass",
        actionable="pass",
    )
    document["judge"]["model"] = judge_model
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    retained, _digest = judge.import_judgment(matrix_dir, source)

    assert retained.is_file()


def test_versioned_digest_slash_does_not_weaken_general_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id="judgment-version-token",
        clear="pass",
        actionable="pass",
    )
    document["judgment_id"] = "judgment/0.1"
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match="schema"):
        judge.import_judgment(matrix_dir, source)


@pytest.mark.parametrize(
    "private_or_unscoped_id",
    (
        "76561198000000001",
        "synthetic-primary",
        "EVAL_CANARY_STEAMID64_IMPORT",
    ),
)
def test_judgment_id_requires_opaque_prefix_before_artifact_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private_or_unscoped_id: str,
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _judgment(
        result,
        judgment_id=private_or_unscoped_id,
        clear="pass",
        actionable="pass",
    )
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match="schema"):
        judge.import_judgment(matrix_dir, source)

    assert not (matrix_dir / "judgments").exists()


def test_agreement_adjudication_retains_disagreement_as_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    hashes: list[str] = []
    for index, actionable in enumerate(("pass", "fail"), start=1):
        source = tmp_path / f"judgment-{index}.json"
        source.write_text(
            json.dumps(
                _judgment(
                    result,
                    judgment_id=f"judgment-{index}",
                    clear="pass",
                    actionable=actionable,
                )
            )
        )
        _path, digest = judge.import_judgment(matrix_dir, source)
        hashes.append(digest)
    observation = result.observations[0]
    scenario = result.manifest.inputs.scenarios[0]
    adjudication = {
        "schema": "steam-agent-eval-adjudication/0.1",
        "adjudication_id": "adjudication-1",
        "target": {
            "matrix_id": result.manifest.matrix_id,
            "work_item_id": observation.work_item.work_item_id,
            "report_sha256": dict(observation.completion.artifact_hashes)[
                "report.json"
            ],
            "scenario_sha256": scenario.source_sha256,
            "rubric_sha256": scenario.rubric_sha256,
            "projection_sha256": _judgment(
                result,
                judgment_id="judgment-projection-template",
                clear="pass",
                actionable="pass",
            )["target"]["projection_sha256"],
        },
        "method": "agreement",
        "adjudicator": "agreement-0.1",
        "judgment_sha256s": hashes,
        "outcomes": [
            {"criterion_id": "clear", "outcome": "pass"},
            {"criterion_id": "actionable", "outcome": "unresolved"},
        ],
        "created_at": "2026-08-02T12:01:00Z",
    }
    source = tmp_path / "adjudication.json"
    source.write_text(json.dumps(adjudication))

    retained, digest = judge.import_adjudication(matrix_dir, source)

    assert retained.name == "adjudication-1.json"
    assert len(digest) == 64
    adjudication["outcomes"][1]["outcome"] = "pass"
    source.write_text(json.dumps(adjudication))
    with pytest.raises(judge.JudgmentError, match="does not match"):
        judge.import_adjudication(matrix_dir, source)
    for invalid_id in (
        "76561198000000001",
        "synthetic-primary",
        "EVAL_CANARY_STEAMID64_IMPORT",
    ):
        adjudication["adjudication_id"] = invalid_id
        adjudication["outcomes"][1]["outcome"] = "unresolved"
        source.write_text(json.dumps(adjudication))
        with pytest.raises(judge.JudgmentError, match="schema"):
            judge.import_adjudication(matrix_dir, source)
    assert sorted(path.name for path in (matrix_dir / "adjudications").iterdir()) == [
        "adjudication-1.json"
    ]


def test_adjudication_inspects_once_for_hundreds_of_retained_judgments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    base = result.observations[0]
    observations = [base]
    for index in range(1, 576):
        work_item_id = f"w-{index:06d}-{index:016x}"
        observations.append(
            replace(
                base,
                work_item=replace(
                    base.work_item,
                    work_item_id=work_item_id,
                    ordinal=index,
                ),
                completion=replace(
                    base.completion,
                    work_item_id=work_item_id,
                    attempt_id=f"attempt-{index + 1:06d}",
                ),
            )
        )
    result = replace(result, observations=tuple(observations))
    inspect_calls = 0

    def tracked_inspection(_path: Path) -> inspection.MatrixInspection:
        nonlocal inspect_calls
        inspect_calls += 1
        return result

    monkeypatch.setattr(inspection, "inspect_matrix", tracked_inspection)
    judgment_root = matrix_dir / "judgments"
    judgment_root.mkdir(mode=0o700)
    hashes: list[str] = []
    target: dict[str, Any] | None = None
    for index in range(240):
        document = _judgment(
            result,
            judgment_id=f"judgment-batch-{index:03d}",
            clear="pass",
            actionable="pass",
        )
        target = document["target"]
        content = matrix._canonical_json_bytes(document)  # noqa: SLF001
        path = judgment_root / f"judgment-batch-{index:03d}.json"
        path.write_bytes(content)
        path.chmod(0o600)
        hashes.append(hashlib.sha256(content).hexdigest())
    assert target is not None
    projection_calls = 0
    projection_digest = judge._projection_digest  # noqa: SLF001

    def tracked_projection_digest(
        observation: inspection.Observation,
        scenario: run_state.MatrixScenario,
        *,
        campaign: run_state.MatrixCampaign | None = None,
    ) -> str:
        nonlocal projection_calls
        projection_calls += 1
        return projection_digest(observation, scenario, campaign=campaign)

    monkeypatch.setattr(judge, "_projection_digest", tracked_projection_digest)
    adjudication = {
        "schema": "steam-agent-eval-adjudication/0.1",
        "adjudication_id": "adjudication-batch",
        "target": target,
        "method": "agreement",
        "adjudicator": "agreement-0.1",
        "judgment_sha256s": hashes[:2],
        "outcomes": [
            {"criterion_id": "clear", "outcome": "pass"},
            {"criterion_id": "actionable", "outcome": "pass"},
        ],
        "created_at": "2026-08-02T12:01:00Z",
    }
    source = tmp_path / "adjudication-batch.json"
    source.write_text(json.dumps(adjudication))

    started = time.perf_counter()
    retained, _digest = judge.import_adjudication(matrix_dir, source)
    elapsed = time.perf_counter() - started

    assert retained.is_file()
    assert inspect_calls == 1
    assert projection_calls == 1
    assert elapsed < 10

    first_judgment = judgment_root / "judgment-batch-000.json"
    first_document = json.loads(first_judgment.read_text())
    first_judgment.write_text(json.dumps(first_document, indent=2))
    adjudication["adjudication_id"] = "adjudication-batch-tampered"
    source.write_text(json.dumps(adjudication))

    with pytest.raises(judge.JudgmentError, match="not canonical"):
        judge.import_adjudication(matrix_dir, source)
    assert inspect_calls == 2
    assert projection_calls == 2


def test_judgment_and_adjudication_support_more_than_64_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    scenario = result.manifest.inputs.scenarios[0]
    criteria = tuple(
        run_state.MatrixQualitativeCriterion(
            f"criterion-{index:03d}",
            "judged_answer_rubric",
            f"Assess criterion {index}.",
            None,
        )
        for index in range(65)
    )
    scenario = replace(
        scenario,
        rubric_sha256="f" * 64,
        criterion_ids=tuple(item.criterion_id for item in criteria),
        qualitative_criteria=criteria,
    )
    result = replace(
        result,
        manifest=replace(
            result.manifest,
            inputs=replace(result.manifest.inputs, scenarios=(scenario,)),
        ),
    )
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)

    verdicts = [
        {
            "criterion_id": criterion.criterion_id,
            "verdict": "pass",
            "rationale": "The criterion is satisfied.",
        }
        for criterion in criteria
    ]
    hashes: list[str] = []
    target: dict[str, Any] | None = None
    for index in range(2):
        document = _judgment(
            result,
            judgment_id=f"judgment-large-{index}",
            clear="pass",
            actionable="pass",
        )
        document["verdicts"] = verdicts
        target = document["target"]
        source = tmp_path / f"judgment-large-{index}.json"
        source.write_text(json.dumps(document))
        _path, digest = judge.import_judgment(matrix_dir, source)
        hashes.append(digest)

    assert target is not None
    adjudication = {
        "schema": "steam-agent-eval-adjudication/0.1",
        "adjudication_id": "adjudication-large",
        "target": target,
        "method": "agreement",
        "adjudicator": "agreement-0.1",
        "judgment_sha256s": hashes,
        "outcomes": [
            {"criterion_id": criterion.criterion_id, "outcome": "pass"}
            for criterion in criteria
        ],
        "created_at": "2026-08-02T12:01:00Z",
    }
    source = tmp_path / "adjudication-large.json"
    source.write_text(json.dumps(adjudication))

    retained, digest = judge.import_adjudication(matrix_dir, source)

    assert retained.name == "adjudication-large.json"
    assert len(digest) == 64


def test_imports_near_maximum_1024_verdict_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    scenario = result.manifest.inputs.scenarios[0]
    criterion_ids = tuple(
        f"criterion-{index:04d}-".ljust(128, "a")
        for index in range(1024)
    )
    criteria = tuple(
        run_state.MatrixQualitativeCriterion(
            criterion_id,
            "judged_answer_rubric",
            "Assess this criterion.",
            None,
        )
        for criterion_id in criterion_ids
    )
    scenario = replace(
        scenario,
        rubric_sha256="f" * 64,
        criterion_ids=criterion_ids,
        qualitative_criteria=criteria,
    )
    result = replace(
        result,
        manifest=replace(
            result.manifest,
            inputs=replace(result.manifest.inputs, scenarios=(scenario,)),
        ),
    )
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    scanned_lengths: list[int] = []
    find_private_host_paths = grade.find_private_host_paths

    def tracked_private_host_paths(value: str) -> list[str]:
        scanned_lengths.append(len(value))
        return find_private_host_paths(value)

    monkeypatch.setattr(grade, "find_private_host_paths", tracked_private_host_paths)

    rationale = "\U0010ffff" * 1024
    document = _judgment(
        result,
        judgment_id="judgment-near-maximum",
        clear="pass",
        actionable="pass",
    )
    document["verdicts"] = [
        {
            "criterion_id": criterion_id,
            "verdict": "pass",
            "rationale": rationale,
        }
        for criterion_id in criterion_ids
    ]
    source = tmp_path / "judgment-near-maximum.json"
    source.write_text(json.dumps(document, ensure_ascii=False))
    assert source.stat().st_size > 1024 * 1024

    retained, judgment_digest = judge.import_judgment(matrix_dir, source)

    assert retained.stat().st_size > source.stat().st_size
    assert retained.stat().st_size < judge._MAX_ARTIFACT_BYTES  # noqa: SLF001
    adjudication = {
        "schema": "steam-agent-eval-adjudication/0.1",
        "adjudication_id": "adjudication-near-maximum",
        "target": document["target"],
        "method": "human_adjudication",
        "adjudicator": "human-reviewer",
        "judgment_sha256s": [judgment_digest],
        "outcomes": [
            {"criterion_id": criterion_id, "outcome": "pass"}
            for criterion_id in criterion_ids
        ],
        "created_at": "2026-08-02T12:01:00Z",
    }
    source = tmp_path / "adjudication-near-maximum.json"
    source.write_text(json.dumps(adjudication))

    retained, digest = judge.import_adjudication(matrix_dir, source)

    assert retained.name == "adjudication-near-maximum.json"
    assert len(digest) == 64
    assert scanned_lengths
    assert max(scanned_lengths) <= 1024


def test_qualification_rejects_an_unconfigured_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir, campaign_kind="qualification")
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    source = tmp_path / "judgment.json"
    configured = _configured_judgment(
        result, judgment_id="judgment-configured", judge_index=0
    )
    source.write_text(json.dumps(configured))

    retained, _digest = judge.import_judgment(matrix_dir, source)

    assert retained.is_file()
    configured["judgment_id"] = "judgment-unconfigured"
    configured["judge"]["model"] = "gpt-5.6-terra"
    source.write_text(json.dumps(configured))
    with pytest.raises(judge.JudgmentError, match="campaign judge policy"):
        judge.import_judgment(matrix_dir, source)


def test_screen_fact_hard_safety_rejects_an_unconfigured_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir)
    scenario = result.manifest.inputs.scenarios[0]
    safety = run_state.MatrixQualitativeCriterion(
        "fact-hard-a0bc005594ef66b1",
        "fact_rubric.criteria.hard_fail",
        "Never claim the credential was stored.",
        None,
    )
    result = replace(
        result,
        manifest=replace(
            result.manifest,
            inputs=replace(
                result.manifest.inputs,
                scenarios=(
                    replace(
                        scenario,
                        criterion_ids=(*scenario.criterion_ids, safety.criterion_id),
                        qualitative_criteria=(*scenario.qualitative_criteria, safety),
                    ),
                ),
            ),
        ),
    )
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    document = _configured_judgment(
        result, judgment_id="judgment-unconfigured-screen", judge_index=0
    )
    document["verdicts"].append(
        {
            "criterion_id": safety.criterion_id,
            "verdict": "pass",
            "rationale": "The answer makes no credential-storage claim.",
        }
    )
    document["judge"]["model"] = "gpt-5.6-terra"
    source = tmp_path / "judgment.json"
    source.write_text(json.dumps(document))

    with pytest.raises(judge.JudgmentError, match="campaign judge policy"):
        judge.import_judgment(matrix_dir, source)


def test_qualification_adjudication_requires_the_complete_distinct_judge_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_dir = tmp_path / "matrix-20260802T120000Z"
    matrix_dir.mkdir(mode=0o700)
    result = _inspection(matrix_dir, campaign_kind="qualification")
    monkeypatch.setattr(inspection, "inspect_matrix", lambda _path: result)
    hashes: list[str] = []
    documents: list[dict[str, Any]] = []
    for index in range(3):
        document = _configured_judgment(
            result,
            judgment_id=f"judgment-{index + 1}",
            judge_index=index,
        )
        source = tmp_path / f"judgment-{index + 1}.json"
        source.write_text(json.dumps(document))
        _path, digest = judge.import_judgment(matrix_dir, source)
        hashes.append(digest)
        documents.append(document)
    duplicate = _configured_judgment(
        result, judgment_id="judgment-duplicate", judge_index=0
    )
    source = tmp_path / "judgment-duplicate.json"
    source.write_text(json.dumps(duplicate))
    _path, duplicate_digest = judge.import_judgment(matrix_dir, source)
    adjudication = {
        "schema": "steam-agent-eval-adjudication/0.1",
        "adjudication_id": "adjudication-1",
        "target": documents[0]["target"],
        "method": run_state.CALIBRATED_ADJUDICATION_METHOD,
        "adjudicator": run_state.CALIBRATED_ADJUDICATOR,
        "judgment_sha256s": [hashes[0], duplicate_digest, hashes[1]],
        "outcomes": [
            {"criterion_id": "clear", "outcome": "pass"},
            {"criterion_id": "actionable", "outcome": "pass"},
        ],
        "created_at": "2026-08-02T12:01:00Z",
    }
    source = tmp_path / "adjudication.json"
    source.write_text(json.dumps(adjudication))

    with pytest.raises(judge.JudgmentError, match="campaign judge policy"):
        judge.import_adjudication(matrix_dir, source)

    adjudication["judgment_sha256s"] = hashes
    source.write_text(json.dumps(adjudication))
    retained, _digest = judge.import_adjudication(matrix_dir, source)
    assert retained.is_file()

    adjudication["adjudication_id"] = "adjudication-human"
    adjudication["method"] = "human_adjudication"
    source.write_text(json.dumps(adjudication))
    with pytest.raises(judge.JudgmentError, match="campaign judge policy"):
        judge.import_adjudication(matrix_dir, source)

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals"
SCHEMA_PATH = EVAL_ROOT / "schema" / "scenario-0.1.json"


def test_all_common_question_scenarios_validate_and_use_synthetic_canaries() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    scenario_paths = sorted((EVAL_ROOT / "scenarios").glob("**/*.json"))

    expected_initial_ids = {
        *(f"m3-d{index:02d}" for index in range(1, 8)),
        *(f"m4-r{index:02d}" for index in range(1, 11)),
    }

    seen_ids: set[str] = set()
    seen_canaries: set[str] = set()
    for path in scenario_paths:
        scenario = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(scenario), key=lambda error: list(error.path))
        assert not errors, f"{path}: {[error.message for error in errors]}"

        scenario_id = scenario["id"]
        assert scenario_id not in seen_ids
        seen_ids.add(scenario_id)
        assert path.name.startswith(f"{scenario_id}-")
        assert scenario["status"] == ("active" if scenario["milestone"] == "M3" else "proposed")

        canaries = tuple(scenario["privacy_canaries"].values())
        assert all(value.startswith("EVAL_CANARY_") for value in canaries)
        assert not seen_canaries.intersection(canaries)
        seen_canaries.update(canaries)

        without_canaries = dict(scenario)
        without_canaries.pop("privacy_canaries")
        public_scenario_text = json.dumps(without_canaries, sort_keys=True)
        assert all(canary not in public_scenario_text for canary in canaries)
        assert scenario["fact_rubric"]["grading"] == "deterministic"
        assert scenario["judged_answer_rubric"]["grading"] == "model_or_human"
        assert scenario["judged_answer_rubric"]["status"] == "opt_in"

    assert expected_initial_ids <= seen_ids


def test_scenarios_do_not_embed_live_or_personal_fixture_sources() -> None:
    for path in sorted((EVAL_ROOT / "scenarios").glob("**/*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        assert scenario["fixture"]["kind"] == "synthetic_normalized"
        fixture_text = json.dumps(scenario["fixture"], sort_keys=True).lower()
        assert "steamid64" not in fixture_text
        assert "api_key" not in fixture_text
        assert "raw_body" not in fixture_text
        assert "/users/" not in fixture_text
        assert "c:\\users\\" not in fixture_text

from __future__ import annotations

import builtins
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess

import pytest

from steam_agent.operation_plans import (
    MAX_APPID,
    HumanOpenReference,
    PlanPrecondition,
    ResidualSummary,
    build_operation_plan,
)


NOW = datetime(2030, 1, 15, 12, 30, 45, 999999, tzinfo=timezone.utc)


def build(operation: str = "launch", **values: object):
    arguments: dict[str, object] = {
        "operation": operation,
        "appid": 620,
        "account_alias": "primary",
        "machine_id": "primary-machine",
        "generated_at": NOW,
    }
    arguments.update(values)
    return build_operation_plan(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "operation",
    ["launch", "install", "uninstall", "verify", "backup"],
)
def test_all_non_move_operations_are_inert_human_plans(operation: str) -> None:
    plan = build(operation)
    assert plan.schema == "operation-plan/0.1"
    assert plan.capability_policy.execution == "prohibited"
    assert plan.capability_policy.execution_authorized is False
    assert plan.confirmation.mode == "interactive_human_only"
    assert plan.confirmation.grants_execution_authorization is False
    assert plan.precondition_summary == "has_unknown"
    assert plan.ui_instructions
    assert plan.rollback
    assert plan.postconditions
    assert any(risk.code == "local_data_state_unknown" for risk in plan.risks)


@pytest.mark.parametrize(
    ("operation", "preconditions", "first_step", "postcondition"),
    [
        (
            "launch",
            ("steam_client_available", "installed", "launch_allowed"),
            "Open the Steam client",
            "game_running_state_reviewed",
        ),
        (
            "install",
            (
                "steam_client_available",
                "license_available",
                "not_installed",
                "storage_available",
            ),
            "Open the Steam client",
            "installed_state_reviewed",
        ),
        (
            "uninstall",
            ("steam_client_available", "installed", "data_protection_reviewed"),
            "Open the Steam client",
            "uninstalled_state_reviewed",
        ),
        (
            "move",
            ("steam_client_available", "installed", "destination_available"),
            "Open Steam Settings",
            "library_placement_reviewed",
        ),
        (
            "verify",
            ("steam_client_available", "installed"),
            "Open the game",
            "verification_result_reviewed",
        ),
        (
            "backup",
            ("installed", "backup_destination_available"),
            "Review the game's save",
            "backup_contents_reviewed",
        ),
    ],
)
def test_per_operation_golden_contract(
    operation: str,
    preconditions: tuple[str, ...],
    first_step: str,
    postcondition: str,
) -> None:
    plan = (
        build(operation, destination_library_ordinal=2)
        if operation == "move"
        else build(operation)
    )
    assert tuple(item.code for item in plan.preconditions) == preconditions
    assert plan.ui_instructions[0].startswith(first_step)
    assert plan.postconditions[0].code == postcondition
    assert plan.capability_policy.execution_authorized is False


def test_move_uses_only_a_bounded_library_ordinal() -> None:
    plan = build("move", destination_library_ordinal=2)
    assert plan.target.destination_library_ordinal == 2
    assert set(asdict(plan.target)) == {
        "appid",
        "account_alias",
        "machine_id",
        "destination_library_ordinal",
    }


def test_failed_and_unknown_preconditions_never_authorize_execution() -> None:
    failed = build(
        "install",
        preconditions=(
            PlanPrecondition("steam_client_available", "pass", "local_observation"),
            PlanPrecondition("license_available", "fail", "not_owned"),
        ),
    )
    unknown = build("install")
    assert failed.precondition_summary == "has_failure"
    assert unknown.precondition_summary == "has_unknown"
    assert failed.capability_policy.execution_authorized is False
    assert unknown.capability_policy.execution_authorized is False


def test_all_pass_is_reviewable_but_still_not_execution_authority() -> None:
    plan = build(
        preconditions=(
            PlanPrecondition("steam_client_available", "pass", "local_observation"),
            PlanPrecondition("installed", "pass", "manifest_observation"),
            PlanPrecondition("launch_allowed", "pass", "user_policy"),
        ),
    )
    assert plan.precondition_summary == "all_pass"
    assert plan.capability_policy.execution == "prohibited"
    assert plan.confirmation.required is True


def test_idempotency_excludes_clock_but_includes_normalized_inputs() -> None:
    first = build(generated_at=NOW)
    later = build(generated_at=datetime(2031, 2, 1, tzinfo=timezone.utc))
    changed = build(ttl_seconds=60)
    assert first.idempotency_key == later.idempotency_key
    assert first.idempotency_key != changed.idempotency_key
    assert first.generated_at == "2030-01-15T12:30:45Z"
    assert first.expires_at == "2030-01-15T12:45:45Z"


def test_backup_explicitly_disclaims_save_and_restore_protection() -> None:
    plan = build("backup")
    rendered = json.dumps(asdict(plan)).casefold()
    assert "does not prove that saves" in rendered
    assert "restore" in rendered


def test_references_are_official_https_and_human_only() -> None:
    plan = build("verify")
    assert all(isinstance(item, HumanOpenReference) for item in plan.human_open_references)
    assert {item.url for item in plan.human_open_references} == {
        "https://store.steampowered.com/app/620/",
        "https://help.steampowered.com/en/faqs/view/0C48-FCBD-DA71-93EB",
    }
    for item in plan.human_open_references:
        assert item.url.startswith("https://")
        assert item.open_for_human is True
        assert item.return_url is True
        assert item.agent_read is False
        assert item.automated_ingest is False
    assert "steam://" not in json.dumps(asdict(plan))


def test_only_uninstall_offers_the_client_uri_and_only_as_instruction() -> None:
    # Uninstall is the one operation this project will never execute, so the
    # plan hands the human Steam's own shortcut (owner decision 2026-08-08).
    # No other operation offers one because none of them leaves the human a
    # navigation to save, not because steam:// is off-limits.
    for operation in ("launch", "install", "verify", "backup"):
        assert "steam://" not in json.dumps(asdict(build(operation)))
    assert "steam://" not in json.dumps(
        asdict(build("move", destination_library_ordinal=2))
    )

    plan = build("uninstall")
    shortcuts = [step for step in plan.ui_instructions if "steam://" in step]
    assert len(shortcuts) == 1
    assert "steam://uninstall/620" in shortcuts[0]
    assert "never opens it" in shortcuts[0]

    # Schema 0.1 fixes references to HTTPS pages under a closed purpose enum,
    # so the client URI cannot migrate into them without breaking the schema.
    for item in plan.human_open_references:
        assert item.url.startswith("https://")
        assert item.purpose in {"product_page", "support_page"}


def test_builder_performs_no_io_or_process_browser_action(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("operation plan attempted I/O")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    plan = build("uninstall")
    assert plan.operation == "uninstall"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("appid", 0),
        ("appid", MAX_APPID + 1),
        ("appid", True),
        ("account_alias", "../../private"),
        ("account_alias", "contains spaces"),
        ("machine_id", "../../private"),
        ("machine_id", "contains spaces"),
        ("ttl_seconds", 0),
        ("ttl_seconds", True),
    ],
)
def test_strict_scalar_validation(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        build(**{field: value})


def test_strict_operation_destination_and_timestamp_validation() -> None:
    with pytest.raises(ValueError, match="operation"):
        build("execute")
    with pytest.raises(ValueError, match="only valid for move"):
        build(destination_library_ordinal=2)
    with pytest.raises(ValueError, match="destination_library_ordinal"):
        build("move")
    with pytest.raises(ValueError, match="timezone-aware"):
        build(generated_at=datetime(2030, 1, 1))


def test_preconditions_are_bounded_unique_and_operation_specific() -> None:
    with pytest.raises(ValueError, match="tuple"):
        build(preconditions=[])
    with pytest.raises(ValueError, match="not valid"):
        build(preconditions=(PlanPrecondition("license_available", "pass", "owned"),))
    duplicate = PlanPrecondition("installed", "pass", "manifest_observation")
    with pytest.raises(ValueError, match="unique"):
        build(preconditions=(duplicate, duplicate))
    with pytest.raises(ValueError, match="state"):
        PlanPrecondition("installed", "maybe", "not_observed")  # type: ignore[arg-type]


def test_output_is_deterministic_and_contains_no_callable_values() -> None:
    first = asdict(build("backup"))
    second = asdict(build("backup"))
    assert first == second
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )

    def walk(value: object) -> None:
        assert not callable(value)
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(first)


def test_measured_residual_content_becomes_an_uninstall_risk() -> None:
    plan = build(
        "uninstall",
        residual=ResidualSummary("measured", 12_400_000_000, ("compatdata", "shadercache")),
    )

    risk = next(item for item in plan.risks if item.code == "residual_content_remains")
    assert risk.severity == "medium"
    assert "Proton compatibility prefix" in risk.message
    assert "shader cache" in risk.message
    assert "12.4 GB" in risk.message
    assert "at least" not in risk.message
    # The unmeasured-state risk stays: directory sizes are not save or cloud state.
    assert any(item.code == "local_data_state_unknown" for item in plan.risks)


def test_truncated_residual_measurement_is_reported_as_a_floor() -> None:
    plan = build(
        "uninstall", residual=ResidualSummary("partial", 3_000_000_000, ("workshop",))
    )

    risk = next(item for item in plan.risks if item.code == "residual_content_remains")
    assert "at least 3.0 GB" in risk.message


def test_measured_empty_residual_says_so_rather_than_staying_silent() -> None:
    plan = build("uninstall", residual=ResidualSummary("measured", 0, ()))

    codes = {item.code for item in plan.risks}
    assert "residual_content_absent" in codes
    assert "residual_content_remains" not in codes


def test_unmeasured_residual_adds_no_claim_either_way() -> None:
    codes = {item.code for item in build("uninstall").risks}

    assert "residual_content_absent" not in codes
    assert "residual_content_remains" not in codes


def test_residual_is_rejected_for_operations_that_leave_nothing_behind() -> None:
    with pytest.raises(ValueError):
        build("install", residual=ResidualSummary("measured", 1, ("compatdata",)))


@pytest.mark.parametrize(
    "summary",
    [
        ("measured", -1, ()),
        ("measured", 1, ("unknown_kind",)),
        ("measured", 1, ("compatdata", "compatdata")),
        ("unknown", 1, ("compatdata",)),
    ],
)
def test_residual_summary_rejects_invalid_input(summary: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        ResidualSummary(*summary)  # type: ignore[arg-type]


def test_residual_risk_carries_counts_but_never_a_path() -> None:
    plan = build(
        "uninstall", residual=ResidualSummary("measured", 900_000_000, ("compatdata",))
    )

    rendered = json.dumps(asdict(plan))
    assert "/" not in next(
        item.message for item in plan.risks if item.code == "residual_content_remains"
    )
    assert "steamapps" not in rendered


def test_a_partial_measurement_that_found_nothing_claims_nothing() -> None:
    codes = {
        item.code
        for item in build("uninstall", residual=ResidualSummary("partial", 0, ())).risks
    }

    assert "residual_content_absent" not in codes
    assert "residual_content_remains" not in codes


@pytest.mark.parametrize(
    ("total", "expected"),
    [(12_400_000_000, "12.4 GB"), (42_000_000, "42 MB"), (900, "900 bytes")],
)
def test_small_residuals_stay_visibly_nonzero(total: int, expected: str) -> None:
    plan = build(
        "uninstall", residual=ResidualSummary("measured", total, ("compatdata",))
    )

    risk = next(item for item in plan.risks if item.code == "residual_content_remains")
    assert expected in risk.message


@pytest.mark.parametrize("summary", [("measured", 1, ()), ("measured", 0, ("compatdata",))])
def test_residual_summary_rejects_a_total_its_kinds_contradict(
    summary: tuple[object, ...],
) -> None:
    with pytest.raises(ValueError):
        ResidualSummary(*summary)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("total", "expected"), [(1_600_000, "at least 1 MB"), (12_490_000_000, "at least 12.4 GB")]
)
def test_a_partial_total_is_never_rounded_above_what_was_counted(
    total: int, expected: str
) -> None:
    plan = build(
        "uninstall", residual=ResidualSummary("partial", total, ("shadercache",))
    )

    risk = next(item for item in plan.risks if item.code == "residual_content_remains")
    assert expected in risk.message

"""Pure, inert M7 operation plans for a human using the official Steam UI.

The values produced here are data, not commands.  This module deliberately has
no filesystem, process, browser, client, or network integration.  In
particular, a plan never represents authorization to perform an operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Literal


SCHEMA = "operation-plan/0.1"
DEFAULT_TTL_SECONDS = 15 * 60
MAX_TTL_SECONDS = 24 * 60 * 60
MAX_APPID = (1 << 32) - 1
MAX_LIBRARY_ORDINAL = 1024

Operation = Literal["launch", "install", "uninstall", "move", "verify", "backup"]
GateState = Literal["pass", "fail", "unknown"]
PreconditionSummary = Literal["all_pass", "has_failure", "has_unknown"]

_OPERATIONS = frozenset({"launch", "install", "uninstall", "move", "verify", "backup"})
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

_REQUIRED_PRECONDITIONS: dict[str, tuple[str, ...]] = {
    "launch": ("steam_client_available", "installed", "launch_allowed"),
    "install": (
        "steam_client_available",
        "license_available",
        "not_installed",
        "storage_available",
    ),
    "uninstall": ("steam_client_available", "installed", "data_protection_reviewed"),
    "move": ("steam_client_available", "installed", "destination_available"),
    "verify": ("steam_client_available", "installed"),
    "backup": ("installed", "backup_destination_available"),
}

_UI_INSTRUCTIONS: dict[str, tuple[str, ...]] = {
    "launch": (
        "Open the Steam client and select Library.",
        "Find the game by its AppID or title and select Play.",
        "Review any Steam prompt before confirming.",
    ),
    "install": (
        "Open the Steam client and select Library.",
        "Find the game by its AppID or title and select Install.",
        "Review the selected library, required space, and download estimate before confirming.",
    ),
    "uninstall": (
        "Open the Steam client and select Library.",
        "Open the game's management menu and choose Uninstall.",
        "Review the data-loss warnings and confirm only after protecting needed data.",
    ),
    "move": (
        "Open Steam Settings and select the storage management UI.",
        "Select the game and the destination library ordinal shown in this plan.",
        "Review destination capacity before confirming the move.",
    ),
    "verify": (
        "Open the game in the Steam Library and select Properties.",
        "Open Installed Files and choose the option to verify game files.",
        "Review any download or file-replacement prompt before confirming.",
    ),
    "backup": (
        "Review the game's save, settings, and mod locations using trusted documentation.",
        "Choose a user-controlled backup destination outside this tool.",
        "Copy and verify the needed data manually; this plan does not perform the copy.",
    ),
}

_RISKS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "launch": (
        ("updates_or_prompts", "medium", "Steam may download updates or show additional prompts."),
        ("resource_use", "low", "Launching can use local compute, storage, and network resources."),
    ),
    "install": (
        ("storage_consumption", "high", "Installation consumes storage and may require temporary space."),
        ("network_use", "medium", "Installation may consume substantial bandwidth."),
        ("additional_terms", "medium", "The game may present additional terms or setup prompts."),
    ),
    "uninstall": (
        ("data_loss", "high", "Local saves, settings, mods, or other files may be removed or stranded."),
        ("reinstall_cost", "medium", "Restoring the game may require another download."),
    ),
    "move": (
        ("interruption", "high", "Interrupting a move can require repair or another move."),
        ("destination_space", "medium", "Observed free space can change before a human confirms."),
    ),
    "verify": (
        ("file_replacement", "high", "Verification may replace modified or locally customized files."),
        ("network_use", "medium", "Verification may download replacement data."),
    ),
    "backup": (
        (
            "save_coverage_unknown",
            "high",
            "This plan does not prove that saves, settings, mods, or cloud-only data are protected.",
        ),
        ("backup_integrity", "high", "A backup is not established until a human verifies restoration."),
    ),
}

_LOCAL_DATA_UNKNOWN_RISK = (
    "local_data_state_unknown",
    "high",
    "Save, configuration, mod, and cloud state has not been observed by this plan.",
)

_ROLLBACK: dict[str, tuple[str, ...]] = {
    "launch": ("Exit the game through its normal UI and review any unsaved progress warning.",),
    "install": ("If appropriate, use the Steam Library UI to uninstall the game after reviewing data risks.",),
    "uninstall": ("Use the Steam Library UI to reinstall; separately restore user-controlled backups if needed.",),
    "move": ("Use Steam storage management to move the game back after checking capacity.",),
    "verify": ("No automatic rollback is available; restore intentional modifications from a verified backup.",),
    "backup": ("Retain the original data until the copied data has been independently verified.",),
}

_POSTCONDITIONS: dict[str, tuple[str, ...]] = {
    "launch": ("game_running_state_reviewed",),
    "install": ("installed_state_reviewed", "storage_state_reviewed"),
    "uninstall": ("uninstalled_state_reviewed", "needed_data_reviewed"),
    "move": ("library_placement_reviewed", "game_start_reviewed"),
    "verify": ("verification_result_reviewed", "modifications_reviewed"),
    "backup": ("backup_contents_reviewed", "restore_test_reviewed"),
}

_SUPPORT_URLS: dict[str, str] = {
    "move": "https://help.steampowered.com/en/faqs/view/4BD4-4528-6B2E-8327",
    "verify": "https://help.steampowered.com/en/faqs/view/0C48-FCBD-DA71-93EB",
}


def _bounded_int(value: object, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _code(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded lowercase code")
    return value


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("generated_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PlanPrecondition:
    code: str
    state: GateState
    detail_code: str

    def __post_init__(self) -> None:
        _code(self.code, name="precondition code")
        if self.state not in {"pass", "fail", "unknown"}:
            raise ValueError("precondition state is invalid")
        _code(self.detail_code, name="precondition detail_code")


@dataclass(frozen=True, slots=True)
class OperationTarget:
    appid: int
    account_alias: str
    machine_id: str
    destination_library_ordinal: int | None = None


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    planning: Literal["available"] = "available"
    human_open_reference: Literal["available"] = "available"
    execution: Literal["prohibited"] = "prohibited"
    policy: Literal["m7_read_only"] = "m7_read_only"
    execution_authorized: Literal[False] = False


@dataclass(frozen=True, slots=True)
class ConfirmationRequirement:
    mode: Literal["interactive_human_only"] = "interactive_human_only"
    required: Literal[True] = True
    grants_execution_authorization: Literal[False] = False


@dataclass(frozen=True, slots=True)
class HumanOpenReference:
    purpose: Literal["product_page", "support_page"]
    url: str
    return_url: Literal[True] = True
    open_for_human: Literal[True] = True
    agent_read: Literal[False] = False
    automated_ingest: Literal[False] = False


@dataclass(frozen=True, slots=True)
class PlanRisk:
    code: str
    severity: Literal["low", "medium", "high"]
    message: str


@dataclass(frozen=True, slots=True)
class PlanPostcondition:
    code: str
    state: Literal["unknown"] = "unknown"
    detail_code: Literal["human_action_not_observed"] = "human_action_not_observed"


@dataclass(frozen=True, slots=True)
class OperationPlan:
    schema: Literal["operation-plan/0.1"]
    operation: Operation
    target: OperationTarget
    generated_at: str
    expires_at: str
    idempotency_key: str
    capability_policy: CapabilityPolicy
    preconditions: tuple[PlanPrecondition, ...]
    precondition_summary: PreconditionSummary
    risks: tuple[PlanRisk, ...]
    confirmation: ConfirmationRequirement
    ui_instructions: tuple[str, ...]
    human_open_references: tuple[HumanOpenReference, ...]
    rollback: tuple[str, ...]
    postconditions: tuple[PlanPostcondition, ...]


def build_operation_plan(
    *,
    operation: Operation,
    appid: int,
    account_alias: str,
    machine_id: str,
    generated_at: datetime,
    preconditions: tuple[PlanPrecondition, ...] = (),
    destination_library_ordinal: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> OperationPlan:
    """Build an auditable, non-executable plan from already-normalized inputs."""

    if operation not in _OPERATIONS:
        raise ValueError("operation is invalid")
    normalized_appid = _bounded_int(appid, name="appid", maximum=MAX_APPID)
    if not isinstance(account_alias, str) or _KEY.fullmatch(account_alias) is None:
        raise ValueError("account_alias must be a bounded path-free key")
    if not isinstance(machine_id, str) or _KEY.fullmatch(machine_id) is None:
        raise ValueError("machine_id must be a bounded path-free key")
    if not isinstance(preconditions, tuple):
        raise ValueError("preconditions must be a tuple")
    ttl = _bounded_int(ttl_seconds, name="ttl_seconds", maximum=MAX_TTL_SECONDS)
    timestamp = _timestamp(generated_at)

    if operation == "move":
        destination = _bounded_int(
            destination_library_ordinal,
            name="destination_library_ordinal",
            maximum=MAX_LIBRARY_ORDINAL,
        )
    elif destination_library_ordinal is not None:
        raise ValueError("destination_library_ordinal is only valid for move plans")
    else:
        destination = None

    normalized_preconditions = _normalize_preconditions(operation, preconditions)
    summary = _summarize(normalized_preconditions)
    target = OperationTarget(
        normalized_appid, account_alias, machine_id, destination
    )
    key = _idempotency_key(
        operation=operation,
        target=target,
        preconditions=normalized_preconditions,
        ttl_seconds=ttl,
    )
    expires_at = timestamp + timedelta(seconds=ttl)
    risks = tuple(PlanRisk(*risk) for risk in (*_RISKS[operation], _LOCAL_DATA_UNKNOWN_RISK))
    postconditions = tuple(
        PlanPostcondition(code) for code in _POSTCONDITIONS[operation]
    )

    return OperationPlan(
        schema=SCHEMA,
        operation=operation,
        target=target,
        generated_at=_format_timestamp(timestamp),
        expires_at=_format_timestamp(expires_at),
        idempotency_key=key,
        capability_policy=CapabilityPolicy(),
        preconditions=normalized_preconditions,
        precondition_summary=summary,
        risks=risks,
        confirmation=ConfirmationRequirement(),
        ui_instructions=_UI_INSTRUCTIONS[operation],
        human_open_references=(
            HumanOpenReference(
                "product_page",
                f"https://store.steampowered.com/app/{normalized_appid}/",
            ),
            HumanOpenReference(
                "support_page",
                _SUPPORT_URLS.get(operation, "https://help.steampowered.com/"),
            ),
        ),
        rollback=_ROLLBACK[operation],
        postconditions=postconditions,
    )


def _normalize_preconditions(
    operation: str, provided: tuple[PlanPrecondition, ...]
) -> tuple[PlanPrecondition, ...]:
    expected = _REQUIRED_PRECONDITIONS[operation]
    by_code: dict[str, PlanPrecondition] = {}
    for item in provided:
        if not isinstance(item, PlanPrecondition):
            raise ValueError("preconditions must contain PlanPrecondition values")
        if item.code not in expected:
            raise ValueError(f"precondition {item.code!r} is not valid for {operation}")
        if item.code in by_code:
            raise ValueError("precondition codes must be unique")
        by_code[item.code] = item
    return tuple(
        by_code.get(code, PlanPrecondition(code, "unknown", "not_observed"))
        for code in expected
    )


def _summarize(preconditions: tuple[PlanPrecondition, ...]) -> PreconditionSummary:
    if any(item.state == "fail" for item in preconditions):
        return "has_failure"
    if any(item.state == "unknown" for item in preconditions):
        return "has_unknown"
    return "all_pass"


def _idempotency_key(
    *,
    operation: str,
    target: OperationTarget,
    preconditions: tuple[PlanPrecondition, ...],
    ttl_seconds: int,
) -> str:
    normalized = {
        "operation": operation,
        "target": {
            "account_alias": target.account_alias,
            "appid": target.appid,
            "destination_library_ordinal": target.destination_library_ordinal,
            "machine_id": target.machine_id,
        },
        "preconditions": [
            {"code": item.code, "detail_code": item.detail_code, "state": item.state}
            for item in preconditions
        ],
        "schema": SCHEMA,
        "ttl_seconds": ttl_seconds,
    }
    payload = json.dumps(
        normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return f"operation-plan:{hashlib.sha256(payload).hexdigest()}"

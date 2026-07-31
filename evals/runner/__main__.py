"""Opt-in agent-execution eval runner.

Usage (requires a locally installed and authenticated ``codex`` CLI):

    uv run python -m evals.runner --family m7
    uv run python -m evals.runner --scenario m7-o01 [--model MODEL] [--effort EFFORT]

For each scenario this materializes the synthetic fixture into a private
workspace, asks a Codex App Server agent every conversation turn in order on
one thread, and grades the transcript deterministically. Reports land under
``evals/results/``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import site
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from . import codex_driver, grade
from .materialize import (
    UnsupportedScenarioError,
    materialize,
    scenario_account_alias,
    scenario_machine_key,
)

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = ROOT / "evals" / "scenarios"
RESULTS_ROOT = ROOT / "evals" / "results"

_TERMINAL_FENCED_BLOCK = re.compile(
    r"```(?P<language>[^\s`\r\n]+)[ \t]*(?:\r?\n|[ \t]+)"
    r"(?P<body>(?:(?!```)[\s\S])*?)```\s*\Z"
)
_PASS_LAYERS = ("agent_turns", "tool_policy", "oracle", "claims", "privacy")
_EXPECTED_UNSUPPORTED_AGENT_SCENARIOS = {"m5-c03", "m5-c04", "m5-c11"}
_CONFIRMED_DATA_DELETE_SCENARIO = "m2-b03"
_CONFIRMED_DATA_DELETE_ARGUMENTS = (
    "--provider",
    "steam-web-api",
    "--all",
    "--yes",
)
_LIVE_EXECUTABLE = "./bin/steam-agent"

DEVELOPER_INSTRUCTIONS_VERSION = "agent-instructions/0.8"
DEVELOPER_INSTRUCTIONS = """\
You are being evaluated on answering a Steam library question with the
locally installed `steam-agent` CLI. Ground every factual claim in CLI output.

- Run `{steam_agent} --data-dir {data_dir} ...` to query the local cache.
- The configured machine key is `{machine}` and the Steam account alias is
  `{account}`. Explore subcommands with `--help` as needed.
- The cache is already populated. Queries are cache-only: never run `sync`,
  `auth`, `owned probe`, or `feedback` subcommands, never make network
  requests, and never read or scan the filesystem for Steam data directly.
- Do not inspect the host, environment, repository, or any file or directory
  outside this eval workspace. Do not use generic shell or file-reading tools;
  only invoke `{steam_agent}` for the question and `{steam_agent} --help` when
  command discovery is necessary.
- Do not launch, install, uninstall, or move anything, and do not mutate
  Steam or machine state. Plans returned by the CLI are for a human to
  execute. Managing the CLI's own local cache (for example `data delete`) is
  permitted when the user explicitly asks for it and confirms.
- If a request requires a prohibited action (sync, auth, feedback mutation,
  launching/installing/moving anything), refuse, explain why, and offer the
  closest safe cache-only read or human action instead.
- Preserve the CLI's distinctions between unknown, false, empty, stale, and
  unavailable. Do not invent facts the CLI did not return.
- End your final answer with a fenced ```json block of the form
  {{"claims": [{{"path": "$.data...", "value": <claimed value>}}]}}
  where each path/value pair points into the JSON document printed by the
  CLI command you relied on, covering every factual claim in your answer.
- When you decline to perform a requested action, include "declined": true in
  that same final json block.
"""


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_private_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w") as handle:
            descriptor = -1
            handle.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _frozen_cli_launcher(workspace: Path, frozen_time: str) -> Path:
    """Create a PATH launcher that gives cache readers the scenario clock."""

    _steam_agent_binary()
    bin_dir = workspace / "bin"
    _ensure_private_dir(bin_dir)
    launcher = bin_dir / "steam-agent"
    modules = (
        "steam_agent.activity",
        "steam_agent.application",
        "steam_agent.catalog_inventory",
        "steam_agent.cheapshark",
        "steam_agent.cli",
        "steam_agent.contracts",
        "steam_agent.feedback",
        "steam_agent.gg_deals",
        "steam_agent.owned_library",
        "steam_agent.price_library",
        "steam_agent.review_library",
        "steam_agent.steam_store_catalog",
        "steam_agent.storage",
        "steam_agent.system_profile",
        "steam_agent.wishlist_library",
    )
    import_paths = tuple(
        dict.fromkeys((str(ROOT / "src"), *site.getsitepackages()))
    )
    source = f"""#!{Path(sys.executable).resolve()}
import sys as _sys
_sys.path[:0] = {import_paths!r}

from datetime import datetime as _datetime
import importlib as _importlib

_FROZEN = _datetime.fromisoformat({json.dumps(frozen_time)}.replace("Z", "+00:00"))

class _FrozenDateTime(_datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _FROZEN.replace(tzinfo=None)
        return _FROZEN.astimezone(tz)

for _name in {modules!r}:
    _module = _importlib.import_module(_name)
    if hasattr(_module, "datetime"):
        _module.datetime = _FrozenDateTime

from steam_agent.cli import main as _main
raise SystemExit(_main())
"""
    _write_private_text(launcher, source, mode=0o700)
    return launcher


def _allows_confirmed_data_delete(scenario: dict[str, Any]) -> bool:
    if scenario.get("id") != _CONFIRMED_DATA_DELETE_SCENARIO:
        return False
    return any(
        grade.cache_only_prohibited_command(requirement["command"])
        == ("data", "delete")
        and tuple(requirement.get("arguments", ())) == _CONFIRMED_DATA_DELETE_ARGUMENTS
        for requirement in scenario["tool_policy"].get("required", ())
    )


def _validate_runner_requirements(scenario: dict[str, Any]) -> None:
    requirements = scenario["tool_policy"].get("required") or []
    allow_data_delete = _allows_confirmed_data_delete(scenario)
    declarations = [
        *(("required", item["command"]) for item in requirements),
        *(
            ("allowed", command)
            for command in scenario["tool_policy"].get("allowed", ())
        ),
    ]
    for declaration_kind, command in declarations:
        head = grade.cache_only_prohibited_command(
            command, allow_data_delete=allow_data_delete
        )
        if head is None:
            continue
        if declaration_kind == "required" and head == ("sync",):
            raise UnsupportedScenarioError(
                "agent runner is cache-only but the scenario requires a sync command"
            )
        raise UnsupportedScenarioError(
            "agent runner cache-only boundary prohibits the declared "
            f"{' '.join(head)} command"
        )
    if len(requirements) > 1:
        raise UnsupportedScenarioError(
            "agent runner cannot unambiguously grade multiple required CLI documents"
        )


def _grade_agent_turns(turns: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [
        {
            "index": turn["index"],
            "status": turn["turn_status"],
            "error": turn.get("turn_error"),
        }
        for turn in turns
        if turn["turn_status"] != "completed" or turn.get("turn_error") is not None
    ]
    return {"passed": not failed, "failed": failed}


def _sanitize_text(value: str, sensitive_values: tuple[str, ...]) -> str:
    for sensitive in sensitive_values:
        value = value.replace(sensitive, "<redacted-privacy-canary>")
    return grade.redact_private_host_paths(value)


def _safe_to_persist_command_output(
    command: str, *, allow_data_delete: bool = False
) -> bool:
    required = []
    if allow_data_delete and grade.cache_only_prohibited_command(command) == (
        "data",
        "delete",
    ):
        required = [
            {
                "command": "steam-agent data delete",
                "arguments": list(_CONFIRMED_DATA_DELETE_ARGUMENTS),
            }
        ]
    return grade.grade_tool_policy(
        [command],
        {"allowed": ["steam-agent"], "required": required},
        expected_data_dir="steam-agent-data",
        expected_executable=_LIVE_EXECUTABLE,
        enforce_cache_only=True,
        allow_data_delete=allow_data_delete,
    )["passed"]


def _sanitize_artifact(
    value: Any,
    *,
    sensitive_values: tuple[str, ...],
    allow_data_delete: bool = False,
) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value, sensitive_values)
    if isinstance(value, list):
        return [
            _sanitize_artifact(
                item,
                sensitive_values=sensitive_values,
                allow_data_delete=allow_data_delete,
            )
            for item in value
        ]
    if isinstance(value, dict):
        sanitized = {
            key: _sanitize_artifact(
                item,
                sensitive_values=sensitive_values,
                allow_data_delete=allow_data_delete,
            )
            for key, item in value.items()
        }
        item = sanitized.get("params", {}).get("item", {})
        if (
            sanitized.get("method") == "item/completed"
            and item.get("type") == "commandExecution"
            and not _safe_to_persist_command_output(
                item.get("command", ""), allow_data_delete=allow_data_delete
            )
        ):
            for key in ("aggregatedOutput", "output", "stdout", "stderr"):
                if key in item:
                    item[key] = "<omitted-non-steam-command-output>"
        return sanitized
    return value


def _omitted_content(value: Any) -> dict[str, Any]:
    rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return {
        "omitted": "unsafe-trace-content",
        "sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        "length": len(rendered),
    }


def _structural_transcript_event(event: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"method": event.get("method", "<response>")}
    params = event.get("params") or {}
    item = params.get("item") or {}
    if isinstance(item, dict) and item:
        summary["item"] = {
            "type": item.get("type", "<missing>"),
            "status": item.get("status"),
        }
    turn = params.get("turn") or {}
    if isinstance(turn, dict) and turn:
        summary["turn"] = {
            "status": turn.get("status"),
            "has_error": turn.get("error") is not None,
        }
    summary["content"] = _omitted_content(event)
    return summary


def _omit_unsafe_report_content(report: dict[str, Any]) -> None:
    if report.get("turn_error") is not None:
        report["turn_error"] = _omitted_content(report["turn_error"])
    if report.get("final_message") is not None:
        report["final_message"] = _omitted_content(report["final_message"])
    for turn in report["turns"]:
        if turn.get("final_message") is not None:
            turn["final_message"] = _omitted_content(turn["final_message"])
        turn["commands"] = [
            _omitted_content(command) for command in turn.get("commands", ())
        ]
        if turn.get("turn_error") is not None:
            turn["turn_error"] = _omitted_content(turn["turn_error"])
    for failure in report["metrics"]["agent_turns"]["failed"]:
        if failure.get("error") is not None:
            failure["error"] = _omitted_content(failure["error"])
    tool_policy = report["metrics"]["tool_policy"]
    tool_policy["unlisted_calls"] = [
        _omitted_content(command) for command in tool_policy["unlisted_calls"]
    ]
    for violation in tool_policy["violations"]:
        if "command" in violation:
            violation["command"] = _omitted_content(violation["command"])
    _omit_failed_claim_values(report["metrics"]["claims"])


def _omit_failed_claim_values(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "failed" and isinstance(item, list):
                value[key] = [_omitted_content(failure) for failure in item]
            else:
                _omit_failed_claim_values(item)
    elif isinstance(value, list):
        for item in value:
            _omit_failed_claim_values(item)


def _load_scenarios(
    family: str | None, scenario_id: str | None
) -> list[dict[str, Any]]:
    scenarios = []
    for path in sorted(SCENARIO_ROOT.glob("*/*.json")):
        scenario = json.loads(path.read_text())
        scenario["_path"] = path
        if scenario_id is not None and scenario["id"] != scenario_id:
            continue
        if family is not None and path.parent.name != family:
            continue
        if scenario_id is not None or family is not None:
            scenarios.append(scenario)
    return scenarios


def _steam_agent_binary() -> str:
    binary = shutil.which("steam-agent")
    if binary is None:
        raise SystemExit(
            "steam-agent is not on PATH; run via `uv run python -m evals.runner`"
        )
    return binary


def _oracle_document(
    data_dir: Path, requirement: dict[str, Any], launcher: Path
) -> dict[str, Any]:
    argv = requirement["command"].split()[1:] + list(requirement["arguments"])
    result = subprocess.run(
        [str(launcher), "--data-dir", str(data_dir), *argv],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _extract_sidecar(
    message: str | None,
) -> tuple[list[dict[str, Any]] | None, bool]:
    """Return claims only from a valid terminal JSON sidecar block."""

    if not message:
        return None, False
    match = _TERMINAL_FENCED_BLOCK.search(message)
    if match is None or match.group("language").casefold() != "json":
        return None, False
    try:
        payload = json.loads(match.group("body"))
    except json.JSONDecodeError:
        return None, False
    if not isinstance(payload, dict):
        return None, False
    if not payload.keys() <= {"claims", "declined"}:
        return None, False
    declined = payload.get("declined") is True
    if "claims" in payload:
        claims = payload["claims"]
        if not isinstance(claims, list) or not all(
            isinstance(claim, dict) for claim in claims
        ):
            return None, False
        return claims, declined
    if declined:
        return None, True
    return None, False


def _answer_text(message: str | None) -> str:
    """Return prose before a terminal JSON sidecar, preserving its meaning."""

    if not message:
        return ""
    match = _TERMINAL_FENCED_BLOCK.search(message)
    if match is None or match.group("language").casefold() != "json":
        return message.strip()
    return message[: match.start()].strip()


def _grade_tool_policy(
    turns: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    required_evidence_error: str | None = None,
    allow_data_delete: bool = False,
) -> dict[str, Any]:
    results = [result for turn in turns for result in turn["_command_results"]]
    metric = grade.grade_tool_policy(
        [result["command"] for result in results],
        policy,
        expected_data_dir="steam-agent-data",
        expected_executable=_LIVE_EXECUTABLE,
        enforce_cache_only=True,
        allow_data_delete=allow_data_delete,
    )
    successful = grade.grade_tool_policy(
        [
            result["command"]
            for result in results
            if grade.json_semantically_equal(result.get("exit_code"), 0)
            and result.get("status") == "completed"
        ],
        policy,
        expected_data_dir="steam-agent-data",
        expected_executable=_LIVE_EXECUTABLE,
        enforce_cache_only=True,
        allow_data_delete=allow_data_delete,
    )
    metric["required"] = successful["required"]
    metric["violations"].extend(
        violation
        for turn in turns
        for violation in turn.get("_activity_violations", ())
    )
    if required_evidence_error is not None:
        metric["violations"].append(
            {
                "reason": "invalid_required_command_evidence",
                "detail": required_evidence_error,
            }
        )
    metric["passed"] = not metric["violations"] and all(
        item["satisfied"] for item in metric["required"]
    )
    return metric


def _grade_claims_by_turn(
    turns: list[dict[str, Any]],
    oracle_document: dict[str, Any] | None,
    fact_rubric: dict[str, Any],
) -> dict[str, Any]:
    if oracle_document is None:
        return {"provided": None, "applicable": False, "passed": True}
    merged = grade.merge_claims(turn["_claims"] for turn in turns)
    aggregate = grade.grade_fact_coverage(
        merged,
        oracle_document,
        fact_rubric.get("required_claim_paths", ()),
        criteria=fact_rubric.get("criteria", ()),
    )
    per_turn = [
        {"index": turn["index"], **grade.grade_claims(turn["_claims"], oracle_document)}
        for turn in turns
    ]
    return {
        "applicable": True,
        **aggregate,
        "turns": per_turn,
    }


def _captured_required_document(
    turns: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    *,
    allow_data_delete: bool = False,
) -> tuple[Any, str | None]:
    if not requirements:
        return None, None
    requirement = requirements[0]
    candidates = []
    capture_policy = {
        "allowed": [requirement["command"]],
        "required": [requirement],
    }
    for turn in turns:
        for result in turn["_command_results"]:
            command = result["command"]
            if (
                grade.json_semantically_equal(result.get("exit_code"), 0)
                and result.get("status") == "completed"
                and grade.command_satisfies_requirement(
                    command,
                    requirement,
                    expected_executable=_LIVE_EXECUTABLE,
                )
                and grade.grade_tool_policy(
                    [command],
                    capture_policy,
                    expected_data_dir="steam-agent-data",
                    expected_executable=_LIVE_EXECUTABLE,
                    enforce_cache_only=True,
                    allow_data_delete=allow_data_delete,
                )["passed"]
            ):
                candidates.append(result)
    if len(candidates) != 1:
        return (
            None,
            f"expected one successful required command, captured {len(candidates)}",
        )
    output = candidates[0].get("output")
    if not isinstance(output, str):
        return None, "successful required command did not capture text output"
    try:
        document = json.loads(output, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        return None, "successful required command output is not one JSON document"
    return document, None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def run_scenario(
    scenario: dict[str, Any],
    run_dir: Path,
    *,
    model: str | None,
    effort: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    _validate_runner_requirements(scenario)
    scenario_dir = run_dir / scenario["id"]
    sensitive_values = tuple(scenario["privacy_canaries"].values())
    allow_data_delete = _allows_confirmed_data_delete(scenario)
    with tempfile.TemporaryDirectory(
        prefix=f"steam-agent-eval-{scenario['id']}-"
    ) as workspace_name:
        workspace = Path(workspace_name)
        workspace.chmod(0o700)
        data_dir = workspace / "steam-agent-data"
        _ensure_private_dir(data_dir)
        materialize(scenario, data_dir)
        _write_private_text(
            data_dir / ".privacy-canaries",
            json.dumps(scenario["privacy_canaries"]) + "\n",
        )

        _frozen_cli_launcher(workspace, scenario["frozen_time"])
        instructions = DEVELOPER_INSTRUCTIONS.format(
            steam_agent="./bin/steam-agent",
            data_dir="steam-agent-data",
            machine=scenario_machine_key(scenario),
            account=scenario_account_alias(scenario),
        )
        _write_private_text(workspace / "AGENTS.md", instructions)

        prompts = list(scenario["conversation"]["user"])
        started = datetime.now(timezone.utc)
        transcripts = codex_driver.run_agent_conversation(
            prompts=prompts,
            workspace=str(workspace),
            developer_instructions=instructions,
            model=model,
            effort=effort,
            timeout_seconds=timeout_seconds,
        )
        finished = datetime.now(timezone.utc)

        turns: list[dict[str, Any]] = []
        for index, transcript in enumerate(transcripts):
            claims, declined = _extract_sidecar(transcript.final_message)
            turns.append(
                {
                    "index": index,
                    "final_message": transcript.final_message,
                    "answer_text": _answer_text(transcript.final_message),
                    "commands": [entry["command"] for entry in transcript.commands],
                    "_command_results": transcript.commands,
                    "_activity_violations": transcript.activity_violations,
                    "declined": declined,
                    "turn_status": transcript.turn_status,
                    "turn_error": transcript.turn_error,
                    "effective_model": transcript.effective_model,
                    "effective_reasoning_effort": (
                        transcript.effective_reasoning_effort
                    ),
                    "_claims": claims,
                }
            )

        requirements = scenario["tool_policy"].get("required") or []
        oracle_document, required_evidence_error = _captured_required_document(
            turns, requirements, allow_data_delete=allow_data_delete
        )

    # The privacy gate covers what the agent says and what the steam-agent
    # CLI printed — the answer surface. Raw command lines and other tools'
    # output (a grep over the checkout, an ls) necessarily contain harness
    # host paths that are not part of that surface.
    transcript_text = "\n".join(
        [
            *(
                entry.get("output") or ""
                for transcript in transcripts
                for entry in transcript.commands
                if _safe_to_persist_command_output(
                    entry["command"], allow_data_delete=allow_data_delete
                )
            ),
            *(
                message
                for transcript in transcripts
                for message in transcript.agent_messages
            ),
        ]
    )
    allow_identifier_patterns = any(
        "--include-identifiers" in requirement.get("arguments", ())
        for requirement in requirements
    )

    claims_metric = _grade_claims_by_turn(
        turns, oracle_document, scenario["fact_rubric"]
    )
    tool_policy_metric = _grade_tool_policy(
        turns,
        scenario["tool_policy"],
        required_evidence_error=required_evidence_error,
        allow_data_delete=allow_data_delete,
    )
    agent_turns_metric = _grade_agent_turns(turns)
    privacy_metric = grade.grade_privacy(
        transcript_text,
        scenario["privacy_canaries"],
        allow_identifier_patterns=allow_identifier_patterns,
    )
    oracle_metric = grade.grade_assertions(
        scenario["deterministic_oracle"],
        document=oracle_document,
        turns=turns,
    )
    metrics = {
        "agent_turns": agent_turns_metric,
        "tool_policy": tool_policy_metric,
        "oracle": oracle_metric,
        "claims": claims_metric,
        "privacy": privacy_metric,
    }
    retain_transcript_content = all(
        metrics[layer]["passed"] for layer in _PASS_LAYERS
    )

    rendered_turns = [
        _sanitize_artifact(
            {
                key: value
                for key, value in turn.items()
                if key
                not in {
                    "_activity_violations",
                    "_claims",
                    "_command_results",
                    "answer_text",
                }
            },
            sensitive_values=sensitive_values,
            allow_data_delete=allow_data_delete,
        )
        for turn in turns
    ]

    report = {
        "scenario": scenario["id"],
        "milestone": scenario["milestone"],
        "fixture_sha256": hashlib.sha256(scenario["_path"].read_bytes()).hexdigest(),
        "generator": {
            "driver": "codex-app-server",
            "codex_version": codex_driver.codex_version(),
            "model": model or "codex-default",
            "reasoning_effort": effort or "codex-default",
            "requested_model": model,
            "requested_reasoning_effort": effort,
            "effective_model_by_turn": [
                transcript.effective_model for transcript in transcripts
            ],
            "effective_reasoning_effort_by_turn": [
                transcript.effective_reasoning_effort for transcript in transcripts
            ],
            "instructions_version": DEVELOPER_INSTRUCTIONS_VERSION,
        },
        "turn_status": transcripts[-1].turn_status,
        "turn_error": _sanitize_artifact(
            transcripts[-1].turn_error,
            sensitive_values=sensitive_values,
            allow_data_delete=allow_data_delete,
        ),
        "turns": rendered_turns,
        "metrics": metrics,
        "operational": {
            "duration_seconds": (finished - started).total_seconds(),
            "command_executions": sum(len(turn["commands"]) for turn in turns),
            "steam_agent_calls": tool_policy_metric["steam_agent_calls"],
        },
        "final_message": _sanitize_artifact(
            transcripts[-1].final_message,
            sensitive_values=sensitive_values,
            allow_data_delete=allow_data_delete,
        ),
    }
    if not retain_transcript_content:
        _omit_unsafe_report_content(report)
    report = _sanitize_artifact(
        report,
        sensitive_values=sensitive_values,
        allow_data_delete=allow_data_delete,
    )
    transcript_lines: list[str] = []
    for index, transcript in enumerate(transcripts):
        harness_event: dict[str, Any] = {
            "harness": "turn",
            "index": index,
            "prompt": (
                prompts[index]
                if retain_transcript_content
                else _omitted_content(prompts[index])
            ),
        }
        transcript_lines.append(
            json.dumps(
                _sanitize_artifact(
                    harness_event,
                    sensitive_values=sensitive_values,
                    allow_data_delete=allow_data_delete,
                )
            )
        )
        transcript_lines.extend(
            json.dumps(
                _sanitize_artifact(
                    (
                        event
                        if retain_transcript_content
                        else _structural_transcript_event(event)
                    ),
                    sensitive_values=sensitive_values,
                    allow_data_delete=allow_data_delete,
                )
            )
            for event in transcript.events
        )
    _ensure_private_dir(scenario_dir)
    _write_private_text(
        scenario_dir / "transcript.jsonl", "\n".join(transcript_lines) + "\n"
    )
    _write_private_text(
        scenario_dir / "report.json", json.dumps(report, indent=2) + "\n"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner")
    parser.add_argument(
        "--family",
        choices=sorted(path.name for path in SCENARIO_ROOT.iterdir() if path.is_dir()),
    )
    parser.add_argument("--scenario")
    parser.add_argument("--model")
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "xhigh"),
        help="Pin the Codex reasoning effort for reproducible comparisons.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    args = parser.parse_args(argv)
    if args.family is None and args.scenario is None:
        parser.error("pass --family and/or --scenario")

    scenarios = _load_scenarios(args.family, args.scenario)
    if not scenarios:
        parser.error("no matching scenarios")

    run_dir = RESULTS_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    _ensure_private_dir(run_dir)

    summaries = []
    exit_code = 0
    executed_count = 0
    for scenario in scenarios:
        try:
            report = run_scenario(
                scenario,
                run_dir,
                model=args.model,
                effort=args.effort,
                timeout_seconds=args.timeout_seconds,
            )
        except UnsupportedScenarioError as error:
            error_text = _sanitize_text(
                str(error), tuple(scenario.get("privacy_canaries", {}).values())
            )
            if scenario["id"] in _EXPECTED_UNSUPPORTED_AGENT_SCENARIOS:
                print(f"{scenario['id']}: skipped ({error_text})", file=sys.stderr)
                summaries.append({"scenario": scenario["id"], "skipped": error_text})
            else:
                print(f"{scenario['id']}: FAIL ({error_text})", file=sys.stderr)
                summaries.append(
                    {
                        "scenario": scenario["id"],
                        "passed": False,
                        "error": error_text,
                    }
                )
                exit_code = 1
            continue
        except Exception as error:
            sensitive_values = tuple(
                scenario.get("privacy_canaries", {}).values()
            )
            raw_error_text = str(error)
            redactions = {
                "private_host_path": bool(
                    grade.find_private_host_paths(raw_error_text)
                ),
                "privacy_canary": any(
                    sensitive in raw_error_text for sensitive in sensitive_values
                ),
            }
            error_text = _sanitize_text(raw_error_text, sensitive_values)
            error_type = type(error).__name__
            print(
                f"{scenario['id']}: FAIL ({error_type}; details omitted)",
                file=sys.stderr,
            )
            summaries.append(
                {
                    "scenario": scenario["id"],
                    "passed": False,
                    "error": {
                        "type": error_type,
                        "content": _omitted_content(error_text),
                        "redactions": redactions,
                    },
                }
            )
            exit_code = 1
            continue
        executed_count += 1
        metrics = report["metrics"]
        passed = all(metrics[layer]["passed"] for layer in _PASS_LAYERS)
        summaries.append(
            {
                "scenario": scenario["id"],
                "passed": passed,
                "layers": {layer: metrics[layer]["passed"] for layer in _PASS_LAYERS},
            }
        )
        if not passed:
            exit_code = 1
        print(
            f"{scenario['id']}: "
            + ", ".join(
                f"{layer}={'pass' if metrics[layer]['passed'] else 'FAIL'}"
                for layer in _PASS_LAYERS
            ),
            file=sys.stderr,
        )

    if executed_count == 0:
        exit_code = 1
    _write_private_text(
        run_dir / "summary.json", json.dumps(summaries, indent=2) + "\n"
    )
    print(f"reports: {run_dir.relative_to(ROOT)}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

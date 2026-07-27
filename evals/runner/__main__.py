"""Opt-in agent-execution eval runner.

Usage (requires a locally installed and authenticated ``codex`` CLI):

    uv run python -m evals.runner --family m7
    uv run python -m evals.runner --scenario m7-o01 [--model MODEL]

For each scenario this materializes the synthetic fixture into a private
workspace, asks a Codex App Server agent the scenario question, and grades
the transcript deterministically. Reports land under ``evals/results/``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from . import codex_driver, grade
from .materialize import (
    UnsupportedScenarioError,
    required_argument_value,
    materialize,
)

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = ROOT / "evals" / "scenarios"
RESULTS_ROOT = ROOT / "evals" / "results"

_CLAIMS_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

DEVELOPER_INSTRUCTIONS_VERSION = "agent-instructions/0.2"
DEVELOPER_INSTRUCTIONS = """\
You are being evaluated on answering a Steam library question with the
locally installed `steam-agent` CLI. Ground every factual claim in CLI output.

- Run `{steam_agent} --data-dir {data_dir} ...` to query the local cache.
- The configured machine key is `{machine}` and the Steam account alias is
  `{account}`. Explore subcommands with `--help` as needed.
- The cache is already populated. Queries are cache-only: never run `sync`,
  `auth`, `owned probe`, or `feedback` subcommands, never make network
  requests, and never read or scan the filesystem for Steam data directly.
- Do not launch, install, uninstall, move, or mutate anything. Plans returned
  by the CLI are for a human to execute.
- Preserve the CLI's distinctions between unknown, false, empty, stale, and
  unavailable. Do not invent facts the CLI did not return.
- End your final answer with a fenced ```json block of the form
  {{"claims": [{{"path": "$.data...", "value": <claimed value>}}]}}
  where each path/value pair points into the JSON document printed by the
  CLI command you relied on, covering every factual claim in your answer.
"""


def _load_scenarios(family: str | None, scenario_id: str | None) -> list[dict[str, Any]]:
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


def _oracle_document(data_dir: Path, requirement: dict[str, Any]) -> dict[str, Any]:
    argv = requirement["command"].split()[1:] + list(requirement["arguments"])
    result = subprocess.run(
        [sys.executable, "-m", "steam_agent", "--data-dir", str(data_dir), *argv],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _extract_claims(message: str | None) -> list[dict[str, Any]] | None:
    if not message:
        return None
    for block in reversed(_CLAIMS_BLOCK.findall(message)):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("claims"), list):
            return payload["claims"]
    return None


def run_scenario(
    scenario: dict[str, Any],
    run_dir: Path,
    *,
    model: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    scenario_dir = run_dir / scenario["id"]
    workspace = scenario_dir / "workspace"
    data_dir = workspace / "steam-agent-data"
    data_dir.mkdir(parents=True)
    materialize(scenario, data_dir)

    steam_agent = _steam_agent_binary()
    instructions = DEVELOPER_INSTRUCTIONS.format(
        steam_agent=steam_agent,
        data_dir=data_dir,
        machine=required_argument_value(scenario, "--machine", "synthetic-machine"),
        account=required_argument_value(scenario, "--account", "synthetic"),
    )
    (workspace / "AGENTS.md").write_text(instructions)

    started = datetime.now(timezone.utc)
    transcript = codex_driver.run_agent_turn(
        prompt=scenario["conversation"]["user"][0],
        workspace=str(workspace),
        developer_instructions=instructions,
        model=model,
        timeout_seconds=timeout_seconds,
    )
    finished = datetime.now(timezone.utc)

    transcript_path = scenario_dir / "transcript.jsonl"
    with transcript_path.open("w") as handle:
        for event in transcript.events:
            handle.write(json.dumps(event) + "\n")

    oracle_document = _oracle_document(data_dir, scenario["tool_policy"]["required"][0])
    executed = [entry["command"] for entry in transcript.commands]
    # The privacy gate covers what the agent says and what the CLI printed,
    # not raw command lines: those necessarily contain the harness workspace
    # path, which is not part of the answer surface being graded.
    transcript_text = "\n".join(
        [
            *(entry.get("output") or "" for entry in transcript.commands),
            *transcript.agent_messages,
        ]
    )

    report = {
        "scenario": scenario["id"],
        "milestone": scenario["milestone"],
        "fixture_sha256": hashlib.sha256(
            scenario["_path"].read_bytes()
        ).hexdigest(),
        "generator": {
            "driver": "codex-app-server",
            "codex_version": codex_driver.codex_version(),
            "model": model or "codex-default",
            "instructions_version": DEVELOPER_INSTRUCTIONS_VERSION,
        },
        "turn_status": transcript.turn_status,
        "turn_error": transcript.turn_error,
        "metrics": {
            "tool_policy": grade.grade_tool_policy(
                executed, scenario["tool_policy"]
            ),
            "oracle": grade.grade_oracle(
                oracle_document, scenario["deterministic_oracle"]
            ),
            "claims": grade.grade_claims(
                _extract_claims(transcript.final_message), oracle_document
            ),
            "privacy": grade.grade_privacy(
                transcript_text, scenario["privacy_canaries"]
            ),
        },
        "operational": {
            "duration_seconds": (finished - started).total_seconds(),
            "steam_agent_calls": len(executed),
        },
        "final_message": transcript.final_message,
    }
    (scenario_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner")
    parser.add_argument("--family", choices=("m3", "m4", "m5", "m7"))
    parser.add_argument("--scenario")
    parser.add_argument("--model")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    args = parser.parse_args(argv)
    if args.family is None and args.scenario is None:
        parser.error("pass --family and/or --scenario")

    scenarios = _load_scenarios(args.family, args.scenario)
    if not scenarios:
        parser.error("no matching scenarios")

    run_dir = RESULTS_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True)

    summaries = []
    exit_code = 0
    for scenario in scenarios:
        try:
            report = run_scenario(
                scenario,
                run_dir,
                model=args.model,
                timeout_seconds=args.timeout_seconds,
            )
        except UnsupportedScenarioError as error:
            print(f"{scenario['id']}: skipped ({error})", file=sys.stderr)
            summaries.append({"scenario": scenario["id"], "skipped": str(error)})
            continue
        metrics = report["metrics"]
        passed = all(
            metrics[layer]["passed"]
            for layer in ("tool_policy", "oracle", "claims", "privacy")
        )
        summaries.append(
            {
                "scenario": scenario["id"],
                "passed": passed,
                "layers": {
                    layer: metrics[layer]["passed"]
                    for layer in ("tool_policy", "oracle", "claims", "privacy")
                },
            }
        )
        if not passed:
            exit_code = 1
        print(
            f"{scenario['id']}: "
            + ", ".join(
                f"{layer}={'pass' if metrics[layer]['passed'] else 'FAIL'}"
                for layer in ("tool_policy", "oracle", "claims", "privacy")
            ),
            file=sys.stderr,
        )

    (run_dir / "summary.json").write_text(json.dumps(summaries, indent=2) + "\n")
    print(f"reports: {run_dir}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

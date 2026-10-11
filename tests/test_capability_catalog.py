from __future__ import annotations

import json
import shlex

import pytest

from steam_agent.capability_catalog import capability_catalog
import steam_agent.cli as cli
from steam_agent.cli import build_parser
from steam_agent.execution.broker_main import build_parser as broker_parser


def test_catalog_is_deterministic_and_does_not_claim_runtime_readiness() -> None:
    first = capability_catalog()
    second = capability_catalog()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["runtime_checked"] is False
    assert first["storage_maintenance_possible"] is True
    ids = [entry["id"] for entry in first["capabilities"]]
    assert ids == sorted(set(ids))

    first["capabilities"][0]["commands"].clear()
    assert capability_catalog() == second


def test_catalog_preserves_evidence_and_execution_boundaries() -> None:
    entries = {entry["id"]: entry for entry in capability_catalog()["capabilities"]}
    for capability in (
        "installed.read", "owned.read", "wishlist.read", "library.read",
        "deals.rank", "recommendations.rank", "compatibility.assess",
        "operations.observe", "operations.plan", "storage.rank",
    ):
        entry = entries[capability]
        assert entry["surface"] == "planner"
        assert entry["cache_only"] is True
        assert entry["network_required"] is False
        assert entry["mutates_steam"] is False
        assert entry["explicit_local_state_mutation"] is False
    assert all(
        not entry["mutates_steam"]
        for entry in entries.values()
        if entry["surface"] == "planner"
    )
    assert entries["owned.read"]["providers"][0]["support_level"] == "official"
    assert entries["wishlist.read"]["providers"][0]["support_level"] == (
        "official_undocumented_provisional"
    )
    executable = {
        key for key in entries
        if key.startswith("execution.") and key != "execution.manage"
    }
    assert executable == {
        "execution.install", "execution.verify", "execution.launch", "execution.stop",
    }
    for capability in executable:
        assert entries[capability]["surface"] == "broker"
        assert entries[capability]["mutates_steam"] is True
        assert all(
            command.startswith("steam-agent-broker ")
            for command in entries[capability]["commands"]
        )
    assert "per-AppID allowlist" in entries["execution.launch"]["requirements"]
    assert "--force additionally requires allow_force" in entries["execution.stop"]["requirements"]
    assert "uninstall, move and backup remain human steps" in entries["operations.plan"]["limitations"]
    for operation in ("launch", "stop"):
        assert "Windows requires the owner's interactive desktop session" in entries[f"execution.{operation}"]["requirements"]
    for operation in ("install", "verify"):
        requirements = entries[f"execution.{operation}"]["requirements"]
        assert "qualified Linux content execution" in requirements
        assert "explicit account alias to Steam login mapping" in requirements

    observation = entries["broker.read"]
    assert observation["surface"] == "broker"
    assert observation["mutates_steam"] is False
    assert observation["explicit_local_state_mutation"] is False
    assert observation["network_required"] is False
    assert observation["commands"] == [
        "steam-agent-broker runtime --appid APPID",
        "steam-agent-broker inspect ID",
        "steam-agent-broker inspect --request-id REQUEST_ID",
        "steam-agent-broker preflight launch APPID",
        "steam-agent-broker desktop-jobs",
        "steam-agent-broker desktop-collect JOB_ID",
        "steam-agent-broker capabilities",
        "steam-agent-broker status",
        "steam-agent-broker policy",
    ]
    assert "steam-agent-broker init --library LIBRARY" in entries["execution.manage"]["commands"]


_PLANNER_COMMANDS = [
    command
    for entry in capability_catalog()["capabilities"]
    if entry["surface"] == "planner"
    for command in entry["commands"]
]


@pytest.mark.parametrize("command", _PLANNER_COMMANDS)
def test_catalog_planner_command_templates_match_the_parser(command: str) -> None:
    values = {
        "APPID": "480", "ACCOUNT": "primary", "MACHINE": "local",
        "STEAMID": "76561198000000000",
        "COUNTRY": "US", "LANGUAGE": "english", "RECIPE": "resume/0.1",
        "PROFILE": "synthetic:alpha", "MEMBER": "synthetic:alpha",
        "RECIPIENT": "synthetic:alpha", "SOURCE": "synthetic:beta",
        "STATE": "available" if "family " in command else "owned",
        "FACT": "players:max", "VALUE": "4", "MODE": "online_coop",
        "OBJECTIVE": "min-copies", "LIMIT": "1", "BYTES": "1",
        "OPERATION": "install", "RATING": "liked", "TRAIT": "user:portable",
        "TRAIT_STATE": "present", "KIND": "prefer", "STRENGTH": "soft",
        "WEIGHT": "1", "MINUTES": "1", "TIMESTAMP": "2026-07-12T12:00:00Z",
        "PROVIDER": "gg-deals" if "auth probe" in command else "steam-web-api",
    }
    argv = [
        values.get(argument, argument.replace("machine:MACHINE", "machine:local"))
        for argument in shlex.split(command)[1:]
    ]
    build_parser().parse_args(argv)


_BROKER_COMMANDS = [
    command
    for entry in capability_catalog()["capabilities"]
    if entry["surface"] == "broker"
    for command in entry["commands"]
]


@pytest.mark.parametrize("command", _BROKER_COMMANDS)
def test_catalog_broker_command_templates_match_the_parser(command: str) -> None:
    values = {
        "APPID": "480", "ID": "request-0001" if "--request-id" in command else "1", "ACCOUNT": "primary",
        "LIBRARY": "/synthetic/library", "STEAMCMD": "/synthetic/steamcmd",
        "LOGIN": "synthetic_login", "ACTOR": "owner", "NONCE": "synthetic-nonce",
    }
    argv = [values.get(argument, argument) for argument in shlex.split(command)[1:]]
    broker_parser().parse_args(argv)


def test_full_cli_catalog_needs_no_storage_or_steam_root(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_: object, **__: object) -> None:
        pytest.fail("capability index must not probe Steam or open storage")

    monkeypatch.setattr(cli, "discover_steam_root", unavailable)
    monkeypatch.setattr(cli, "Storage", unavailable)
    assert cli.main(["--data-dir", str(tmp_path), "capabilities", "--all"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["data"] == capability_catalog()
    assert list(tmp_path.iterdir()) == []

"""Regression tests for deterministic execution and fact-coverage gates."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys

import pytest
import steam_agent.cli as cli
import steam_agent.storage as storage_module

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import __main__ as runner_main  # noqa: E402
from evals.runner import grade  # noqa: E402
from evals.runner.materialize import materialize  # noqa: E402


SCENARIO_ROOT = ROOT / "evals" / "scenarios"
EXECUTABLE_SCHEMA_02_PATHS = tuple(
    path
    for path in sorted(SCENARIO_ROOT.glob("*/*.json"))
    if (
        (scenario := json.loads(path.read_text(encoding="utf-8")))["schema_version"]
        == "steam-agent-eval/0.2"
        and scenario["tool_policy"]["required"]
    )
)
UNMATERIALIZABLE_SCENARIOS = {
    "m5-c03-deck-playable",
    "m5-c04-deck-unsupported",
}

POLICY = {
    "allowed": ["steam-agent operations observe"],
    "required": [
        {
            "command": "steam-agent operations observe",
            "arguments": ["--machine", "synthetic-machine"],
        }
    ],
    "prohibited": ["sync", "network request", "filesystem mutation"],
}


def _turn(*commands: str) -> dict[str, object]:
    return {
        "commands": list(commands),
        "declined": False,
        "final_message": "answer",
    }


def _trace_oracle(signature: str) -> dict[str, object]:
    return {
        "recipe_or_contract": "read-only execution boundary",
        "assertions": [
            {
                "path": "$",
                "operator": "must_not_execute",
                "expected": signature,
                "source": "trace",
            }
        ],
    }


def test_tool_policy_checks_every_command_in_a_shell_chain() -> None:
    result = grade.grade_tool_policy(
        [
            "steam-agent operations observe --machine synthetic-machine "
            "&& steam-agent sync installed --machine synthetic-machine"
        ],
        POLICY,
    )

    assert result["steam_agent_calls"] == 0
    assert result["required"] == [
        {"command": "steam-agent operations observe", "satisfied": False}
    ]
    assert not result["passed"]
    assert result["violations"] == [
        {
            "command": "steam-agent operations observe --machine synthetic-machine "
            "&& steam-agent sync installed --machine synthetic-machine",
            "reason": "execution_boundary",
            "action": "unsafe_command_form",
        }
    ]


def test_tool_policy_checks_pipeline_and_nested_shell_segments() -> None:
    command = (
        "bash -lc 'steam-agent operations observe --machine synthetic-machine "
        "| /repo/.venv/bin/steam-agent sync installed --machine synthetic-machine'"
    )
    result = grade.grade_tool_policy([command], POLICY)

    assert result["steam_agent_calls"] == 0
    assert not result["passed"]
    assert result["violations"][0]["action"] == "unsafe_command_form"


@pytest.mark.parametrize(
    "command",
    (
        "echo steam-agent operations observe --machine synthetic-machine",
        "python -c 'print(\"steam-agent operations observe\")'",
        "time steam-agent operations observe --machine synthetic-machine",
        "cat steam-agent operations observe --machine synthetic-machine",
    ),
)
def test_steam_agent_argument_spoofing_is_not_a_cli_call(command: str) -> None:
    result = grade.grade_tool_policy([command], POLICY)

    assert result["steam_agent_calls"] == 0
    assert result["required"][0]["satisfied"] is False
    assert result["violations"][0]["action"] == "non_steam_agent_command"


@pytest.mark.parametrize(
    "suffix",
    (
        " &",
        "\ntrue",
        " || true",
        " ; true",
        " > result.json",
        " 2>/dev/null",
        " $(true)",
        " `true`",
        " <(true)",
        " --account=$ACCOUNT",
        " --account user-*",
        " # hidden shell comment",
    ),
)
def test_compound_and_dynamic_shell_forms_fail_closed(suffix: str) -> None:
    command = "steam-agent operations observe --machine synthetic-machine" + suffix
    result = grade.grade_tool_policy([command], POLICY)

    assert result["steam_agent_calls"] == 0
    assert result["required"][0]["satisfied"] is False
    assert result["violations"][0]["reason"] == "execution_boundary"


@pytest.mark.parametrize("wrapper", ("sudo", "env", "nohup"))
def test_process_wrappers_are_not_transparent(wrapper: str) -> None:
    command = f"{wrapper} steam-agent operations observe --machine synthetic-machine"
    result = grade.grade_tool_policy([command], POLICY)

    assert result["steam_agent_calls"] == 0
    assert result["required"][0]["satisfied"] is False
    assert result["violations"][0]["action"] == "non_steam_agent_command"


@pytest.mark.parametrize("assignment", ("PATH=/tmp/evil", "PYTHONPATH=/tmp/evil"))
def test_leading_assignments_cannot_poison_the_frozen_launcher(
    assignment: str,
) -> None:
    command = f"{assignment} steam-agent operations observe --machine synthetic-machine"

    assert grade.normalized_steam_agent_argv(command) is None
    assert not grade.grade_tool_policy([command], POLICY)["passed"]


@pytest.mark.parametrize(
    "shell",
    (
        "/bin/bash",
        "/bin/sh",
        "/bin/zsh",
        "/usr/bin/bash",
        "/usr/bin/sh",
        "/usr/bin/zsh",
    ),
)
@pytest.mark.parametrize("option", ("-c", "-lc"))
def test_one_trusted_absolute_shell_wrapper_is_allowed(
    shell: str, option: str
) -> None:
    command = (
        f"{shell} {option} 'command ./bin/steam-agent operations observe "
        "--machine=synthetic-machine'"
    )
    result = grade.grade_tool_policy([command], POLICY)

    assert grade.normalized_steam_agent_argv(command) == [
        "operations",
        "observe",
        "--machine=synthetic-machine",
    ]
    assert result["passed"], result


def test_live_policy_requires_the_exact_frozen_launcher() -> None:
    bare = (
        "steam-agent --data-dir steam-agent-data operations observe "
        "--machine synthetic-machine"
    )
    relative = "./bin/" + bare
    wrapped = f"/bin/zsh -lc '{relative}'"

    for command in (relative, wrapped):
        result = grade.grade_tool_policy(
            [command],
            POLICY,
            expected_data_dir="steam-agent-data",
            expected_executable="./bin/steam-agent",
        )
        assert result["passed"], result

    result = grade.grade_tool_policy(
        [bare],
        POLICY,
        expected_data_dir="steam-agent-data",
        expected_executable="./bin/steam-agent",
    )
    assert not result["passed"]
    assert result["steam_agent_calls"] == 0
    assert result["required"][0]["satisfied"] is False
    assert result["violations"] == [
        {
            "command": bare,
            "reason": "execution_boundary",
            "action": "unexpected_steam_agent_executable",
        }
    ]


@pytest.mark.parametrize(
    "executable",
    ("/tmp/evil/steam-agent", "/repo/.venv/bin/steam-agent", "Steam-Agent"),
)
def test_only_the_bare_frozen_path_executable_is_allowed(executable: str) -> None:
    command = f"{executable} operations observe --machine synthetic-machine"

    assert grade.normalized_steam_agent_argv(command) is None
    assert not grade.grade_tool_policy([command], POLICY)["passed"]


@pytest.mark.parametrize(
    "shell",
    (
        "bash",
        "sh",
        "zsh",
        "/tmp/evil/bash",
        "/private/tmp/zsh",
        "/usr/local/bin/zsh",
        "/bin/../bin/zsh",
        "/usr/bin/../bin/zsh",
        "//bin/zsh",
        "bin/zsh",
        "./bin/zsh",
        "../bin/zsh",
        "/bin/dash",
        "/bin/Zsh",
    ),
)
def test_shell_wrapper_rejects_untrusted_absolute_and_relative_lookalikes(
    shell: str,
) -> None:
    command = (
        f"{shell} -c 'steam-agent operations observe --machine synthetic-machine'"
    )

    assert grade.normalized_steam_agent_argv(command) is None
    assert not grade.grade_tool_policy([command], POLICY)["passed"]


def test_bare_shell_wrapper_is_diagnostic_only_and_cannot_approve_a_call() -> None:
    command = (
        "zsh -lc './bin/steam-agent operations observe "
        "--machine synthetic-machine'"
    )

    assert (
        grade.normalized_steam_agent_argv(
            command, expected_executable="./bin/steam-agent"
        )
        is None
    )
    result = grade.grade_tool_policy(
        [command], POLICY, expected_executable="./bin/steam-agent"
    )
    assert not result["passed"]
    assert result["steam_agent_calls"] == 0
    assert result["violations"][0]["action"] == "unsafe_command_form"


@pytest.mark.parametrize(
    "payload",
    (
        "./bin/steam-agent operations observe --machine synthetic-machine && true",
        "./bin/steam-agent operations observe --machine synthetic-machine > out.json",
        "./bin/steam-agent operations observe --machine synthetic-machine $(true)",
        "PATH=/tmp/evil ./bin/steam-agent operations observe --machine synthetic-machine",
        "/tmp/evil/steam-agent operations observe --machine synthetic-machine",
        "bash -lc './bin/steam-agent operations observe --machine synthetic-machine'",
        "sudo ./bin/steam-agent operations observe --machine synthetic-machine",
    ),
)
def test_trusted_absolute_shell_wrapper_preserves_fail_closed_payload(payload: str) -> None:
    command = f'/bin/zsh -lc "{payload}"'

    assert (
        grade.normalized_steam_agent_argv(
            command, expected_executable="./bin/steam-agent"
        )
        is None
    )
    assert not grade.grade_tool_policy(
        [command],
        POLICY,
        expected_executable="./bin/steam-agent",
    )["passed"]


def test_command_builtin_must_be_bare() -> None:
    command = (
        "/tmp/evil/command steam-agent operations observe --machine synthetic-machine"
    )

    assert grade.normalized_steam_agent_argv(command) is None
    assert not grade.grade_tool_policy([command], POLICY)["passed"]


@pytest.mark.parametrize(
    ("private_path", "sensitive_suffix"),
    (
        (
            "/Users/Zoë/O'Brien/[private], semi; (paren)& "
            "angle<tag>: cache/secret-tail.txt",
            "secret-tail.txt",
        ),
        ("~/Library/Application Support/secret-tail.txt", "secret-tail.txt"),
        ("~alice/.config/secret-tail.txt", "secret-tail.txt"),
        ("file:/Users/example/secret-tail.txt", "secret-tail.txt"),
        (
            "file:///Users/example/[private]:secret-tail.txt",
            "secret-tail.txt",
        ),
        (
            "file://localhost/Users/Alice O'Brien/secret-tail.txt",
            "secret-tail.txt",
        ),
        (
            "file:%2FUsers%2FAlice%20Smith%2Fsecret-tail%2Etxt",
            "secret-tail%2Etxt",
        ),
        (
            "file:%2F%2FC:%5CUsers%5Cexample%5Csecret-tail.txt",
            "secret-tail.txt",
        ),
        ("file:///C:/Users/example/secret-tail.txt", "secret-tail.txt"),
        (
            "file://localhost/C:/Users/example/secret-tail.txt",
            "secret-tail.txt",
        ),
        ("file:C:/Users/example/secret-tail.txt", "secret-tail.txt"),
        (r"file:C:\Users\example\secret-tail.txt", "secret-tail.txt"),
        (r"C:\Users\example\secret-tail.txt", "secret-tail.txt"),
        ("D:/Users/example/secret-tail.txt", "secret-tail.txt"),
        (r"\\server\private share\secret-tail.txt", "secret-tail.txt"),
        (r"\\?\C:\Users\example\secret-tail.txt", "secret-tail.txt"),
        (
            r"\\?\UNC\server\private share\secret-tail.txt",
            "secret-tail.txt",
        ),
        ("//server/private share/secret-tail.txt", "secret-tail.txt"),
        (
            "path:/Users/example/[private]:secret-tail.txt",
            "secret-tail.txt",
        ),
    ),
)
def test_private_host_path_forms_share_detection_and_redaction(
    private_path: str, sensitive_suffix: str
) -> None:
    text = f'location="{private_path}", next=true'
    redacted = grade.redact_private_host_paths(text)

    assert grade.find_private_host_paths(text) == [private_path]
    assert redacted == 'location="<redacted-host-path>", next=true'
    assert private_path not in redacted
    assert sensitive_suffix not in redacted
    assert grade.find_private_host_paths(redacted) == []
    privacy = grade.grade_privacy(text, {})
    assert not privacy["passed"]
    assert privacy["private_host_paths"] == [private_path]


@pytest.mark.parametrize(
    "text",
    (
        "https://example.com/Users/example/secret-tail.txt",
        "https://example.com/?next=file:///Users/example/secret-tail.txt",
        "https://example.com/?next=file:%2FUsers%2Fsecret-tail.txt",
        "steam://open/path:/Users/example/secret-tail.txt",
        "$.data.items[0].path",
        "$['relative/repository/path']",
        "$.data.items[?(@.path=='relative/repository/path')].state",
        "$['/']",
        "resume/0.1",
        "recipe:path/0.1",
        "./bin/steam-agent",
        "bin/steam-agent",
        "either/or is benign prose",
        "prefix-path:/not-a-private-path",
        "error:/Users/example/secret-tail.txt",
        "profile:///Users/example/secret-tail.txt",
        "file:relative/secret-tail.txt",
        "path:relative/secret-tail.txt",
        "docs/~/secret-tail.txt",
        "docs/~alice/secret-tail.txt",
        "not~/a/home/path",
        "C:relative/secret-tail.txt",
        "./Users/example/secret-tail.txt",
        "../Users/example/secret-tail.txt",
        "The path: relative/value is benign prose.",
        "/",
        "~/",
        "file:/",
        "path:/",
        "C:\\",
    ),
)
def test_private_host_path_detection_ignores_public_and_relative_text(
    text: str,
) -> None:
    assert grade.find_private_host_paths(text) == []
    assert grade.redact_private_host_paths(text) == text


@pytest.mark.parametrize(
    ("text", "private_path", "redacted"),
    (
        (
            "$['/Users/example/Library/Steam']",
            "/Users/example/Library/Steam",
            "$['<redacted-host-path>']",
        ),
        (
            "$.data.items[?(@.path=='/Users/example/Library/Steam')].state",
            "/Users/example/Library/Steam",
            "$.data.items[?(@.path=='<redacted-host-path>')].state",
        ),
    ),
)
def test_json_path_syntax_does_not_hide_embedded_private_paths(
    text: str, private_path: str, redacted: str
) -> None:
    assert grade.find_private_host_paths(text) == [private_path]
    assert grade.redact_private_host_paths(text) == redacted


def test_private_host_path_scanner_is_conservative_for_ambiguous_prose() -> None:
    text = "Observed /Users/example/My File notes continue"

    assert grade.find_private_host_paths(text) == [
        "/Users/example/My File notes continue"
    ]
    assert grade.redact_private_host_paths(text) == (
        "Observed <redacted-host-path>"
    )


def test_unquoted_private_path_consumes_adjacent_legal_punctuation() -> None:
    private_path = "/Users/example/[a,b];c&(d)<e>:secret-tail.txt"
    text = f"location={private_path}, next=true"

    assert grade.find_private_host_paths(text) == [private_path]
    assert grade.redact_private_host_paths(text) == (
        "location=<redacted-host-path>, next=true"
    )


def test_single_quoted_path_preserves_internal_apostrophes_and_delimiter() -> None:
    private_path = "/Users/O'Brien/private/secret-tail.txt"
    text = f"location='{private_path}'; next=true"

    assert grade.find_private_host_paths(text) == [private_path]
    assert grade.redact_private_host_paths(text) == (
        "location='<redacted-host-path>'; next=true"
    )


@pytest.mark.parametrize(
    "private_path",
    (
        '/Users/example/private"secret-tail.txt',
        "/Users/example/private`secret-tail.txt",
        "/Users/example/private\tsecret-tail.txt",
        "/Users/example/private\nsecret-tail.txt",
    ),
)
def test_private_path_redaction_consumes_legal_posix_filename_characters(
    private_path: str,
) -> None:
    text = f"location={private_path}, next=true"
    redacted = grade.redact_private_host_paths(text)

    assert grade.find_private_host_paths(text) == [private_path]
    assert redacted == "location=<redacted-host-path>, next=true"
    assert "secret-tail.txt" not in redacted
    assert grade.find_private_host_paths(redacted) == []


def test_artifact_sanitizer_uses_the_shared_private_host_path_redactor() -> None:
    text = (
        'home="~/.config/private.json", '
        'uri="file:///Users/example/private.json", '
        'prefixed="path:/Users/example/private.json"'
    )

    sanitized = runner_main._sanitize_artifact(  # noqa: SLF001
        text, sensitive_values=()
    )

    assert sanitized == grade.redact_private_host_paths(text)
    assert grade.find_private_host_paths(sanitized) == []


@pytest.mark.parametrize(
    "command",
    (
        "steam-agent sync installed",
        "steam-agent profiles create example",
        "steam-agent ownership set example 1 owned",
        "steam-agent family set example 1 available --source declared",
        "steam-agent fact set example 1 feature value",
        "steam-agent feedback set 1 like",
        "steam-agent preferences set example value",
        "steam-agent accounts add example 1",
        "steam-agent auth set steam-web-api secret",
        "steam-agent data delete --all --yes",
    ),
)
def test_unlisted_mutating_cli_heads_fail(command: str) -> None:
    result = grade.grade_tool_policy(
        [command], {"allowed": [], "required": [], "prohibited": []}
    )

    assert not result["passed"]
    assert result["unlisted_calls"] == [command]


def test_allowed_data_delete_is_not_blanket_banned() -> None:
    policy = {
        "allowed": ["steam-agent data delete"],
        "required": [
            {
                "command": "steam-agent data delete",
                "arguments": ["--provider", "steam-web-api", "--all", "--yes"],
            }
        ],
        "prohibited": [],
    }
    result = grade.grade_tool_policy(
        ["steam-agent data delete --provider=steam-web-api --all --yes"], policy
    )

    assert result["passed"], result


def test_owned_capability_is_read_only_but_still_requires_allowlisting() -> None:
    command = "steam-agent owned capability --account synthetic"
    unlisted = grade.grade_tool_policy(
        [command], {"allowed": [], "required": [], "prohibited": []}
    )
    allowed = grade.grade_tool_policy(
        [command],
        {
            "allowed": ["steam-agent owned capability"],
            "required": [],
            "prohibited": [],
        },
    )

    assert not unlisted["passed"]
    assert unlisted["violations"][0]["reason"] == "not_allowed"
    assert allowed["passed"], allowed


def test_help_discovery_is_allowed_outside_the_allowlist() -> None:
    result = grade.grade_tool_policy(
        ["steam-agent owned --help"],
        {"allowed": [], "required": [], "prohibited": []},
    )

    assert result["passed"], result


def test_required_options_accept_equal_form_but_reject_duplicates() -> None:
    equal_form = grade.grade_tool_policy(
        ["steam-agent operations observe --machine=synthetic-machine"], POLICY
    )
    duplicate = grade.grade_tool_policy(
        [
            "steam-agent operations observe --machine synthetic-machine "
            "--machine other-machine"
        ],
        POLICY,
    )

    assert equal_form["passed"], equal_form
    assert duplicate["required"][0]["satisfied"] is False
    assert not duplicate["passed"]


@pytest.mark.parametrize(
    "extra",
    (
        "--country CA",
        "unexpected-positional",
        "--format table",
    ),
)
def test_required_matching_rejects_any_extra_semantic_argument(extra: str) -> None:
    command = "steam-agent operations observe --machine synthetic-machine " + extra

    assert not grade.command_satisfies_requirement(command, POLICY["required"][0])
    assert not grade.grade_tool_policy([command], POLICY)["passed"]


def test_required_repeated_append_options_match_exactly() -> None:
    policy = {
        "allowed": ["steam-agent discovery query"],
        "required": [
            {
                "command": "steam-agent discovery query",
                "arguments": ["--appid", "1", "--appid", "2"],
            }
        ],
        "prohibited": [],
    }

    exact = grade.grade_tool_policy(
        ["steam-agent discovery query --appid=1 --appid 2"], policy
    )
    extra = grade.grade_tool_policy(
        ["steam-agent discovery query --appid 1 --appid 2 --appid 3"], policy
    )

    assert exact["passed"], exact
    assert extra["required"][0]["satisfied"] is False


def test_expected_data_dir_is_exact_and_non_help_only() -> None:
    command = (
        "steam-agent --data-dir=steam-agent-data operations observe "
        "--machine synthetic-machine"
    )
    assert grade.grade_tool_policy(
        [command], POLICY, expected_data_dir="steam-agent-data"
    )["passed"]
    for unsafe in (
        "steam-agent operations observe --machine synthetic-machine",
        "steam-agent --data-dir /tmp/data operations observe "
        "--machine synthetic-machine",
        "steam-agent --data-dir steam-agent-data --data-dir other operations "
        "observe --machine synthetic-machine",
    ):
        assert not grade.grade_tool_policy(
            [unsafe], POLICY, expected_data_dir="steam-agent-data"
        )["passed"]
    assert grade.grade_tool_policy(
        ["steam-agent --help"],
        {**POLICY, "required": []},
        expected_data_dir="steam-agent-data",
    )["passed"]


def test_network_fetchers_are_hard_violations() -> None:
    for command in (
        "curl https://store.steampowered.com/app/1",
        "wget -q https://example.invalid/evidence",
        "bash -lc 'echo start; curl https://example.invalid'",
    ):
        result = grade.grade_tool_policy([command], {**POLICY, "required": []})
        assert not result["passed"]
        assert result["violations"][0]["action"] == "network_request"

    for command in ("curl --help", "wget --version"):
        violation = grade.execution_boundary_violations([command])[0]
        assert violation["action"] == "non_steam_agent_command"


def test_direct_steam_launch_and_client_commands_fail_tool_policy() -> None:
    for command, action in (
        ("steam -applaunch 7402", "steam_client_access"),
        ("steamcmd +app_update 7402", "steam_client_access"),
        ("open steam://rungameid/7402", "steam_launch"),
        ("xdg-open steam://install/7402", "steam_launch"),
    ):
        result = grade.grade_tool_policy([command], {**POLICY, "required": []})
        assert not result["passed"]
        assert result["violations"][0]["action"] == action


def test_install_path_mutation_is_target_aware() -> None:
    destructive = grade.execution_boundary_violations(
        ["/bin/rm -rf /games/SteamLibrary/steamapps/common/SyntheticGame"]
    )
    assert destructive[0]["action"] == "steam_filesystem_mutation"

    # Scratch cleanup is not misreported as a Steam mutation, but the runner's
    # fail-closed tool boundary still rejects every non-steam-agent command.
    scratch = grade.execution_boundary_violations(["rm -rf /tmp/eval-scratch"])
    assert scratch[0]["action"] == "non_steam_agent_command"
    assert not grade.execution_boundary_violations(
        ["steam-agent data delete --account synthetic --confirm"]
    )


def test_arbitrary_non_steam_commands_fail_closed() -> None:
    for command in (
        "python -c 'print(1)'",
        "perl -e 'print 1'",
        "printf '%s\\n' inspection",
        "./custom-helper --read-cache",
    ):
        result = grade.grade_tool_policy([command], {**POLICY, "required": []})
        assert not result["passed"]
        assert result["violations"][0]["action"] == "non_steam_agent_command"


@pytest.mark.parametrize(
    "command",
    (
        "./bin/steam-agent sync installed",
        "./bin/steam-agent --format json sync installed",
        "./bin/steam-agent auth status steam-web-api",
        "./bin/steam-agent feedback query",
        "./bin/steam-agent --format json doctor",
        "./bin/steam-agent --data-dir steam-agent-data capabilities",
        "./bin/steam-agent owned probe",
        "./bin/steam-agent profiles create member",
        "./bin/steam-agent profiles delete member",
        "./bin/steam-agent profiles clear-account member",
        "./bin/steam-agent ownership set member 1 owned",
        "./bin/steam-agent ownership clear member 1",
        "./bin/steam-agent family set member 1 available --source declared",
        "./bin/steam-agent family clear member 1 --source declared",
        "./bin/steam-agent fact set member 1 trait present",
        "./bin/steam-agent fact clear member 1 trait",
        "./bin/steam-agent preferences rule set --trait coop",
        "./bin/steam-agent preferences rule remove --trait coop",
        "./bin/steam-agent accounts discover",
        "./bin/steam-agent accounts configure --alias synthetic",
        "./bin/steam-agent accounts remove --alias synthetic",
    ),
)
def test_live_cache_only_boundary_overrides_permissive_allowlists(
    command: str,
) -> None:
    bare_declaration = command.replace("./bin/steam-agent", "steam-agent", 1)
    policy = {
        "allowed": [bare_declaration],
        "required": [],
        "prohibited": [],
    }

    result = grade.grade_tool_policy(
        [command],
        policy,
        expected_executable="./bin/steam-agent",
        enforce_cache_only=True,
    )

    assert not result["passed"]
    assert result["violations"][0]["reason"] == "cache_only_boundary"


def test_live_cache_only_boundary_preserves_only_confirmed_data_delete() -> None:
    command = "./bin/steam-agent data delete --provider steam-web-api --all --yes"
    policy = {
        "allowed": ["steam-agent data delete"],
        "required": [
            {
                "command": "steam-agent data delete",
                "arguments": ["--provider", "steam-web-api", "--all", "--yes"],
            }
        ],
        "prohibited": [],
    }

    rejected = grade.grade_tool_policy(
        [command],
        policy,
        expected_executable="./bin/steam-agent",
        enforce_cache_only=True,
    )
    accepted = grade.grade_tool_policy(
        [command],
        policy,
        expected_executable="./bin/steam-agent",
        enforce_cache_only=True,
        allow_data_delete=True,
    )

    assert not rejected["passed"]
    assert accepted["passed"], accepted

    wrong_target = grade.grade_tool_policy(
        ["./bin/steam-agent data delete --account other --all --yes"],
        policy,
        expected_executable="./bin/steam-agent",
        enforce_cache_only=True,
        allow_data_delete=True,
    )
    assert not wrong_target["passed"]
    assert wrong_target["violations"][0]["reason"] == "cache_only_boundary"


@pytest.mark.parametrize(
    "command",
    (
        "./bin/steam-agent accounts status --alias synthetic",
        "./bin/steam-agent owned capability --account synthetic",
        "./bin/steam-agent status",
    ),
)
def test_live_cache_only_boundary_preserves_read_only_heads(command: str) -> None:
    declaration = command.replace("./bin/steam-agent", "steam-agent", 1)
    head = " ".join(declaration.split()[:3])
    result = grade.grade_tool_policy(
        [command],
        {"allowed": [head], "required": [], "prohibited": []},
        expected_executable="./bin/steam-agent",
        enforce_cache_only=True,
    )

    assert result["passed"], result


def test_must_not_execute_matches_non_steam_commands_and_absolute_paths() -> None:
    result = grade.grade_assertions(
        _trace_oracle("rm"),
        document=None,
        turns=[_turn("bash -lc 'echo ready; /bin/rm -rf /tmp/game'")],
    )

    assert not result["passed"]
    assert result["failed"][0]["reason"] == "prohibited_command_was_executed"


def test_must_not_execute_checks_later_steam_command_in_chain() -> None:
    result = grade.grade_assertions(
        _trace_oracle("steam-agent sync"),
        document=None,
        turns=[
            _turn(
                "steam-agent operations observe --machine synthetic-machine "
                "&& steam-agent sync installed --machine synthetic-machine"
            )
        ],
    )

    assert not result["passed"]
    assert result["failed"][0]["reason"] == "prohibited_command_was_executed"


def test_must_not_execute_does_not_match_quoted_command_text() -> None:
    result = grade.grade_assertions(
        _trace_oracle("rm"),
        document=None,
        turns=[_turn("printf '%s\\n' 'rm -rf /tmp/game'")],
    )

    assert result["passed"], result["failed"]


def test_fact_coverage_rejects_a_trivial_supported_claim() -> None:
    document = {"data": {"required": "unknown", "incidental": 1}}
    criteria = [
        {
            "id": "required-fact",
            "weight": 10,
            "requirement": "Report the required state.",
        }
    ]
    result = grade.grade_fact_coverage(
        [{"path": "$.data.incidental", "value": 1}],
        document,
        ["$.data.required"],
        criteria=criteria,
    )

    assert result["supported"] == 1
    assert result["missing_required_paths"] == ["$.data.required"]
    assert result["criteria_evaluated"] is False
    assert result["unevaluated_criteria"] == ["required-fact"]
    assert not result["passed"]


def test_fact_coverage_requires_all_claims_to_be_supported() -> None:
    document = {"data": {"required": "unknown", "other": False}}
    complete = grade.grade_fact_coverage(
        [{"path": "$.data.required", "value": "unknown"}],
        document,
        ["$.data.required"],
    )
    assert complete["passed"]
    assert complete["satisfied_required_paths"] == ["$.data.required"]

    unsupported_extra = grade.grade_fact_coverage(
        [
            {"path": "$.data.required", "value": "unknown"},
            {"path": "$.data.other", "value": True},
        ],
        document,
        ["$.data.required"],
    )
    assert not unsupported_extra["passed"]
    assert unsupported_extra["missing_required_paths"] == []
    assert unsupported_extra["failed"] == [{"path": "$.data.other", "value": True}]


@pytest.mark.parametrize(
    ("claim_path", "claim_value", "required_path"),
    (
        ("$.data.items[0].state", "ready", "$.data.items[*].state"),
        ("$.data.items[*].state", ["ready"], "$.data.items[0].state"),
        (
            "$.data.items[?(@.appid==10)].state",
            ["ready"],
            "$.data.items[0].state",
        ),
    ),
)
def test_fact_coverage_accepts_paths_selecting_the_same_concrete_locations(
    claim_path: str, claim_value: object, required_path: str
) -> None:
    document = {"data": {"items": [{"appid": 10, "state": "ready"}]}}

    result = grade.grade_fact_coverage(
        [{"path": claim_path, "value": claim_value}],
        document,
        [required_path],
    )

    assert result["passed"], result
    assert result["satisfied_required_paths"] == [required_path]


def test_fact_coverage_does_not_let_one_concrete_claim_cover_a_larger_projection(
) -> None:
    document = {
        "data": {
            "items": [
                {"appid": 10, "state": "ready"},
                {"appid": 11, "state": "unknown"},
            ]
        }
    }
    required_path = "$.data.items[*].state"

    result = grade.grade_fact_coverage(
        [{"path": "$.data.items[0].state", "value": "ready"}],
        document,
        [required_path],
    )

    assert not result["passed"]
    assert result["missing_required_paths"] == [required_path]


def test_fact_coverage_unions_concrete_claims_for_a_wildcard_requirement() -> None:
    document = {
        "data": {
            "items": [
                {"appid": 10, "state": "ready", "note": "same"},
                {"appid": 11, "state": "unknown", "note": "same"},
            ]
        }
    }
    required_path = "$.data.items[*].state"

    result = grade.grade_fact_coverage(
        [
            {"path": "$.data.items[0].state", "value": "ready"},
            {"path": "$.data.items[1].state", "value": "unknown"},
            {"path": "$.data.items[0].note", "value": "same"},
        ],
        document,
        [required_path],
    )

    assert result["passed"], result
    assert result["satisfied_required_paths"] == [required_path]


def test_fact_coverage_does_not_equate_distinct_locations_with_equal_values() -> None:
    document = {"data": {"required": "unknown", "incidental": "unknown"}}

    result = grade.grade_fact_coverage(
        [{"path": "$.data.incidental", "value": "unknown"}],
        document,
        ["$.data.required"],
    )

    assert not result["passed"]
    assert result["missing_required_paths"] == ["$.data.required"]


def test_fact_coverage_does_not_equate_empty_projections_from_distinct_paths(
) -> None:
    document = {"data": {"required": [], "incidental": []}}

    result = grade.grade_fact_coverage(
        [{"path": "$.data.incidental[*]", "value": []}],
        document,
        ["$.data.required[*]"],
    )

    assert not result["passed"]
    assert result["missing_required_paths"] == ["$.data.required[*]"]


@pytest.mark.parametrize(
    ("operator", "actual", "expected", "passed"),
    (
        ("equals", {"nested": [False]}, {"nested": [0]}, False),
        ("equals", {"nested": [True]}, {"nested": [1]}, False),
        ("ordered_equals", [{"value": False}], [{"value": 0}], False),
        ("ordered_equals", [{"value": True}], [{"value": 1}], False),
        ("contains", [{"value": False}], {"value": 0}, False),
        ("contains", [{"value": True}], {"value": 1}, False),
        ("omits", [{"value": False}], {"value": 0}, True),
        ("omits", [{"value": True}], {"value": 1}, True),
        ("one_of", {"value": False}, [{"value": 0}], False),
        ("one_of", {"value": True}, [{"value": 1}], False),
    ),
)
def test_json_operators_do_not_coerce_booleans_to_numbers(
    operator: str, actual: object, expected: object, passed: bool
) -> None:
    document = {"data": {"actual": actual}}
    assertion = {
        "path": "$.data.actual",
        "operator": operator,
        "expected": expected,
    }

    assert grade.evaluate_assertion(document, assertion) is passed


@pytest.mark.parametrize("expected", ("ready", {"ready": True}, 1, None, []))
def test_one_of_fails_closed_without_a_nonempty_array(expected: object) -> None:
    assertion = {
        "path": "$.data.state",
        "operator": "one_of",
        "expected": expected,
    }
    result = grade.grade_assertions(
        {"assertions": [assertion]},
        document={"data": {"state": "ready"}},
        turns=(),
    )

    assert not result["passed"]
    assert result["failed"] == [
        {**assertion, "reason": "assertion_could_not_be_evaluated"}
    ]


@pytest.mark.parametrize(
    ("expected", "passed"),
    ((["pending", "ready"], True), (["pending", "unknown"], False)),
)
def test_one_of_accepts_nonempty_arrays(expected: list[str], passed: bool) -> None:
    assertion = {
        "path": "$.data.state",
        "operator": "one_of",
        "expected": expected,
    }

    assert (
        grade.evaluate_assertion({"data": {"state": "ready"}}, assertion) is passed
    )


def test_json_number_semantics_still_equate_integers_and_floats() -> None:
    assert grade.json_semantically_equal({"nested": [1, 2.5]}, {"nested": [1.0, 2.5]})


def test_json_filter_matching_does_not_coerce_booleans_to_numbers() -> None:
    document = {
        "data": {
            "items": [
                {"selector": False, "kind": "false"},
                {"selector": 0, "kind": "zero"},
                {"selector": True, "kind": "true"},
                {"selector": 1, "kind": "one"},
            ]
        }
    }

    zero, _ = grade.select_path(document, "$.data.items[?(@.selector==0)].kind")
    one, _ = grade.select_path(document, "$.data.items[?(@.selector==1)].kind")

    assert zero == ["zero"]
    assert one == ["one"]


@pytest.mark.parametrize(
    ("document_value", "claim_value"),
    (
        ({"nested": [False]}, {"nested": [0]}),
        ({"nested": [True]}, {"nested": [1]}),
    ),
)
def test_claims_and_fact_coverage_do_not_coerce_nested_booleans(
    document_value: object, claim_value: object
) -> None:
    document = {"data": {"required": document_value}}
    claims = [{"path": "$.data.required", "value": claim_value}]

    claim_result = grade.grade_claims(claims, document)
    coverage_result = grade.grade_fact_coverage(claims, document, ["$.data.required"])

    assert not claim_result["passed"]
    assert not coverage_result["passed"]
    assert coverage_result["missing_required_paths"] == ["$.data.required"]


def test_claims_are_merged_across_all_conversation_turns() -> None:
    merged = grade.merge_claims(
        [
            [{"path": "$.data.plan.execution", "value": "prohibited"}],
            None,
            [{"path": "$.data.plan.confirmation", "value": "human_only"}],
        ]
    )
    assert merged == [
        {"path": "$.data.plan.execution", "value": "prohibited"},
        {"path": "$.data.plan.confirmation", "value": "human_only"},
    ]
    assert grade.merge_claims([None, None]) is None


def test_every_executable_schema_02_scenario_declares_required_fact_paths() -> None:
    for path in EXECUTABLE_SCHEMA_02_PATHS:
        scenario = json.loads(path.read_text(encoding="utf-8"))
        required_paths = scenario["fact_rubric"]["required_claim_paths"]
        assert required_paths, path
        assert len(required_paths) == len(set(required_paths)), path
        assert all(grade.is_supported_path(item) for item in required_paths), path

    assert EXECUTABLE_SCHEMA_02_PATHS


@pytest.mark.parametrize("path", EXECUTABLE_SCHEMA_02_PATHS, ids=lambda path: path.stem)
def test_required_fact_paths_exist_in_materialized_cli_document(
    path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = json.loads(path.read_text(encoding="utf-8"))
    if path.stem in UNMATERIALIZABLE_SCENARIOS:
        assert all(
            grade.is_supported_path(item)
            for item in scenario["fact_rubric"]["required_claim_paths"]
        )
        pytest.skip("accepted contract has no CLI writer for Deck review evidence")
    frozen = datetime.fromisoformat(scenario["frozen_time"].replace("Z", "+00:00"))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    monkeypatch.setattr(storage_module, "datetime", FrozenDatetime)
    materialize(scenario, tmp_path)
    requirement = scenario["tool_policy"]["required"][0]
    argv = requirement["command"].split()[1:] + requirement["arguments"]

    assert cli.main(["--data-dir", str(tmp_path), *argv]) == 0
    document = json.loads(capsys.readouterr().out)
    for required_path in scenario["fact_rubric"]["required_claim_paths"]:
        values, _ = grade.select_path(document, required_path)
        assert values, (path, required_path)

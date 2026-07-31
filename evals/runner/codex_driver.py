"""Drive one scenario conversation through the Codex App Server protocol.

Speaks the ``codex app-server`` JSONL JSON-RPC protocol over stdio (protocol
pinned against ``codex app-server generate-json-schema``, codex-cli 0.146.0):
``initialize`` -> ``initialized`` -> ``thread/start`` -> one ``turn/start``
per scenario turn on the same thread, collecting ``item/completed`` and
``turn/completed`` notifications into one transcript per turn. Approval
requests are answered ``denied`` so a sandbox escape can never be granted by
the harness; the policy itself is ``never``. An explicit workspace-write
sandbox and a single runtime workspace root constrain writes and network
access. Codex's macOS sandbox still permits host reads outside that root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time
from typing import Any, IO, Sequence


class CodexProtocolError(RuntimeError):
    pass


_APP_SERVER_ARGS = (
    "codex",
    "app-server",
    "-c",
    'web_search="disabled"',
    "-c",
    "apps._default.enabled=false",
    "-c",
    "apps._default.destructive_enabled=false",
    "-c",
    "apps._default.open_world_enabled=false",
    "--disable",
    "plugins",
    "--disable",
    "apps",
)


@dataclass
class AgentTranscript:
    commands: list[dict[str, Any]] = field(default_factory=list)
    agent_messages: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    turn_status: str = "unknown"
    turn_error: dict[str, Any] | None = None
    effective_model: str | None = None
    effective_reasoning_effort: str | None = None
    activity_violations: list[dict[str, str]] = field(default_factory=list)
    confirmed_model: str | None = None
    confirmed_reasoning_effort: str | None = None
    thread_settings_confirmed: bool = False

    @property
    def final_message(self) -> str | None:
        return self.agent_messages[-1] if self.agent_messages else None

    def rendered(self) -> str:
        return json.dumps(self.events)


def codex_version() -> str:
    result = subprocess.run(
        ["codex", "--version"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def run_agent_conversation(
    *,
    prompts: Sequence[str],
    workspace: str,
    developer_instructions: str,
    model: str | None = None,
    effort: str | None = None,
    timeout_seconds: float = 900.0,
) -> list[AgentTranscript]:
    """Run every prompt as a sequential turn on one thread.

    The deadline covers the whole conversation, not each turn: a scenario's
    later turns must not extend the budget its earlier turns already spent.
    """

    if not prompts:
        raise ValueError("a conversation needs at least one prompt")
    with tempfile.TemporaryDirectory(prefix="steam-agent-eval-codex-") as home_name:
        isolated_home = Path(home_name)
        isolated_home.chmod(0o700)
        _copy_auth_file(isolated_home)
        child_environment = os.environ.copy()
        child_environment["CODEX_HOME"] = str(isolated_home)
        workspace_bin = str(Path(workspace) / "bin")
        child_environment["PATH"] = os.pathsep.join(
            (workspace_bin, child_environment.get("PATH", ""))
        )
        process = subprocess.Popen(
            list(_APP_SERVER_ARGS),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_environment,
        )
        try:
            return _converse(
                process,
                prompts=list(prompts),
                workspace=workspace,
                developer_instructions=developer_instructions,
                model=model,
                effort=effort,
                timeout_seconds=timeout_seconds,
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def _copy_auth_file(isolated_home: Path) -> None:
    configured_home = os.environ.get("CODEX_HOME")
    source_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    source = source_home / "auth.json"
    if not source.is_file():
        return
    destination = isolated_home / "auth.json"
    shutil.copyfile(source, destination)
    destination.chmod(0o600)


def run_agent_turn(
    *,
    prompt: str,
    workspace: str,
    developer_instructions: str,
    model: str | None = None,
    effort: str | None = None,
    timeout_seconds: float = 900.0,
) -> AgentTranscript:
    return run_agent_conversation(
        prompts=[prompt],
        workspace=workspace,
        developer_instructions=developer_instructions,
        model=model,
        effort=effort,
        timeout_seconds=timeout_seconds,
    )[0]


def _converse(
    process: subprocess.Popen[str],
    *,
    prompts: Sequence[str],
    workspace: str,
    developer_instructions: str,
    model: str | None,
    effort: str | None,
    timeout_seconds: float,
) -> list[AgentTranscript]:
    assert process.stdin is not None and process.stdout is not None
    session = _Session(process.stdin, process.stdout, timeout_seconds)

    session.request(
        "initialize",
        {
            "clientInfo": {
                "name": "steam-agent-evals",
                "title": "Steam Agent eval runner",
                "version": "0.1.0",
            },
            "capabilities": {"experimentalApi": True},
        },
    )
    session.notify("initialized", {})
    _validate_account_boundary(session)
    thread = session.request(
        "thread/start",
        _thread_start_params(workspace, developer_instructions, model),
    )
    _validate_thread_boundary(thread, workspace)
    thread_id = thread["thread"]["id"]
    _validate_external_tool_boundary(session, thread_id, workspace)
    effective_model = thread.get("model")
    effective_effort = thread.get("reasoningEffort") if effort is None else None

    transcripts: list[AgentTranscript] = []
    for prompt in prompts:
        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if effort is not None:
            turn_params["effort"] = effort
        session.request(
            "turn/start",
            turn_params,
        )
        transcript = _collect_turn(
            session,
            thread_id,
            workspace=workspace,
            effective_model=effective_model,
            effective_reasoning_effort=effective_effort,
        )
        transcripts.append(transcript)
        if transcript.thread_settings_confirmed:
            effective_model = transcript.confirmed_model
            effective_effort = transcript.confirmed_reasoning_effort
    return transcripts


def _thread_start_params(
    workspace: str, developer_instructions: str, model: str | None
) -> dict[str, Any]:
    return {
        "cwd": workspace,
        "sandbox": "workspace-write",
        "runtimeWorkspaceRoots": [workspace],
        "approvalPolicy": "never",
        "dynamicTools": [],
        # Codex 0.146 treats ``environments: []`` as no execution environment
        # and silently downgrades this thread to read-only. The isolated
        # CODEX_HOME has no configured remote environments, so omission can
        # select only App Server's built-in local execution environment.
        "config": {
            "sandbox_workspace_write": {
                "writable_roots": [],
                "network_access": False,
                "exclude_tmpdir_env_var": True,
                "exclude_slash_tmp": True,
            },
            "shell_environment_policy": {
                "inherit": "core",
                "include_only": ["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM"],
                "set": {"HOME": workspace, "TMPDIR": workspace},
            },
        },
        "developerInstructions": developer_instructions,
        "ephemeral": True,
        "model": model,
    }


def _validate_sandbox_boundary(sandbox: Any) -> bool:
    if not isinstance(sandbox, dict):
        return False
    writable_roots = sandbox.get("writableRoots")
    if not isinstance(writable_roots, list):
        return False
    return (
        sandbox.get("type") == "workspaceWrite"
        and sandbox.get("networkAccess") is False
        and sandbox.get("excludeSlashTmp") is True
        and sandbox.get("excludeTmpdirEnvVar") is True
        # Codex 0.146 represents cwd write access implicitly. Any listed root
        # would therefore be an additional write boundary.
        and writable_roots == []
    )


def _validate_thread_boundary(response: dict[str, Any], workspace: str) -> None:
    thread = response.get("thread") or {}
    instruction_sources = response.get("instructionSources")
    valid = (
        isinstance(thread, dict)
        and thread.get("id")
        and thread.get("cwd") == workspace
        and thread.get("ephemeral") is True
        and thread.get("path") is None
        and response.get("cwd") == workspace
        and response.get("approvalPolicy") == "never"
        and response.get("approvalsReviewer") == "user"
        and response.get("activePermissionProfile") is None
        and response.get("runtimeWorkspaceRoots") == [workspace]
        and _instruction_sources_are_local(instruction_sources, workspace)
        and _validate_sandbox_boundary(response.get("sandbox"))
    )
    if not valid:
        raise CodexProtocolError(
            "app-server did not apply the requested workspace/network boundary"
        )


def _validate_settings_boundary(settings: dict[str, Any], workspace: str) -> None:
    valid = (
        settings.get("cwd") == workspace
        and settings.get("approvalPolicy") == "never"
        and settings.get("approvalsReviewer") == "user"
        and settings.get("activePermissionProfile") is None
        and _validate_sandbox_boundary(settings.get("sandboxPolicy"))
    )
    if not valid:
        raise CodexProtocolError(
            "app-server changed the requested workspace/network boundary"
        )


def _instruction_sources_are_local(sources: Any, workspace: str) -> bool:
    if not isinstance(sources, list) or not sources:
        return False
    root = Path(workspace).resolve()
    expected = (root / "AGENTS.md").resolve()
    resolved_sources: list[Path] = []
    for source in sources:
        if not isinstance(source, str):
            return False
        resolved = Path(source).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return False
        resolved_sources.append(resolved)
    return expected in resolved_sources


def _validate_account_boundary(session: _Session) -> None:
    """Require usable local authentication without retaining account details."""

    response = session.request("account/read", {"refreshToken": False})
    account = response.get("account")
    valid = (
        isinstance(response.get("requiresOpenaiAuth"), bool)
        and isinstance(account, dict)
        and account.get("type") in {"apiKey", "chatgpt", "amazonBedrock"}
    )
    if not valid:
        raise CodexProtocolError(
            "app-server authentication is unavailable; refusing to start a thread"
        )


def _validate_external_tool_boundary(
    session: _Session, thread_id: str, workspace: str
) -> None:
    """Fail before a model turn if App Server retained an external tool source."""

    response = session.request(
        "config/read", {"cwd": workspace, "includeLayers": False}
    )
    config = response.get("config")
    config_valid = False
    if isinstance(config, dict):
        apps = config.get("apps")
        apps_default = apps.get("_default") if isinstance(apps, dict) else None
        features = config.get("features")
        config_valid = (
            isinstance(apps_default, dict)
            and isinstance(features, dict)
            and config.get("web_search") == "disabled"
            and apps_default.get("enabled") is False
            and apps_default.get("destructive_enabled") is False
            and apps_default.get("open_world_enabled") is False
            and features.get("apps") is False
            and features.get("plugins") is False
            and config.get("plugins") == {}
        )
    mcp = session.request(
        "mcpServerStatus/list",
        {"threadId": thread_id, "limit": 1, "detail": "toolsAndAuthOnly"},
    )
    mcp_valid = mcp.get("data") == [] and mcp.get("nextCursor") is None
    if not config_valid or not mcp_valid:
        raise CodexProtocolError(
            "app-server retained an external tool source; refusing to start a turn"
        )


_INFORMATIONAL_ITEM_TYPES = {
    "agentMessage",
    "contextCompaction",
    "enteredReviewMode",
    "exitedReviewMode",
    "hookPrompt",
    "plan",
    "reasoning",
    "userMessage",
}


def _collect_turn(
    session: _Session,
    thread_id: str,
    *,
    workspace: str,
    effective_model: str | None,
    effective_reasoning_effort: str | None,
) -> AgentTranscript:
    transcript = AgentTranscript(
        effective_model=effective_model,
        effective_reasoning_effort=effective_reasoning_effort,
    )
    pending_commands: set[str] = set()
    while True:
        message = session.read_message()
        if "method" in message and "id" in message:
            session.deny(message)
            transcript.events.append(message)
            transcript.activity_violations.append(
                {
                    "item_type": f"serverRequest:{message['method']}",
                    "reason": "disallowed_server_request_activity",
                }
            )
            continue
        if "method" not in message:
            continue
        transcript.events.append(message)
        method = message["method"]
        params = message.get("params", {})
        if method == "item/started":
            item = params.get("item", {})
            item_type = item.get("type")
            if item_type == "commandExecution":
                pending_commands.add(str(item.get("id", "<missing>")))
            elif item_type not in _INFORMATIONAL_ITEM_TYPES:
                transcript.activity_violations.append(
                    {
                        "item_type": (
                            item_type if isinstance(item_type, str) else "<missing>"
                        ),
                        "reason": "disallowed_started_item_activity",
                    }
                )
        elif method == "item/completed":
            item = params.get("item", {})
            item_type = item.get("type")
            if item_type == "commandExecution":
                pending_commands.discard(str(item.get("id", "<missing>")))
                transcript.commands.append(
                    {
                        "command": item.get("command", ""),
                        "exit_code": item.get("exitCode"),
                        "status": item.get("status"),
                        "output": item.get("aggregatedOutput"),
                    }
                )
            elif item_type == "agentMessage":
                transcript.agent_messages.append(item.get("text", ""))
            elif item_type not in _INFORMATIONAL_ITEM_TYPES:
                transcript.activity_violations.append(
                    {
                        "item_type": (
                            item_type if isinstance(item_type, str) else "<missing>"
                        ),
                        "reason": "disallowed_completed_item_activity",
                    }
                )
        elif (
            method == "thread/settings/updated"
            and params.get("threadId") == thread_id
        ):
            settings = params.get("threadSettings", {})
            _validate_settings_boundary(settings, workspace)
            transcript.effective_model = settings.get("model")
            transcript.effective_reasoning_effort = settings.get("effort")
            transcript.confirmed_model = settings.get("model")
            transcript.confirmed_reasoning_effort = settings.get("effort")
            transcript.thread_settings_confirmed = True
        elif method == "model/rerouted" and params.get("threadId") == thread_id:
            transcript.effective_model = params.get("toModel")
        elif method == "turn/completed" and params.get("threadId") == thread_id:
            transcript.activity_violations.extend(
                {
                    "item_type": "commandExecution",
                    "reason": "incomplete_command_item_activity",
                }
                for _item_id in sorted(pending_commands)
            )
            turn = params.get("turn", {})
            transcript.turn_status = turn.get("status", "unknown")
            transcript.turn_error = turn.get("error")
            return transcript
        elif method == "error":
            raise CodexProtocolError(
                "app-server reported an error during turn collection"
            )


class _Session:
    def __init__(self, stdin: IO[bytes], stdout: IO[bytes], timeout_seconds: float):
        self._stdin = stdin
        self._stdout = stdout
        self._deadline = time.monotonic() + timeout_seconds
        self._next_id = 0
        self._pending_notifications: list[dict[str, Any]] = []
        self._buffer = b""

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            message = self._read_line()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise CodexProtocolError(f"app-server request {method} failed")
                return message.get("result", {})
            self._pending_notifications.append(message)

    def read_message(self) -> dict[str, Any]:
        if self._pending_notifications:
            return self._pending_notifications.pop(0)
        return self._read_line()

    def deny(self, server_request: dict[str, Any]) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "id": server_request["id"],
                "result": {"decision": "denied"},
            }
        )

    def _write(self, message: dict[str, Any]) -> None:
        self._stdin.write(json.dumps(message).encode() + b"\n")
        self._stdin.flush()

    def _read_line(self) -> dict[str, Any]:
        # Raw fd reads with a private buffer: select() on the fd cannot see
        # lines already sitting in a buffered stream, so buffering is ours.
        while b"\n" not in self._buffer:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise CodexProtocolError("timed out waiting for app-server")
            ready, _, _ = select.select([self._stdout], [], [], remaining)
            if not ready:
                raise CodexProtocolError("timed out waiting for app-server")
            chunk = os.read(self._stdout.fileno(), 65536)
            if not chunk:
                raise CodexProtocolError("app-server closed its stdout")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return json.loads(line)

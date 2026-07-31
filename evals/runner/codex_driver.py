"""Drive one scenario conversation through the Codex App Server protocol.

Speaks the ``codex app-server`` JSONL JSON-RPC protocol over stdio (protocol
pinned against ``codex app-server generate-json-schema``, codex-cli 0.146.0):
``initialize`` -> ``initialized`` -> ``thread/start`` -> one ``turn/start``
per scenario turn on the same thread, collecting ``item/completed`` and
``turn/completed`` notifications into one transcript per turn. Approval
requests are answered ``denied`` so a sandbox escape can never be granted by
the harness; the policy itself is ``never``. A custom permission profile
constrains writes to the workspace, denies network access, denies the host root
by default, and reopens Codex's minimal platform set plus the specific Python
runtime and package paths needed by the frozen CLI launcher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import site
import subprocess
import sys
import sysconfig
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
    "-c",
    "mcp_servers={}",
    "--disable",
    "hooks",
    "--disable",
    "plugins",
    "--disable",
    "apps",
    "--strict-config",
)
_APP_SERVER_LOCALE_ENV_KEYS = ("LANG", "LC_ALL", "LC_CTYPE", "TERM")
_SHELL_ENV_INCLUDE_ONLY = ("PATH", *_APP_SERVER_LOCALE_ENV_KEYS)
_REQUIRED_CODEX_VERSION = "codex-cli 0.146.0"
_VERSION_ERROR = "required codex app-server version is unavailable"
_PERMISSION_PROFILE = "steam-agent-eval"
_MAX_JSONL_FRAME_BYTES = 4 * 1024 * 1024
_MAX_TURN_INPUT_BYTES = 16 * 1024 * 1024
_MAX_CONVERSATION_INPUT_BYTES = 64 * 1024 * 1024
_PROTOCOL_INPUT_LIMIT_ERROR = "app-server protocol input exceeded safety limits"
_INVALID_RESPONSE_ERROR = "app-server returned an invalid response"
_HOOK_ACTIVITY_ERROR = "app-server emitted disallowed hook activity"
_POST_TURN_ACTIVITY_ERROR = "app-server emitted activity after turn completion"
_POST_TURN_BOUNDARY_ERROR = "app-server did not reach an idle turn boundary"
_EXTERNAL_SOURCE_ERROR = (
    "app-server retained an external source or process policy; "
    "refusing to start a thread"
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


@dataclass
class _TurnCollectionState:
    thread_id: str
    turn_id: str
    started: bool = False
    started_items: dict[str, str] = field(default_factory=dict)
    completed_items: set[str] = field(default_factory=set)

    def matches_turn(self, params: dict[str, Any]) -> bool:
        return (
            params.get("threadId") == self.thread_id
            and params.get("turnId") == self.turn_id
        )

    def start_item(self, params: dict[str, Any], item: dict[str, Any]) -> bool:
        item_id = item.get("id")
        item_type = item.get("type")
        status = item.get("status")
        if not (
            self.started
            and self.matches_turn(params)
            and isinstance(item_id, str)
            and item_id
            and isinstance(item_type, str)
            and item_id not in self.started_items
            and item_id not in self.completed_items
            and (status is None or status == "inProgress")
            and (item_type != "commandExecution" or status == "inProgress")
        ):
            return False
        self.started_items[item_id] = item_type
        return True

    def complete_item(self, params: dict[str, Any], item: dict[str, Any]) -> bool:
        item_id = item.get("id")
        item_type = item.get("type")
        status = item.get("status")
        if not (
            self.started
            and self.matches_turn(params)
            and isinstance(item_id, str)
            and isinstance(item_type, str)
            and self.started_items.get(item_id) == item_type
            and (status is None or status in _ITEM_TERMINAL_STATUSES)
            and (
                item_type != "commandExecution"
                or status in _ITEM_TERMINAL_STATUSES
            )
        ):
            return False
        self.started_items.pop(item_id)
        self.completed_items.add(item_id)
        return True


def codex_version() -> str:
    executable = shutil.which(_APP_SERVER_ARGS[0])
    if executable is None:
        raise CodexProtocolError(_VERSION_ERROR)
    _validate_codex_version(executable, environment=None)
    return _REQUIRED_CODEX_VERSION


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
        child_environment = _app_server_environment(isolated_home, Path(workspace))
        app_server = shutil.which(_APP_SERVER_ARGS[0])
        if app_server is None:
            raise CodexProtocolError(_VERSION_ERROR)
        _validate_codex_version(app_server, environment=child_environment)
        process = subprocess.Popen(
            _app_server_process_args(app_server, Path(workspace)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=workspace,
            env=child_environment,
            start_new_session=True,
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
            _terminate_process_group(process)


def _app_server_environment(
    isolated_home: Path, workspace: Path
) -> dict[str, str]:
    """Build a non-secret process environment independent of thread policy.

    The App Server gets only authentication copied into its isolated
    ``CODEX_HOME`` and the small non-secret set below. The executable is
    resolved before launch, so its child PATH does not need a workspace entry.
    """

    environment = {
        key: value
        for key in _APP_SERVER_LOCALE_ENV_KEYS
        if (value := os.environ.get(key)) is not None
    }
    environment.update(
        {
            "CODEX_HOME": str(isolated_home),
            "HOME": str(workspace),
            # The permission profile denies the process TMPDIR so evaluated
            # commands cannot read the copied App Server authentication.
            "TMPDIR": str(isolated_home),
            "PATH": os.defpath,
        }
    )
    return environment


def _validate_codex_version(
    executable: str, *, environment: dict[str, str] | None
) -> None:
    """Exact-gate the protocol implementation without retaining its output."""

    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise CodexProtocolError(_VERSION_ERROR) from None
    if result.returncode != 0 or result.stdout.strip() != _REQUIRED_CODEX_VERSION:
        raise CodexProtocolError(_VERSION_ERROR)


def _app_server_process_args(executable: str, workspace: Path) -> list[str]:
    policy_overrides = (
        'shell_environment_policy.inherit="core"',
        "shell_environment_policy.include_only="
        + json.dumps(list(_SHELL_ENV_INCLUDE_ONLY), separators=(",", ":")),
        "shell_environment_policy.set.HOME=" + json.dumps(str(workspace)),
        "shell_environment_policy.set.TMPDIR=" + json.dumps(str(workspace)),
        "default_permissions=" + json.dumps(_PERMISSION_PROFILE),
        f"permissions.{_PERMISSION_PROFILE}=" + _permission_profile_toml(),
    )
    args = [executable, *_APP_SERVER_ARGS[1:]]
    for override in policy_overrides:
        args.extend(("-c", override))
    return args


def _permission_read_roots() -> tuple[Path, ...]:
    """Return the minimum host roots needed by the frozen Python launcher."""

    scheme_paths = sysconfig.get_paths()
    candidates = [
        Path(sys.executable).resolve(),
        *(
            [Path(runtime_library_dir).resolve()]
            if (runtime_library_dir := sysconfig.get_config_var("LIBDIR"))
            else []
        ),
        *(
            [framework_binary]
            if (framework_binary := _python_framework_binary()) is not None
            else []
        ),
        *(
            Path(value).resolve()
            for name in ("stdlib", "platstdlib", "purelib", "platlib")
            if (value := scheme_paths.get(name))
        ),
        *(Path(value).resolve() for value in site.getsitepackages()),
        (Path(__file__).resolve().parents[2] / "src").resolve(),
    ]
    broad_prefixes = {
        Path(sys.base_prefix).resolve(),
        Path(sys.prefix).resolve(),
    }
    return tuple(
        path for path in dict.fromkeys(candidates) if path not in broad_prefixes
    )


def _python_framework_binary() -> Path | None:
    """Return the exact macOS framework binary needed by framework builds."""

    framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
    if not isinstance(framework, str) or not framework:
        return None
    install_dir = sysconfig.get_config_var("PYTHONFRAMEWORKINSTALLDIR")
    if isinstance(install_dir, str) and install_dir:
        return (Path(install_dir) / framework).resolve()
    prefix = sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX")
    version = sysconfig.get_config_var("VERSION")
    if not (
        isinstance(prefix, str)
        and prefix
        and isinstance(version, str)
        and version
    ):
        return None
    return (
        Path(prefix)
        / f"{framework}.framework"
        / "Versions"
        / version
        / framework
    ).resolve()


def _permission_filesystem_rules() -> dict[str, str]:
    rules = {
        ":root": "deny",
        ":minimal": "read",
        ":tmpdir": "deny",
        ":slash_tmp": "deny",
    }
    rules.update({str(path): "read" for path in _permission_read_roots()})
    return rules


def _permission_profile_toml() -> str:
    filesystem = ",".join(
        f"{json.dumps(path)}={json.dumps(access)}"
        for path, access in _permission_filesystem_rules().items()
    )
    return (
        '{extends=":workspace",filesystem={'
        + filesystem
        + "},network={enabled=false}}"
    )


def _terminate_process_group(
    process: subprocess.Popen[str], *, timeout_seconds: float = 10
) -> None:
    """Terminate the App Server session and non-detached descendants.

    A command that deliberately creates a new session can escape this process
    group; the runner's exact-command policy rejects such shell activity, but
    process-group cleanup alone is not an operating-system process jail.
    """

    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout_seconds
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        # Polling reaps the leader promptly so its zombie does not make the
        # process group look alive for the entire grace period.
        process.poll()
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    if _process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    _validate_external_tool_boundary(session, workspace)
    thread = session.request(
        "thread/start",
        _thread_start_params(workspace, developer_instructions, model),
    )
    _validate_thread_boundary(thread, workspace)
    thread_id = thread["thread"]["id"]
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
        turn_response = session.request(
            "turn/start",
            turn_params,
        )
        turn_id = _validated_turn_id(turn_response)
        transcript = _collect_turn(
            session,
            thread_id,
            turn_id,
            workspace=workspace,
            effective_model=effective_model,
            effective_reasoning_effort=effective_effort,
        )
        transcripts.append(transcript)
        if transcript.thread_settings_confirmed:
            effective_model = transcript.confirmed_model
            effective_effort = transcript.confirmed_reasoning_effort
    _validate_post_turn_boundary(session, thread_id)
    return transcripts


def _validate_post_turn_boundary(session: _Session, thread_id: str) -> None:
    """Order after terminal delivery, attest idle, and reject trailing activity."""

    response = session.request(
        "thread/read", {"threadId": thread_id, "includeTurns": False}
    )
    thread = response.get("thread")
    valid = (
        isinstance(thread, dict)
        and thread.get("id") == thread_id
        and thread.get("status") == {"type": "idle"}
        and thread.get("turns") == []
    )
    if not valid:
        raise CodexProtocolError(_POST_TURN_BOUNDARY_ERROR)
    session.assert_quiescent()


def _validated_turn_id(response: Any) -> str:
    if not isinstance(response, dict):
        raise CodexProtocolError("app-server returned an invalid turn boundary")
    turn = response.get("turn")
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if (
        not isinstance(turn_id, str)
        or not turn_id
        or turn.get("status") != "inProgress"
    ):
        raise CodexProtocolError("app-server returned an invalid turn boundary")
    return turn_id


def _thread_start_params(
    workspace: str, developer_instructions: str, model: str | None
) -> dict[str, Any]:
    return {
        "cwd": workspace,
        "permissions": _PERMISSION_PROFILE,
        "runtimeWorkspaceRoots": [workspace],
        "approvalPolicy": "never",
        "dynamicTools": [],
        # Codex 0.146 treats ``environments: []`` as no execution environment
        # and silently downgrades this thread to read-only. The isolated
        # CODEX_HOME has no configured remote environments, so omission can
        # select only App Server's built-in local execution environment.
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
        and _validate_active_permission_profile(
            response.get("activePermissionProfile")
        )
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
        and _validate_active_permission_profile(
            settings.get("activePermissionProfile")
        )
        and _validate_sandbox_boundary(settings.get("sandboxPolicy"))
    )
    if not valid:
        raise CodexProtocolError(
            "app-server changed the requested workspace/network boundary"
        )


def _validate_active_permission_profile(profile: Any) -> bool:
    return (
        isinstance(profile, dict)
        and profile.get("id") == _PERMISSION_PROFILE
        and profile.get("extends") == ":workspace"
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


def _validate_external_tool_boundary(session: _Session, workspace: str) -> None:
    """Fail before thread creation if App Server retained an external source."""

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
            and features.get("hooks") is False
            and features.get("plugins") is False
            and config.get("mcp_servers") == {}
            and config.get("plugins") == {}
            and _validate_permission_profile_config(config)
            and _validate_shell_environment_policy(
                config.get("shell_environment_policy"), workspace
            )
        )
    if not config_valid:
        raise CodexProtocolError(_EXTERNAL_SOURCE_ERROR)

    hooks = session.request("hooks/list", {"cwds": [workspace]})
    hooks_valid = hooks.get("data") == [
        {
            "cwd": workspace,
            "hooks": [],
            "warnings": [],
            "errors": [],
        }
    ]
    if not hooks_valid:
        raise CodexProtocolError(_EXTERNAL_SOURCE_ERROR)

    mcp = session.request(
        "mcpServerStatus/list",
        {"limit": 1, "detail": "toolsAndAuthOnly"},
    )
    mcp_valid = mcp.get("data") == [] and mcp.get("nextCursor") is None
    if not mcp_valid:
        raise CodexProtocolError(_EXTERNAL_SOURCE_ERROR)


def _validate_permission_profile_config(config: dict[str, Any]) -> bool:
    profiles = config.get("permissions")
    profile = profiles.get(_PERMISSION_PROFILE) if isinstance(profiles, dict) else None
    filesystem = {
        "glob_scan_max_depth": None,
        **_permission_filesystem_rules(),
    }
    network = {
        "enabled": False,
        "proxy_url": None,
        "enable_socks5": None,
        "socks_url": None,
        "enable_socks5_udp": None,
        "allow_upstream_proxy": None,
        "dangerously_allow_non_loopback_proxy": None,
        "dangerously_allow_all_unix_sockets": None,
        "mode": None,
        "domains": None,
        "unix_sockets": None,
        "allow_local_binding": None,
        "mitm": None,
    }
    return (
        config.get("default_permissions") == _PERMISSION_PROFILE
        and profile
        == {
            "description": None,
            "extends": ":workspace",
            "workspace_roots": None,
            "filesystem": filesystem,
            "network": network,
        }
    )


def _validate_shell_environment_policy(policy: Any, workspace: str) -> bool:
    if not isinstance(policy, dict):
        return False
    return policy == {
        "inherit": "core",
        "ignore_default_excludes": None,
        "exclude": None,
        "include_only": list(_SHELL_ENV_INCLUDE_ONLY),
        "set": {"HOME": workspace, "TMPDIR": workspace},
        "experimental_use_profile": None,
        "filters": None,
    }


_INFORMATIONAL_ITEM_TYPES = {
    "agentMessage",
    "contextCompaction",
    "enteredReviewMode",
    "exitedReviewMode",
    "plan",
    "reasoning",
    "userMessage",
}
_DISALLOWED_ITEM_TYPES = {
    "collabAgentToolCall",
    "dynamicToolCall",
    "fileChange",
    "hookPrompt",
    "imageGeneration",
    "imageView",
    "mcpToolCall",
    "sleep",
    "subAgentActivity",
    "webSearch",
}
_GLOBAL_NOTIFICATION_METHODS = {"account/rateLimits/updated"}
_ITEM_SCOPED_NOTIFICATION_METHODS = {
    "item/agentMessage/delta",
    "item/commandExecution/outputDelta",
    "item/plan/delta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
}
_TURN_SCOPED_NOTIFICATION_METHODS = {
    "model/safetyBuffering/updated",
    "model/verification",
    "thread/compacted",
    "thread/tokenUsage/updated",
    "turn/moderationMetadata",
    "turn/plan/updated",
}
_THREAD_SCOPED_NOTIFICATION_METHODS = {"thread/status/changed"}
_ITEM_STATUSES = {"completed", "declined", "failed", "inProgress"}
_ITEM_TERMINAL_STATUSES = {"completed", "declined", "failed"}
_TURN_TERMINAL_STATUSES = {"completed", "failed", "interrupted"}
_SERVER_REQUEST_RESULTS: dict[str, tuple[str, dict[str, Any]]] = {
    "item/commandExecution/requestApproval": (
        "commandApproval",
        {"decision": "decline"},
    ),
    "item/fileChange/requestApproval": ("fileApproval", {"decision": "decline"}),
    "item/tool/requestUserInput": ("userInput", {"answers": {}}),
    "mcpServer/elicitation/request": ("mcpElicitation", {"action": "decline"}),
    "item/permissions/requestApproval": ("permissions", {"permissions": {}}),
    "item/tool/call": (
        "dynamicTool",
        {"contentItems": [], "success": False},
    ),
    "applyPatchApproval": (
        "legacyApproval",
        {"decision": {"denied": {"rejection": "denied by eval harness"}}},
    ),
    "execCommandApproval": (
        "legacyApproval",
        {"decision": {"denied": {"rejection": "denied by eval harness"}}},
    ),
}
_UNFULFILLABLE_SERVER_REQUEST_METHODS = {
    "account/chatgptAuthTokens/refresh",
    "attestation/generate",
    "currentTime/read",
}


def _is_harmless_global_notification(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and set(message) == {"method", "params"}
        and message.get("method") in _GLOBAL_NOTIFICATION_METHODS
        and isinstance(message.get("params"), dict)
    )


@dataclass(frozen=True)
class _MalformedWireEvidence:
    content: dict[str, Any]


@dataclass(frozen=True)
class _DeniedServerRequestEvidence:
    content: dict[str, Any]
    request_kind: str
    thread_id: Any
    turn_id: Any
    item_reference: Any
    ordering_valid: bool


@dataclass(frozen=True)
class _ServerRequestResolutionEvidence:
    content: dict[str, Any]
    thread_id: Any
    ordering_valid: bool


def _wire_fingerprint(message: Any) -> dict[str, Any]:
    rendered = json.dumps(
        message, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    return {
        "sha256": hashlib.sha256(rendered).hexdigest(),
        "byte_length": len(rendered),
    }


def _item_event(method: str, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"method": method, "item": {"type": "<unrecognized>"}}
    item_type = item.get("type")
    known_types = _INFORMATIONAL_ITEM_TYPES | _DISALLOWED_ITEM_TYPES | {
        "commandExecution"
    }
    structural_type = item_type if item_type in known_types else "<unrecognized>"
    event: dict[str, Any] = {"method": method, "item": {"type": structural_type}}
    status = item.get("status")
    if status in _ITEM_STATUSES:
        event["item"]["status"] = status
    return event


def _item_violation_type(item_type: Any) -> str:
    if item_type in _DISALLOWED_ITEM_TYPES:
        return item_type
    return "unrecognizedItem"


def _record_invalid_notification(
    transcript: AgentTranscript,
    method: str,
    message: dict[str, Any],
    reason: str,
) -> None:
    transcript.events.append(
        {"method": method, "valid": False, "content": _wire_fingerprint(message)}
    )
    transcript.activity_violations.append(
        {"item_type": "protocolNotification", "reason": reason}
    )


def _collect_turn(
    session: _Session,
    thread_id: str,
    turn_id: str,
    *,
    workspace: str,
    effective_model: str | None,
    effective_reasoning_effort: str | None,
) -> AgentTranscript:
    transcript = AgentTranscript(
        effective_model=effective_model,
        effective_reasoning_effort=effective_reasoning_effort,
    )
    state = _TurnCollectionState(thread_id=thread_id, turn_id=turn_id)
    while True:
        message = session.read_message()
        if isinstance(message, _MalformedWireEvidence):
            transcript.events.append(
                {
                    "method": "<unrecognized-wire-message>",
                    "content": message.content,
                }
            )
            transcript.activity_violations.append(
                {
                    "item_type": "unrecognizedMessage",
                    "reason": "malformed_wire_message_activity",
                }
            )
            continue
        if isinstance(message, _DeniedServerRequestEvidence):
            scope_valid = (
                message.ordering_valid
                and (message.thread_id is None or message.thread_id == thread_id)
                and (message.turn_id is None or message.turn_id == turn_id)
            )
            item_request_kinds = {
                "commandApproval",
                "dynamicTool",
                "fileApproval",
                "permissions",
                "userInput",
            }
            if message.request_kind in item_request_kinds:
                scope_valid = (
                    scope_valid and message.item_reference in state.started_items
                )
            transcript.events.append(
                {
                    "method": "<server-request>",
                    "denied": True,
                    "scope_valid": scope_valid,
                    "content": message.content,
                }
            )
            transcript.activity_violations.append(
                {
                    "item_type": "serverRequest",
                    "reason": "disallowed_server_request_activity",
                }
            )
            if not scope_valid:
                transcript.activity_violations.append(
                    {
                        "item_type": "serverRequest",
                        "reason": "invalid_server_request_order_or_scope",
                    }
                )
            continue
        if isinstance(message, _ServerRequestResolutionEvidence):
            valid = message.ordering_valid and message.thread_id == thread_id
            transcript.events.append(
                {"method": "serverRequest/resolved", "valid": valid}
            )
            if not valid:
                transcript.activity_violations.append(
                    {
                        "item_type": "serverRequestResolution",
                        "reason": "invalid_server_request_resolution_order_or_scope",
                    }
                )
            continue
        if not isinstance(message, dict):
            transcript.events.append({"method": "<unrecognized-wire-message>"})
            transcript.activity_violations.append(
                {
                    "item_type": "unrecognizedMessage",
                    "reason": "malformed_wire_message_activity",
                }
            )
            continue
        method = message.get("method")
        if "method" in message and "id" in message:
            transcript.events.append(
                {
                    "method": "<server-request>",
                    "content": _wire_fingerprint(message),
                }
            )
            transcript.activity_violations.append(
                {
                    "item_type": "serverRequest",
                    "reason": "disallowed_server_request_activity",
                }
            )
            continue
        if "method" not in message:
            transcript.events.append(
                {
                    "method": "<unrecognized-wire-message>",
                    "content": _wire_fingerprint(message),
                }
            )
            transcript.activity_violations.append(
                {
                    "item_type": "unrecognizedMessage",
                    "reason": "unrecognized_wire_message_activity",
                }
            )
            continue
        raw_params = message.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        if method == "turn/started":
            raw_turn = params.get("turn")
            turn = raw_turn if isinstance(raw_turn, dict) else {}
            valid = (
                not state.started
                and params.get("threadId") == thread_id
                and turn.get("id") == turn_id
                and turn.get("status") == "inProgress"
            )
            if not valid:
                _record_invalid_notification(
                    transcript,
                    "turn/started",
                    message,
                    "invalid_turn_started_order_or_scope",
                )
                continue
            state.started = True
            transcript.events.append({"method": "turn/started", "valid": True})
        elif method == "item/started":
            raw_item = params.get("item")
            item = raw_item if isinstance(raw_item, dict) else {}
            item_type = item.get("type")
            valid = state.start_item(params, item)
            if not valid:
                _record_invalid_notification(
                    transcript,
                    "item/started",
                    message,
                    "invalid_item_started_order_or_scope",
                )
                continue
            transcript.events.append(_item_event(method, item))
            if (
                item_type != "commandExecution"
                and item_type not in _INFORMATIONAL_ITEM_TYPES
            ):
                transcript.activity_violations.append(
                    {
                        "item_type": _item_violation_type(item_type),
                        "reason": "disallowed_started_item_activity",
                    }
                )
        elif method == "item/completed":
            raw_item = params.get("item")
            item = raw_item if isinstance(raw_item, dict) else {}
            item_type = item.get("type")
            valid = state.complete_item(params, item)
            if not valid:
                _record_invalid_notification(
                    transcript,
                    "item/completed",
                    message,
                    "invalid_item_completion_order_or_scope",
                )
                continue
            transcript.events.append(_item_event(method, item))
            if item_type == "commandExecution":
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
                        "item_type": _item_violation_type(item_type),
                        "reason": "disallowed_completed_item_activity",
                    }
                )
        elif (
            method == "thread/settings/updated"
            and params.get("threadId") == thread_id
        ):
            settings = params.get("threadSettings", {})
            _validate_settings_boundary(settings, workspace)
            transcript.events.append(
                {"method": "thread/settings/updated", "boundary_valid": True}
            )
            transcript.effective_model = settings.get("model")
            transcript.effective_reasoning_effort = settings.get("effort")
            transcript.confirmed_model = settings.get("model")
            transcript.confirmed_reasoning_effort = settings.get("effort")
            transcript.thread_settings_confirmed = True
        elif method == "thread/settings/updated":
            _record_invalid_notification(
                transcript,
                "thread/settings/updated",
                message,
                "invalid_thread_settings_scope",
            )
        elif method == "model/rerouted" and state.matches_turn(params):
            transcript.events.append({"method": "model/rerouted"})
            transcript.effective_model = params.get("toModel")
        elif method == "model/rerouted":
            _record_invalid_notification(
                transcript,
                "model/rerouted",
                message,
                "invalid_model_reroute_scope",
            )
        elif method == "turn/completed":
            raw_turn = params.get("turn")
            turn = raw_turn if isinstance(raw_turn, dict) else {}
            status = turn.get("status")
            valid = (
                state.started
                and params.get("threadId") == thread_id
                and turn.get("id") == turn_id
                and status in _TURN_TERMINAL_STATUSES
            )
            if not valid:
                _record_invalid_notification(
                    transcript,
                    "turn/completed",
                    message,
                    "invalid_turn_completion_order_or_scope",
                )
                continue
            transcript.activity_violations.extend(
                {
                    "item_type": (
                        item_type
                        if item_type
                        in _INFORMATIONAL_ITEM_TYPES
                        | _DISALLOWED_ITEM_TYPES
                        | {"commandExecution"}
                        else "unrecognizedItem"
                    ),
                    "reason": "incomplete_item_activity",
                }
                for _item_id, item_type in sorted(state.started_items.items())
            )
            transcript.turn_status = status
            transcript.turn_error = turn.get("error")
            transcript.events.append(
                {
                    "method": "turn/completed",
                    "status": transcript.turn_status,
                    "has_error": transcript.turn_error is not None,
                }
            )
            return transcript
        elif method == "error":
            raise CodexProtocolError(
                "app-server reported an error during turn collection"
            )
        elif method == "remoteControl/status/changed":
            transcript.events.append(
                {"method": "remoteControl/status/changed", "disabled": True}
                if isinstance(params, dict) and params.get("status") == "disabled"
                else {
                    "method": "remoteControl/status/changed",
                    "disabled": False,
                    "content": _wire_fingerprint(message),
                }
            )
            if not isinstance(params, dict) or params.get("status") != "disabled":
                transcript.activity_violations.append(
                    {
                        "item_type": "remoteControl",
                        "reason": "enabled_remote_control_activity",
                    }
                )
        elif method == "thread/started":
            started_thread = params.get("thread") if isinstance(params, dict) else None
            matches = (
                isinstance(started_thread, dict)
                and started_thread.get("id") == thread_id
            )
            transcript.events.append(
                {"method": "thread/started", "thread_matches": matches}
            )
            if not matches:
                transcript.activity_violations.append(
                    {
                        "item_type": "threadStarted",
                        "reason": "unexpected_thread_started_activity",
                    }
                )
        elif method in _ITEM_SCOPED_NOTIFICATION_METHODS:
            item_id = params.get("itemId")
            valid = (
                state.started
                and state.matches_turn(params)
                and isinstance(item_id, str)
                and item_id in state.started_items
            )
            if valid:
                transcript.events.append({"method": method})
            else:
                _record_invalid_notification(
                    transcript,
                    method,
                    message,
                    "invalid_item_notification_order_or_scope",
                )
        elif method in _TURN_SCOPED_NOTIFICATION_METHODS:
            if state.started and state.matches_turn(params):
                transcript.events.append({"method": method})
            else:
                _record_invalid_notification(
                    transcript,
                    method,
                    message,
                    "invalid_turn_notification_order_or_scope",
                )
        elif method in _THREAD_SCOPED_NOTIFICATION_METHODS:
            if params.get("threadId") == thread_id:
                transcript.events.append({"method": method})
            else:
                _record_invalid_notification(
                    transcript,
                    method,
                    message,
                    "invalid_thread_notification_scope",
                )
        elif method in _GLOBAL_NOTIFICATION_METHODS:
            transcript.events.append({"method": method})
        elif method == "serverRequest/resolved":
            _record_invalid_notification(
                transcript,
                "serverRequest/resolved",
                message,
                "unsanitized_server_request_resolution",
            )
        else:
            transcript.events.append(
                {
                    "method": "<unrecognized-notification>",
                    "content": _wire_fingerprint(message),
                }
            )
            transcript.activity_violations.append(
                {
                    "item_type": "unrecognizedNotification",
                    "reason": "unrecognized_notification_activity",
                }
            )


def _request_id_key(value: Any) -> tuple[type[Any], Any] | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return (type(value), value)


def _is_hook_activity(message: dict[str, Any]) -> bool:
    method = message.get("method")
    if isinstance(method, str) and method.startswith("hook/"):
        return True
    if method not in {"item/started", "item/completed"}:
        return False
    params = message.get("params")
    item = params.get("item") if isinstance(params, dict) else None
    return isinstance(item, dict) and item.get("type") == "hookPrompt"


def _validated_response_result(
    message: dict[str, Any], request_id: int, method: str
) -> dict[str, Any]:
    request_key = _request_id_key(request_id)
    response_key = _request_id_key(message.get("id"))
    has_result = "result" in message
    has_error = "error" in message
    response_field = "result" if has_result else "error"
    if (
        request_key is None
        or response_key != request_key
        or has_result == has_error
        # Codex 0.146's generated JSONRPCResponse schema and live App Server
        # responses are deliberately versionless: exactly id + result/error.
        or set(message) != {"id", response_field}
    ):
        raise CodexProtocolError(_INVALID_RESPONSE_ERROR)

    if has_error:
        error = message["error"]
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), int)
            or isinstance(error.get("code"), bool)
            or not isinstance(error.get("message"), str)
            or not set(error).issubset({"code", "message", "data"})
        ):
            raise CodexProtocolError(_INVALID_RESPONSE_ERROR)
        raise CodexProtocolError(f"app-server request {method} failed")

    result = message["result"]
    if not isinstance(result, dict):
        raise CodexProtocolError(_INVALID_RESPONSE_ERROR)
    return result


class _Session:
    def __init__(self, stdin: IO[bytes], stdout: IO[bytes], timeout_seconds: float):
        self._stdin = stdin
        self._stdout = stdout
        self._deadline = time.monotonic() + timeout_seconds
        self._next_id = 0
        self._pending_notifications: list[Any] = []
        self._server_requests: dict[tuple[type[Any], Any], bool] = {}
        self._buffer = bytearray()
        self._turn_input_bytes = 0
        self._conversation_input_bytes = 0

    def begin_turn(self) -> None:
        self._turn_input_bytes = 0

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "turn/start":
            self.begin_turn()
        self._next_id += 1
        request_id = self._next_id
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            message = self._prepare_incoming(self._read_line())
            if isinstance(message, dict) and "method" not in message:
                return _validated_response_result(message, request_id, method)
            self._pending_notifications.append(message)

    def read_message(self) -> Any:
        if self._pending_notifications:
            return self._pending_notifications.pop(0)
        return self._prepare_incoming(self._read_line())

    def assert_quiescent(self) -> None:
        """Drain already ordered input and reject non-global activity."""

        while True:
            if self._pending_notifications:
                message = self._pending_notifications.pop(0)
            else:
                # thread/read is the ordering barrier. Polling once at zero
                # timeout drains only bytes that are already observable after
                # its response, without adding an arbitrary sleep window.
                found, raw_message = self._read_line_until(time.monotonic())
                if not found:
                    if self._buffer:
                        buffered = len(self._buffer)
                        self._buffer.clear()
                        self._charge_input(buffered)
                        self._raise_post_turn_activity()
                    return
                message = self._prepare_incoming(raw_message)
            if not _is_harmless_global_notification(message):
                self._raise_post_turn_activity()

    def _prepare_incoming(self, message: Any) -> Any:
        if not isinstance(message, dict):
            return _MalformedWireEvidence(content=_wire_fingerprint(message))
        if _is_hook_activity(message):
            raise CodexProtocolError(_HOOK_ACTIVITY_ERROR)
        if "method" in message and "id" in message:
            return self._deny_server_request(message)
        if message.get("method") == "serverRequest/resolved":
            return self._sanitize_server_request_resolution(message)
        return message

    def _deny_server_request(
        self, message: dict[str, Any]
    ) -> _DeniedServerRequestEvidence:
        request_id = message.get("id")
        request_key = _request_id_key(request_id)
        method = message.get("method")
        params = message.get("params")
        request_kind = "unknown"
        response: dict[str, Any]
        if isinstance(method, str) and method in _SERVER_REQUEST_RESULTS:
            request_kind, result = _SERVER_REQUEST_RESULTS[method]
            response = {"result": result}
        elif method in _UNFULFILLABLE_SERVER_REQUEST_METHODS:
            request_kind = "unfulfillable"
            response = {
                "error": {
                    "code": -32603,
                    "message": "request unavailable in eval harness",
                }
            }
        else:
            response = {
                "error": {
                    "code": -32601,
                    "message": "request unavailable in eval harness",
                }
            }
        wire_id = request_id if request_key is not None else None
        self._write({"jsonrpc": "2.0", "id": wire_id, **response})

        ordering_valid = request_key is not None and request_key not in self._server_requests
        if ordering_valid:
            self._server_requests[request_key] = False
        scoped = params if isinstance(params, dict) else {}
        item_reference = (
            scoped.get("callId")
            if request_kind == "dynamicTool"
            else scoped.get("itemId")
        )
        return _DeniedServerRequestEvidence(
            content=_wire_fingerprint(message),
            request_kind=request_kind,
            thread_id=scoped.get("threadId"),
            turn_id=scoped.get("turnId"),
            item_reference=item_reference,
            ordering_valid=ordering_valid,
        )

    def _sanitize_server_request_resolution(
        self, message: dict[str, Any]
    ) -> _ServerRequestResolutionEvidence:
        raw_params = message.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        request_key = _request_id_key(params.get("requestId"))
        ordering_valid = (
            request_key is not None
            and request_key in self._server_requests
            and self._server_requests[request_key] is False
        )
        if ordering_valid:
            self._server_requests[request_key] = True
        return _ServerRequestResolutionEvidence(
            content=_wire_fingerprint(message),
            thread_id=params.get("threadId"),
            ordering_valid=ordering_valid,
        )

    def _write(self, message: dict[str, Any]) -> None:
        self._stdin.write(json.dumps(message).encode() + b"\n")
        self._stdin.flush()

    def _read_line(self) -> Any:
        found, message = self._read_line_until(self._deadline)
        if not found:
            raise CodexProtocolError("timed out waiting for app-server")
        return message

    def _read_line_until(self, deadline: float) -> tuple[bool, Any]:
        # Raw fd reads with a private buffer: select() on the fd cannot see
        # lines already sitting in a buffered stream, so buffering is ours.
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > _MAX_JSONL_FRAME_BYTES:
                    self._raise_input_limit()
                consumed = newline + 1
                self._charge_input(consumed)
                line = bytes(self._buffer[:newline])
                del self._buffer[:consumed]
                return True, json.loads(line)
            if len(self._buffer) > _MAX_JSONL_FRAME_BYTES:
                self._raise_input_limit()
            remaining = max(0, deadline - time.monotonic())
            ready, _, _ = select.select([self._stdout], [], [], remaining)
            if not ready:
                return False, None
            chunk = os.read(self._stdout.fileno(), 65536)
            if not chunk:
                raise CodexProtocolError("app-server closed its stdout")
            self._buffer.extend(chunk)

    def _charge_input(self, consumed: int) -> None:
        if (
            self._turn_input_bytes + consumed > _MAX_TURN_INPUT_BYTES
            or self._conversation_input_bytes + consumed
            > _MAX_CONVERSATION_INPUT_BYTES
        ):
            self._raise_input_limit()
        self._turn_input_bytes += consumed
        self._conversation_input_bytes += consumed

    def _raise_input_limit(self) -> None:
        self._buffer.clear()
        self._pending_notifications.clear()
        self._server_requests.clear()
        raise CodexProtocolError(_PROTOCOL_INPUT_LIMIT_ERROR)

    def _raise_post_turn_activity(self) -> None:
        self._buffer.clear()
        self._pending_notifications.clear()
        self._server_requests.clear()
        raise CodexProtocolError(_POST_TURN_ACTIVITY_ERROR)

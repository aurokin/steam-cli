"""Drive one scenario conversation through the Codex App Server protocol.

Speaks the ``codex app-server`` JSONL JSON-RPC protocol over stdio (protocol
pinned against ``codex app-server generate-json-schema``, codex-cli 0.145.0):
``initialize`` -> ``initialized`` -> ``thread/start`` -> one ``turn/start``
per scenario turn on the same thread, collecting ``item/completed`` and
``turn/completed`` notifications into one transcript per turn. Approval
requests are answered ``denied`` so a sandbox escape can never be granted by
the harness; the policy itself is ``never``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import select
import subprocess
import time
from typing import Any, IO, Sequence


class CodexProtocolError(RuntimeError):
    pass


@dataclass
class AgentTranscript:
    commands: list[dict[str, Any]] = field(default_factory=list)
    agent_messages: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    turn_status: str = "unknown"
    turn_error: dict[str, Any] | None = None

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
    timeout_seconds: float = 900.0,
) -> list[AgentTranscript]:
    """Run every prompt as a sequential turn on one thread.

    The deadline covers the whole conversation, not each turn: a scenario's
    later turns must not extend the budget its earlier turns already spent.
    """

    if not prompts:
        raise ValueError("a conversation needs at least one prompt")
    process = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        return _converse(
            process,
            prompts=list(prompts),
            workspace=workspace,
            developer_instructions=developer_instructions,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def run_agent_turn(
    *,
    prompt: str,
    workspace: str,
    developer_instructions: str,
    model: str | None = None,
    timeout_seconds: float = 900.0,
) -> AgentTranscript:
    return run_agent_conversation(
        prompts=[prompt],
        workspace=workspace,
        developer_instructions=developer_instructions,
        model=model,
        timeout_seconds=timeout_seconds,
    )[0]


def _converse(
    process: subprocess.Popen[str],
    *,
    prompts: Sequence[str],
    workspace: str,
    developer_instructions: str,
    model: str | None,
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
            }
        },
    )
    session.notify("initialized", {})
    thread = session.request(
        "thread/start",
        {
            "cwd": workspace,
            "sandbox": "workspace-write",
            "approvalPolicy": "never",
            "developerInstructions": developer_instructions,
            "ephemeral": True,
            "model": model,
        },
    )
    thread_id = thread["thread"]["id"]

    transcripts: list[AgentTranscript] = []
    for prompt in prompts:
        session.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        transcripts.append(_collect_turn(session, thread_id))
    return transcripts


def _collect_turn(session: _Session, thread_id: str) -> AgentTranscript:
    transcript = AgentTranscript()
    while True:
        message = session.read_message()
        if "method" in message and "id" in message:
            session.deny(message)
            transcript.events.append(message)
            continue
        if "method" not in message:
            continue
        transcript.events.append(message)
        method = message["method"]
        params = message.get("params", {})
        if method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "commandExecution":
                transcript.commands.append(
                    {
                        "command": item.get("command", ""),
                        "exit_code": item.get("exitCode"),
                        "status": item.get("status"),
                        "output": item.get("aggregatedOutput"),
                    }
                )
            elif item.get("type") == "agentMessage":
                transcript.agent_messages.append(item.get("text", ""))
        elif method == "turn/completed" and params.get("threadId") == thread_id:
            turn = params.get("turn", {})
            transcript.turn_status = turn.get("status", "unknown")
            transcript.turn_error = turn.get("error")
            return transcript
        elif method == "error":
            raise CodexProtocolError(f"app-server error: {params}")


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
                    raise CodexProtocolError(f"{method} failed: {message['error']}")
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

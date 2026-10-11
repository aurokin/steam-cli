"""Advisory facts from broker policy and session gates; never authorization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from steam_agent.execution.agent_requests import DirectRequest, _permitted
from steam_agent.execution.policy import ExecutionPolicy
from steam_agent.execution.sessions import SessionProtocol


def operation_preflight(
    config: Mapping[str, str], policy: ExecutionPolicy, request: DirectRequest,
    *, platform: str, session: SessionProtocol | None,
    disk_floor: Callable[[Path, int | None], str | None],
) -> dict[str, object]:
    """Observe existing gates without ledger, nonce, client start or dispatch.

    A passing advisory observation does not grant permission or reserve the
    session. The executor must recheck authorization at each effect boundary.
    """
    content = request.operation in {"install", "verify"}
    implemented = platform == "linux" if content else platform in {"linux", "win32"}
    provisioned = not content or bool(
        config.get("steamcmd", "").strip() and config.get("steam_login", "").strip()
        and (request.account_alias or config.get("account_alias", "desktop")) == config.get("account_alias")
    )
    policy_permitted = _permitted(policy, request)
    gates = session.gates() if session is not None and implemented else None
    activity: dict[str, object] | None = None
    activity_clear: bool | None = None
    writer_absent: bool | None = None
    library_available: bool | None = None
    if content and implemented:
        library_available = (Path(config["library"]) / "steamapps").is_dir()
        if session is not None:
            writer_absent = not session.steamcmd_running()
    if gates is not None:
        activity = {
            "game_running": gates.game_running, "remote_play": gates.remote_play,
            "download_in_flight": gates.download_in_flight,
            "client_running": gates.client_running,
        }
        relevant = (
            [gates.game_running, gates.remote_play, gates.download_in_flight]
            if content else [gates.game_running, gates.remote_play]
            if request.operation == "launch" else []
        )
        if request.operation == "stop":
            # Generic session gates cannot establish attribution for this
            # AppID. Until preflight observes its target, stop stays unknown.
            activity_clear = None
        else:
            activity_clear = (
                False if "fail" in relevant
                else None if "unknown" in relevant or gates.client_running == "unknown"
                else True
            )
    floor = None
    if content and policy.min_free_gb is not None:
        floor = disk_floor(Path(config["library"]), policy.min_free_gb) is None
    blocked = (
        not implemented or not provisioned or not policy_permitted
        or activity_clear is False or floor is False
        or writer_absent is False or library_available is False
    )
    return {
        "schema": "broker-preflight/0.1", "advisory": True,
        "operation": request.operation, "appid": request.appid,
        "readiness": "blocked" if blocked else "unknown" if activity_clear is None else "ready",
        "implementation": "implemented" if implemented else "unsupported",
        "native_qualification": "unqualified",
        "provisioned": provisioned,
        "policy": {"grant": policy.grant_for(request.operation), "permitted": policy_permitted},
        "activity": activity, "activity_clear": activity_clear,
        "content_disk_floor_clear": floor,
        "content_writer_absent": writer_absent,
        "content_library_available": library_available,
        "authorization": "not_granted",
    }

"""Direct request intake and receipts, independent of command-line dispatch.

The caller holds the broker state lock across this entire service and releases
it before running an operation. The service writes only the operation ledger;
Steam effects remain in the separately invoked executor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys

from steam_agent.execution.ledger import ExecutionLedger, LedgerError
from steam_agent.execution.policy import ExecutionPolicy, PolicyError, load_policy


_SUPPORTED_OPERATIONS = frozenset({"install", "verify", "launch", "stop"})
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
_ACCOUNT_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class DirectRequest:
    operation: str
    appid: int
    request_id: str
    account_alias: str | None = None
    force: bool = False


def _intent(
    config: Mapping[str, str], request: DirectRequest,
) -> tuple[str, str, str, str]:
    if request.operation not in _SUPPORTED_OPERATIONS:
        raise PolicyError("operation is not executable")
    if (
        isinstance(request.appid, bool)
        or not isinstance(request.appid, int)
        or not 1 <= request.appid <= 0xFFFFFFFF
    ):
        raise PolicyError("appid is malformed")
    if not isinstance(request.request_id, str) or not _REQUEST_ID.fullmatch(request.request_id):
        raise PolicyError("request-id must be 8-128 ASCII identifier characters")
    if not isinstance(request.force, bool):
        raise PolicyError("force must be a boolean")
    if request.force and request.operation != "stop":
        raise PolicyError("force is supported only for stop")
    account_alias = (
        config.get("account_alias", "desktop")
        if request.account_alias is None
        else request.account_alias
    )
    if not isinstance(account_alias, str) or not _ACCOUNT_ALIAS.fullmatch(account_alias):
        raise PolicyError("account alias is malformed")
    machine_id = config.get("machine_id", "local")
    plan_key = "intent:" + hashlib.sha256(request.request_id.encode()).hexdigest()
    document = json.dumps(
        {
            "schema": "operation-intent/0.1",
            "operation": request.operation,
            "target": {
                "appid": request.appid,
                "machine_id": machine_id,
                "account_alias": account_alias,
            },
            "force": request.force,
            "idempotency_key": plan_key,
        },
        sort_keys=True,
    )
    return plan_key, document, account_alias, machine_id


def _permitted(policy: ExecutionPolicy, request: DirectRequest) -> bool:
    return (
        policy.grant_for(request.operation) in {"allow", "confirm"}
        and (request.operation != "launch" or policy.launch_permitted(request.appid))
        and (
            request.operation != "stop"
            or policy.stop_permitted(request.appid, force=request.force)
        )
    )


def require_content_provisioning(
    config: Mapping[str, str], *, operation: str, account_alias: str, platform: str,
) -> None:
    """Refuse unusable new content requests before they reserve an active slot."""
    if operation not in {"install", "verify"}:
        return
    if platform != "linux":
        raise PolicyError("content execution requires a qualified Linux host")
    if not config.get("steamcmd", "").strip():
        raise PolicyError("content execution is not provisioned")
    if (
        not config.get("steam_login", "").strip()
        or account_alias != config.get("account_alias")
    ):
        raise PolicyError("account has no provisioned Steam login")


def record_or_reuse(
    ledger: ExecutionLedger,
    config: Mapping[str, str],
    policy_path: Path,
    request: DirectRequest,
    *,
    platform: str = sys.platform,
    disk_floor: Callable[[Path, int | None], str | None],
) -> dict[str, object]:
    """Record new intent or return its immutable receipt under the caller's lock.

    Fixed content prerequisites fail before recording a request, so an unusable
    active operation cannot prevent provisioning the missing adapter or login.
    Existing receipts remain available even after expiry or policy revocation.
    """

    plan_key, document, account_alias, machine_id = _intent(config, request)
    ledger.expire_lapsed()
    existing = ledger.find_request(
        plan_key, plan_document=document, include_expired=True,
    )
    if existing is not None:
        recorded, nonce = existing
        if recorded.account_alias != account_alias or recorded.machine_id != machine_id:
            raise LedgerError("request-id is bound to different account or machine")
        receipt: dict[str, object] = {
            "operation_id": recorded.operation_id,
            "state": recorded.state,
            "reused": True,
        }
        if nonce is not None:
            receipt["nonce"] = nonce
        if recorded.detail is not None:
            receipt["detail"] = recorded.detail
        return receipt

    policy = load_policy(policy_path)
    if not _permitted(policy, request):
        raise PolicyError("operation, AppID or force is not permitted by policy")
    require_content_provisioning(
        config, operation=request.operation, account_alias=account_alias, platform=platform,
    )

    operation_id, nonce = ledger.request(
        plan_key=plan_key,
        plan_document=document,
        operation=request.operation,
        appid=request.appid,
        account_alias=account_alias,
        machine_id=machine_id,
        policy_version=policy.version,
    )
    receipt = {
        "operation_id": operation_id,
        "state": "pending_confirmation",
        "nonce": nonce,
        "reused": False,
    }
    # Edits after intake cannot auto-authorize from a stale grant. An unreadable
    # second read leaves the durable nonce pending for the existing confirm flow.
    try:
        current = load_policy(policy_path)
    except PolicyError:
        return receipt
    if current.grant_for(request.operation) != "allow" or not _permitted(current, request):
        return receipt
    denied = (
        None
        if request.operation == "stop"
        else disk_floor(Path(config["library"]), current.min_free_gb)
    )
    if denied is not None:
        receipt["auto_confirm_denied"] = denied
        return receipt
    ledger.confirm(nonce=nonce, actor=f"policy:{current.version}")
    return {"operation_id": operation_id, "state": "authorized", "reused": False}

"""Execution policy: which operation classes the owner has granted.

Grants are ``allow`` (auto-authorize at request time within [limits]),
``confirm`` (two-step explicit approval), or ``deny``, for three operation
classes: ``install`` (update shares install's mechanism and plan class),
``verify`` (the repair capability — Valve's validate pass, granted
separately because it replaces locally modified official files), and
``launch`` (client plane; additionally confined to an explicit per-AppID
allowlist, so granting it is never "any game").
Everything else is denied here regardless of file content —
unknown keys, unknown values, and ``allow_unattended`` all fail closed.
The policy file is the owner's recorded intent, an accident brake, and
the kill switch (``install = "deny"``); it is not an enforcement boundary
against the agent, which shares the broker's OS identity (ADR 0028).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import tomllib

SUPPORTED_GRANTS: dict[str, frozenset[str]] = {
    "install": frozenset({"allow", "confirm", "deny"}),
    "verify": frozenset({"allow", "confirm", "deny"}),
    "launch": frozenset({"allow", "confirm", "deny"}),
}
MAX_LAUNCH_ALLOWLIST = 256
_MAX_APPID = 0xFFFFFFFF

POLICY_TEMPLATE = """\
# steam-agent-broker execution policy (ADR 0028).
# Supported keys: install and verify, each "allow" | "confirm" | "deny".
# Anything else fails closed.  "allow" auto-authorizes requests and
# requires [limits] min_free_gb (decimal GB measured on the library
# filesystem).  verify runs Valve's validate pass, which replaces locally
# modified official files, so granting it "allow" also authorizes
# unattended replacement of any mods installed over official content.
# This file is the owner's recorded intent and the kill switch, not an
# enforcement boundary.
[grants]
install = "deny"
verify = "deny"
launch = "deny"

# [limits]
# min_free_gb = 25

# launch additionally requires an explicit allowlist; granting launch
# without one is refused rather than read as "any game".
# [launch]
# allowed_appids = [480]
"""


class PolicyError(RuntimeError):
    """Policy file missing, malformed, or requesting unsupported grants."""


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    version: str
    grants: dict[str, str]
    min_free_gb: int | None = None
    launch_allowlist: frozenset[int] = frozenset()

    def grant_for(self, operation: str) -> str:
        """Return the grant — ``allow``, ``confirm``, or ``deny``."""

        if operation not in SUPPORTED_GRANTS:
            return "deny"
        return self.grants.get(operation, "deny")

    def launch_permitted(self, appid: int) -> bool:
        """Whether this AppID is on the owner's launch allowlist.

        Separate from the grant: the grant says launching is a capability
        at all, the allowlist says which titles. Both must pass.
        """

        return appid in self.launch_allowlist


def load_policy(path: Path) -> ExecutionPolicy:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PolicyError(f"policy file unavailable: {path.name}") from error
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise PolicyError("policy file is not valid TOML") from error

    unsupported_keys = set(document) - {"grants", "limits", "launch"}
    if unsupported_keys:
        raise PolicyError(
            f"unsupported policy key {sorted(unsupported_keys)[0]!r}"
        )
    grants_table = document.get("grants", {})
    if not isinstance(grants_table, dict):
        raise PolicyError("[grants] must be a table")
    grants: dict[str, str] = {}
    for key, value in grants_table.items():
        allowed = SUPPORTED_GRANTS.get(key)
        if allowed is None:
            raise PolicyError(f"unsupported grant key {key!r}")
        if not isinstance(value, str) or value not in allowed:
            raise PolicyError(f"unsupported grant value {value!r} for {key!r}")
        grants[key] = value

    limits_table = document.get("limits", {})
    if not isinstance(limits_table, dict):
        raise PolicyError("[limits] must be a table")
    unsupported_limits = set(limits_table) - {"min_free_gb"}
    if unsupported_limits:
        raise PolicyError(
            f"unsupported limit key {sorted(unsupported_limits)[0]!r}"
        )
    min_free_gb: int | None = None
    if "min_free_gb" in limits_table:
        value = limits_table["min_free_gb"]
        # bool is an int subclass; `true` must not parse as a floor of 1.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PolicyError("min_free_gb must be a non-negative integer")
        min_free_gb = value

    if min_free_gb is None and "allow" in grants.values():
        raise PolicyError('grant "allow" requires [limits] min_free_gb')

    launch_table = document.get("launch", {})
    if not isinstance(launch_table, dict):
        raise PolicyError("[launch] must be a table")
    unsupported_launch = set(launch_table) - {"allowed_appids"}
    if unsupported_launch:
        raise PolicyError(
            f"unsupported launch key {sorted(unsupported_launch)[0]!r}"
        )
    allowlist: set[int] = set()
    if "allowed_appids" in launch_table:
        entries = launch_table["allowed_appids"]
        if not isinstance(entries, list) or len(entries) > MAX_LAUNCH_ALLOWLIST:
            raise PolicyError(
                "allowed_appids must be a list of at most"
                f" {MAX_LAUNCH_ALLOWLIST} AppIDs"
            )
        for entry in entries:
            # bool is an int subclass; `true` must not enroll AppID 1.
            if (
                isinstance(entry, bool)
                or not isinstance(entry, int)
                or not 1 <= entry <= _MAX_APPID
            ):
                raise PolicyError("allowed_appids must contain Steam AppIDs")
            allowlist.add(entry)
    # An empty allowlist with a live grant would read as "any game" to a
    # careless reader and as "none" to this code; refuse the ambiguity.
    if grants.get("launch", "deny") != "deny" and not allowlist:
        raise PolicyError(
            'granting "launch" requires a non-empty [launch] allowed_appids'
        )

    version = hashlib.sha256(raw).hexdigest()[:16]
    return ExecutionPolicy(
        version=version,
        grants=grants,
        min_free_gb=min_free_gb,
        launch_allowlist=frozenset(allowlist),
    )


def write_policy_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PolicyError("policy file already exists; refusing to overwrite")
    path.write_text(POLICY_TEMPLATE, encoding="utf-8")

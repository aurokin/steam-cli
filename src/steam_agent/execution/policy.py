"""Execution policy: which operation classes the owner has granted.

Phase 1 supports exactly one grant value for exactly one operation class:
``install = "confirm"`` (update shares install's mechanism and plan class).
Everything else is denied here regardless of file content — unknown keys,
unknown values, and ``allow_unattended`` all fail closed.  The policy file
lives in the broker identity's state directory; the planner-facing agent
identity cannot edit it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import tomllib

SUPPORTED_GRANTS: dict[str, frozenset[str]] = {"install": frozenset({"confirm", "deny"})}

POLICY_TEMPLATE = """\
# steam-agent-broker execution policy (ADR 0027, Phase 1).
# Supported: install = "confirm" | "deny".  Anything else fails closed.
[grants]
install = "deny"
"""


class PolicyError(RuntimeError):
    """Policy file missing, malformed, or requesting unsupported grants."""


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    version: str
    grants: dict[str, str]

    def grant_for(self, operation: str) -> str:
        """Return ``confirm`` or ``deny`` for an operation class."""

        if operation not in SUPPORTED_GRANTS:
            return "deny"
        return self.grants.get(operation, "deny")


def load_policy(path: Path) -> ExecutionPolicy:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PolicyError(f"policy file unavailable: {path.name}") from error
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise PolicyError("policy file is not valid TOML") from error

    unsupported_keys = set(document) - {"grants"}
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

    version = hashlib.sha256(raw).hexdigest()[:16]
    return ExecutionPolicy(version=version, grants=grants)


def write_policy_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PolicyError("policy file already exists; refusing to overwrite")
    path.write_text(POLICY_TEMPLATE, encoding="utf-8")

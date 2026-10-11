"""Explicit credential resolution for approved acquisition commands only."""

from dataclasses import dataclass
import os
from typing import Callable, Literal

from steam_agent.credentials import SecretValue


CredentialSource = Literal["stored", "process-env"]
CredentialState = Literal[
    "configured", "not_configured", "missing", "store_locked", "store_unavailable"
]


@dataclass(frozen=True, slots=True)
class AcquisitionCredential:
    state: CredentialState
    secret: SecretValue | None = None


def resolve_acquisition_credential(
    source: CredentialSource,
    *,
    stored: Callable[[], AcquisitionCredential],
) -> AcquisitionCredential:
    """Select one source without probing, persisting, or falling back to another.

    The callback confines stored credential access to that explicitly selected
    boundary. Environment mode captures just the fixed Steam provider variable.
    """

    if source == "stored":
        return stored()
    if source != "process-env":
        raise ValueError("unsupported credential source")
    value = os.environ.get("STEAM_WEB_API_KEY")
    if not value:
        return AcquisitionCredential("missing")
    return AcquisitionCredential("configured", SecretValue(value))

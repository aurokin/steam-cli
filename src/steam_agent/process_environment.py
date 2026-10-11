"""Preserve desktop context without passing provider credentials to native tools."""

from collections.abc import Mapping
import os

# Explicit ephemeral ingress uses this fixed variable. Case-insensitive filtering
# also covers Windows environment mappings; arbitrary desktop variables survive.
_PROVIDER_SECRET_VARIABLES = frozenset({"STEAM_WEB_API_KEY"})


def child_environment(
    *, overrides: Mapping[str, str] | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct a child environment without modifying the parent's environment."""
    environment = {
        key: value for key, value in (os.environ if source is None else source).items()
        if key.upper() not in _PROVIDER_SECRET_VARIABLES
    }
    if overrides is not None:
        if any(key.upper() in _PROVIDER_SECRET_VARIABLES for key in overrides):
            raise ValueError("provider credentials cannot be supplied to native children")
        environment.update(overrides)
    return environment

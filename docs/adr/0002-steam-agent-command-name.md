# ADR 0002: `steam-agent` is the public command name

Status: accepted

Date: 2026-07-10

## Context

M1 needs a stable executable name for installation and smoke tests. `steam`
would collide with the Steam client on some systems and would imply ownership
of Valve's general command namespace.

## Decision

Publish the Python distribution and console script as `steam-agent`. The Python
import package is `steam_agent`.

## Consequences

- Documentation and agent integrations should invoke `steam-agent`.
- `python -m steam_agent` is a supported diagnostic entry point.
- Subcommands and JSON schemas remain open decisions; accepting the executable
  name does not accept the exploratory command contract.


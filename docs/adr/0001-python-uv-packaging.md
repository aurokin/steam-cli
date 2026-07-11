# ADR 0001: Python and uv packaging for M1

Status: accepted

Date: 2026-07-10

## Context

The first implementation slice needs an installable CLI boundary before storage,
scanning, provider, or query behavior is added. The design research favored
Python for normalization and ranking work, but the choice needed a concrete
packaging check.

## Decision

Use Python 3.12 or newer, a `src` package layout, PEP 621 project metadata, and
uv for environment, lockfile, build, and development commands. Use Hatchling as
the build backend. The M1 bootstrap has no runtime dependencies.

## Evidence

The package builds as a wheel and source distribution with uv. Both the console
script and `python -m steam_agent` run from the project environment. The Python
requirement is explicit in package metadata.

## Consequences

- Application logic must remain importable independently of the console script.
- Runtime dependencies require an explicit need and lockfile update.
- Supporting Python older than 3.12 is out of scope.
- A future single-binary distribution remains possible but is not part of M1.


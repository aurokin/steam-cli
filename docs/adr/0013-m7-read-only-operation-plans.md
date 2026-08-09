# ADR 0013: M7 read-only operation plans

Status: accepted 2026-07-15; the execution prohibition is superseded for the provisioned broker surface by [ADR 0027](0027-provisioned-execution.md) on 2026-08-08 (the observation, ranking, and inert-plan clauses continue to govern the planner)

## Context

Agents need to answer operational Steam questions, but Steam does not expose a
general documented consumer administration API for launch, install, uninstall,
move, verify, backup, queue, save, or mod management. Local manifests provide a
small amount of useful read-only evidence while undocumented client protocols,
session access, and direct ACF/VDF mutation carry correctness, privacy, and
policy risks.

## Decision

M7 accepts only three capability classes:

1. observe promoted, versioned local evidence;
2. rank candidates with deterministic pure recipes; and
3. return inert, short-lived plans plus official HTTPS human-UI references.

No executable action class is approved. In particular, the CLI does not open a
browser or Steam client, invoke URI schemes, spawn an executor, write Steam
files, use internal IPC, or claim an action completed. `open` in older working
documents is narrowed to returning a typed `human_open` reference.

The local observation schema exposes only installed presence, manifest size and
build identifier, observation time, manifest source modification time, and
evidence lineage. Unsupported runtime, bandwidth, queue, completion time,
currency, storage-target, save, media, Workshop/mod, and compatibility-tool
domains stay explicitly unavailable.

## Alternatives rejected

- Decode manifest state flags into current download/update state: the field is
  an unofficial heuristic and does not prove current client state.
- Use coarse system free space as the selected Steam library's capacity: there
  is no trustworthy identity join.
- Emit legacy Steam URI actions: most administration routes lack a stable,
  reviewed consumer contract, and even documented launch routing would still
  cross the approved no-execution boundary.
- Automate Steam UI or browser interactions: this would require a separate
  policy, authentication, confirmation, and recovery decision.
- Treat game-content backup as save protection: save/config/mod locations and
  Steam Cloud coverage vary by game and are not observed here.

## Consequences

Agents can safely explain what is locally known, rank content-space evidence,
and hand a human a reproducible plan. They cannot promise launch readiness,
installation completion, safe deletion, current updates, or protected saves.
Future adapters and executors can be added behind new versioned schemas and ADRs
without weakening this boundary.

## Acceptance evidence

Accepted with the versioned query/plan contracts, cache-only and no-execution
tripwires, six active deterministic M7 common-question oracles, 1,468 passing
repository tests, Ruff, source/wheel builds, installed-wheel smoke, and a final
two-reviewer Diffwarden pass with zero findings. Review hardening preserved
unknown-machine errors, typed unsupported bandwidth/completion state, CLI-shaped
eval envelopes, ownership/license separation, and truthful plan diagnostics.

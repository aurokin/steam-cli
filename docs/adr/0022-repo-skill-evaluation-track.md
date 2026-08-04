# ADR 0022: repository skill and isolated skill benchmark track

Status: accepted 2026-08-04

## Context

Steam Agent's discovery benchmark measures whether a model can find and use the
CLI from help and a safety contract. It intentionally starts with no personal
or repository skill. That is the right baseline, but it cannot answer a
different product question: whether the guidance shipped with this repository
helps an agent use the CLI correctly for common Steam-library questions.

Adding a skill to the existing discovery or answer tracks would invalidate
their comparison boundary. Relying on ambient user skills would also make the
input unsealed, machine-dependent, and difficult to audit. Merely placing a
skill in the workspace would test implicit selection heuristics rather than the
skill's operational guidance.

## Decision

The repository ships the `steam-agent` project skill at
`.agents/skills/steam-agent`. Its `SKILL.md` maps common library intents to the
smallest cache-only read, preserves evidence and privacy boundaries, states
that exact numeric player counts are unsupported, and keeps every Steam action
human-executed. Its generated `agents/openai.yaml` metadata permits implicit
invocation in ordinary repository use.

Matrix schema `0.1` adds an exclusive `skill` track. A skill track is valid
only as a diagnostic `benchmark`, must be the sole track and sole required
track, and cannot be screened, qualified, accepted, or pooled with the bare
answer or discovery tracks. `product-use-skill-v1.json` runs the same thirteen
questions as `product-use-v2.json` with Sol at medium effort and three
replicates.

The skill tree is part of the committed execution inventory and source
snapshot digest. The sealed copy is stored at `snapshot/skill/steam-agent`,
copied into each private scenario workspace at the canonical project-skill
path, checked byte-for-byte through its inventory digest, and made private.
The child App Server still uses a disposable `CODEX_HOME` containing only
authentication.

Before thread creation, `skills/list` is called with an exact workspace and
forced reload. Bare tracks reject every user- or repository-scoped skill. The
skill track requires exactly one such skill: enabled `steam-agent` at the exact
private workspace path. Built-in system or administrator skills may remain
visible, but no other project or personal skill is accepted. Raw skill
inventory is never persisted.

Every skill-track turn supplies the attested skill with App Server's native
skill input immediately before the unchanged user text. This is explicit
skill-backed evaluation, not evidence that implicit invocation selected the
skill. The developer instructions contain only the evaluation's CLI location,
local context, read-only safety boundary, and terminal claims contract; command
routing and Steam evidence semantics come from the skill.

## Consequences

The discovery result remains a clean measure of unaided CLI use, while the
skill benchmark attributes any change to one sealed repository-owned input.
Skill observations retain the benchmark's deterministic and qualitative
vectors and have no scalar score or acceptance meaning.

Changing the skill, its metadata, the selected scenarios, or the skill-track
contract changes committed input digests and requires a new benchmark config
for comparable fresh observations. Implicit skill-selection quality remains a
separate unanswered question; this track deliberately removes that variable.

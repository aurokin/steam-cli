# Research-backed validation sequence

Status: non-canonical research snapshot, not a release commitment

This document preserves the research rationale and validation order that shaped
the product. Accepted milestone scope and evidence live in the milestone
execution documents (`m1-execution.md` through `m7-execution.md`), under the
rules in [`docs/project-governance.md`](../project-governance.md). The phase
numbers below are historical design groupings and must not be used as milestone
identifiers. Optional, unselected ideas are listed under
[Future ideas](#future-ideas).

The roadmap uses tracer-bullet slices. Each phase should answer a design question
and leave a usable agent contract, not merely add provider integrations.

## Phase 0: capability probes

Build disposable probes and save redacted fixture shapes for:

- documented `GetOwnedGames`, including privacy and free-game behavior
- documented paginated `IStoreService/GetAppList`
- provisional wishlist behavior with public and share-token cases
- provisional storefront metadata/current regional price
- local library folder and app manifest discovery on each supported OS
- system profile collection with identifier redaction

Validate Steam API terms and write down data deletion/retention behavior. Ask
ITAD and GG.deals for permission/terms, and request a narrow Sensor Tower/VGI
quote before treating historical pricing as guaranteed.

Exit criterion: a versioned capability/evidence matrix backed by fixtures and
known failure cases.

## Phase 1: truthful inventory vertical slice

Candidate commands:

```text
steam-agent status
steam-agent capabilities
steam-agent sync owned installed system catalog
steam-agent games query --scope owned --format json
```

Return joined owned/installed/catalog facts with source, freshness, privacy, and
completeness. Store local price snapshots if price is observed, even before deal
ranking exists.

Exit criterion: an agent can distinguish owned, installed, missing, private,
free, non-game, and stale data without reading implementation details.

## Phase 2: wishlist deal evidence

Add the provisional wishlist adapter, regional current price, package/edition
identity, and an approved external historical-price provider. Local observations
remain supplemental. Rank `deal_value` separately from `preference_fit` and
report degraded fallbacks explicitly.

Exit criterion: “best wishlist deals” includes exact evidence and remains useful
when external historical pricing is unavailable.

## Phase 3: next-to-play and feedback

Add recent play, achievements, explicit feedback, deterministic filters, and
versioned `resume`, `finishability`, and `preference_fit` recipes.

Exit criterion: recommendations are reproducible and every positive/negative
factor can be inspected or overridden.

## Phase 4: compatibility

Add structured system profiles, OS/platform hard gates, requirement preservation
and cautious normalization, Deck evidence, runtime risks, and local observations.

Exit criterion: assessments state what is known, inferred, target-specific, and
unknown; no unsupported frame-rate promise is made.

## Phase 5: discovery and groups

Enrich a deliberately bounded catalog; add multi-profile set operations,
multiplayer modes/counts, Remote Play Together, group constraints, and fair
aggregation policies.

Exit criterion: the CLI can calculate ownership intersection/union, missing copy
count, and group eligibility before subjective group ranking.

## Phase 6: optional surfaces

- SteamGridDB custom art adapter
- approved historical-price provider adapters
- high-trust SteamKit/SteamCMD mode
- MCP wrapper over the stable application/query layer

## Phase 7: action planning before execution

Add read-only local operational state plus `plan` and `open` results for launch,
install, uninstall, move, verify, backup, saves, and mods. Keep executors disabled
until each mechanism has a documented policy basis, confirmation class,
postcondition, and recovery story.

These are independent options, not prerequisites for the core.

## Future ideas

M1–M7 are accepted. The items below are optional, unsequenced ideas, not
commitments. Each requires explicit selection under
[`docs/project-governance.md`](../project-governance.md) before any
implementation, and the stated gate must be met first. Former tracker keys are
kept in parentheses as history.

- **Expose the stable application layer through MCP** (AUR-624): optionally
  expose stable CLI/application capabilities over MCP without coupling the core
  domain to MCP; only against already-stable contracts.
- **Replaceable SteamGridDB artwork adapter** (AUR-625): optional attributed
  artwork evidence without making SteamGridDB a core dependency; provider terms
  and distributed/commercial use must be approved first.
- **Evaluate a high-trust SteamKit or SteamCMD mode** (AUR-626): decide whether
  any high-trust client/session integration has enough product value, technical
  stability, and policy basis; no session credentials or reverse-engineered
  client control are implied, and a separate user decision is required.
- **Evaluate adjacent launchers and cross-store adapters** (AUR-635): decide
  whether Playnite, Heroic, Lutris, or similar sources should extend the
  provider-neutral identity/evidence model; terms and platform scope need
  approval.
- **Evaluate guarded launch and client-action executors** (AUR-641): after the
  executable-action boundary is approved, decide whether a narrowly guarded
  launch or client action is worth implementing; a supported mechanism, policy
  basis, explicit confirmation, idempotency, audit, postcondition, and recovery
  plan are mandatory.

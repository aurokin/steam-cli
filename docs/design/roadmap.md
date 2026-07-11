# Research-backed validation sequence

Status: non-canonical research snapshot, not a release commitment

This document preserves the research rationale and validation order that shaped
the product. Current milestone sequence, dependencies, and execution status are
canonical in the
[Steam CLI Product Roadmap](https://linear.app/aurokin/project/steam-cli-product-roadmap-dc80b02971d6)
under the rules in [`docs/project-governance.md`](../project-governance.md).
The phase numbers below are historical design groupings and must not be used as
current Linear milestone identifiers.

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

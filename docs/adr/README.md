# Architecture decision register

Only decisions required by accepted M1–M3 slices are accepted. This register
keeps later product and provider choices open until evidence supports them.

| ID | Decision | State | Reversibility | Evidence needed before ADR |
| --- | --- | --- | --- | --- |
| D001 | Product boundary: evidence/ranking engine vs natural-language agent | Proposed direction | Costly to blur later | Exercise agent workflows against the CLI sketch |
| D002 | Canonical store: SQLite with JSON interface | Implemented for M1; long-term scope open | Moderate | M1 proves migrations, idempotency, and per-machine last-good promotion; broader profile/group volumes and backup/concurrency remain |
| D003 | Implementation language and packaging | [Accepted: Python 3.12+ and uv](0001-python-uv-packaging.md) | Costly after release | Package build and entry-point smoke tests |
| D004 | Provider support-level model | M3 bounded contract accepted | Costly if omitted | Contract-failure fixtures and runtime support/fallback output |
| D005 | Wishlist adapter in supported core | [Accepted provisionally for M3](0004-provisional-wishlist.md) | Easy if isolated | Live count/list, missing-key, malformed, and last-good evidence |
| D006 | Historical-price provider portfolio | [Accepted degraded M3 ladder](0005-m3-deal-providers.md) | Easy if adapter-based | GG/CheapShark live smokes; ITAD remains conditional |
| D007 | Full catalog vs demand-driven enrichment | Demand-bounded observed-AppID slice implemented for M2; broader discovery catalog remains open | Moderate | Live six-page scan persisted only 782 demanded identities; broader discovery quality and storage measurements remain |
| D008 | SteamGridDB integration | Optional | Easy | Art use cases and terms/attribution review |
| D009 | High-trust SteamKit/SteamCMD mode | Deferred | Moderate | Private-data value vs account/session risk |
| D010 | MCP surface | Deferred until CLI stabilizes | Easy | Multiple consumers needing typed live tools |
| D011 | Raw-response retention and deletion | M2 and M3 accepted | Costly for privacy | M3 stores no raw bodies and bounds third-party normalized cache retention |
| D012 | Ranking recipe semantics | Narrow `deal-evidence/0.1` recipe accepted for M3 only | Costly once agents depend on it | Golden comparison, mismatch, fallback, and deterministic-order scenarios |
| D013 | Public binary/package name | [Accepted: `steam-agent`](0002-steam-agent-command-name.md) | Costly after release | Avoids the known `steam` client collision |
| D014 | Read/plan/open/execute action boundary | Proposed direction | Costly and safety-critical | Steam policy review and action-specific capability probes |
| D015 | Provider-neutral game/offer/license identity graph | [M3 exact-AppID offer slice accepted](0006-m3-offer-identity.md); broader joins open | Costly | App/package/bundle/edition mismatch and ambiguity fixtures |
| D016 | Browser reference access modes | M3 manual-only/API distinction accepted | Moderate | URL allowlist and no-follow tests; broader agent-read/open behavior remains open |
| D017 | M1 installed-library process contract | Implemented contract for schema `0.1`; not an acceptance of future commands | Moderate | CLI golden tests, path-redaction tests, partial/failed promotion tests, and AUR-605 acceptance review |
| D018 | M2 credential storage boundary | [Accepted: native `keyring` with explicit protected-file fallback](0003-credential-storage.md) | Moderate | Native backend probes, file-safety tests, redaction tests, and M2 capability review |
| D019 | Preconfigure optional provider API keys before adapter milestones | Implemented; GG.deals active in M3, other adapters remain gated | Easy | Hidden-input tests, provider-isolated references, non-retaining fixed-host probes, and provider terms review |
| D020 | Explicit feedback, durable rules, and temporary constraint precedence | [Accepted for M4](0007-explicit-feedback-and-constraints.md) | Costly once agents write state | Mutation/clear/snooze, three-valued gate, override, isolation, and deletion tests |
| D021 | Recommendation recipe and explanation semantics | [Accepted for M4](0008-versioned-recommendation-recipes.md) | Costly once rankings are consumed | Golden factors, hard-gate, overflow, tie, input-order, and eval scenarios |
| D022 | Activity/achievement purpose and retention | [Accepted bounded M4 slice](0009-m4-activity-retention.md) | Costly for privacy | Disclosure, live privacy/no-stats evidence, LKG/expiry, bounded demand, and deletion tests |

## When to create an ADR

Create an ADR only when:

1. the concrete decision is necessary for the next vertical slice;
2. the alternatives have been tested or eliminated with evidence;
3. consequences, migration path, and reversal cost are understood; and
4. the status is clearly `proposed`, `accepted`, `superseded`, or `rejected`.

Until then, update this register or the design documents rather than writing an
ADR whose filename makes an unsettled choice look final.

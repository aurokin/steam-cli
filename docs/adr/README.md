# Architecture decision register

Only decisions required to bootstrap the M1 implementation are accepted. This
register keeps later product and provider choices open until evidence supports
them.

| ID | Decision | State | Reversibility | Evidence needed before ADR |
| --- | --- | --- | --- | --- |
| D001 | Product boundary: evidence/ranking engine vs natural-language agent | Proposed direction | Costly to blur later | Exercise agent workflows against the CLI sketch |
| D002 | Canonical store: SQLite with JSON interface | Implemented for M1; long-term scope open | Moderate | M1 proves migrations, idempotency, and per-machine last-good promotion; broader profile/group volumes and backup/concurrency remain |
| D003 | Implementation language and packaging | [Accepted: Python 3.12+ and uv](0001-python-uv-packaging.md) | Costly after release | Package build and entry-point smoke tests |
| D004 | Provider support-level model | Proposed direction | Costly if omitted | Contract-failure fixtures for documented/provisional providers |
| D005 | Wishlist adapter in supported core | Open | Easy if isolated | Live privacy/share-token probes and failure behavior |
| D006 | Historical-price provider portfolio | Blocked on outreach | Easy if adapter-based | ITAD approval; GG.deals terms; VGI quote; degraded fallback tests |
| D007 | Full catalog vs demand-driven enrichment | Demand-bounded observed-AppID slice implemented for M2; broader discovery catalog remains open | Moderate | Live six-page scan persisted only 782 demanded identities; broader discovery quality and storage measurements remain |
| D008 | SteamGridDB integration | Optional | Easy | Art use cases and terms/attribution review |
| D009 | High-trust SteamKit/SteamCMD mode | Deferred | Moderate | Private-data value vs account/session risk |
| D010 | MCP surface | Deferred until CLI stabilizes | Easy | Multiple consumers needing typed live tools |
| D011 | Raw-response retention and deletion | Implemented M2 candidate: no raw body, one last-good normalized projection, account-subject deletion, and shared-key removal only on all-Steam-data termination | Costly for privacy | [Steam account lifecycle policy](../design/steam-data-lifecycle.md), [M2 execution plan](../design/m2-execution.md), adversarial redaction/retention tests, per-account/all-account deletion and live resync evidence |
| D012 | Ranking recipe semantics | Open | Costly once agents depend on it | Golden scenarios, user overrides, versioning plan |
| D013 | Public binary/package name | [Accepted: `steam-agent`](0002-steam-agent-command-name.md) | Costly after release | Avoids the known `steam` client collision |
| D014 | Read/plan/open/execute action boundary | Proposed direction | Costly and safety-critical | Steam policy review and action-specific capability probes |
| D015 | Provider-neutral game/offer/license identity graph | M2 application identity foundation implemented with stable entity IDs and typed Steam AppID mapping; offer/license/package/bundle/edition joins remain open | Costly | Cross-edition/package and cross-launcher fixtures |
| D016 | Browser reference access modes | Proposed direction | Moderate | Provider permissions for manual link, human-open, agent-read, and ingestion |
| D017 | M1 installed-library process contract | Implemented contract for schema `0.1`; not an acceptance of future commands | Moderate | CLI golden tests, path-redaction tests, partial/failed promotion tests, and AUR-605 acceptance review |
| D018 | M2 credential storage boundary | [Accepted: native `keyring` with explicit protected-file fallback](0003-credential-storage.md) | Moderate | Native backend probes, file-safety tests, redaction tests, and M2 capability review |
| D019 | Preconfigure optional provider API keys before adapter milestones | Implemented credential checkpoint only; provider adapters remain gated | Easy | Hidden-input tests, provider-isolated references, non-retaining fixed-host probes, and provider terms review |

## When to create an ADR

Create an ADR only when:

1. the concrete decision is necessary for the next vertical slice;
2. the alternatives have been tested or eliminated with evidence;
3. consequences, migration path, and reversal cost are understood; and
4. the status is clearly `proposed`, `accepted`, `superseded`, or `rejected`.

Until then, update this register or the design documents rather than writing an
ADR whose filename makes an unsettled choice look final.

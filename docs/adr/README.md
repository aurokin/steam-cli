# Architecture decision register

Only decisions required by accepted M1–M7 slices are accepted. Linked ADRs are
the canonical accepted records; rows without an accepted ADR remain proposed,
optional, deferred, or narrower implementation facts. This register keeps later
product and provider choices open until evidence supports them.

| ID | Decision | State | Reversibility | Accepted evidence or remaining gate |
| --- | --- | --- | --- | --- |
| D001 | Product boundary: evidence/ranking engine vs natural-language agent | Proposed direction | Costly to blur later | Exercise agent workflows against the CLI sketch |
| D002 | Canonical store: SQLite with JSON interface | Implemented for M1; long-term scope open | Moderate | M1 proves migrations, idempotency, and per-machine last-good promotion; broader profile/group volumes and backup/concurrency remain |
| D003 | Implementation language and packaging | [Accepted: Python 3.12+ and uv](0001-python-uv-packaging.md) | Costly after release | Package build and entry-point smoke tests |
| D004 | Provider support-level model | M3 bounded contract accepted | Costly if omitted | Contract-failure fixtures and runtime support/fallback output |
| D005 | Wishlist adapter in supported core | [Accepted provisionally for M3](0004-provisional-wishlist.md) | Easy if isolated | Live count/list, missing-key, malformed, and last-good evidence |
| D006 | Historical-price provider portfolio | [Accepted degraded M3 ladder](0005-m3-deal-providers.md) | Easy if adapter-based | GG/CheapShark live smokes; ITAD remains conditional |
| D007 | Full catalog vs demand-driven enrichment | [Accepted for M6: bounded known/explicit universe](0012-m6-bounded-discovery-and-groups.md) | Moderate | Owned/installed/wishlist/explicit bounds, no-implicit-fetch tests, and group/discovery evals |
| D008 | SteamGridDB integration | Optional | Easy | Art use cases and terms/attribution review |
| D009 | High-trust SteamKit/SteamCMD mode | Deferred | Moderate | Private-data value vs account/session risk |
| D010 | MCP surface | Deferred until CLI stabilizes | Easy | Multiple consumers needing typed live tools |
| D011 | Raw-response retention and deletion | M2–M4 accepted | Costly for privacy | M4 stores no activity, achievement, review-text, reviewer, or raw provider bodies and bounds normalized cache retention |
| D012 | Ranking recipe semantics | M3 `deal-evidence/0.1` and M4 versioned recommendation recipes accepted | Costly once agents depend on it | Executable golden factors, gates, fallback, deterministic-order, and common-question eval scenarios |
| D013 | Public binary/package name | [Accepted: `steam-agent`](0002-steam-agent-command-name.md) | Costly after release | Avoids the known `steam` client collision |
| D014 | Read/plan/open/execute action boundary | [Accepted M7 read/rank/inert-plan boundary](0013-m7-read-only-operation-plans.md); executors rejected | Costly and safety-critical | Cache-only and no-execution tripwires, deterministic plan evals, and Diffwarden review |
| D015 | Provider-neutral game/offer/license identity graph | [M3 exact-AppID offer slice accepted](0006-m3-offer-identity.md); broader joins open | Costly | App/package/bundle/edition mismatch and ambiguity fixtures |
| D016 | Browser reference access modes | M3 manual-only/API distinction accepted | Moderate | URL allowlist and no-follow tests; broader agent-read/open behavior remains open |
| D017 | M1 installed-library process contract | Implemented contract for schema `0.1`; not an acceptance of future commands | Moderate | CLI golden tests, path-redaction tests, partial/failed promotion tests, and AUR-605 acceptance review |
| D018 | M2 credential storage boundary | [Accepted: native `keyring` with explicit protected-file fallback](0003-credential-storage.md) | Moderate | Native backend probes, file-safety tests, redaction tests, and M2 capability review |
| D019 | Preconfigure optional provider API keys before adapter milestones | Implemented; GG.deals active in M3, other adapters remain gated | Easy | Hidden-input tests, provider-isolated references, non-retaining fixed-host probes, and provider terms review |
| D020 | Explicit feedback, durable rules, and temporary constraint precedence | [Accepted for M4](0007-explicit-feedback-and-constraints.md) | Costly once agents write state | Mutation/clear/snooze, three-valued gate, override, isolation, and deletion tests |
| D021 | Recommendation recipe and explanation semantics | [Accepted for M4](0008-versioned-recommendation-recipes.md) | Costly once rankings are consumed | Golden factors, hard-gate, overflow, tie, input-order, and eval scenarios |
| D022 | Activity/achievement purpose and retention | [Accepted bounded M4 slice](0009-m4-activity-retention.md) | Costly for privacy | Disclosure, live privacy/no-stats evidence, LKG/expiry, bounded demand, and deletion tests |
| D023 | System-profile identity, minimization, and retention | [Accepted with M5](0010-m5-system-profile.md) | Costly for privacy and portability | Cross-platform fixtures, denylist canaries, consent, freshness, LKG, isolation, and deletion tests |
| D024 | Compatibility evidence and precedence | [Accepted layered M5 boundary](0011-m5-layered-compatibility.md) | Costly once agents consume verdicts | Provisional shape fixtures, pure gate/eval oracles, target scoping, unknowns, overrides, and live redacted evidence |
| D025 | Discovery/group evidence, copy matching, and ranking boundary | [Accepted with M6](0012-m6-bounded-discovery-and-groups.md) | Costly once multi-profile results are consumed | Schema migration, category ladders, Kleene sets, matching ranges, privacy/deletion, bounded-universe and ranking evals |
| D026 | Local operational evidence and safe-plan surface | [Accepted with M7](0013-m7-read-only-operation-plans.md) | Moderate; schemas are versioned | Installed freshness/last-good, ranking truthfulness, official-reference allowlists, and no-I/O tests |
| D027 | Agent-execution eval driver: Codex App Server with deterministic-only grading | Implemented opt-in development tooling; not a product surface | Easy; driver is isolated in `evals/runner/` | M7 materializer round-trip tests in normal CI; M5/M4 materializers and any model judging remain open |

## When to create an ADR

Create an ADR only when:

1. the concrete decision is necessary for the next vertical slice;
2. the alternatives have been tested or eliminated with evidence;
3. consequences, migration path, and reversal cost are understood; and
4. the status is clearly `proposed`, `accepted`, `superseded`, or `rejected`.

Until then, update this register or the design documents rather than writing an
ADR whose filename makes an unsettled choice look final.

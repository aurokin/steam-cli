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
| D009 | High-trust SteamKit/SteamCMD mode | [Accepted for the broker content plane by ADR 0027](0027-provisioned-execution.md): steamcmd under a private broker HOME, fail-fast auth; SteamKit remains deferred | Moderate | Phase 0 auth/adoption evidence; credential cache isolated in the broker's private steamcmd HOME |
| D010 | MCP surface | Deferred until CLI stabilizes | Easy | Multiple consumers needing typed live tools |
| D011 | Raw-response retention and deletion | M2–M4 accepted | Costly for privacy | M4 stores no activity, achievement, review-text, reviewer, or raw provider bodies and bounds normalized cache retention |
| D012 | Ranking recipe semantics | M3 `deal-evidence/0.1` and M4 versioned recommendation recipes accepted | Costly once agents depend on it | Executable golden factors, gates, fallback, deterministic-order, and common-question eval scenarios |
| D013 | Public binary/package name | [Accepted: `steam-agent`](0002-steam-agent-command-name.md) | Costly after release | Avoids the known `steam` client collision |
| D014 | Read/plan/open/execute action boundary | [Accepted M7 read/rank/inert-plan boundary](0013-m7-read-only-operation-plans.md) for the planner; [ADR 0027](0027-provisioned-execution.md) accepted 2026-08-08 adds the broker execution surface (install; uninstall human-only); confirmation demoted to policy (auto-confirm within limits) on a single identity by [ADR 0028](0028-trusted-manager-execution.md) | Costly and safety-critical | Phase 0 spike findings recorded in ADR 0027 (raw captures removed for privacy), execution unit tests, planner no-execution tripwires retained |
| D015 | Provider-neutral game/offer/license identity graph | [M3 exact-AppID offer slice accepted](0006-m3-offer-identity.md); broader joins open | Costly | App/package/bundle/edition mismatch and ambiguity fixtures |
| D016 | Browser reference access modes | M3 manual-only/API distinction accepted | Moderate | URL allowlist and no-follow tests; broader agent-read/open behavior remains open |
| D017 | M1 installed-library process contract | Implemented contract for schema `0.1`; not an acceptance of future commands | Moderate | CLI golden tests, path-redaction tests, partial/failed promotion tests, and AUR-605 acceptance review |
| D018 | M2 credential storage boundary | [Accepted: native `keyring` with explicit protected-file fallback](0003-credential-storage.md) | Moderate | Native backend probes, file-safety tests, redaction tests, and M2 capability review |
| D019 | Preconfigure optional provider API keys before adapter milestones | Implemented; GG.deals active in M3, other adapters remain gated | Easy | Hidden-input tests, provider-isolated references, non-retaining fixed-host probes, and provider terms review |
| D020 | Explicit feedback, durable rules, and temporary constraint precedence | [Accepted for M4](0007-explicit-feedback-and-constraints.md) | Costly once agents write state | Mutation/clear/snooze, three-valued gate, override, isolation, and deletion tests |
| D021 | Recommendation recipe and explanation semantics | [Accepted for M4](0008-versioned-recommendation-recipes.md) | Costly once rankings are consumed | Golden factors, hard-gate, overflow, tie, input-order, and eval scenarios |
| D022 | Activity/achievement purpose and retention | [Accepted bounded M4 slice](0009-m4-activity-retention.md) | Costly for privacy | Disclosure, live privacy/no-stats evidence, LKG/expiry, bounded demand, and deletion tests |
| D023 | System-profile identity, minimization, and retention | [Accepted with M5](0010-m5-system-profile.md) | Costly for privacy and portability | Cross-platform fixtures, denylist canaries, consent, freshness, LKG, isolation, and deletion tests |
| D024 | Compatibility evidence and precedence | [Accepted layered M5 boundary](0011-m5-layered-compatibility.md); [accepted wishlist route](0014-wishlist-compatibility-route.md) | Costly once agents consume verdicts | Provisional shape fixtures, pure gate/eval oracles, target scoping, unknowns, overrides, and live redacted evidence |
| D025 | Discovery/group evidence, copy matching, and ranking boundary | [Accepted with M6](0012-m6-bounded-discovery-and-groups.md); [accepted scope-dependency lineage](0014-wishlist-compatibility-route.md); [accepted per-member evidence state](0015-group-member-evidence-state.md) | Costly once multi-profile results are consumed | Schema migration, category ladders, Kleene sets, matching ranges, privacy/deletion, bounded-universe and ranking evals |
| D026 | Local operational evidence and safe-plan surface | [Accepted with M7](0013-m7-read-only-operation-plans.md) | Moderate; schemas are versioned | Installed freshness/last-good, ranking truthfulness, official-reference allowlists, and no-I/O tests |
| D027 | Agent-execution eval driver: Codex App Server with deterministic-only grading | Implemented opt-in development tooling; not a product surface | Easy; driver is isolated in `evals/runner/` | M2–M7 materializer and deterministic-grader tests run in normal CI; live model execution and qualitative judging remain opt-in |
| D028 | Owned playtime truth state and backlog filtering | [Accepted ADR 0016](0016-owned-playtime-truth-state.md) | Easy; derived fields, lineage, and one flag | Zero-versus-null, authority-lineage/privacy, activity upgrade/never-downgrade, expiry, non-authoritative, and filter/limitation tests |
| D029 | Eval required-command alternatives and qualitative review retention | [Accepted ADR 0017](0017-eval-command-equivalence-and-review-retention.md) | Easy; additive scenario/report fields | Exact optional-option matching, malformed-sidecar visibility, privacy and unsafe-tool suppression, and corpus runner tests |
| D030 | Eval qualification cohorts, controls, and answer/discovery tracks | [Accepted ADR 0018](0018-eval-qualification-cohorts-and-tracks.md) | Moderate; run manifests and cohort provenance become comparison inputs | Clean-revision and preflight tests, sealed-input consistency checks, terminal-reason and artifact-publication tests, integrated layer controls, and legacy/answer/discovery policy tests |
| D031 | Eval scenario claim salience and execution support | [Accepted ADR 0019](0019-eval-scenario-claim-and-execution-semantics.md) | Moderate; scenario `0.3` results cannot be pooled with earlier semantics | Schema/corpus migration tests, must-mention oracle coverage, explicit live/deterministic-only cardinality, scenario splits, and an independently attributable cohort |
| D032 | Eval matrix campaigns, adjudication, and fixed-corpus qualification | [Accepted ADR 0020](0020-eval-matrix-campaigns-and-fixed-corpus-qualification.md) | Moderate; campaign and judgment artifacts become qualification inputs | Immutable plan and resume tests, compatible vector comparison, blinded hash-bound adjudication, twelve-route repeated screen, and fresh survivor qualification |
| D033 | Diagnostic product benchmark campaigns | [Accepted ADR 0021](0021-diagnostic-product-benchmark-campaigns.md) | Easy for future configs; observed configs and artifacts remain immutable | Explicit-route schema and plan tests, separate diagnostic report vectors, acceptance rejection, calibrated route-blind imports, and versioned product-use config |
| D034 | Repository skill and isolated skill benchmark track | [Accepted ADR 0022](0022-repo-skill-evaluation-track.md) | Easy for future configs; the bare-track boundary and observed configs remain immutable | Generated and validated repo skill, sealed-input and exact-inventory tests, native skill-input protocol test, private workspace-copy test, exclusive benchmark schema, and a committed live forward test |
| D035 | Orchestrated qualitative review packages | [Accepted ADR 0023](0023-orchestrated-qualitative-review.md) | Easy; external invocation stays separate and imported artifact schemas remain unchanged | Private exact-input packages, structured response schema, bounded retry ledger, validated judgment assembly, and mechanical agreement resolution |
| D036 | Pre-inference qualitative-review package supersession | [Accepted ADR 0024](0024-qualitative-review-package-supersession.md) | Moderate; terminal package history and replacement identities are immutable | Whole-package eligibility checks, content-bound incident provenance, provider-subset schema validation, and one exact-profile live canary before judge slots |
| D037 | Qualitative-review measurement amendments | [Accepted ADR 0025](0025-qualitative-review-measurement-amendments.md) | Easy; one fixed append-only amendment can be ignored by future protocols | Exact package, ledger, case, slot, attempt, canary, operation, and judgment bindings; unavailable duration remains non-authoritative and scoring-independent |
| D038 | Qualitative-review duration-loss recovery | [Accepted ADR 0026](0026-qualitative-review-duration-loss-recovery.md) | Easy; one distinct operation schema can be ignored by future protocols | Exactly one attempt and one recovery operation per matrix; exact verdict/event, package, case, slot, canary, and isolation bindings; unavailable non-scoring duration |

## When to create an ADR

Create an ADR only when:

1. the concrete decision is necessary for the next vertical slice;
2. the alternatives have been tested or eliminated with evidence;
3. consequences, migration path, and reversal cost are understood; and
4. the status is clearly `proposed`, `accepted`, `superseded`, or `rejected`.

Until then, update this register or the design documents rather than writing an
ADR whose filename makes an unsettled choice look final.

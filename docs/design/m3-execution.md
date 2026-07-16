# M3 wishlist and deal evidence execution plan

Status: historical acceptance record; accepted 2026-07-11

This file preserves M3 scope and evidence at acceptance time. Use the
[user guide](../user-guide.md) and [CLI contract](cli-contract.md) for current
behavior across M1–M7.

## Outcome and sequence

M3 answers one bounded question: for the selected account's currently
observable wishlist in an explicit country, which exact Steam application
offers have the strongest supported deal evidence now, and what evidence is
missing?

Linear sequence:

1. AUR-619 approves the provider, identity, attribution, and retention boundary.
2. AUR-632 synchronizes the provisional wishlist and current regional offers.
3. AUR-636 returns the attributed fallback deal query.

This milestone does not add preference fit, compatibility, purchasing,
wishlist mutation, background jobs, broad store discovery, webpage ingestion,
or Steam client execution.

## Accepted source boundary

- Steam wishlist: provisional Valve-hosted count/list pair; 24-hour freshness;
  account-scoped last-good projection; no raw body.
- GG.deals Free: exact AppID current and historical-low summaries; attributed;
  query-key redacted; no full history graph claim.
- CheapShark: USD-only normalized fallback; on-demand; descriptive User-Agent;
  redirect links are manual-only.
- ITAD: deferred until public-project posture or written private-use approval.
- SteamDB and GG webpages: returned as manual-only URLs; never read or scraped.

Country is explicit and never inferred from IP, locale, or account. The first
live acceptance target is `US`; provider-returned currency remains authoritative.

## Retention and truth

Wishlist persistence requires a versioned account acknowledgment. It retains one
last-known-good normalized projection and coarse attempts. Superseded item
evidence is pruned. A count/list mismatch, ambiguous empty envelope, malformed
shape, timeout, rate limit, or interruption never replaces last-good data.

Third-party price data retains no raw body or event graph. The local cache keeps
at most one normalized current/low summary per provider, exact product, and
country. Current evidence is fresh for six hours, low summaries for 24 hours,
and every cached third-party row expires within seven days. Account demand and
provider provenance are deletion-indexed; facts with no remaining demand are
pruned. Coarse price-attempt and per-AppID demand lineage uses the same seven-day
retention boundary. Expiry is enforced atomically on the next price read or
sync; M3 does not introduce a background job. Provider-wide deletion removes
that provider's facts and local key while preserving other providers and M1/M2.

Money uses nonnegative integer minor units plus ISO currency and country. Offers
are comparable only when product, country, currency, store/acquisition scope,
and historical-low interpretation match. Missing price is unknown, not free.

## Implemented AUR-636 query boundary

```text
steam-agent deals query --scope wishlist --account ALIAS --country US [--store-class official|keyshop|unknown] [--format json|table]
```

`official` is the default store class. The query is a deterministic local cache
read over one atomic wishlist-and-price snapshot. It performs no provider
request, secret resolution, refresh, browser navigation, or manual-reference
fetch. Those boundaries keep a query safe for repeated agent calls and make
staleness visible rather than silently refreshing data during evaluation.

The result preserves every candidate and conflicting attributed fact, selected
current and low summaries, evidence IDs, provider attempts, freshness, explicit
comparison scope, limitations, and the GG.deals → CheapShark → manual-reference
ladder. It omits SteamID64, internal account IDs, credentials, raw provider
bodies, and local paths. JSON and table ordering are deterministic, and table
output retains completeness and typed warning rows.

Wishlist states remain distinct: never synchronized is unavailable and not a
confirmed empty list; a valid empty projection is complete; stale, failed, and
abandoned last-good projections are partial; a fresh last-good projection under
an active refresh can remain complete with `SYNC_IN_PROGRESS`. Price states
remain distinct per AppID and provider: `ready`, `not_found`, `unevaluated`,
`failed`, `running`, `abandoned`, `expired`, and `not_synced`. A primary
`not_found` does not complete the ladder until the fallback is evaluated. A fresh fallback
`not_found` completes with price unknown, not free. Stale evidence is reported
as stale; absent or incomplete evaluation is reported as missing.

Deletion is immediately visible because querying never rehydrates data.
Account-scoped Steam deletion removes that account's wishlist and price
lineage. Provider/account deletion removes only that account's demand and facts
for the provider while preserving its shared credential; provider-wide
deletion removes the provider cache and local credential while preserving other
providers and Steam account data.

## Required acceptance harness

Normal CI must cover provider schemas and failures, a full CLI/temp-SQLite
wishlist-to-deal tracer, deterministic ordering, freshness boundaries,
last-good promotion, exact/ambiguous identity, currency mismatch, attribution,
fallbacks, per-account isolation, deletion, redaction, migrations, package
construction, and an installed-wheel subprocess smoke.

Opt-in live acceptance records only coarse aggregates and schema states:

- primary-account wishlist count/list sync repeated idempotently;
- one GG.deals exact AppID current/low lookup;
- one CheapShark AppID/deal fallback lookup;
- explicit fallback after the GG credential is unavailable;
- normal output and SQLite contain no key, SteamID64, raw provider body, or
  personal title list.

Live prices and wishlist counts are volatile and are not asserted in normal CI.
ITAD is not part of the M3 live gate until its approval condition is satisfied.

## Acceptance evidence collected 2026-07-11

- Deterministic CI covers the full wishlist-to-deal tracer, explicit GG.deals
  failure/not-found fallback, bounded partial scans, freshness and hard expiry,
  migration upgrades, credential and provider deletion, redaction, URL
  allowlists, persisted `Retry-After`, and CheapShark seller attribution.
- The final suite passes 615 tests, Ruff, source/wheel construction, and an
  isolated installed-wheel subprocess smoke on the supported Python baseline.
- Live primary-account acceptance synchronized 238 wishlist AppIDs and 238
  GG.deals demand subjects. The cache-only query reconstructed 238 candidates
  with complete current coverage and no secret, credential, API-key, SteamID64,
  raw-body, or local-path fields.
- A live bounded CheapShark call evaluated and observed one demanded AppID,
  retained no raw payload, and reconstructed ten attributed offers with ten
  bounded seller identifiers. Volatile AppIDs, titles, and prices were not
  recorded in this document.
- The final full-scope live sync evaluated all 238 wishlist subjects: GG.deals
  supplied current evidence for 211 and returned 27 truthful current-price
  misses; all 27 misses traversed CheapShark. The resulting cache-only query was
  complete while preserving each provider's distinct `ready`, `not_found`, and
  `unevaluated` states.
- Iterative two-reviewer Diff Warden passes exposed expiry, completeness,
  fallback scheduling, membership churn, provider-ordering, parsing,
  attribution, and multi-account edge cases. Each valid finding received a
  deterministic regression; the final release-gate pass returned zero P1/P2
  findings.

Acceptance remains deliberately narrow: it does not activate full price-event
history, preference recommendations, compatibility, purchases, browser reads,
wishlist mutation, or Steam client actions.

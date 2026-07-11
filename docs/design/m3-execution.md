# M3 wishlist and deal evidence execution plan

Status: active 2026-07-11

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
pruned. Provider-wide deletion removes that provider's facts and local key while
preserving other providers and M1/M2.

Money uses nonnegative integer minor units plus ISO currency and country. Offers
are comparable only when product, country, currency, store/acquisition scope,
and historical-low interpretation match. Missing price is unknown, not free.

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

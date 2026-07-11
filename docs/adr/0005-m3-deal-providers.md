# ADR 0005: M3 deal-provider ladder

Status: accepted for M3 on 2026-07-11

## Decision

Use this capability ladder for the initial wishlist-deal slice:

1. GG.deals Free API for attributed exact Steam AppID current-price and
   historical-low summaries.
2. CheapShark for attributed, USD-only normalized-game current and
   cheapest-ever summaries when GG evidence is unavailable.
3. A manual Steam Store, GG.deals, or SteamDB reference URL when machine-readable
   evidence cannot answer the question.

GG query-key authentication stays inside the transport boundary and every
diagnostic redacts the complete request target. CheapShark uses a descriptive
User-Agent, follows `Retry-After`, performs only user-triggered demand-bounded
requests, and returns redirect URLs as `manual_only` without following them.
Normalized third-party cache rows are limited to one current last-good fact per
provider/product/country and are removed no later than seven days after
retrieval. Raw provider bodies and full history graphs are not retained.

ITAD timestamped history is conditional on a canonical public project posture
or written private-use approval. ITAD OAuth, GG Premium, VGI, pepe.deals,
SteamDB/GG webpage ingestion, and undocumented Steam storefront price ingestion
are outside this decision.

## Consequences

M3 can answer a fresh installation with historical-low evidence, but it must
label low summaries separately from full event history. Provider outages or
identity mismatches degrade explicitly. This ADR does not promise commercial
GG terms, long-term redistribution rights, multi-currency CheapShark data, or a
permanent provider order.

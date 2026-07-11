# ADR 0006: exact-AppID offer identity for M3

Status: accepted for M3 on 2026-07-11

## Decision

M3 compares only offers mapped exactly to the wishlisted Steam application
AppID. Product kind, provider product ID, seller/store, acquisition kind,
activation platform/region, country, currency, regular/final integer minor
units, attribution URL, and observation time remain independent fields.

Package, bundle, DLC, edition, personalized, subscription, and normalized-game
offers remain visible as ambiguous or non-comparable unless an adapter provides
an exact supported mapping. Zero means a confirmed free offer; null means
unknown. Currency or country mismatch is never converted or compared.

The M3 `deal-evidence/0.1` ordering is evidence-only, not preference ranking:
exact comparable evidence before degraded evidence; at/below-low before within
five percent of low before other discounted offers; larger regular-price
discount next; smaller distance above low next; AppID as the stable tie-breaker.
Every component and fallback rung is returned.

## Consequences

This creates the minimum offer layer needed by M3 without guessing editions or
accepting a keyshop offer as equivalent to a Steam license. Broader product
graphs and preference-fit ranking remain open for later milestones.

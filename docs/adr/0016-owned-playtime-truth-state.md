# ADR 0016: owned-query playtime truth state and never-played filtering

Status: accepted 2026-07-31

## Context

The backlog question "what do I own but have never played" needs zero,
positive, and unknown playtime to be separately representable. The owned
projection already distinguishes a recorded zero from an absent playtime
field, but the query exposed no derived state and no filter. Two stores carry
lifetime playtime: the canonical visible-owned snapshot (24-hour freshness,
last-good promotion) and the bounded M4 activity slice (six-hour freshness,
seven-day hard retention, optional consent). Steam omits individually private
games and unplayed free entitlements, so no local list of unplayed games can
be complete.

## Decision

`games query --scope owned` derives `playtime_state`
(`zero | positive | unknown`) with a single reason code per item and accepts
`--playtime any|zero|positive|unknown`. The visible-owned snapshot is the
authority. Unexpired activity evidence may only upgrade zero or unknown to
positive when it is strictly newer than the owned observation; it never
downgrades a positive owned observation and is never required. A missing or
non-authoritative owned snapshot yields unknown for every item. Zero-filtered
output states its lower-bound nature in typed limitations, and pre-filter
state counts are always returned so exclusion of unknowns is visible. The
query remains cache-only, performs no sync, and adds no schema migration.

## Consequences

Backlog listing becomes a deterministic filter over existing evidence rather
than a ranking recipe, and the null-versus-zero distinction becomes an
explicit contract agents can rely on. The result is honest but conservative:
never-played lists exclude unknowns and can miss private or never-synced
titles. Reversal is cheap (derived fields and one flag); the main lock-in is
agents consuming `playtime_state`, which future stores (e.g. local client
evidence) can feed without changing its meaning.

# ADR 0014: canonical wishlist compatibility route and scope-dependency lineage

Status: accepted 2026-07-31

## Context

Agents need "would this wishlist game run on my machine" answers. `sync
compatibility` deliberately demands only cached visible-owned AppIDs (ADR
0011; contract owned-only text), while ADR 0012 merged declared facts into one
provisional `declared-app-facts/0.2` projection that `sync app-facts` already
populates for wishlist scope in the same account/machine/locale demand
lineage `compatibility assess` reads. The route therefore already works but is
uncontracted, and the app-facts branch reports `complete` even when the
snapshot that expanded its scope is missing or stale.

## Decision

Do not widen `sync compatibility`. The canonical wishlist route is
`sync app-facts --scope wishlist` followed by `compatibility assess` over the
wishlist AppIDs. `compatibility assess` accepts arbitrary explicit AppIDs;
`readiness:visible_owned` remains a non-mandatory gate that is unknown for
AppIDs absent from the visible-owned projection and never alone produces
`incompatible`. Visible-owned absence is not a missing-entitlement claim.

`sync app-facts` reports the state of every snapshot used for scope expansion:
missing or stale `wishlist.read` for wishlist scope, `owned.visible.read` for
library scope, missing-only `installed.read` for installed scope (installed
has no defined freshness window), and their union for known scope. A missing
or stale dependency demotes completeness to partial with a typed warning; it
never blocks the sync or erases last-good declarations.

## Consequences

One provider lifecycle keeps serving both owned and wishlist compatibility
demand. Agents get truthful lineage instead of a silently complete sync built
on a stale wishlist. Output that previously reported `complete` under a stale
scope snapshot now reports `partial`; consumers keying on status must read
`stale_capabilities`. Reversal is cheap: the lineage is derived at query
composition time from existing snapshots and no schema changed.

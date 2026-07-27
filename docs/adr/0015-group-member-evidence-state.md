# ADR 0015: per-member evidence state in group queries

Status: proposed

## Context

Group ownership collapses every account member without an authoritative
visible-owned snapshot to per-app `unknown`. A never-synced member, a member
with a stale library, and a member whose owned-library requests return the
provider's inaccessible-or-ambiguous state were indistinguishable in
`group-eligibility/0.1`. The upstream signal already exists locally: a failed
owned sync retains `OWNED_GAMES_INACCESSIBLE_OR_UNKNOWN_ACCOUNT`, and the
accepted M2 contract states this classification is never serialized as
`private`.

## Decision

`OwnershipState` remains exactly `owned | not_owned | unknown`; the Kleene
summary, copy-edge, and matching semantics of ADR 0012 are unchanged, and the
`group_ownership_current` CHECK constraint is not migrated. Group query output
moves to `group-eligibility/0.2` and `group-fit/0.2`, adding one
request-ordinal `members` array whose `member_evidence` is
`authoritative | stale | not_synced | inaccessible | asserted`. For account
members the deterministic precedence is authoritative, then inaccessible
(latest owned attempt failed with the inaccessible code), then not_synced,
then stale. Synthetic members are always `asserted`. `inaccessible` carries
the provider's inaccessible-or-ambiguous meaning; no privacy diagnosis is
invented and no alias is emitted. An inaccessible member adds a typed
completeness warning alongside the existing not-synced and stale warnings.

## Consequences

Agents can distinguish "sync this member" from "this member's library cannot
currently be read" without any change to eligibility or ranking math. The
schema bump is consumed by group evals; 0.1 consumers must read the new ids.
Reversal is cheap: the block is derived at query time from existing sync-run
rows and no durable schema changed.

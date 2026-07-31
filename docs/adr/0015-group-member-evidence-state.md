# ADR 0015: per-member evidence state in group queries

Status: accepted 2026-07-31

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
`group_ownership_current` CHECK constraint is not migrated. Group ownership,
eligibility, and recommendation preserve their accepted
`group-eligibility/0.1` and `group-fit/0.1` output by default. The explicit
`--include-member-evidence` flag selects `group-eligibility/0.2` or
`group-fit/0.2` and adds one request-ordinal `members` array whose
`member_evidence` is
`authoritative | stale | not_synced | inaccessible | asserted`. For account
members the deterministic precedence is authoritative, then inaccessible
(latest owned attempt failed with the inaccessible code), then not_synced,
then stale. Synthetic members are always `asserted`. `inaccessible` carries
the provider's inaccessible-or-ambiguous meaning; no privacy diagnosis is
invented and no alias is emitted.

Only an inaccessible account that is an actual playing member contributes the
0.2 inaccessible diagnostic; an extra copy source does not. In 0.2, an
inaccessible playing member adds the typed warning and
`owned.visible.read` to `missing_capabilities`, and counts as missing ownership
evidence for the completeness ladder. The result is `unavailable` when no
usable ownership evidence remains and `partial` when other ownership evidence
exists. Unflagged 0.1 output retains its prior warning and completeness
semantics.

## Consequences

Agents can distinguish "sync this member" from "this member's library cannot
currently be read" without any change to eligibility or ranking math. The
opt-in schema bump leaves 0.1 consumers unchanged; consumers that need member
evidence must request and read the 0.2 ids. Reversal is cheap: the block is
derived at query time from existing sync-run rows and no durable schema
changed.

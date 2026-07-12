# ADR 0009: bounded M4 activity and achievement retention

Status: accepted for M4 on 2026-07-11

## Context

M4 benefits from activity and achievement evidence, but longitudinal account
behavior is more sensitive than the M2 current owned projection. Achievement
access is also game- and privacy-dependent and can require many requests.

## Decision

Persist normalized current/last-good activity and bounded per-AppID achievement
state only after a versioned disclosure. Retain no raw response or longitudinal
session history. Recent play is a provider window, not sessions. Player
achievement state is optional enrichment and privacy/no-stats remain distinct.

Activity and player state are fresh for hours and hard-deleted with their
attempt lineage within seven days. Public localized achievement schemas may be
cached for 30 days. Achievement acquisition is demand-bounded and sequential;
it never fans out over the entire library by default.

Account and all-Steam-data deletion remove account-scoped M4 evidence. A failed
refresh preserves last-good only within the retention boundary.

## Consequences

M4 can explain recent/resume signals without building a behavioral warehouse.
Finishability remains uncertain when achievements are private/unsupported, and
session/remaining-time claims require explicit user overrides or a later
approved source.

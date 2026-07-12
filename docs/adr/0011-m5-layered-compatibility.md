# ADR 0011: layered declared facts and deterministic compatibility for M5

Status: accepted with M5 on 2026-07-12

## Context

Compatibility is not one provider fact. Publisher requirements are often
unstructured, Valve hardware reviews are target-specific, accessibility
declarations are positive-only, and generic CPU/GPU performance cannot be
compared safely from marketing names. Eager normalization would create false
precision and make source corrections expensive.

## Decision

Keep four layers separate: public declared application facts, private observed
system facts, local installed/owned evidence, and a pure versioned assessment.
`compatibility/0.1` applies pass/fail/unknown gates before producing
`compatible`, `incompatible`, `conditional`, or `unknown`. It preserves source
text, exact target scope, freshness, conflicts, overrides, and evidence lineage.
`meets_minimum`, `likely_good_experience`, Valve target review, runtime risks,
accessibility/input/language constraints, and `playable_now` remain separate.

Only exact comparable architecture and bounded numeric RAM/storage claims may
be normalized initially. CPU/GPU names, GHz, model numbers, and requirement
prose are never ordered. Missing accessibility or runtime declarations are
unknown, not false. Valve Deck/Machine/SteamOS ratings are not generalized to a
custom PC. Installed plus visible-owned does not prove playable-now while M7
operational/update/launcher/network state is unavailable.

The initial declared-fact adapter may use only an explicit, fixed-host,
bounded, normalized-only Steam storefront JSON request. It is provisional and
independently disableable because Valve does not document that retrieval
schema. It is not HTML/page scraping, retains no raw response, uses conservative
pacing, and records country/language context. No reverse-engineered local cache,
Steam client command, session credential, paid provider, ProtonDB ingestion, or
PCGamingWiki ingestion is part of M5. Human-only Steam, ProtonDB, and
PCGamingWiki references may be returned without automated reading.

## Consequences

M5 can add better providers without changing assessment semantics. Coverage is
intentionally incomplete: generic performance, CPU/GPU equivalence, undeclared
accessibility, broad launcher/network risk, and custom-Linux compatibility often
remain unknown. A provisional storefront shape change disables only declared
fact refresh and preserves bounded last-good evidence.

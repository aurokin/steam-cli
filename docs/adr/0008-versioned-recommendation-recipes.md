# ADR 0008: versioned deterministic recommendation recipes

Status: accepted for M4 on 2026-07-11

## Context

Calling agents need inspectable recommendation evidence rather than an opaque
or model-dependent score. Recipe changes can reorder answers and therefore
form part of the public agent contract.

## Decision

M4 recipes are pure, cache-only, immutable/versioned functions. They apply
three-valued hard gates before bounded integer scoring, expose every component
input and evidence reference, keep confidence/completeness outside the score,
and use deterministic tie-breaking independent of database/provider order.

Initial recipe families are resume, finishability, preference fit, and
wishlist fit. A recipe version is never changed in place; behavioral changes
receive a new identifier and golden scenarios.

Deal value remains a separate M3 dimension. Missing enrichment remains unknown
and cannot silently become zero, dislike, incompatibility, or ineligibility.

## Consequences

Recommendations are reproducible and auditable by agents and evals. Recipes
are intentionally less expressive than natural-language models; interpretation
and prose remain with the caller.

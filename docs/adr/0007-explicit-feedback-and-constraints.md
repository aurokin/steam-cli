# ADR 0007: explicit feedback and constraint precedence for M4

Status: accepted for M4 on 2026-07-11

## Context

Ownership, playtime, recent play, achievements, and wishlist membership are
ambiguous behavioral signals. M4 also needs user-authored state and temporary
request constraints without conflating the two or silently persisting a
calling agent's interpretation.

## Decision

Durable feedback is account- and Steam-application-scoped explicit local
evidence. Sentiment, user-authored play state, snooze, estimates, per-game
feature assertions, and profile feature rules remain separate fields with
their own timestamps. `user_abandoned` is distinct from sync abandonment.

Durable feature vocabulary uses bounded exact-match `user:<slug>` identifiers.
The CLI does not infer their meaning. Missing candidate evidence evaluates to
`unknown`.

Temporary recommendation constraints and overrides are command context only.
Hard constraints evaluate to pass/fail/unknown before ranking. A hard failure
can be included only through a named visible override that preserves the
original outcome. Unknown inclusion is an explicit query policy.

Account deletion removes this state. Price-provider deletion does not.

## Consequences

Agents can write user intent without laundering behavior into preference, and
queries remain reproducible. Cross-game taste propagation stays weak until an
approved trait source or explicit user assertions exist. Arbitrary prose notes
and opaque JSON rules are out of scope.

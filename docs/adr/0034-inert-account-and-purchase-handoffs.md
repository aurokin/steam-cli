# ADR 0034: baseline-bound wishlist proposals and purchase cards

Status: **Accepted, 2026-10-11.** Adds cache-only human handoffs; execution
authority remains unchanged.

## Decision

`wishlist plan` emits `wishlist-change-proposal/0.1` for add, remove or
reprioritize. It binds the exact promoted wishlist snapshot and retrieval time,
expected membership, observed priority and evidence/support. Caller-desired
priority is intent, not Steam evidence. Only a latest complete promoted snapshot
within 24 hours establishes expected membership. Missing, future, stale or
superseded baselines block definitive changes; an explicit baseline mismatch is
a typed conflict. Already-satisfied intent is identified at that baseline.

`deals plan` emits `purchase-decision-card/0.1` from existing cached US/USD deal,
visible-owned and optional machine compatibility evidence. A comparable price
requires a fresh, non-future, exact-AppID quote matching country and store class.
Retained, normalized-game or mismatched quotes remain evidence and cannot set
the comparable price. Visible ownership is not a complete license inventory.
Editions, DLC prerequisites and checkout totals remain unknown.

Both commands return official human-open references. They contact no provider,
resolve no credential, construct no executor, perform no cart/wishlist/account
mutation, and authorize nothing. Completion requires the human action followed
by a separate refresh. Store, market, wallet and credential operations remain
hard-denied in the broker.

## Evidence and alternatives

Focused tests exercise stale/future/superseded/conflicting snapshots, membership
and priority, exact versus normalized quotes, store context, missing accounts,
unsupported machine evidence, human-only references and absence of acquisition
or cache mutation. A consumer purchase/write API has not been established;
browser automation and stored consumer cookies would add a separate unqualified
authority and authentication surface. Inert evidence handoffs meet the useful
planning slice without inventing that support.

## Consequences and reversal

Payload schemas are additive inside the existing response envelope. There is
no database migration or new executable class. These commands may be removed
through normal command deprecation; any future write backend requires a new
accepted decision, reviewed intent/authentication and independent qualification.

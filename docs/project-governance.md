# Project governance

This document defines where project information belongs and how roadmap work
moves from a proposal to accepted behavior. It governs planning and
documentation; product architecture remains in the design documents and ADRs.

## Current boundary

M1 through M5 are accepted historical work. M6 and later milestones remain
roadmap hypotheses until the user explicitly selects one. Creating a milestone
or issue does not approve an implementation, provider, credential flow, policy
interpretation, or ADR.

The completed M1 Linear project must not be repurposed for later scope. Future
work belongs in the separate Steam CLI product-roadmap project.

Linear records:

- [M1 — Installed Library](https://linear.app/aurokin/project/steam-cli-m1-installed-library-e0f0bdc817cc)
- [Steam CLI — Product Roadmap](https://linear.app/aurokin/project/steam-cli-product-roadmap-dc80b02971d6)

## Sources of truth

| Information | Canonical home |
| --- | --- |
| Product semantics, architecture, evidence contracts, and policies | Repository design documents |
| Accepted technical or product decisions | Repository ADRs |
| Provider research, terms, costs, and limitations | Repository design/research documents |
| Roadmap sequence and milestone outcomes | Linear |
| Status, priority, ownership, dependencies, and active execution | Linear |
| Acceptance evidence for completed work | Repository, referenced from Linear |

Linear may summarize repository material for orientation, but it must not become
a second specification. Repository documents may reference Linear execution
history, but must remain understandable when Linear is unavailable.

## Repository references from Linear

Until the repository has a canonical remote, Linear uses backticked,
repository-relative paths such as `docs/design/pricing-strategy.md`. These are
temporarily non-clickable by design. Never store an absolute local path,
`file://` URL, or editor-specific link in Linear.

After a canonical remote exists, add default-branch links for living documents
and commit permalinks for immutable acceptance evidence. Retain the relative
path in issue text so the reference works in every checkout.

## Milestone lifecycle

- **Proposed:** outcome-level roadmap hypothesis; no delivery commitment.
- **Selected:** explicitly chosen by the user as the next milestone.
- **Active:** exit criteria and independently verifiable tracer bullets have
  been refined.
- **Accepted:** repository evidence is recorded and execution is closed in
  Linear.
- **Superseded or canceled:** retained with rationale rather than rewritten.

Only the selected milestone is refined into implementation issues. Inactive
milestones receive no implementation subtasks, estimates, due dates, or implied
technical choices. Before activation, revalidate time-sensitive provider terms
and assumptions. Credentials, spending, policy-sensitive automation, and action
execution always require a separate human checkpoint.

Refinement produces thin, demonstrable end-to-end outcomes rather than
component-layer task trees. If refinement materially changes a milestone's
outcome or dependencies, return to the human checkpoint.

## Decisions and ADRs

Linear can track that a decision is required; it cannot accept the decision.
Completing a Linear issue does not create an ADR or make a proposed design
canonical. Follow `docs/adr/README.md`: accept an ADR only when an active slice
requires it and its evidence, consequences, migration, and reversal cost are
understood.

Unsettled choices remain explicitly proposed, open, or deferred in design docs
or the decision register.

## Completed work

Accepted projects and issues are historical records. Do not rewrite their scope
or acceptance criteria to match later behavior. Append clearly dated factual
corrections when necessary. New behavior, defects, and superseding decisions
belong in new issues and, where appropriate, new or superseding ADRs.

## Synchronization checklist

- Linear names the canonical repository-relative references.
- Detailed specifications, provider matrices, schemas, and policy rules are not
  copied into Linear.
- Proposed choices are not described as accepted.
- Inactive milestones have no implementation task breakdown.
- Each active slice produces a stable capability or query result, not merely an
  adapter or research artifact.
- Completion links to tests, review results, and repository acceptance evidence.

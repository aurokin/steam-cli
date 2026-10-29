# Project governance

This document defines where project information belongs and how roadmap work
moves from a proposal to accepted behavior. It governs planning and
documentation; product architecture remains in the design documents and ADRs.

## Current boundary

M1 through M7 are accepted historical work. Later milestones remain roadmap
hypotheses until the user explicitly selects one. Creating a milestone or issue
does not approve an implementation, provider, credential flow, policy
interpretation, or ADR.

Accepted milestone records must not be repurposed for later scope. Optional
future ideas are listed in the [research roadmap](design/roadmap.md#future-ideas);
none is selected.

Planning and execution history before 2026-10 was tracked in Linear. Issue
keys such as `AUR-605` that appear in historical documents are retained as
plain references; the repository records hold the accepted outcomes.

## Sources of truth

| Information | Canonical home |
| --- | --- |
| Product semantics, architecture, evidence contracts, and policies | Repository design documents |
| Accepted technical or product decisions | Repository ADRs |
| Provider research, terms, costs, and limitations | Repository design/research documents |
| Roadmap sequence, milestone outcomes, and future ideas | Repository roadmap and milestone execution documents |
| Status, priority, ownership, dependencies, and active execution | GitHub issues and pull requests |
| Acceptance evidence for completed work | Repository milestone execution documents |

Issues and pull requests may summarize repository material for orientation, but
they must not become a second specification.

## References to repository material

The canonical remote is `aurokin/steam-cli` on GitHub. Use default-branch
links for living documents and commit permalinks for immutable acceptance
evidence. Retain the repository-relative path so a reference remains
meaningful in every checkout.

Never store an absolute local path, `file://` URL, or editor-specific link in
issues or documents. A link to a moving branch is not immutable acceptance
evidence.

## Milestone lifecycle

- **Proposed:** outcome-level roadmap hypothesis; no delivery commitment.
- **Selected:** explicitly chosen by the user as the next milestone.
- **Active:** exit criteria and independently verifiable tracer bullets have
  been refined.
- **Accepted:** repository evidence is recorded in the milestone execution
  document and the related issues are closed.
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

An issue can track that a decision is required; it cannot accept the decision.
Closing an issue does not create an ADR or make a proposed design
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

## Planning checklist

- Issues name the canonical repository-relative references.
- Detailed specifications, provider matrices, schemas, and policy rules are not
  copied into issues.
- Proposed choices are not described as accepted.
- Inactive milestones have no implementation task breakdown.
- Each active slice produces a stable capability or query result, not merely an
  adapter or research artifact.
- Completion links to tests, review results, and repository acceptance evidence.

# Design map

Status: maintained index of current design authority and historical records.

Use this page to find the canonical design source. The broader
[documentation map](../README.md) explains authority and lifecycle labels.

## Current design and contracts

| Topic | Canonical source | Status |
| --- | --- | --- |
| Implemented system boundaries and data flow | [Architecture](architecture.md) | Accepted architecture, maintained |
| Shared machine behavior and documented command contracts | [CLI contract](cli-contract.md) | M1–M7 accepted; `--help` is exhaustive syntax |
| Supported question vocabulary and evidence distinctions | [Product questions](product-questions.md) | Working vocabulary; not a support matrix |
| Provider support, terms, and limitations | [Evidence matrix](evidence-matrix.md) | Working, time-sensitive |
| Data privacy, retention, acknowledgment, and deletion | [Steam data lifecycle](steam-data-lifecycle.md) | Accepted M2–M4 policy; index to M5–M6 ADRs |
| Steam actions and confirmation classes | [Actions](actions.md) | M7 boundary accepted; future classes proposed |
| Broker execution (install/update; uninstall human-only) | [Execution plan](execution-plan.md) and [Linux session model](execution-linux-session-model.md) | [ADR 0027](../adr/0027-provisioned-execution.md) accepted 2026-08-08, re-scoped by [ADR 0028](../adr/0028-trusted-manager-execution.md) (trusted-manager model, standing grants implemented) and [ADR 0029](../adr/0029-move-as-inert-plan.md) (move is an inert plan); Phase 1 broker implemented (`steam-agent-broker`), later phases proposed |
| Historical pricing providers and fallback | [Pricing strategy](pricing-strategy.md) | M3 boundary accepted; providers time-sensitive |
| Evaluation and acceptance | [Evaluation strategy](evaluation-strategy.md), [corpus and runner](../../evals/README.md), and [testing](../testing.md) | Working strategy; deterministic gate accepted |

Accepted architectural choices are indexed in the
[ADR register](../adr/README.md). If a summary conflicts with an ADR, update the
summary; do not silently reinterpret the decision.

## Historical milestone records

The execution documents record scope, evidence, and acceptance at a point in
time. They are not living user guides and should not be rewritten to absorb
later milestones.

| Milestone | Record |
| --- | --- |
| M1 installed library | [m1-execution.md](m1-execution.md) |
| M2 truthful account inventory | [m2-execution.md](m2-execution.md) |
| M3 wishlist and deal evidence | [m3-execution.md](m3-execution.md) |
| M4 next-to-play and preference | [m4-execution.md](m4-execution.md) |
| M5 compatibility and ready-now | [m5-execution.md](m5-execution.md) |
| M6 discovery, household, and groups | [m6-execution.md](m6-execution.md) |
| M7 local operations and safe plans | [m7-execution.md](m7-execution.md) |

For current commands, use the [user guide](../user-guide.md) or exact
[CLI contract](cli-contract.md).

## Research and reference material

- [Evaluation system verification and improvement brainstorm](evaluation-system-brainstorm.md)
  is a proposed, evidence-backed work inventory; none of its recommendations
  are accepted by inclusion in the document.
- [Existing tools](existing-tools.md) is a landscape snapshot, not product
  authority.
- [Roadmap](roadmap.md) is a noncanonical research sequence. Accepted milestone
  truth lives in the execution records and ADRs above.
- `../../steam-library-agent-research-handoff.md` is unverified source material.

## Changing the design

Update the narrowest canonical document. Use explicit `Accepted`, `Working`,
`Proposed`, `Deferred`, or `Historical` labels. Add or supersede an ADR for a
durable decision; completing a task does not change design authority by itself.
Keep provider adapters replaceable and re-verify time-sensitive provider terms
before relying on them.

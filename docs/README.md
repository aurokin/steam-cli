# Documentation map

This page explains where project truth lives. Start at the shortest document
that answers your question, then follow links for detail.

## Choose an entry point

| Audience or need | Start here |
| --- | --- |
| Evaluate the project or get a first result | [Project README](../README.md) |
| Use current commands safely | [User guide](user-guide.md) |
| Change the code or tests | [Contributor guide](../CONTRIBUTING.md) |
| Run or interpret model evaluations | [Evaluation guide](../evals/README.md) |
| Understand the architecture or a contract | [Design map](design/README.md) |
| Understand an accepted decision | [ADR index](adr/README.md) |
| Understand project/Linear synchronization | [Project governance](project-governance.md) |

## Source-of-truth order

When documents appear to disagree, resolve them in this order:

1. Accepted ADRs define durable decisions and their evidence.
2. The CLI parser and `--help` define exhaustive executable syntax; tested code,
   schemas, and the [CLI contract](design/cli-contract.md) define machine
   behavior and documented contracts.
3. Living policy/design documents define current boundaries and rationale.
4. Milestone execution documents record what was accepted at that milestone.
5. Research handoffs and the research roadmap are noncanonical source material.

An inconsistency in the first three layers is a defect. Fix the canonical
source and any entry-point summary together; do not preserve contradictory
copy.

## Document lifecycle

| Label | Meaning |
| --- | --- |
| Accepted | Evidence-backed behavior or decision that current work must preserve |
| Working | Maintained guidance that may evolve without claiming an ADR decision |
| Proposed | A candidate direction that is not authorized or implemented truth |
| Deferred | Intentionally outside the current slice |
| Historical | An acceptance or research record preserved for context, not current navigation |

Every document whose role is not obvious should state its status near the top.
Time-sensitive provider facts should also record a verification date.

## Canonical design sources

The [design map](design/README.md) routes to the maintained architecture, CLI
contract, product vocabulary, safety model, provider evidence, lifecycle rules,
testing strategy, and historical M1–M7 records.

The [ADR index](adr/README.md) is the decision register. A completed issue or a
sentence in a proposed document does not make a choice accepted.

## Maintenance rules

- Put a fact in one canonical document. Other pages summarize briefly and link.
- Keep README and AGENTS.md as routes, not compressed specifications.
- Update documentation in the same change as user-visible behavior.
- Do not copy command contracts, provider matrices, schemas, or retention rules
  into issues, milestones, and multiple guides.
- Preserve historical milestone documents. Add a dated correction when history
  is wrong; describe current behavior in a living document.
- Link with repository-relative paths and verify links after moves or renames.
- Remove obsolete guidance instead of appending a second, conflicting rule.
- Use synthetic examples. Never publish credentials, account identifiers,
  personal paths, or retained provider responses.

## Noncanonical source material

- [Research roadmap](design/roadmap.md) — sequencing hypotheses from discovery.
- [Research handoff](../steam-library-agent-research-handoff.md) — unverified
  source material.
- Linear — work state and acceptance links, not a duplicate specification.

See [project governance](project-governance.md) for how repository evidence and
Linear stay synchronized without copying specifications.

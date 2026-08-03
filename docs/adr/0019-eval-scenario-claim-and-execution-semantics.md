# ADR 0019: eval scenario claim and execution semantics

Status: accepted 2026-08-02

## Context

Scenario schema `steam-agent-eval/0.2` requires executable scenarios to name
`required_claim_paths`. That field does not distinguish a fact the user asked
the answer to state from a fact that only needs support if the answer chooses
to state it. Requiring exhaustive sidecar coverage therefore measures rubric
enumeration as well as factual grounding.

The corpus also relies on runner-owned scenario identifiers to distinguish
live-supported scenarios from deterministic-only contracts. Required command
count is not an explicit scenario contract, even though the live runner accepts
exactly one captured required document. M5-c11 combines synchronization
lineage with a later compatibility assessment, while M7-o01 combines known
installed facts with several independent unsupported operational domains.

Changing those meanings at the same time as a model comparison would make a
result impossible to attribute. ADR 0018 therefore deferred the semantic
migration until it could be reviewed and versioned independently.

## Decision

### Add scenario schema `steam-agent-eval/0.3`

Every active scenario migrates to schema `0.3`. The historical `0.1` and `0.2`
schemas remain available for validating historical documents, but a live
qualification cohort uses one schema version and results from different
scenario schema versions are not pooled.

Schema `0.3` adds these required top-level fields:

- `execution_support` is `live` or `deterministic_only`.
- `unsupported_reason` is `null` for a live scenario and a bounded non-empty
  reason for a deterministic-only scenario.
- `required_document_count` equals the number of entries in
  `tool_policy.required`.

A live scenario may require at most one captured CLI document. A
deterministic-only scenario requires exactly one CLI document, matching the
smallest contract the current deterministic preflight executor can evaluate.
It remains outside live denominators. The runner derives this behavior from
scenario metadata rather than a private identifier list. A scenario whose
metadata and executable shape disagree fails preflight.

### Separate required mentions from optional grounded claims

`fact_rubric.required_claim_paths` is replaced by two unique, non-overlapping
path sets:

- `must_mention` lists facts explicitly required by the user question. The
  deterministic claims layer requires supported sidecar coverage for every
  selected value at each path.
- `support_if_claimed` lists prompt-salient facts that are optional in the
  answer. They do not create coverage requirements.

Every claim the subject provides remains checked against the captured CLI
document, whether or not its path is in either list. These lists do not
whitelist unsupported claims. The deterministic grader cannot infer whether a
prose statement was faithfully represented in the sidecar; hard prose/sidecar
alignment remains a blinded judge or human-review responsibility and cannot
override deterministic safety or factual failure.

Every `must_mention` path is backed by a deterministic oracle assertion that
fixes its expected value or relation. Entity selectors are preferred over
incidental array positions when stable entity identity is available.

### Split compound scenarios without deleting accepted concerns

- M5-c11 retains wishlist-scope synchronization and stale-lineage truth as a
  deterministic-only scenario. A separate live scenario evaluates the
  compatibility answer over the already-materialized facts.
- M7-o01 narrows to installed state, known size, and the general boundary that
  unsupported operational evidence remains unavailable. A separate scenario
  covers runtime, bandwidth, queue, and completion-time domains.
- M4-r07 uses entity-selected stale and unevaluated facts.
- M5-c10 does not imply that compatibility alone makes a purchase safe, and
  M5-c12 uses scenario-specific compatible-but-uninstalled fixture language.
- Prose-only requirements do not appear as executable prohibited-command
  signatures.

The splits preserve the accepted M1–M7 product, privacy, cache-only, and M7
read-only boundaries. They do not add product commands or provider behavior.

## Consequences

The claims layer measures required answer coverage without forcing unrelated
facts into every response, while all provided facts remain grounded. Live
denominators and deterministic-only exclusions become reviewable corpus data.
Scenario splits increase the corpus size and require new fixtures, tests, and
fresh independently attributable cohorts.

Schema `0.3` does not implement model judging, replicate policy, matrix
orchestration, or qualification thresholds. Those remain separate decisions so
that scenario-semantic changes can be verified before model selection begins.

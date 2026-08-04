# ADR 0021: diagnostic product benchmark campaigns

Status: accepted 2026-08-04

## Context

ADR 0020 distinguishes route-selection screens from fixed-corpus
qualification, but the checked-in product-use matrix was encoded as a screen
even though it was intended only to diagnose thirteen common questions on one
fixed route. Its documentation prohibited acceptance by convention. That left
the schema unable to express the campaign's actual claim and allowed generic
acceptance code to treat any non-screen campaign as qualification.

A product benchmark must retain the existing sealed inputs, strict capture,
privacy, deterministic grading, and route-blind qualitative evidence. It must
also show where failures occur without converting mixed evidence into a model
score or a release decision.

## Decision

Matrix schema `0.1` adds `campaign_kind: benchmark`. A benchmark declares an
explicit ordered `routes` list and forbids the screen `models` and `efforts`
cross-product axes. Its `screen_provenance` is null. Its policy is
`diagnostic-corpus/0.1`, its qualitative rule is
`diagnostic_criterion_vector`, and its hard-layer declaration contains the
complete ordered `agent_turns`, `tool_policy`, `oracle`, `claims`, and
`privacy` vector. The scheduler expands explicit routes exactly as it does for
qualification, without inheriting qualification provenance or semantics.

Every benchmark qualitative criterion uses the existing calibrated
three-judge agreement policy. Projections and imported artifacts remain strict,
route-blind, privacy-scanned, and hash-bound to the exact retained report and
rubric. The repository validates imported judgments and adjudications only; it
does not automate model-judge calls or invent missing outcomes.

The `report` command is available only for benchmark matrices and emits schema
`steam-agent-eval-benchmark-report/0.1`. It preserves detailed deterministic
layer outcomes and their true, false, and null counts separately from
qualitative criterion `pass`, `fail`, `unresolved`, and `unreviewed` outcomes.
Operational duration and command-count summaries remain measurements, not
quality points. Missing judgments or adjudications produce unreviewed criteria;
malformed, private, unbound, ambiguous, or noncanonical retained artifacts fail
closed.

There is no scalar score, survivor, qualified route, or overall passed concept
for a benchmark. Benchmark campaigns are diagnostic and cannot be accepted or
finalized. They cannot emit an acceptance artifact, bind an acceptance digest,
seed qualification, or carry screen provenance.

The canonical common-question config is `product-use-v2.json`. The observed
screen-shaped `product-use-v1.json` remains unchanged as historical evidence.
Any change to a question, scenario selection, rubric, route, track, replicate
policy, or other denominator creates a new versioned config and requires fresh
observations. Historical configs and their observed artifacts are never
rewritten to adopt newer semantics.

## Consequences

The matrix executor and qualitative import pipeline remain shared, while the
campaign's reporting and decision semantics become explicit. Diagnostic work
can accumulate calibrated review over time without being mistaken for
acceptance evidence. Consumers must interpret vectors and per-scenario detail
rather than cite one headline number.

Existing screen and qualification configs, behavior, acceptance artifacts, and
schema `0.1` round trips remain unchanged. Reversing the benchmark shape for a
future campaign is inexpensive through another versioned config; changing an
observed config or reusing its observations under revised questions is not
allowed.

# ADR 0018: qualification cohorts and answer/discovery tracks

Status: accepted 2026-08-01

## Context

Before this decision, the opt-in live runner could grade an individual
transcript, but an individual passing or failing report was not enough to
qualify a model and effort route.
Results produced from a changing worktree, incomplete run, failed harness
control, or unrecorded prompt mode are not comparable even when every retained
scenario report is internally valid. Reports exposed detailed layer failures
without a typed way to distinguish evidence-capture conditions such as an
absent document from policy or factual failures.

Tool discovery is a separate capability from interpreting CLI evidence and
answering the user's question. Giving one route the exact command while another
must find it changes what the run measures. Broadly allowing extra commands in
the discovery arm would weaken the cache-only boundary and could turn unrelated
output into grading evidence.

The proposed scenario `0.3` claim split and live-execution metadata would also
change corpus semantics. Combining that migration with the first qualification
cohort would make it impossible to tell whether a result changed because of
the runner foundation or the scenario contract.

## Decision

### A sealed cohort is the unit of qualification

A qualification cohort starts only from an identified Git revision with a
clean worktree. The already-loaded runner process then seals an immutable input
snapshot containing the product `src/` tree, the `evals/runner` tree, the
selected scenario bytes, and the applicable schema bytes. Scenario CLI
execution imports the product from the snapshot's `src/` tree. The harness
itself continues to execute as the already-loaded clean-revision process; it is
not relaunched from the snapshot.

In a subject cohort, deterministic preflight runs before controls or subject
execution. It validates every selection and, for every supported scenario,
materializes its fixture, invokes the snapshot-backed CLI, and grades its CLI
assertions. The current corpus has 51 such scenarios. M5-c03, M5-c04, and
M5-c11 are explicitly deterministic-only and do not enter live subject
execution; any other unsupported scenario aborts preflight.

The snapshot has a canonical digest and cross-file inventory checks establish
that its sealed copies match the bytes read from the clean worktree. Before
each subject and at completion, the runner rechecks the live product source,
harness, selected scenarios, schema, revision, and worktree cleanliness. A
changed live input contaminates the cohort, as does a changed or unverifiable
snapshot. Neither can be compared with a valid cohort.

Each cohort has a versioned run manifest. It records only bounded, non-private
provenance needed to reproduce or reject the run, including:

- manifest version, cohort identifier, selected track, and ordered scenario
  selection;
- source revision, worktree cleanliness, snapshot digest, and per-scenario
  input digests;
- requested model and effort, tool versions, and control-set version;
- start and finish times, expected run contents, completed contents, and
  control outcomes; and
- lifecycle state and a bounded typed reason for non-completed terminal states.

The lifecycle states are `initializing`, `controls`, `running`, `completed`,
`failed`, `interrupted`, and `contaminated`. `completed` means the declared
cohort accounted for every selected scenario; it does not mean its scenarios
passed. `failed` means a prerequisite or structural run contract failed.
`interrupted` records handled cancellation or an incomplete run.
`contaminated` records changed or unverifiable source or cohort provenance.
Non-completed states, including stale nonterminal manifests, are ineligible for
qualification. This slice provides no automated recovery or stale-run
terminalization.

Manifest updates use a mode-`0600` temporary file in the private run directory
followed by atomic replacement. Results with `failed`, `interrupted`, or
`contaminated` state are quarantined from qualification denominators and route
comparisons. Manifests omit repository paths, account identifiers, secrets,
raw responses, and other private protocol content.

### Scripted controls precede subject runs

Each qualification cohort runs a versioned scripted control set against the
integrated production-layer grading functions in the already-loaded runner.
After deterministic preflight, eight controls exercise a fully passing case
and isolated defects in agent-turn completion, unlisted tool policy, wrong
arguments, prohibited mutation, oracle assertions, unsupported claims, and
privacy. Each control must produce its declared canonical layer vector. A
missing or unexpected control result makes the cohort failed; controls are not
mixed into subject pass rates.

### Tracks are run-level and explicit

The first slice keeps scenario schema `steam-agent-eval/0.2` unchanged. A run
selects one of three tracks, and the manifest and every report identify it:

- `legacy` is the default and preserves the existing instructions and tool
  policy for historical comparisons.
- `answer` discloses the scenario's exact required command manifest to the
  subject. It isolates use of captured evidence, claim construction, answer
  quality, and refusal behavior. Required-command matching and all safety gates
  remain unchanged. Answer-track results are a diagnostic control, not the
  headline product score.
- `discovery` does not disclose the required command. Exact validated help
  calls and fully validated Steam Agent reads whose command head is in the
  runner's explicit positive set of known cache-only read heads may be counted
  as discovery cost when they are not on the scenario allowlist. Such reads
  never satisfy a required command, supply the oracle document, support claims,
  or relax a canonical layer.

An unlisted discovery read counts as cost only after the existing command and
execution parser establishes an unambiguous Steam Agent invocation, the exact
runner executable and data directory, the cache-only/read-only command class,
successful containment, and the absence of shell, network, mutation,
filesystem, Steam-client, or other activity violations. Anything not fully
validated remains a hard tool-policy failure. Discovery does not broadly allow
`capabilities`, `doctor`, arbitrary or future unknown subcommands, or output
from another tool.

After each accounted scenario, the runner verifies the private mode and
content hash of either its report and transcript or its deterministic-only skip
record. The summary records those hashes. Missing, substituted, or
unexpectedly permissive artifacts fail the cohort with a bounded terminal
reason.

### Diagnostics are additive and non-causal

Reports and summaries may add bounded typed diagnostics for observed runner
states, including required-command capture, aggregate or delta capture,
missing output, nonzero exit, multiple candidate documents, and invalid JSON.
They supplement the existing `agent turns / tool policy / oracle / claims /
privacy` vector and its `false`/`null`/`true` semantics. They do not replace a
layer result, convert a failure to a pass, choose a headline cause, or attribute
an observation to the model, harness, transport, or product without separate
evidence.

### Scenario redesign remains deferred

This decision does not add schema `0.3`, `execution_support`, `must_mention`,
or `support_if_claimed`; reclassify existing required claim paths; or split
M5-c11, M7-o01, or any other scenario. Those changes remain outside this
accepted slice. Any later scenario-semantic migration requires its own
reviewed decision and an independently attributable cohort.

## Consequences

Qualification can reject a failed experiment without misreporting it as a
subject failure, and comparable cohorts carry enough bounded provenance to
establish that they exercised the same selected input bytes, clean revision,
fixtures, controls, and track. The answer arm can separate command
discoverability from evidence use, while the discovery arm measures bounded
exploration without admitting extra evidence or weakening M1–M7 safety
boundaries.

Sealed snapshots and atomic manifests add setup and storage work. Stale-run
recovery remains future work. The default legacy track keeps existing
invocations stable, but legacy results cannot be pooled with answer or
discovery results. Typed diagnostics remain less convenient than a single
root-cause label; that is intentional because a layer failure alone does not
establish causality.

Scenario claim requirements and known omnibus or deterministic-only cases
remain imperfect during this slice. Deferring them preserves attribution: the
first qualification foundation changes how a run is formed and validated, not
what a schema `0.2` scenario means.

# Steam Agent evaluation corpus

This directory contains synthetic, versioned common-question scenarios. It is
a contract corpus, not captured user data and not a live-provider benchmark.

- `schema/scenario-0.1.json` through `schema/scenario-0.3.json` define the
  historical and active scenario formats; each scenario names the version it
  validates against.
- `scenarios/m2/` covers the identity, identifier opt-in, and data-deletion
  boundaries, including the credential refusal probe.
- `scenarios/m3/` covers accepted deal-question behavior.
- `scenarios/m4/` contains active deterministic recommendation questions for
  the accepted `recommendations/0.1` command and recipe contracts.
- `scenarios/m5/` covers accepted target-specific compatibility boundaries.
- `scenarios/m6/` covers bounded discovery and three-valued group ownership,
  copy certainty, mode, and player-count evidence.
- `scenarios/m7/` covers local-operation truth, storage ranking, and inert-plan
  boundaries without filesystem, provider, browser, or client access.
- `runner/` is the opt-in agent-execution runner: it materializes fixtures
  into a real `--data-dir` cache, drives every scenario turn through one
  Codex App Server thread, and grades the transcript deterministically. Run it
  with `uv run python -m evals.runner --family m7`. Live execution requires a
  POSIX host (macOS or Linux) and a local `codex` binary; on other hosts the
  runner exits before loading scenarios or creating result artifacts. This is
  a runner limitation, not a claim about platform support for the product CLI.
  Reproducible model comparisons should pin both dimensions, for
  example `--model gpt-5.6-sol --effort high`; supported effort values are
  `low`, `medium`, `high`, and `xhigh`. A pinned route must be attested before
  subject activity, and every observed setting or reroute must remain equal to
  it; otherwise the cohort fails structurally. Runs also name a run-level evaluation
  track: `legacy` is the default and preserves the original instructions,
  `answer` discloses the exact required command manifest, and `discovery`
  leaves command selection to the subject. Answer results are diagnostic and
  are not the headline product score. In discovery, only fully validated Steam
  Agent reads whose command head is in the runner's explicit positive set of
  known cache-only reads can be counted as exploration cost. They never
  satisfy the required command or provide oracle or claim evidence; unknown
  future command heads fail closed. All ambiguous, mutating, networked,
  filesystem, client, and other unvalidated activity remains a hard failure.
  Normal CI covers the runner's materializer, grader, and eight integrated
  scripted layer controls; it does not execute a live model. Deterministic
  preflight validates the active corpus before model execution. The `0.3`
  corpus contains 56 scenarios: 53 live and three deterministic-only.
  `m5-c03` and `m5-c04` lack a CLI writer, while `m5-c11` requires a sync that
  the cache-only live runner intentionally rejects. Any unexpected
  materialization failure fails the run.
  A run in which every selected scenario is skipped also fails.
  Exit status `0` means every executed layer passed, `1` means at least one
  deterministic or safety layer failed, and `3` means deterministic grading
  passed but at least one hard natural-language fact criterion still needs
  model or human review. Pending scenarios use JSON `null`, not `true` or
  `false`, for their aggregate and claims-layer `passed` fields; any real
  failure still makes the process exit `1`.
  Refusal grading is structural only: `refusal_expected` requires
  `declined: true` and its `required_all`/`required_any` vocabulary, while
  `must_not_execute` checks observed commands separately. Contradictions and
  completion claims are semantic hard-fail criteria, so they remain pending
  for model or human review even when the structural refusal check passes.
- `results/` is reserved for generated traces, answers, and judge reports and
  is ignored by Git. New run directories are mode `0700`, artifact files are
  mode `0600`, unrelated command output is omitted, and host paths are
  redacted before persistence. A qualification cohort starts only from a known
  clean Git revision. The already-loaded runner seals the product `src/` tree,
  its `evals/runner` bytes, selected scenarios, and schema into an immutable
  input snapshot. Scenario CLI execution uses the snapshot's product source;
  the harness is not relaunched from its snapshot copy. Cross-file checks
  establish that the sealed bytes match the clean worktree, and revision,
  cleanliness, live input inventories, and the snapshot seal are rechecked
  throughout the cohort. Deterministic preflight precedes a versioned set of
  eight scripted positive/negative controls that call the integrated
  production-layer grading functions. Its versioned run manifest records a
  bounded snapshot digest, per-scenario input digests, ordered selection,
  track, route, control, and completion provenance without private paths or
  account identifiers.
  Manifest updates use a private `0600` temporary file followed by atomic
  replacement. Only `completed` cohorts are eligible for qualification;
  `failed`, `interrupted`, and `contaminated` cohorts are quarantined from
  denominators and comparisons. The lifecycle also includes `initializing`,
  `controls`, and `running`; non-completed states, including stale nonterminal
  manifests, are ineligible. Failed, interrupted, and contaminated manifests
  carry a bounded terminal reason. Matrix campaigns add an immutable,
  route-interleaved plan above these single-route child cohorts. Resume creates
  an append-only attempt for the next unaccounted work item and never edits a
  prior child run. Matrix creation itself runs the exact frozen CLI oracle for
  every selected deterministic-only scenario before publishing a matrix and
  persists their source, child-source, schema, and rubric hashes plus `passed`
  outcomes in the manifest. Resume, inspection, and acceptance require that
  attestation to match the frozen inputs exactly; a caller-supplied attestation
  cannot suppress creation-time preflight. Each accounted scenario publishes either a mode-`0600`
  report and transcript or a mode-`0600` deterministic-only skip record. The
  runner
  verifies their hashes and records them in the summary; artifact failure
  fails the cohort.
  The live runner revalidates canonical scenario
  identifiers and resolved source/result containment before creating a
  workspace or writing artifacts. Each scenario's writable agent workspace is
  a private temporary directory that is removed before its sanitized
  transcript and report are written; caches and agent-created files never
  persist under `results/`. Runs that fail a full-retention gate persist only
  structural activity plus content hashes and lengths in the transcript and
  ordinary report fields. A separate, untrusted qualitative-answer projection
  may remain available after a required-evidence-only failure, but never after
  an incomplete turn, privacy failure, unlisted command, or unsafe activity.
  The canonical retention and sanitization rules are in the
  [evaluation strategy](../docs/design/evaluation-strategy.md#current-implementation).
  Evaluated commands run under the named `steam-agent-eval` permission
  profile. It denies the host root by default, inherits workspace writes, and
  reopens read access only for Codex's minimal platform set, the resolved
  Python interpreter, its standard-library and site-package directories, and
  this repository's `src/` directory. The App Server's isolated authentication
  and temporary directory remain denied; network access is disabled. These
  explicit runtime paths are host-readable, so this is not an absolute
  no-host-read boundary.
  App Server and ordinary background command descendants run in one process
  group and are terminated together. A deliberately detached descendant (for
  example, one that creates a new session) can escape process-group cleanup;
  the runner rejects that non-CLI activity, but it is not a process jail.

Each scenario keeps four concerns distinct: expected deterministic behavior, a
tool-use policy, a fact rubric, and an opt-in qualitative answer rubric. Normal
CI schema- and privacy-validates every scenario without network access or a
model API. Executable deterministic CLI coverage spans every family: oracle
modules for M3, M4, M5, and M7, and the materializer round trip for M2 and
M6 contract scenarios. Active scenarios use schema `0.3`, which separates
facts that answers must mention from facts that need support only when claimed,
records live versus deterministic-only execution support, and fixes required
document cardinality. See the
[evaluation strategy](../docs/design/evaluation-strategy.md) for scoring,
privacy, volatility, matrix, and adjudication rules.

The accepted anchor screen is predeclared in
`matrices/screen-anchor-v1.json`. Start, resume, inspect, and apply its strict
acceptance policy with:

```text
uv run python -m evals.runner matrix --config evals/matrices/screen-anchor-v1.json
uv run python -m evals.runner resume MATRIX_ID --config evals/matrices/screen-anchor-v1.json
uv run python -m evals.runner inspect evals/results/MATRIX_ID
uv run python -m evals.runner accept evals/results/MATRIX_ID
```

Screen results select routes only; they are not qualification evidence.
The screen requires calibrated agreement only on hard-fail fact criteria
explicitly authored with `screen_safety_gate: true`. Other hard-fail
correctness or fidelity criteria, authored quality, must-mention, and
conditional-support criteria remain diagnostic until qualification. No route
appears as a survivor while required screen safety adjudication is missing or
unresolved. Qualification gates every qualitative criterion.
For a completed accepted screen, `accept` atomically publishes the canonical
private `acceptance.json` decision. This freezes its survivor and qualitative
evidence selection and records the finalization time: later judgment or
adjudication imports are rejected.
Qualification `screen_provenance` must name the source screen matrix and include
the SHA-256 digests of that exact acceptance file, screen manifest, and
qualitative-evidence root. Matrix creation and resume reject missing, changed,
or chronologically later source decisions. Qualification acceptance requires
the same finalized source directory via `--screen-dir`.

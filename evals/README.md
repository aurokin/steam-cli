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
  `answer` discloses the exact required command manifest, `discovery` leaves
  command selection to the subject, and `skill` explicitly supplies the sealed
  repository skill with otherwise minimal instructions. Skill is an exclusive
  benchmark track, not an acceptance track. Answer results are diagnostic and
  are not the headline product score. In discovery, only fully validated Steam
  Agent reads whose command head is in the runner's explicit positive set of
  known cache-only reads can be counted as exploration cost. They never
  satisfy the required command or provide oracle or claim evidence; unknown
  future command heads fail closed. All ambiguous, mutating, networked,
  filesystem, client, and other unvalidated activity remains a hard failure.
  Normal CI covers the runner's materializer, grader, and eight integrated
  scripted layer controls; it does not execute a live model. Deterministic
  preflight validates the active corpus before model execution. The `0.3`
  corpus contains 59 scenarios: 56 live and three deterministic-only.
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
  prior child run. Matrix creation itself executes and grades the exact frozen
  deterministic oracle for every selected deterministic-only scenario before
  publishing a matrix. Deck cases run the compatibility domain oracle because
  no CLI writer reconstructs exact-target review; wishlist scope runs the
  frozen CLI. The manifest binds the executor and source, child-source, schema,
  rubric, oracle-document, and grading-result hashes plus `passed` outcomes.
  Resume, inspection, and acceptance require that
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
  requires exactly one retained answer and one parsed claims-sidecar entry for
  every scenario turn. Its route-blind, privacy-scanned bytes bind all answer
  prose and same-turn `{path, value}` claims. Every scenario receives a stable
  generated prose/sidecar-alignment criterion so qualification can reject
  factual prose that is missing from, broader than, unsupported by, or
  contradictory to its sidecar even when deterministic sidecar grading passes.
  That projection may remain available after a required-evidence-only failure,
  but never after an incomplete turn, privacy failure, unlisted command, or
  unsafe activity.
  Reports also carry a non-gating `diagnostics.command_audit` after command
  privacy passes. It exposes only finite allowlisted cache-only heads, public
  option names inside fixed mismatch codes, success state, and transport
  booleans. Argument values, positionals, aliases, identifiers, paths, output,
  hashes, and lengths are never included; unknown options are opaque and unsafe
  activity makes the audit null. Judges do not receive this diagnostic.
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

The canonical product-use benchmark is predeclared in
`matrices/product-use-v2.json`. It asks 13 direct questions about finding
library titles, installed state, multiplayer modes, wishlist membership,
filtering, recommendations, deals, compatibility, group fit, and storage. Its
only subject route is Sol at medium effort; three replicates run on both
tracks. The `discovery` track is the headline result because it measures
whether an agent can find and use the CLI itself. The `answer` track discloses
the required command and is a diagnostic of answer construction after routing
has been removed from the task. Run, inspect, and render its diagnostic vectors
with:

```text
uv run python -m evals.runner matrix --config evals/matrices/product-use-v2.json
uv run python -m evals.runner resume MATRIX_ID --config evals/matrices/product-use-v2.json
uv run python -m evals.runner inspect evals/results/MATRIX_ID
uv run python -m evals.runner report evals/results/MATRIX_ID
```

After changing CLI discoverability or question-aligned rubrics, use the
immutable discovery-only confirmation in
`matrices/product-use-discovery-v1.json`. It runs the same 13 questions for
three Sol-medium discovery replicates (39 observations) without rerunning the
answer track:

```text
uv run python -m evals.runner matrix --config evals/matrices/product-use-discovery-v1.json
uv run python -m evals.runner inspect evals/results/MATRIX_ID
uv run python -m evals.runner report evals/results/MATRIX_ID
```

The immutable edge confirmation in
`matrices/product-use-discovery-edge-v1.json` isolates the compatibility
`--explain` equivalence and the multiplayer query with or without its
`--require-mode online_co_op` filter. It runs only those two questions for
three Sol-medium discovery replicates (six observations):

```text
uv run python -m evals.runner matrix --config evals/matrices/product-use-discovery-edge-v1.json
uv run python -m evals.runner inspect evals/results/MATRIX_ID
uv run python -m evals.runner report evals/results/MATRIX_ID
```

The repository-skill benchmark is predeclared in
`matrices/product-use-skill-v1.json`. It runs the same 13 questions for three
Sol-medium replicates (39 observations). Each turn explicitly supplies the
attested `steam-agent` skill before the unchanged user question, so the result
measures the skill's operational guidance; it does not measure implicit skill
selection. Bare discovery remains skill-free.

```text
uv run python -m evals.runner matrix --config evals/matrices/product-use-skill-v1.json
uv run python -m evals.runner resume MATRIX_ID --config evals/matrices/product-use-skill-v1.json
uv run python -m evals.runner inspect evals/results/MATRIX_ID
uv run python -m evals.runner report evals/results/MATRIX_ID
```

Benchmark campaigns are diagnostic and cannot be accepted or finalized. Their
five deterministic layer outcomes and qualitative criterion outcomes remain
separate vectors; there is no benchmark score, survivor, qualified route, or
overall pass. Missing qualitative artifacts remain `unreviewed`, while retained
malformed artifacts fail report generation. Imported qualitative judgments use
the calibrated, route-blind policy for every criterion; the repository does not
call a model judge. Scenario `m6-d03` is an honesty probe at a current capability
boundary: the CLI can report declared online co-op support, while the numeric
player count remains `unsupported`. A correct answer names that gap; the
scenario is not evidence that numeric player-count lookup works.

`matrices/product-use-v1.json` is an immutable historical screen-shaped
diagnostic config. Keep it unchanged when questions or benchmark semantics
change; create a new version and collect fresh observations instead. The
benchmark contract is recorded in
[ADR 0021](../docs/adr/0021-diagnostic-product-benchmark-campaigns.md).
The repo-skill isolation contract is recorded in
[ADR 0022](../docs/adr/0022-repo-skill-evaluation-track.md).

Screen results select routes only; they are not qualification evidence.
The screen requires calibrated agreement only on hard-fail fact criteria
explicitly authored with `screen_safety_gate: true`. Other hard-fail
correctness or fidelity criteria, authored quality, must-mention, and
conditional-support criteria remain diagnostic until qualification. No route
appears as a survivor while required screen safety adjudication is missing or
unresolved. When an `answer`-track screen report safely suppresses or lacks the
one exact CLI document, its non-gating must-mention diagnostic carries an
explicit zero unavailable-evidence state so the independent safety criterion
remains judgeable. Screen `discovery` and qualification still require exact
must-mention evidence. Qualification gates every qualitative criterion.
For a completed accepted screen, `accept` atomically publishes the canonical
private `acceptance.json` decision. This freezes its survivor and qualitative
evidence selection and records its exact SHA-256 and finalization time in an
append-only manifest checkpoint: later judgment or adjudication imports are
rejected, and deleting or replacing the bound artifact fails closed rather than
reopening the screen.
Even a complete screen with zero survivors publishes this immutable evidence;
its empty survivor set simply cannot seed qualification.
Qualification `screen_provenance` must name the source screen matrix and include
the SHA-256 digests of that exact acceptance file, screen manifest, and
qualitative-evidence root. Matrix creation and resume reject missing, changed,
or chronologically later source decisions. Qualification acceptance requires
the same finalized source directory via `--screen-dir`.

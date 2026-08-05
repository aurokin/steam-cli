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

`matrices/product-use-discovery-v2.json` is the current-main qualitative-review
cohort. It preserves the same 13-question, three-replicate Sol-medium discovery
denominator under a new immutable config identity because scenario rubrics and
accepted command equivalents changed after `product-use-discovery-v1.json` was
observed.

After its 39 observations complete, prepare exact route-blind judge inputs in a
private directory outside the matrix:

```text
uv run python -m evals.runner review prepare \
  evals/results/MATRIX_ID /private/path/MATRIX_ID-review
```

Preparation validates the response schema against Draft 2020-12 and the pinned
Codex structured-output subset before it publishes the private package. In
particular, every `const` or `enum` property has an explicit type, objects are
strict and require every declared property, keywords and types are supported,
and values retain their declared bounds. A schema that fails this check is a
package defect, not a judge-slot attempt.

Before invoking any judge case, run the package's one non-slot live canary with the
exact response-schema bytes, copied native Codex version, model and effort, and
host-isolated profile used below. The canary contains no subject/candidate
evidence or projection, only fixed synthetic transport scaffolding. It must end
in one tool-free agent message whose JSON validates
against the response schema, and its private attestation must bind those exact
identities and digests. The operator attests to the external process identity
that the runner cannot observe. A canary is package-specific and is never reused.
A pass or bounded failure is terminal; a failed or missing canary stops the
package before judging and consumes no slot attempt.

Each `cases/WORK_ITEM_ID-JUDGE_ID.json` file is the complete prompt for exactly
one judge slot. Pipe it verbatim; do not add a wrapper prompt. Every invocation,
including a retry, uses a newly created private root with a fresh `CODEX_HOME`
containing only a mode-`0600` copy of the authenticated source `auth.json` and
an otherwise empty, invocation-exclusive workspace. Never reuse that root or
workspace for another judge, case, or retry. Codex
0.146.0 must use the exact no-shell, host-isolated profile below. Both shell
implementations, delegation, browser/computer/image features, goals, skill and
workspace discovery, tool suggestions, time reminders, hooks, apps, plugins,
MCP servers, and web search are disabled. Sol's CodeModeOnly request still
exposes top-level `exec`/`wait` with nested `apply_patch` and `view_image` despite
those feature settings. That residual V8 dispatcher has no Node, filesystem,
network, or console APIs; its nested file operations remain constrained by the
turn filesystem sandbox. The generic `:minimal` platform files may still expose
a coarse platform fingerprint. Host root and temporary paths are denied,
network is disabled, and the fresh empty workspace is the only writable path.
This prevents shell/process/environment access, delegation, external sources,
personal filesystem access, and account or process identity disclosure. Keep
the isolated root temporary and remove it after the import.

The structured response must echo `target.work_item_id`,
`target.projection_sha256`, and the complete `invocation` object from that case.
The assembler requires all three exact matches, so a response copied from
another replicate or judge slot cannot be imported.

The accepted judge attestation below is macOS-only. The deterministic runner
and test suite also support Linux, but no Linux judge-isolation recipe currently
satisfies this exact attestation. One setup pattern is:

```text
set -eu
umask 077
JUDGE_ROOT="$(mktemp -d /tmp/steam-agent-judge.XXXXXX)"
test -d "$JUDGE_ROOT" && test ! -L "$JUDGE_ROOT"
test "$(stat -f '%Lp' "$JUDGE_ROOT")" = 700
trap 'rm -rf "$JUDGE_ROOT"' EXIT
mkdir -m 700 "$JUDGE_ROOT/codex-home" "$JUDGE_ROOT/workspace"
SOURCE_CODEX_BIN="$(command -v codex)"
CODEX_BIN="$JUDGE_ROOT/codex"
cp -c "$SOURCE_CODEX_BIN" "$CODEX_BIN"
chmod 700 "$CODEX_BIN"
uv run python -m evals.runner review preflight-codex "$CODEX_BIN"
install -m 600 "${CODEX_HOME:-$HOME/.codex}/auth.json" \
  "$JUDGE_ROOT/codex-home/auth.json"
SOURCE_REVIEW_ROOT=/operator-owned/private-review-root
CASE_PATH="$JUDGE_ROOT/case-judge-1.json"
SCHEMA_PATH="$JUDGE_ROOT/response-schema.json"
install -m 600 \
  "$SOURCE_REVIEW_ROOT/cases/WORK_ITEM_ID-judge-1.json" "$CASE_PATH"
install -m 600 "$SOURCE_REVIEW_ROOT/response-schema.json" "$SCHEMA_PATH"
VERDICT_PATH="$JUDGE_ROOT/verdict-judge-1.json"
STDOUT_LOG="$JUDGE_ROOT/codex.stdout"
STDERR_LOG="$JUDGE_ROOT/codex.stderr"
test ! -e "$VERDICT_PATH"
install -m 600 /dev/null "$VERDICT_PATH"
test -f "$VERDICT_PATH" && test ! -L "$VERDICT_PATH"
test "$(stat -f '%Lp' "$VERDICT_PATH")" = 600
```

On macOS, `/tmp` canonicalizes to `/private/tmp`; both are neutral system paths
without an account identifier. `mktemp -d` creates the unpredictable root
atomically, and the mode check rejects anything other than `0700`. Do not move
the root under a home directory or other account-named path. `SOURCE_REVIEW_ROOT`
is used only by the host-side copy above and is never passed to the model
process. `JUDGE_ROOT` is outside the model workspace. The private case, response
schema, verdict, and stdout/stderr logs are neutral-path siblings of that empty
workspace. Then invoke the judge with the isolated environment:

The preflight requires the exact copied payload to be a regular, non-symlink
native executable and to report `codex-cli 0.146.0`; npm/JavaScript launchers,
symlinks, and other scripts fail closed. macOS `cp -c` creates an APFS clone
when supported, avoiding a full binary copy for every invocation. The original
executable path is used only by the host-side clone and never appears in the
sanitized environment, working
directory, or model input. Keep `PATH=/usr/bin:/bin`; do not add a Node or
package-manager directory.

```text
env -i CODEX_HOME="$JUDGE_ROOT/codex-home" \
  HOME="$JUDGE_ROOT/workspace" \
  TMPDIR="$JUDGE_ROOT/codex-home" PATH=/usr/bin:/bin LANG=C.UTF-8 \
  "$CODEX_BIN" exec --json --ephemeral --ignore-user-config --ignore-rules \
  --skip-git-repo-check --strict-config \
  --cd "$JUDGE_ROOT/workspace" \
  --config 'approval_policy="never"' \
  --config 'web_search="disabled"' \
  --config 'apps._default.enabled=false' \
  --config 'apps._default.destructive_enabled=false' \
  --config 'apps._default.open_world_enabled=false' \
  --config 'agents.enabled=false' \
  --config 'tools.update_plan.enabled=false' \
  --config 'tools.experimental_request_user_input.enabled=false' \
  --config 'mcp_servers={}' \
  --disable shell_tool --disable unified_exec --disable multi_agent \
  --disable apps --disable plugins --disable browser_use \
  --disable browser_use_external --disable browser_use_full_cdp_access \
  --disable computer_use --disable image_generation --disable in_app_browser \
  --disable goals --disable skill_search --disable workspace_dependencies \
  --disable tool_suggest --disable current_time_reminder --disable hooks \
  --config 'shell_environment_policy.inherit="core"' \
  --config 'shell_environment_policy.include_only=["PATH","LANG","LC_ALL","LC_CTYPE","TERM"]' \
  --config "shell_environment_policy.set.HOME=\"$JUDGE_ROOT/workspace\"" \
  --config "shell_environment_policy.set.TMPDIR=\"$JUDGE_ROOT/workspace\"" \
  --config 'default_permissions="steam-agent-eval"' \
  --config 'permissions.steam-agent-eval={extends=":workspace",filesystem={":root"="deny",":minimal"="read",":tmpdir"="deny",":slash_tmp"="deny"},network={enabled=false}}' \
  --model gpt-5.6-sol --config model_reasoning_effort=xhigh \
  --output-schema "$SCHEMA_PATH" \
  --output-last-message "$VERDICT_PATH" \
  - < "$CASE_PATH" \
  >"$STDOUT_LOG" 2>"$STDERR_LOG"
test -f "$VERDICT_PATH" && test ! -L "$VERDICT_PATH"
test "$(stat -f '%Lp' "$VERDICT_PATH")" = 600
uv run python -m evals.runner review check-events "$STDOUT_LOG"
```

Run that exact setup and isolated invocation first with the package's
`canary-case.json` copied to `CASE_PATH`. On success, record
and validate the package canary before creating any judge invocation root:

```text
uv run python -m evals.runner review record-canary \
  evals/results/MATRIX_ID /private/path/MATRIX_ID-review \
  "$VERDICT_PATH" --events "$STDOUT_LOG" --duration-ms 12345 \
  --isolation-attestation codex-0.146-no-shell-host-isolated-profile-v1 \
  --operator-invocation-attestation \
  operator-attested-codex-0.146-gpt-5.6-sol-xhigh-no-shell-host-isolated-profile-v1
uv run python -m evals.runner review validate-canary \
  evals/results/MATRIX_ID /private/path/MATRIX_ID-review
```

If transport or provider rejection prevents a verdict/event pair, record the
terminal failure instead, selecting the matching failure class:

```text
uv run python -m evals.runner review record-canary-failure \
  evals/results/MATRIX_ID /private/path/MATRIX_ID-review \
  --failure-class transport_failure --duration-ms 12345 \
  --isolation-attestation codex-0.146-no-shell-host-isolated-profile-v1 \
  --operator-invocation-attestation \
  operator-attested-codex-0.146-gpt-5.6-sol-xhigh-no-shell-host-isolated-profile-v1
```

`record-canary` itself records a terminal structural failure when supplied
artifacts are malformed, tool-using, or incorrectly bound. Never rerun a failed
canary. After a pass, discard its invocation root and create a fresh root for
each judge slot.

Import the result for each of `judge-1`, `judge-2`, and `judge-3`, recording the
externally measured total attempts and duration:

```text
uv run python -m evals.runner review assemble \
  evals/results/MATRIX_ID /private/path/MATRIX_ID-review WORK_ITEM_ID \
  "$VERDICT_PATH" --events "$STDOUT_LOG" --judge judge-1 \
  --attempt-count 1 --duration-ms 12345 \
  --isolation-attestation codex-0.146-no-shell-host-isolated-profile-v1
```

The attempt count includes the initial call and at most two retries. Retry only
a transport failure or structurally invalid response. Never retry a valid
`uncertain` verdict or a later disagreement. The runner cannot observe failed
external calls, so the operator supplies these bounded operational fields;
usage remains explicitly unavailable. The required isolation attestation means
the operator used Codex CLI 0.146.0 with the exact fresh-home, auth-only,
source-disabled, no-shell, delegation-disabled, environment-minimized,
filesystem-restricted, and network-disabled profile above; the runner cannot
infer that fact from an external process. Configuration readback proves the
settings were accepted, not the effective model-visible inventory; the residual
inventory described above was established separately from source and wire
evidence. Any CLI, model catalog, or profile change invalidates this
attestation. `check-events` is a standalone preflight, while `assemble`
independently enforces the same checks on the required `--events` file and
requires its sole completed `agent_message.text` UTF-8 bytes to equal the raw
verdict-file bytes exactly. The private operation `0.3` evidence records only
bounded counts and SHA-256 bindings for the event log, raw verdict bytes, and
normalized verdict document; it never records event, reasoning, or message
text. `check-events` privately parses the `--json` log and requires one
`thread.started`, then one `turn.started`, only stable-ID reasoning or
`agent_message` item lifecycles that all complete, exactly one completed
`agent_message`, and finally one `turn.completed` with nothing after it. A
completed-only item is valid. Any actual tool-use item, failed/unknown event,
duplicate or out-of-order lifecycle, incomplete item, malformed log, or missing
completion invalidates that attempt. Treat it like a transport or structural
failure: discard that invocation root and retry from a new root, counting it
toward the initial-plus-two limit. Never retry a valid `uncertain` verdict. The
`umask 077`, precreated output, and post-call checks are also required: the
assembler rejects either input unless it is a mode-`0600` regular file no
larger than 16 MiB. Initial import must finish before the `EXIT` trap removes
both disposable files. An operation-first crash may resume from the bound
private operation after both files are gone. Operation timestamps use the
existing matrix invariant: a non-empty, parseable, timezone-aware timestamp.

If the sole retained duration is unavailable because an invocation was
interrupted before its operation was published, or is known to be unreliable
despite a valid retained skill-track judgment, record the applicable
measurement amendment before assembling or resuming that slot:

```text
uv run python -m evals.runner review record-measurement-amendment \
  evals/results/MATRIX_ID /private/path/MATRIX_ID-review WORK_ITEM_ID \
  --judge JUDGE_ID \
  --amendment-class interrupted_attempt_duration_unavailable
uv run python -m evals.runner review assemble \
  evals/results/MATRIX_ID /private/path/MATRIX_ID-review WORK_ITEM_ID \
  /private/path/verdict.json --judge JUDGE_ID --attempt-count 2 \
  --duration-unavailable --events /private/path/events.jsonl \
  --isolation-attestation codex-0.146-no-shell-host-isolated-profile-v1
```

The matrix may contain exactly one private mode-`0600`
`review-measurement-amendment.json`. It is append-only and binds the matrix,
review package, external attempt ledger, case, judge slot, canary, and any
retained operation and judgment. The interrupted-attempt class authorizes only
attempt 2 at the existing canonical operation path. The unreliable-duration
class preserves the existing skill-track operation and judgment and authorizes
only a same-attempt resume; it cannot reroll the verdict. Amended duration is
non-authoritative and never changes scoring. See
[ADR 0025](../docs/adr/0025-qualitative-review-measurement-amendments.md).

If the exact valid verdict and event-log bytes survived but their externally
measured duration was lost before any operation or judgment was persisted,
retain both private files and invoke the dedicated recovery command:

```text
uv run python -m evals.runner review recover-duration-loss \
  evals/results/MATRIX_ID /private/path/MATRIX_ID-review WORK_ITEM_ID \
  "$VERDICT_PATH" --events "$STDOUT_LOG" --judge JUDGE_ID \
  --attempt-count 1 \
  --isolation-attestation codex-0.146-no-shell-host-isolated-profile-v1
```

`--attempt-count` must be exactly `1`: the surviving event log proves one valid
invocation, but no durable event or attempt ledger proves any retry history.
Initial publication requires a slot with no operation or judgment. The command
revalidates the exact verdict and event bytes against the case, judge, review
package, canary, privacy boundary, and isolation attestation, then publishes
the already observed attempt under the distinct
`steam-agent-eval-review-duration-loss-operation/0.1` schema and imports its
judgment. Its duration is recorded as `state=unavailable` with reason
`attempt_duration_lost_before_persist`; it is non-authoritative and
scoring-independent.

The operation is the only durable recovery artifact; no duration-loss
amendment file is created. At most one duration-loss operation may exist per
matrix. A second operation, an occupied slot, changed evidence, or invalid
preserved files fails closed. Ordinary `review assemble` cannot create this
operation or accept the missing duration. If recovery stops after operation
publication, repeat `recover-duration-loss`: it resumes the missing import
from the bound operation before consulting the disposable inputs. Never rerun
a valid verdict merely to recover timing, and never infer, estimate, or
substitute a duration. ADR 0025 and its single measurement-amendment artifact
remain unchanged. See
[ADR 0026](../docs/adr/0026-qualitative-review-duration-loss-recovery.md).

Once all three judgments exist for every case, resolve agreement mechanically
and render the updated benchmark report:

```text
uv run python -m evals.runner review resolve \
  evals/results/MATRIX_ID /private/path/MATRIX_ID-review
uv run python -m evals.runner report evals/results/MATRIX_ID
```

The resolver preserves every disagreement or `uncertain` verdict as
`unresolved`; it never calls a model or retries for agreement. The exact-input
and append-only operation contract is recorded in
[ADR 0023](../docs/adr/0023-orchestrated-qualitative-review.md).

### Recovering a provider-rejected review package

A legacy review-package `0.1` response-schema or invocation-protocol defect may
be recovered to package protocol `0.2` only through the whole-package
supersession contract in
[ADR 0024](../docs/adr/0024-qualitative-review-package-supersession.md). Do not
edit, replace, or resume the defective package. It is terminal, and all of its
requests retain their original package-local attempt counts even when the
provider rejected them before inference.

Supersession is permitted only when the defect is package-wide, every attempted
request was rejected before inference, and the package contains no valid model
output, judgment operation, imported judgment, adjudication, or observed
review outcome. A valid `uncertain` verdict, disagreement, post-inference
failure, published operation, or import makes supersession ineligible. It is
never a way to seek a different result or more retries.

Retain every existing old-package file byte-for-byte. Under the old-package and
matrix locks, successful preparation reserves one private canonical mode-`0600`
`review-package.json` registry in the matrix and adds one private append-only
`supersession.json` tombstone to the old package; it never rewrites an old asset.
The registry binds the manifest, package, canonical destination hash, and
tombstone without retaining a private path. A copied root, different destination,
or second package fails closed, while an interrupted exact publication resumes.
The registry is operational state and is ignored by subject scoring/reporting.

First obtain the privacy-safe legacy identity needed by the incident record:

```text
uv run python -m evals.runner review supersession-identity \
  evals/results/MATRIX_ID /private/path/OLD-REVIEW-ROOT
```

Create a canonical private mode-`0600` incident JSON document with this exact
shape. Copy `matrix_id`, `manifest_sha256`, `source_revision`, and the complete
`legacy` object from that command; list every configured judge in `by_judge`,
and list only attempted slots in `slots`. Counts and `unattempted_slots` must
reconcile with the full configured roster. Use `{"state":"unavailable"}` when
a duration or diagnostic digest was not retained rather than inventing one.

```json
{
  "schema": "steam-agent-eval-review-incident/0.1",
  "incident_id": "response-schema-rejection-20260805",
  "matrix_id": "MATRIX_ID",
  "manifest_sha256": "COPY_FROM_IDENTITY_COMMAND",
  "source_revision": "COPY_FROM_IDENTITY_COMMAND",
  "superseded": {"ledger_schema":"steam-agent-eval-review-ledger/0.1","tree_sha256":"COPY","ledger_sha256":"COPY","response_schema_sha256":"COPY"},
  "reason": "provider_rejected_response_schema",
  "provider_error": {"class":"invalid_request_error","code":"invalid_json_schema","message":"Response schema rejected before inference."},
  "codex": {"version":"codex-cli 0.146.0","isolation_attestation":"codex-0.146-no-shell-host-isolated-profile-v1"},
  "attempt_summary": {
    "total_requests": 1,
    "by_judge": [
      {"judge_identifier":"judge-1","request_count":1},
      {"judge_identifier":"judge-2","request_count":0},
      {"judge_identifier":"judge-3","request_count":0}
    ],
    "slots": [
      {"work_item_id":"w-000000-0123456789abcdef","judge_identifier":"judge-1","attempt_count":1,"duration":{"state":"unavailable"}}
    ],
    "unattempted_slots": 116
  },
  "states": {"inference":"absent","model_output":"absent","operations":"absent","imports":"absent","adjudications":"absent"},
  "diagnostic_evidence": {"state":"unavailable"},
  "recorded_at": "2026-08-05T00:00:00Z"
}
```

The incident input must use the same compact, sorted, newline-terminated
canonical JSON bytes as other review artifacts. Canonicalize an ASCII or Unicode
draft through a private temporary regular file, then rename it atomically:

```text
set -eu
umask 077
INCIDENT_TMP="$(mktemp /private/path/.steam-agent-incident.XXXXXX)"
chmod 600 "$INCIDENT_TMP"
uv run python -c 'import json,sys; value=json.load(open(sys.argv[1])); sys.stdout.write(json.dumps(value,allow_nan=False,ensure_ascii=True,separators=(",",":"),sort_keys=True)+"\n")' \
  /private/path/incident-draft.json > "$INCIDENT_TMP"
test -f "$INCIDENT_TMP" && test ! -L "$INCIDENT_TMP"
test "$(stat -f '%Lp' "$INCIDENT_TMP")" = 600
mv "$INCIDENT_TMP" /private/path/incident.json
```

Then prepare the one replacement package:

```text
uv run python -m evals.runner review prepare \
  evals/results/MATRIX_ID /private/path/NEW-REVIEW-ROOT \
  --supersede-review-dir /private/path/OLD-REVIEW-ROOT \
  --incident-record /private/path/incident.json
```

The package identity binds the complete incident, old tree/ledger/schema,
operation and canary protocol versions, and exact Codex/model/effort/isolation
identities. Do not retain or disclose raw provider bodies, reasoning or event
text, credentials, account or thread identifiers, or private paths.

Every configured slot moves to the replacement package together. Never combine
old and new cases, operations, judgments, or attempt accounting. The replacement
gets its own initial-plus-two limit because it is a new version of the review
instrument; that does not erase the old calls. Run the static schema checks and
the exact-profile live canary before its first slot.

Do not rerun the subject matrix for a response-schema-only supersession. Its
candidate inputs, outputs, reports, projections, prompts, parsers, rubrics, and
route settings remain immutable. Run every qualitative judge slot under the
replacement package, then rerun only mechanical agreement resolution and the
benchmark report.

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
call a model judge. When a retained benchmark observation has no usable CLI
capture or selected path, each affected must-mention criterion carries a
bounded zero `capture_unavailable` or `path_unavailable` state. This keeps the
privacy-cleared answer reviewable without inventing evidence or erasing the
separate deterministic failure. A valid capture keeps its exact selected value
and cardinality; ambiguous documents, private material, malformed values, and
oversized evidence still fail closed. Scenario `m6-d03` is an honesty probe at
a current capability boundary: the CLI can report declared online co-op
support, while the numeric player count remains `unsupported`. A correct answer
names that gap; the scenario is not evidence that numeric player-count lookup
works.

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
must-mention evidence. Diagnostic benchmarks use the bounded unavailable state
on every track while preserving exact evidence whenever a valid capture exists.
Qualification gates every qualitative criterion.
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

# Cross-milestone common-question evaluation strategy

Status: working quality strategy; deterministic scenario, materializer,
scripted-control, and applicable CLI-oracle coverage for M2–M7 is in normal
CI, while live model execution and judging remain opt-in.

## Purpose and boundary

Steam Agent needs two different kinds of evaluation. The CLI must first prove
that its evidence, constraints, and versioned recipes are deterministic. A
calling agent can then be evaluated on whether it uses that contract and gives
a useful grounded answer. A fluent answer cannot compensate for a false CLI
fact, and a model judge is not an oracle for either layer.

Accepted milestones rely first on deterministic product tests. Natural-language
model evaluation, real-user prompts, live-account runs, and judge calibration
remain opt-in follow-up work and cannot compensate for a contract failure.

## Evaluation layers

1. **CLI contract oracle (normal CI).** A synthetic normalized fixture, frozen
   clock, exact command, and deterministic assertions cover schema, candidates,
   three-valued constraints, ordering, factor arithmetic, freshness,
   attribution, warnings, and redaction. This layer has no model dependency.
2. **Tool-use trace (normal CI where deterministic).** Captured calls are
   checked against allowed, required, and prohibited commands and arguments.
   Cache-only questions must not cause synchronization, browsing, or mutation.
3. **Answer fact rubric (opt-in agent run, deterministic grading where
   possible).** Entity, numeric, state, citation, constraint, and ranking claims
   are compared with the captured CLI result. A controlled eval adapter should
   request a machine-readable claim/evidence sidecar instead of trying to infer
   every claim from prose.
4. **Judged answer quality (opt-in only).** Scenario-specific criteria may
   assess relevance, clarity, tradeoffs, and actionability. These scores remain
   separate from contract and fact failures. The repository validates imported
   blinded judgments and adjudications; it does not automate a model judge.

The scenario corpus under [`evals/`](../../evals/) represents these layers
explicitly. Normal CI schema- and privacy-validates every M2, M3, M4, M5, M6,
and M7 scenario. Executable deterministic CLI oracles cover M3, M4, M5, and
M7, and the materializer round trip executes every M2 and M6 contract
scenario through the installed CLI.

## Metrics

Report a metric vector and per-scenario failures rather than one blended score:

- tool and argument correctness, plus prohibited-call count;
- deterministic contract assertion pass rate;
- hard-constraint adherence, where any explicit violation fails the scenario;
- exact top-k or tie-group fidelity for deterministic recipes;
- supported verifiable claims divided by all verifiable claims, with citation
  precision and evidence coverage reported separately;
- fidelity to `unknown`, stale, inaccessible, partial, and unavailable states;
- weighted required-fact completeness;
- explanation fidelity to returned positive and negative factors;
- privacy/redaction as a binary hard gate; and
- consistency across paraphrase and metamorphic variants.

Cost, latency, and tool-call counts are useful operational measurements but do
not increase a quality score. Typed capture and runner diagnostics may describe
an observed condition such as missing output, a nonzero exit, multiple
candidate documents, or invalid JSON. They are additive to the canonical
metric vector: they do not replace a layer value, convert a failure to a pass,
or assign the condition to the model, harness, transport, or product without
separate evidence.

## Qualification cohorts and run tracks

The unit of model qualification is a complete cohort, not an individual
scenario report. A cohort starts only from an identified Git revision with a
clean worktree. The already-loaded runner process seals the product `src/`
tree, its `evals/runner` tree, the selected scenario bytes, and the applicable
schema bytes into an immutable input snapshot with a canonical digest.
Scenario CLI execution imports the product from that snapshot. The harness is
not relaunched from the snapshot; it continues as the already-loaded
clean-revision process.

In a subject cohort, deterministic preflight runs before controls or subject
execution. It validates every selection and, for each supported scenario,
materializes its fixture, invokes the snapshot-backed CLI, and grades its CLI
assertions. The current corpus has 51 supported scenarios. M5-c03, M5-c04, and
M5-c11 are explicitly deterministic-only. Any other unsupported scenario
aborts preflight. Cross-file inventory checks establish that the snapshot
copies match the bytes read from the clean worktree. Before each subject and
at completion, the runner rechecks the live product source, harness, selected
scenarios, schema, Git revision, and cleanliness, as well as the snapshot seal.
Changed live inputs contaminate the cohort; an invalid snapshot does the same.

Each cohort has a versioned run manifest containing bounded reproducibility
metadata: manifest schema and run identifier, snapshot and per-scenario input
digests, ordered scenario selection and track, requested route, tool versions,
control-set version and outcome, expected and completed work, timestamps, and
lifecycle state. It omits host paths, account identifiers, secrets, and raw
protocol or model content. Manifest updates use a mode-`0600`
temporary file inside the private run directory followed by atomic replacement.
The lifecycle distinguishes `initializing`, `controls`, `running`, `completed`,
`failed`, `interrupted`, and `contaminated`. Only a `completed` cohort from the
verified inputs can enter qualification denominators or route comparisons;
other terminal states are quarantined. A stale nonterminal manifest is also
ineligible. The current implementation does not recover or terminalize stale
runs.

A versioned scripted control set runs through the integrated production-layer
grading functions in the already-loaded runner. After deterministic preflight,
eight controls exercise a fully passing case and isolated defects in agent-turn
completion, unlisted tool policy, wrong arguments, prohibited mutation, oracle
assertions, unsupported claims, and privacy. Each must produce its declared
canonical layer vector. Controls validate the harness and remain outside
subject pass rates.

After each accounted scenario, the runner verifies the private mode and
content hash of its report and transcript, or of its deterministic-only skip
record. The summary records those hashes. Missing, substituted, or
unexpectedly permissive artifacts fail the cohort with a bounded terminal
reason.

The run manifest names one of three run-level tracks:

- `legacy` is the default and preserves the existing subject instructions and
  tool-policy behavior for historical comparison.
- `answer` discloses the scenario's exact required command manifest. It
  isolates evidence use, claims, prose, and refusal quality, but is a diagnostic
  control rather than the headline product score.
- `discovery` withholds the required command. Exact validated help calls and
  fully validated Steam Agent reads whose command head is in an explicit
  positive set of known cache-only reads may count as discovery cost outside
  the scenario allowlist, but never as required evidence, oracle input, or
  claim support.

A discovery read is a cost rather than a hard failure only when the existing
parser proves an unambiguous invocation of the exact runner executable and data
directory, a cache-only/read-only command class, and the absence of shell,
network, mutation, filesystem, Steam-client, or other activity violations.
Anything not fully validated remains a hard policy failure. There is no global
allowance for `capabilities`, `doctor`, arbitrary or future unknown
subcommands, or another tool's output. Required-command matching, privacy, M1
last-good behavior, and the M7 no-execution boundary do not vary by track. This
qualification contract is recorded in
[ADR 0018](../adr/0018-eval-qualification-cohorts-and-tracks.md).

### Diagnostic product benchmarks

A `benchmark` matrix is a diagnostic campaign over explicit ordered routes. It
uses the same sealed child cohorts, complete five-layer deterministic vector,
and strict route-blind qualitative imports as screen and qualification
campaigns, but it has no screen provenance and makes no acceptance claim.
Every qualitative criterion uses the calibrated policy. The repository only
validates imported judgments and adjudications; it does not invoke a model
judge.

Benchmark reports keep deterministic true, false, and null layer outcomes,
operational measurements, and qualitative `pass`, `fail`, `unresolved`, and
`unreviewed` outcomes separate. They define no scalar score, survivor,
qualified route, or overall pass. Benchmark campaigns are diagnostic and
cannot be accepted or finalized. Missing qualitative artifacts remain
unreviewed; malformed retained artifacts fail closed.

An observed benchmark config is historical evidence and is not rewritten.
Changing a product question, scenario selection, rubric, route, track, or
replicate policy requires a new versioned config and fresh observations. These
semantics are recorded in
[ADR 0021](../adr/0021-diagnostic-product-benchmark-campaigns.md).

## Corpus and volatility

Normative scenarios use synthetic AppIDs, aliases, evidence identifiers,
prices, and a frozen clock. They never assert a live price, wishlist count,
title list, provider result, or rank. Fixture order, clock advancement,
provider removal, and explicit constraint/feedback changes are useful
metamorphic variants because their expected effect is deterministic.

Opt-in live canaries may validate only schemas, coarse states, redaction, and
state transitions. Personal titles, AppIDs, SteamID64 values, feedback, keys,
paths, and raw provider bodies are not evaluation artifacts. Replays retain
only normalized data allowed by the provider boundary.

## Judge protocol

Absolute scenario-specific criteria are the default regression rubric.
Pairwise judging is reserved for comparing a candidate agent or prompt against
a pinned baseline. Pair order must be blinded and evaluated in both orders;
ties are valid and order-dependent disagreement is retained rather than
averaged away. Critical criteria should use independently configured judges,
with disagreement marked unresolved for human adjudication.

Every judged result must record generator and judge snapshots, prompt and
rubric versions, settings, fixture hash, and cost. Before judged scores gate a
release, a human-labeled calibration set must include deliberate defects such
as an invented price, stale evidence stated as current, unknown stated as free,
a hidden hard-constraint violation, and a verbose but incomplete answer.
Judge changes require recalibration and periodic blind human audits.
The current synthetic calibration set and observed result are recorded in
[`evals/calibration/`](../../evals/calibration/).

This follows the multi-scenario, multi-metric framing of
[HELM](https://arxiv.org/abs/2211.09110), the criterion-based approach in
[G-Eval](https://arxiv.org/abs/2303.16634), and the documented position and
verbosity limitations of
[LLM-as-a-judge evaluation](https://openreview.net/pdf?id=uccHPGDlao).
[GDPval](https://openai.com/index/gdpval/) provides a useful precedent for
blind comparison and task-specific rubrics while retaining expert review.

## Current implementation

- Normal CI validates every scenario against the schema its own
  `schema_version` names, including synthetic privacy canaries. `0.2` added a
  required `scenario_kind` of `contract` or `boundary`, reads
  `conversation.user` as sequential turns, and lets an assertion name its
  source: the captured CLI document (the default), a turn's final answer
  (`refusal_expected`, `contains`, `omits`), or the executed-command trace
  (`must_not_execute`). A boundary scenario may therefore carry no fixture
  facts and no required command.
  Active scenarios now use `0.3`. It records `execution_support` and required
  document cardinality and splits answer facts into `must_mention` and
  `support_if_claimed`. Corpus and runtime checks require those sets to be
  disjoint and require every `must_mention` path to have deterministic CLI
  oracle support. The semantic migration is recorded in
  [ADR 0019](../adr/0019-eval-scenario-claim-and-execution-semantics.md).
- Qualification cohorts require a known clean Git revision before preflight
  and recheck the revision, worktree, selected input inventories, and snapshot
  seal throughout the run. The snapshot attests the product source, loaded
  harness bytes, selected scenarios, and schema; only the product source is
  executed from the snapshot. The active corpus contains 56 live scenarios
  and classifies M5-c03, M5-c04, and M5-c11 as deterministic-only.
  Eight scripted controls call the integrated production-layer evaluators.
  Scenario publication verifies mode-`0600` reports and transcripts or skip
  records and puts their content hashes in the summary. Lifecycle states are
  `initializing`, `controls`, `running`, `completed`, `failed`, `interrupted`,
  and `contaminated`; every non-completed terminal state carries one bounded
  reason. Stale nonterminal child manifests are ineligible. Matrix campaigns
  preserve those child runs and resume at an append-only scheduler attempt
  boundary. Their plan binds the revision, input digests, ordered scenarios,
  routes, tracks, replicate schedule, timeout, and exclusions. Inspection
  verifies the child manifest-summary-report hash chain and reports per-layer
  vectors without blending tracks or deterministic and qualitative outcomes.
  Strictly earlier failed or abandoned attempts remain hash-bound audit history
  but do not disqualify a later official successful retry; later, overlapping,
  conflicting, or child-evidence-duplicating attempt history remains
  ineligible. Compatibility vectors bind the complete ordered selected corpus,
  including deterministic-only scenarios, and the exact deterministic
  preflight attestation, so differing preflight cohorts cannot be pooled. Each
  deterministic preflight also retains private canonical input, oracle-document,
  versioned replay-definition, and grading-result artifacts. Resume, inspection,
  and acceptance replay the frozen generic definition without consulting the
  current checkout or invoking the current runner; deletion, rewriting, or
  swapping fails closed. Observed orphan attempts revalidate their complete,
  distinct child bundles through the official child validator. Finalized screen
  acceptance is append-only manifest state binding the exact decision digest and
  finalization time; deleting or replacing the bound artifact is invalid, not a
  way to reopen the screen. It also binds the hashes of all
  retained prior retry artifacts, so their audit history cannot change after
  freezing. Inspection rejects a symlink at the results root or any unresolved
  ancestor before resolving the containment boundary.
  Imported blinded judgments and adjudications are hash-bound to the exact
  report and rubric; they cannot override deterministic failure. These
  contracts are recorded in
  [ADR 0020](../adr/0020-eval-matrix-campaigns-and-fixed-corpus-qualification.md).
  The same matrix schema also supports diagnostic benchmark campaigns with
  explicit routes, null screen provenance, calibrated review of every
  criterion, and a separate vector report. Benchmark campaigns have no
  acceptance or finalization path, as recorded in
  [ADR 0021](../adr/0021-diagnostic-product-benchmark-campaigns.md).
- M3, M4, M5, and M7 oracle modules execute installed command behavior
  against deterministic scenarios; M2 and M6 contract scenarios are executed
  through the materializer round trip, and boundary probes (refusal,
  must-not-execute, multi-turn pressure) are graded from the agent transcript
  by the opt-in runner.
- A new executable scenario is added only after its exact CLI and recipe
  contract is accepted and backed by normal product tests.
- An opt-in agent-execution runner exists under `evals/runner/`. It
  materializes normalized fixtures through public storage APIs, drives every
  scenario turn in order on one Codex App Server thread, and grades the
  transcript deterministically: successful completion of every turn, tool
  policy over every completed App Server item (including zero exit status for
  a required command), oracle assertions against their declared source, every
  document-backed turn's nonempty and fully supported claim/evidence sidecar,
  those sidecars aggregated into required fact-path coverage, and a binary
  privacy gate over the answer surface. Missing, empty, or unsupported per-turn
  evidence is a deterministic claims failure even when the aggregate covers
  every required path; reports preserve both the per-turn failure and aggregate
  coverage diagnostics. Claims and
  path coverage can pass while a natural-language `hard_fail` fact criterion
  remains unevaluated. That state is reported as pending review with JSON
  `passed: null` and runner exit status `3`; it is neither a pass nor a
  deterministic failure. A failed deterministic or safety layer takes
  precedence and returns status `1`, while a fully passing run returns `0`.
  `refusal_expected` is deliberately structural: it requires the refusal
  sidecar's `declined: true` plus scenario-authored `required_all` and
  `required_any` vocabulary. It does not judge whether the answer contradicts
  itself or falsely claims completion. `must_not_execute` separately grades
  the observed command trace; semantic contradictions and completion claims
  stay in each scenario's natural-language hard-fail criterion and therefore
  remain pending until model or human review.
  Claims and CLI-document assertions are graded against the JSON output captured from
  exactly one successful required command in the transcript, with exact
  normalized arguments and the relative `--data-dir steam-agent-data`; the
  runner does not invoke the CLI again to manufacture grading evidence. A
  schema `0.2` requirement may additionally declare a bounded set of exact
  `accepted_optional_options`. Each declaration names either one valueless
  long flag or one long option with one exact value. Undeclared semantic
  options and positionals remain failures, as do attempts to declare
  `--format`, duplicate declarations, overlap with required arguments, and
  malformed or option-like values. The existing transport normalization still
  permits one undeclared `--format json`. The declaration means only that the
  forms are equivalent for that scenario's assertions; it does not assert
  byte-identical output or global product equivalence.
  Non-JSON or multiple-document output fails closed. Fixture and CLI clocks use
  the scenario's `frozen_time`. It makes no provider requests. Live execution
  is explicitly POSIX-only because the runner uses `/bin/sh`, pipe selection,
  and process-group signals; non-POSIX hosts are rejected before scenarios are
  loaded or artifacts are created. That runner boundary does not narrow the
  product CLI's platform contract. A local `codex` binary is required; normal
  CI exercises only the platform-independent materializers and grader. Agent
  execution treats only `m5-c03`, `m5-c04`, and `m5-c11` as
  deterministic-only. An unexpected unsupported scenario or a selection in
  which every scenario is skipped fails the run.
- The runner selects a named `steam-agent-eval` permission profile, one runtime
  workspace root, no network, and no approvals. The profile extends
  `:workspace`, denies the host root by default, retains Codex's minimal
  platform reads, inherits workspace writes, and explicitly reopens only the
  resolved Python interpreter file, its runtime-library, standard-library,
  platform-library, pure-library, and site-package directories, the exact
  framework runtime binary when Python is a macOS framework build, and this
  repository's `src/` directory. The framework prefix is not reopened. `/tmp`
  and the App Server process `TMPDIR` remain denied. The
  driver verifies the active profile identity, its exact resolved filesystem
  and network rules, runtime roots, working directory, ephemeral and
  non-persisted thread state, approval policy, sandbox response, empty
  additional writable roots, temporary-directory exclusions, and a nonempty
  instruction-source list containing the exact private workspace `AGENTS.md`
  (with every source workspace-local) before any model turn.
- App Server runs with a disposable private `CODEX_HOME` containing only a
  mode-`0600` copy of the existing `auth.json`; personal config, MCP servers,
  plugins, hooks, skills, state, and history are not inherited. It is launched
  from the private scenario workspace before protocol initialization, so
  startup project discovery cannot inherit configuration from the repository
  running the harness. Web search, hooks, plugins, apps, and configured MCP
  servers are disabled with pinned Codex 0.146 startup controls, strict config
  parsing is enabled, and client dynamic tools are empty. Before
  `thread/start`, a declaration-only preflight requires usable authentication;
  resolved web/app settings and hook/app/plugin feature flags to remain
  disabled; resolved MCP and plugin declarations to be empty; the workspace's
  `hooks/list` result to contain no hooks, warnings, or errors; and the
  threadless MCP inventory to be empty. Invalid declarations stop the preflight
  before hook or MCP inventory. Hook-origin protocol activity aborts the run.
  Authentication and protocol failures use generic errors and never include raw
  App Server payloads. Codex 0.146's generated response schema and live JSONL
  transport omit the otherwise standard `jsonrpc` member, so client response
  handling accepts only its pinned versionless `id` plus `result`/`error`
  envelope. It requires an exact ID type/value match, exactly one valid result
  or error member, and an object result; version-bearing or extended envelopes
  fail closed.
  Between turns, the next `turn/start` response orders prior notifications into
  the next collector, where turn and item scope checks reject late activity.
  After the final terminal `turn/completed`, the driver sends one lightweight
  `thread/read` ordering barrier with turn loading disabled, requires the
  thread to be idle, and immediately drains already queued notifications,
  complete buffered frames, and zero-time-ready stdout. Any trailing turn,
  item, command, tool, hook, request, malformed, or partial-frame activity fails
  closed; only the explicitly harmless global rate-limit update is discarded.
  Drained bytes remain subject to the same frame, per-turn, and
  per-conversation limits, and any failure uses normal process-group cleanup.
  Inbound JSONL is bounded to 4 MiB per frame, 16 MiB per turn, and 64 MiB per
  conversation; exceeding any bound fails the scenario and triggers normal
  process-group cleanup without retaining the rejected input.
  App Server's process `TMPDIR` is its isolated Codex home, which the permission
  profile denies to evaluated commands. The model command environment inherits
  only a small locale/PATH allowlist and receives workspace-local `HOME` and
  `TMPDIR` values, so it neither discloses nor grants access to the disposable
  Codex home. Codex 0.146 silently changes
  a thread to read-only when `environments: []` is sent; because that would
  also disable the required CLI, the field is omitted. The disposable Codex
  home carries no configured remote environment, leaving only App Server's
  built-in local execution environment. The private Codex home and all of its
  transient state are removed after App Server exits.
- On macOS with Codex 0.146, the host root is denied but the profile's minimal
  platform set and explicitly reopened Python runtime, package, and source
  paths remain readable. This is therefore a least-privilege host-read boundary,
  not an absolute no-host-read guarantee; stronger isolation requires an
  isolated OS account or VM. Developer instructions prohibit host inspection,
  but instructions are not a security boundary. App Server and ordinary
  background command descendants share a process group that is terminated as
  a unit; a deliberately detached descendant can escape that cleanup, so the
  runner's exact-command policy rejects such activity and the runner does not
  claim to be a process jail. The driver records every completed item, permits
  command execution and explicitly informational item types, and fails the
  tool-policy gate on file changes, MCP/dynamic calls, web search,
  collaboration, and unknown activity. Final-answer policy and privacy grading
  cover every ordered user-visible agent-message item in a turn; only the last
  item may supply the terminal claims sidecar. A terminal JSON fence is removed
  from review prose only when it validates as that sidecar; malformed or
  unrecognized JSON remains visible. When deterministic safety,
  path-coverage, oracle, and privacy gates pass, artifacts retain that
  sanitized ordered message list and the exact sanitized required CLI JSON
  document used by oracle and claim grading so pending qualitative review can
  audit or replay the decision without rerunning the model. Clean artifacts
  redact host paths and privacy canaries. A report also carries a separate,
  untrusted `qualitative_review_answers` projection: an ordered list of
  nonempty per-turn visible prose after sanitization and valid-sidecar removal.
  It is available only when all turns completed, privacy passed, and tool policy
  either passed or failed solely because required evidence was missing or
  unusable. Unlisted commands, execution or activity violations, and every
  other tool-policy failure make the projection null. It never contains
  prompts, commands, outputs, events, CLI documents, claims, or route metadata,
  is bounded by the existing App Server turn and conversation input budgets,
  and is not oracle evidence. If any full-retention gate fails, the transcript
  and ordinary report fields still retain only structural activity plus content
  hashes and lengths; raw prompts, reasoning, commands, output, evidence, and
  complete answer traces remain omitted. The sole provenance exception is an
  exact pinned model-and-effort attestation. The requested route remains
  readable as declared run configuration; effective, observed, and per-turn
  values remain readable only when that request is valid and confirmed and
  every such value equals it. This bounded metadata lets a matrix verify failed
  or pending observations without exposing arbitrary App Server content, and
  it is never copied into the qualitative projection. This boundary is
  recorded in
  [ADR 0017](../adr/0017-eval-command-equivalence-and-review-retention.md).
- Each scenario uses a private temporary writable workspace. Its synthetic
  data directory contains a hidden canary file that the product CLI ignores,
  making prohibited filesystem inspection observable. The entire workspace,
  including the cache, canary file, and agent-created files, is removed on
  success or failure before runner-authored artifacts are persisted. Result
  directories are `0700`; sanitized transcript and report files are `0600`.
- Reports distinguish requested model and effort from the effective values
  returned by App Server thread settings for each turn. If App Server does not
  confirm an effective turn override, the effective value remains null rather
  than being inferred from the request. A turn-scoped reroute is reported for
  that turn but is not carried forward without a thread-settings confirmation.
  When model or effort is pinned, every observed value must match it and both
  dimensions must be attested before command or answer activity. A missing,
  late, transient, or reverted mismatch fails the cohort before that scenario
  is accounted. Reproducible qualification therefore pins both dimensions.
- M2, M3, M4, M5, M6, and M7 fixtures are materializable today through the
  per-milestone `evals/runner/materialize_*.py` modules. The two Valve Deck
  scenarios stay pure-oracle-only because no CLI writer produces
  exact-target Deck review evidence, and each materializer module documents
  the states it cannot reproduce.
- The privacy gate always fails on a leaked canary or a personal path. The
  personal Steam ID pattern is skipped only when the scenario's own required
  command asks for identifiers with `--include-identifiers`.
- Sanitized transcripts and reports stay under `evals/results/`, which is
  ignored by Git; writable workspaces and raw agent output do not.

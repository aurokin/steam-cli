# Cross-milestone common-question evaluation strategy

Status: working quality strategy; deterministic scenario, materializer, and
applicable CLI-oracle coverage for M2–M7 is in normal CI, while live model
execution and judging remain opt-in.

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
   separate from contract and fact failures. This repository does not yet
   implement a judge or depend on a model API.

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
not increase a quality score.

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

## Judge protocol (future)

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

This follows the multi-scenario, multi-metric framing of
[HELM](https://arxiv.org/abs/2211.09110), the criterion-based approach in
[G-Eval](https://arxiv.org/abs/2303.16634), and the documented position and
verbosity limitations of
[LLM-as-a-judge evaluation](https://openreview.net/pdf?id=uccHPGDlao).
[GDPval](https://openai.com/index/gdpval/) provides a useful precedent for
blind comparison and task-specific rubrics while retaining expert review.

## Current implementation

- Normal CI validates every scenario against the schema its own
  `schema_version` names, including synthetic privacy canaries. `0.2` adds a
  required `scenario_kind` of `contract` or `boundary`, reads
  `conversation.user` as sequential turns, and lets an assertion name its
  source: the captured CLI document (the default), a turn's final answer
  (`refusal_expected`, `contains`, `omits`), or the executed-command trace
  (`must_not_execute`). A boundary scenario may therefore carry no fixture
  facts and no required command.
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
  turn's claim/evidence
  sidecar aggregated into required fact-path coverage (with per-turn support
  diagnostics), and a binary privacy gate over the answer surface. Claims and
  CLI-document assertions are graded against the JSON output captured from
  exactly one successful required command in the transcript, with exact
  normalized arguments and the relative `--data-dir steam-agent-data`; the
  runner does not invoke the CLI again to manufacture grading evidence.
  Non-JSON or multiple-document output fails closed. Fixture and CLI clocks use
  the scenario's `frozen_time`. It makes no provider requests and requires a
  locally installed `codex` binary; normal CI exercises only its materializers
  and grader. Agent execution expects only `m5-c03`, `m5-c04`, and `m5-c11` to
  be unsupported. An unexpected unsupported scenario or a selection in which
  every scenario is skipped fails the run.
- The runner selects a named `steam-agent-eval` permission profile, one runtime
  workspace root, no network, and no approvals. The profile extends
  `:workspace`, denies the host root by default, retains Codex's minimal
  platform reads, inherits workspace writes, and explicitly reopens only the
  resolved Python interpreter file, its runtime-library, standard-library,
  platform-library, pure-library, and site-package directories, and this
  repository's `src/` directory. `/tmp` and the App Server process `TMPDIR`
  remain denied. The
  driver verifies the active profile identity, its exact resolved filesystem
  and network rules, runtime roots, working directory, ephemeral and
  non-persisted thread state, approval policy, sandbox response, empty
  additional writable roots, temporary-directory exclusions, and a nonempty
  instruction-source list containing the exact private workspace `AGENTS.md`
  (with every source workspace-local) before any model turn.
- App Server runs with a disposable private `CODEX_HOME` containing only a
  mode-`0600` copy of the existing `auth.json`; personal config, MCP servers,
  plugins, hooks, skills, state, and history are not inherited. Web search and
  apps are explicitly disabled, client dynamic tools are empty, and a
  declaration-only preflight requires usable authentication, resolved web/app
  settings and app/plugin feature flags to remain disabled, the resolved plugin
  catalog to be empty, and the thread's MCP inventory to be empty before
  `thread/start` or `turn/start`, as applicable. Authentication and protocol
  failures use generic errors and never include raw App Server payloads.
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
  item may supply the terminal claims sidecar. When deterministic safety,
  path-coverage, oracle, and privacy gates pass, artifacts retain that
  sanitized ordered message list and the exact sanitized required CLI JSON
  document used by oracle and claim grading so pending qualitative review can
  audit or replay the decision without rerunning the model. Clean artifacts
  redact host paths and privacy canaries. If turn completion,
  required evidence, tool policy, or privacy fails, the transcript and report
  retain only structural activity plus content hashes and lengths; raw prompts,
  reasoning, answers, commands, and tool output are omitted.
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

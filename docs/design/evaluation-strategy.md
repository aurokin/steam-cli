# Cross-milestone common-question evaluation strategy

Status: working, non-blocking quality track 2026-07-11

## Purpose and boundary

Steam Agent needs two different kinds of evaluation. The CLI must first prove
that its evidence, constraints, and versioned recipes are deterministic. A
calling agent can then be evaluated on whether it uses that contract and gives
a useful grounded answer. A fluent answer cannot compensate for a false CLI
fact, and a model judge is not an oracle for either layer.

This track does not block M4 implementation. M4 acceptance still requires its
own deterministic tests for feedback, activity, hard gates, recipes, factors,
uncertainty, and deletion. Natural-language model evaluation, real-user
prompts, live-account runs, and judge calibration remain opt-in follow-up work.

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
explicitly. M3 and M4 cases are active descriptions of their accepted CLI and
deterministic recipe behavior.

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

## Initial adoption

- Normal CI validates every scenario against `scenario-0.1.json`, including
  synthetic privacy canaries.
- M4 implementation may promote a proposed scenario only after its exact CLI
  and recipe contract is accepted and backed by normal product tests.
- A future runner may materialize normalized fixtures through public storage
  APIs and execute an installed CLI. It must not make provider requests.
- Generated traces, answers, judgments, and reports stay under
  `evals/results/`, which is ignored by Git.
